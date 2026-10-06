"""Bronze -> chunk -> embedding (Gemini) -> Qdrant.

Chạy theo kiểu tăng dần. Point id được dẫn từ (source, article_id, chunk_idx)
nên ổn định giữa các lần chạy; trước khi gọi API, module tra Qdrant xem point đã
tồn tại chưa và nội dung có đổi không, rồi chỉ embed phần mới hoặc đã cập nhật.
Embedding là bước đắt nhất trong chuỗi xử lý nên đây là nơi đáng tối ưu nhất.

    python -m newscrawler.embed                 # chỉ phần chưa embed
    EMBED_FORCE=1 python -m newscrawler.embed   # embed lại tất cả, ví dụ khi đổi model
"""
from __future__ import annotations

import hashlib
import os
import time

from google import genai
from google.genai import types
from pyiceberg.catalog.rest import RestCatalog
from qdrant_client import models as qm

from . import bm25, vectorstore as vs
from .gemini import KeyPool, is_quota_error
from .chunker import est_tokens, split

BRONZE_NAMESPACE = os.getenv("BRONZE_NAMESPACE", "bronze")
BRONZE_TABLE = os.getenv("BRONZE_TABLE", "articles")
BRONZE_IDENT = f"{BRONZE_NAMESPACE}.{BRONZE_TABLE}"

EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001")
EMBED_DIM = int(os.getenv("EMBED_DIM", "768"))
# Trần trên số chunk trong một lô. Đây không phải ràng buộc quyết định: cùng con
# số 40 chunk có thể là 4.000 token nếu toàn summary hoặc 38.000 token nếu toàn
# body, nên số chunk không nói lên được lô có vượt trần TPM hay không.
BATCH = int(os.getenv("EMBED_BATCH", "40"))
# Trần token cho một request, và là ràng buộc thực sự. Phải nằm dưới trần TPM
# (30.000 mỗi phút mỗi project) vì cả lô đi trong một lần gọi: vượt trần là bị
# từ chối ngay, độc lập với hạn mức ngày.
_BATCH_TOKENS = int(os.getenv("EMBED_BATCH_TOKENS", "20000"))
FORCE = os.getenv("EMBED_FORCE", "").lower() in ("1", "true", "yes")

_MAX_RETRY = 5


def _catalog() -> RestCatalog:
    return RestCatalog(
        name="rest",
        **{
            "uri": os.getenv("ICEBERG_REST_URI", "http://iceberg-rest:8181"),
            "warehouse": os.getenv("ICEBERG_WAREHOUSE", "s3://warehouse/"),
            "py-io-impl": "pyiceberg.io.pyarrow.PyArrowFileIO",
            "s3.endpoint": os.getenv("MINIO_ENDPOINT", "http://minio:9000"),
            "s3.access-key-id": os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
            "s3.secret-access-key": os.getenv("MINIO_SECRET_KEY", "minioadmin"),
            "s3.path-style-access": "true",
            "s3.region": os.getenv("AWS_REGION", "us-east-1"),
        },
    )


def _normalize(v: list[float]) -> list[float]:
    """Chuẩn hoá L2 cho vector đã cắt chiều.

    gemini-embedding-001 chỉ trả vector chuẩn hoá sẵn ở 3.072 chiều. Khi dùng
    cắt chiều kiểu Matryoshka xuống 768, chuẩn L2 không còn bằng 1 nên phải
    chuẩn hoá lại, nếu không khoảng cách cosine sẽ lệch.
    """
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v] if n else v


