"""Bronze -> chunk -> embedding (Gemini) -> Qdrant.

TĂNG DẦN: point id sinh từ (source, article_id, chunk_idx) nên ổn định giữa các
lần chạy. Trước khi gọi API, tra Qdrant xem point đã có chưa và `content_hash`
có đổi không — chỉ embed bài MỚI hoặc bài ĐƯỢC CẬP NHẬT. Embedding là bước đắt
nhất chuỗi nên đây là chỗ phải tiết kiệm.

    python -m newscrawler.embed              # chỉ bài chưa embed
    EMBED_FORCE=1 python -m newscrawler.embed  # embed lại tất cả (vd đổi model)
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
# Trần TRÊN số chunk trong một lô. Ràng buộc THẬT là `_BATCH_TOKENS` bên dưới —
# 40 chunk có thể là 4k token (toàn summary) hoặc 38k (toàn body), nên đếm chunk
# không nói lên điều gì về việc lô có vượt trần TPM hay không.
BATCH = int(os.getenv("EMBED_BATCH", "40"))
# Trần token cho MỘT request. Phải dưới trần TPM (30.000/phút/project) vì cả
# lô đi trong một lần gọi — vượt là 429 ngay, không liên quan hạn ngạch ngày.
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
    """gemini-embedding-001 chỉ chuẩn hóa sẵn ở 3072 chiều; khi cắt bớt chiều
    (Matryoshka) phải tự chuẩn hóa, nếu không cosine sẽ lệch."""
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v] if n else v


def embed_texts(pool: KeyPool, texts: list[str], task: str) -> list[list[float]]:
    """Gọi API embedding.

    Hai chế độ hỏng khác nhau, xử lý khác nhau:
      * hết quota (429)  -> cho key đó nghỉ; `pool.acquire()` tự chọn key sẵn
        sàng sớm nhất và CHỜ tới lúc dùng được. Không tính vào _MAX_RETRY vì
        đây không phải lỗi của request mà là key đó đang chạm trần TPM.
      * lỗi tạm thời khác -> retry lũy tiến trên cùng key.
    """
    cfg = types.EmbedContentConfig(task_type=task, output_dimensionality=EMBED_DIM)
    attempt = 0
    rotations = 0
    # Pool đã giãn nhịp chủ động nên 429 giờ là ngoại lệ, không phải thường lệ.
    # Nới hạn để một đợt nghẽn tạm thời không giết cả lượt chạy dài.
    max_rotations = len(pool) * 6
    while True:
        # Đặt chỗ theo ĐÚNG lượng token của lô này -> pool tự giãn nhịp,
        # đường đi bình thường không còn sinh 429 nào.
        idx, client = pool.acquire(sum(est_tokens(t) for t in texts))
        try:
            resp = client.models.embed_content(model=EMBED_MODEL, contents=texts, config=cfg)
            return [_normalize(list(e.values)) for e in resp.embeddings]
        except Exception as e:  # noqa: BLE001
            if is_quota_error(e):
                rotations += 1
                if rotations > max_rotations:
                    raise SystemExit(
                        f"Mọi key ({len(pool)}) đều hết quota. Dừng để không đốt thêm. "
                        f"Chạy lại sau khi quota reset — embed là tăng dần nên sẽ đi "
                        f"tiếp từ chỗ dừng, không làm lại từ đầu."
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
    """Gom lô theo NGÂN SÁCH TOKEN, không theo số chunk cố định.

    Lô cố định 40 chunk là bẫy: chunk summary ~100 token còn body ~950 token,
    nên cùng một con số cho ra lô 4k hoặc 38k token tuỳ nội dung. Trần TPM là
    30.000/phút -> lô toàn body vượt trần NGAY TRONG MỘT REQUEST và 429 tức
    khắc, dù hạn ngạch ngày còn nguyên.

    Đo được: sau khi các chunk ngắn đã embed xong, phần còn lại toàn body nên
    mọi lô đều ~38k token và không lô nào qua được — tiến độ đứng hẳn ở 62%.

    BATCH vẫn là trần TRÊN số chunk (chặn lô quá nhiều item); ngân sách token
    mới là ràng buộc thật.
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
        # `tags` (từ khóa chủ đề) -> CHỈ nối vào sparse BM25, không vào dense:
        # là từ khóa rời rạc, đưa vào embedding sẽ làm loãng vector.
        # author_name / source_attribution / sub_category / topic KHÔNG trộn vào
        # text — chúng là trường định danh, lọc chính xác qua payload index.
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
                    "text": ch.text,          # cần cho retrieval -> đưa vào prompt
                    # Băm TỪNG CHUNK, không băm cả bài. Băm cả bài thì sửa một
                    # ký tự ở đoạn 2 là MỌI chunk của bài phải embed lại — rất
                    # tốn với hạn ngạch 1.000 chunk/ngày/project.
                    # Đo trên lượt vá lỗi dính chữ: băm theo bài -> 85% số bài
                    # đổi; băm theo chunk -> chỉ 45% số chunk đổi, tiết kiệm 55%.
                    # (Không phải ~100%: `_equal_groups` chia đoạn theo tổng
                    # token, nên thêm vài ký tự làm mốc chia dịch, kéo theo cả
                    # chunk có đoạn văn không hề bị sửa.)
                    "content_hash": hashlib.sha256(
                        ch.text.encode()).hexdigest()[:16],
                    "embed_model": EMBED_MODEL,
                },
            })
    print(f"[embed] chunk: {len(pending)}")

    # --- 1b. Dọn point không còn thuộc bài nào (xem vs.prune_orphans) --------
    # Phải làm TRƯỚC bước lọc bên dưới, vì sau đó `pending` chỉ còn chunk cần embed.
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
            # sparse ăn thêm nhãn (tags/tác giả/...) mà dense không ăn
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