def embed_texts(pool: KeyPool, texts: list[str], task: str) -> list[list[float]]:
    """Gọi API embedding cho một lô văn bản.

    Hai dạng lỗi được xử lý khác nhau:

      * Hết hạn mức (429): cho key đó nghỉ, rồi `pool.acquire()` chọn key sẵn
        sàng sớm nhất và chờ tới lúc dùng được. Không tính vào `_MAX_RETRY`, vì
        bản thân request không sai — key chỉ đang chạm trần TPM.
      * Lỗi tạm thời khác: thử lại trên cùng key với backoff luỹ tiến.
    """
    cfg = types.EmbedContentConfig(task_type=task, output_dimensionality=EMBED_DIM)
    attempt = 0
    rotations = 0
    # Pool đã chủ động giãn nhịp nên 429 là ngoại lệ chứ không phải thường lệ.
    # Hạn này đặt rộng để một đợt nghẽn ngắn không làm đổ cả lượt chạy dài.
    max_rotations = len(pool) * 6
    while True:
        # Đặt chỗ theo đúng lượng token của lô, để pool tự giãn nhịp và đường đi
        # bình thường không sinh 429.
        idx, client = pool.acquire(sum(est_tokens(t) for t in texts))
        try:
            resp = client.models.embed_content(model=EMBED_MODEL, contents=texts, config=cfg)
            return [_normalize(list(e.values)) for e in resp.embeddings]
        except Exception as e:  # noqa: BLE001
            if is_quota_error(e):
                rotations += 1
                if rotations > max_rotations:
                    raise SystemExit(
                        f"Cả {len(pool)} key đều hết hạn mức, dừng lượt chạy. "
                        f"Chạy lại sau khi hạn mức reset: embed hoạt động tăng dần "
                        f"nên sẽ tiếp tục từ chỗ dừng, không làm lại từ đầu."
                    ) from e
                pool.penalize()
                continue
            attempt += 1
            if attempt >= _MAX_RETRY:
                raise
            wait = 2 ** (attempt - 1) * 5
            print(f"  [embed] key #{idx + 1} lỗi ({type(e).__name__}), thử lại sau {wait}s: "
                  f"{str(e)[:120]}")
            time.sleep(wait)


def _batches(pending: list[dict]):
    """Gom lô theo ngân sách token thay vì theo số chunk cố định.

    Chunk summary dài khoảng 100 token, chunk body khoảng 950, nên một lô cố
    định 40 chunk có thể là 4.000 hoặc 38.000 token tuỳ thành phần. Vì cả lô đi
    trong một request, lô toàn body vượt trần TPM 30.000 ngay trong lần gọi đó
    và bị từ chối, kể cả khi hạn mức ngày còn nguyên.

    Chế độ hỏng này chỉ bộc lộ khi chạy tăng dần: các chunk ngắn embed xong
    trước, phần còn lại toàn là body, và từ đó mọi lô đều vượt trần — tiến độ
    dừng lại hẳn chứ không chậm dần.

    `BATCH` vẫn giữ vai trò trần trên số phần tử mỗi request; ngân sách token là
    ràng buộc quyết định.
    """
    lo: list[dict] = []
    tok = 0
    for p in pending:
        t = est_tokens(p["text"])
        if lo and (tok + t > _BATCH_TOKENS or len(lo) >= BATCH):
            yield lo
            lo, tok = [], 0
        lo.append(p)
        tok += t
    if lo:
        yield lo


def run() -> int:
    gclient = KeyPool()
    qc = vs.client()
    vs.ensure_collection(qc, EMBED_DIM)

    tb = _catalog().load_table(BRONZE_IDENT).scan().to_arrow()
    cols = {c: tb.column(c).to_pylist() for c in (
        "source", "article_id", "url", "title", "description", "content",
        "category", "sub_category", "topic", "tags",
        "published_at", "published_date", "thumbnail_url", "author_name",
        "source_attribution", "word_count",
    )}
    print(f"[embed] Bronze: {tb.num_rows} bài")

    # --- 1. Chunk toàn bộ + dựng point (chưa có vector) -----------------------
    pending: list[dict] = []
    for i in range(tb.num_rows):
        chunks = split(cols["title"][i], cols["description"][i], cols["content"][i])
        pub = cols["published_at"][i]
        # `tags` là từ khoá chủ đề, chỉ nối vào nhánh sparse BM25 chứ không vào
        # dense: đó là các từ khoá rời rạc, đưa vào embedding sẽ làm loãng
        # vector. Các trường định danh (author_name, source_attribution,
        # sub_category, topic) không vào cả hai nhánh — chúng được lọc chính xác
        # qua payload index.
        kw = bm25.keyword_text(tags=cols["tags"][i])
        for ch in chunks:
            pending.append({
                "id": vs.point_id(cols["source"][i], cols["article_id"][i], ch.idx),
                "text": ch.text,
                "keywords": kw,
                "payload": {
                    "article_id": cols["article_id"][i],
                    "url": cols["url"][i],
                    "source": cols["source"][i],
                    "category": cols["category"][i],
                    "sub_category": cols["sub_category"][i],
                    "topic": cols["topic"][i],
                    "tags": list(cols["tags"][i] or []),
                    "title": cols["title"][i],
                    "description": cols["description"][i],
                    "author_name": cols["author_name"][i],
                    "source_attribution": list(cols["source_attribution"][i] or []),
                    "thumbnail_url": cols["thumbnail_url"][i],
                    "word_count": cols["word_count"][i],
                    "published_at": pub.isoformat() if pub else None,
                    "published_ts": int(pub.timestamp()) if pub else None,
                    "published_date": str(cols["published_date"][i]),
                    "chunk_idx": ch.idx,
                    "n_chunks": ch.n_chunks,
                    "chunk_type": ch.kind,      # summary | body
                    "text": ch.text,          # cần khi truy hồi để dựng prompt
                    # Băm theo từng chunk, không băm cả bài. Nếu băm cả bài thì
                    # sửa một ký tự ở đoạn thứ hai cũng buộc mọi chunk của bài
                    # embed lại, rất tốn với hạn mức 1.000 chunk mỗi ngày mỗi
                    # project. Đo trên một lượt sửa nội dung diện rộng: băm theo
                    # bài làm 85% số bài phải embed lại, băm theo chunk chỉ 45%
                    # số chunk — tiết kiệm khoảng 55%.
                    #
                    # Con số không xuống gần 0 vì `_equal_groups` chia đoạn theo
                    # tổng token: thêm vài ký tự làm mốc chia dịch đi, kéo theo
                    # cả những chunk có đoạn văn không hề bị sửa.
                    "content_hash": hashlib.sha256(
                        ch.text.encode()).hexdigest()[:16],
                    "embed_model": EMBED_MODEL,
                },
            })
    print(f"[embed] chunk: {len(pending)}")

    # --- 1b. Dọn point không còn thuộc bài nào (xem vs.prune_orphans) --------
    # Phải chạy trước bước lọc bên dưới: sau đó `pending` chỉ còn những chunk
    # cần embed, không còn là tập id hợp lệ đầy đủ để đối chiếu.
    vs.prune_orphans(qc, {p["id"] for p in pending})

    # --- 2. Bỏ point đã embed và nội dung không đổi ---------------------------
    if not FORCE:
        have = vs.existing_texts(qc, [p["id"] for p in pending])
        before = len(pending)
        pending = [p for p in pending if have.get(p["id"]) != p["payload"]["content_hash"]]
        print(f"[embed] đã có, bỏ qua: {before - len(pending)} | cần embed: {len(pending)}")
    if not pending:
        print("[embed] Không có gì mới.")
        return 0

    # --- 3. Embed theo lô + upsert -------------------------------------------
    done = 0
    for batch in _batches(pending):
        vectors = embed_texts(gclient, [p["text"] for p in batch], "RETRIEVAL_DOCUMENT")
        points = []
        for p, v in zip(batch, vectors):
            vec: dict = {vs.DENSE: v}
            # Nhánh sparse nhận thêm `tags` mà nhánh dense không nhận.
            idx, val = bm25.doc_vector(p["text"], p["keywords"])
            if idx:
                vec[vs.SPARSE] = qm.SparseVector(indices=idx, values=val)
            points.append(qm.PointStruct(id=p["id"], vector=vec, payload=p["payload"]))
        qc.upsert(collection_name=vs.COLLECTION, points=points)
        done += len(batch)
        print(f"[embed] {done}/{len(pending)}")

    print(f"[embed] Xong. {done} chunk vào Qdrant `{vs.COLLECTION}`.")
    return done


if __name__ == "__main__":
    run()
