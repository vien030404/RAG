"""Qdrant: collection hybrid (dense + BM25 sparse), index payload, tìm kiếm.

Dùng chung cho `embed.py` (ghi) và `ask.py` (đọc).

Collection có 2 vector đặt tên:
  - `dense` : embedding Gemini, cosine — bắt ngữ nghĩa ("xe điện" ~ "ô tô chạy pin")
  - `bm25`  : sparse term-frequency, IDF do Qdrant tính — bắt khớp CHÍNH XÁC từ
              (tên riêng, số liệu, mã hiệu) vốn là điểm yếu của dense vector.
Truy vấn hợp nhất 2 bảng xếp hạng bằng RRF ngay trên server (1 request).
"""
from __future__ import annotations

import hashlib
import os
import uuid

from qdrant_client import QdrantClient, models as qm

QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
COLLECTION = os.getenv("QDRANT_COLLECTION", "news_chunks")
EMBED_DIM = int(os.getenv("EMBED_DIM", "768"))

DENSE = "dense"
SPARSE = "bm25"

# Namespace cố định -> point id sinh ra ổn định giữa các lần chạy, nên chạy lại
# embed không tạo bản trùng mà ghi đè đúng point cũ (idempotent).
_NS = uuid.UUID("6f1d4a5e-1c2b-4f3a-9d8e-7a6b5c4d3e2f")


def point_id(source: str, article_id, chunk_idx: int) -> str:
    return str(uuid.uuid5(_NS, f"{source}:{article_id}:{chunk_idx}"))


def client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=120)


def ensure_collection(qc: QdrantClient, dim: int = EMBED_DIM) -> None:
    """Tạo collection + index cho các trường sẽ lọc. An toàn khi gọi lại."""
    if not qc.collection_exists(COLLECTION):
        qc.create_collection(
            collection_name=COLLECTION,
            vectors_config={DENSE: qm.VectorParams(size=dim, distance=qm.Distance.COSINE)},
            sparse_vectors_config={
                # IDF tính ở server -> client chỉ cần gửi term frequency, và IDF
                # luôn phản ánh đúng corpus hiện tại khi kho tin lớn dần.
                SPARSE: qm.SparseVectorParams(modifier=qm.Modifier.IDF),
            },
        )
        print(f"[qdrant] tạo collection {COLLECTION} (dense={dim} cosine + sparse bm25/IDF)")

    # Index cho các trường ĐỊNH DANH: lọc bằng so khớp CHÍNH XÁC, không trộn vào
    # text để khỏi mất tính định vị (vd tác giả "Ngọc Ánh" lẫn với "Ngọc Ánh"
    # được nhắc trong thân bài).
    for field, schema in (
        ("source", qm.PayloadSchemaType.KEYWORD),
        # `category` PHẢI có index: nó là bộ lọc trong ask.py và là dropdown
        # thứ hai trên giao diện. Thiếu index thì Qdrant vẫn trả kết quả ĐÚNG
        # nhưng quét toàn collection — đo được 337ms so với ~5ms của các
        # trường có index. Trước đây vai lọc thuộc về `category_std`; khi bỏ
        # cột đó thì `category` lên thay mà danh sách này không đổi theo.
        ("category", qm.PayloadSchemaType.KEYWORD),
        ("chunk_type", qm.PayloadSchemaType.KEYWORD),   # summary | body
        ("sub_category", qm.PayloadSchemaType.KEYWORD),
        ("topic", qm.PayloadSchemaType.KEYWORD),
        ("author_name", qm.PayloadSchemaType.KEYWORD),
        ("source_attribution", qm.PayloadSchemaType.KEYWORD),  # list, như `tags`
        ("tags", qm.PayloadSchemaType.KEYWORD),      # list -> khớp bất kỳ phần tử
        ("published_ts", qm.PayloadSchemaType.INTEGER),
        ("published_date", qm.PayloadSchemaType.KEYWORD),
        ("article_id", qm.PayloadSchemaType.INTEGER),
    ):
        try:
            qc.create_payload_index(COLLECTION, field_name=field, field_schema=schema)
        except Exception:  # noqa: BLE001 - index đã tồn tại
            pass


def existing_texts(qc: QdrantClient, ids: list[str]) -> dict[str, str]:
    """{point_id: sha256(text đã lưu)} của các point ĐÃ có.

    Băm lại tại chỗ từ trường `text` trong payload, KHÔNG đọc `content_hash` đã
    lưu. Lý do: `content_hash` phụ thuộc cách băm của phiên bản code ghi ra nó —
    khi đổi cách băm (cả bài -> từng chunk) thì mọi point cũ đều "khác" và phải
    embed lại toàn bộ, dù văn bản y hệt. Đo được: so bằng content_hash chỉ bỏ
    qua được 615/12.302 chunk; so bằng chính văn bản thì đúng số chunk thật sự
    đổi. Cách này miễn nhiễm với mọi lần đổi cách băm về sau.
    """
    out: dict[str, str] = {}
    for i in range(0, len(ids), 256):
        got = qc.retrieve(
            COLLECTION, ids=ids[i : i + 256],
            with_payload=["text"], with_vectors=False,
        )
        for p in got:
            t = (p.payload or {}).get("text") or ""
            out[str(p.id)] = hashlib.sha256(t.encode()).hexdigest()[:16]
    return out


def prune_orphans(qc: QdrantClient, valid_ids: set[str]) -> int:
    """Xoá point không còn thuộc bài nào trong Bronze.

    Point id gắn với `chunk_idx`, nên khi số chunk của một bài GIẢM, các point ở
    chỉ số lớn hơn không bị ghi đè (id khác) — chúng thành mồ côi nhưng vẫn tham
    gia truy hồi và trả về VĂN BẢN CŨ. Ba trường hợp gây ra:

      * báo sửa bài ngắn lại ở lần crawl sau
      * đổi CHUNK_MAX_TOKENS / CHUNK_SINGLE_TOKENS -> số chunk đổi hàng loạt
      * `compact` khử trùng bỏ hẳn một dòng khỏi Bronze

    KHÔNG phải để dọn sau lượt vá lỗi dính chữ: bản vá CHÈN thêm dấu cách nên
    content dài ra chứ không ngắn đi (đo 300 bài: 253 dài hơn, 5 ngắn hơn) và số
    chunk không đổi ở bài nào.

    Quét toàn collection nhưng chỉ lấy id, không lấy payload/vector -> rẻ, và
    không tốn request Gemini nào.
    """
    stale, offset = [], None
    while True:
        pts, offset = qc.scroll(COLLECTION, limit=1000, offset=offset,
                                with_payload=False, with_vectors=False)
        stale += [p.id for p in pts if str(p.id) not in valid_ids]
        if offset is None:
            break
    if stale:
        qc.delete(COLLECTION, points_selector=qm.PointIdsList(points=stale))
        print(f"[qdrant] xoá {len(stale)} point mồ côi")
    return len(stale)


def hybrid_search(
    qc: QdrantClient,
    dense_vec: list[float],
    sparse_idx: list[int],
    sparse_val: list[float],
    limit: int,
    query_filter: qm.Filter | None = None,
    candidates: int | None = None,
    with_vectors: bool = False,
) -> list:
    """Chạy song song 2 nhánh rồi hợp nhất bằng RRF (Reciprocal Rank Fusion).

    RRF chỉ dùng THỨ HẠNG nên không cần chuẩn hóa điểm giữa cosine và BM25 —
    hai thang điểm vốn không so sánh trực tiếp được.
    Mỗi nhánh lấy dư (`candidates`) để RRF có đủ ứng viên xếp lại.
    """
    candidates = candidates or max(limit * 4, 40)
    prefetch = [
        qm.Prefetch(query=dense_vec, using=DENSE, limit=candidates, filter=query_filter),
    ]
    if sparse_idx:
        prefetch.append(
            qm.Prefetch(
                query=qm.SparseVector(indices=sparse_idx, values=sparse_val),
                using=SPARSE, limit=candidates, filter=query_filter,
            )
        )
    res = qc.query_points(
        collection_name=COLLECTION,
        prefetch=prefetch,
        query=qm.FusionQuery(fusion=qm.Fusion.RRF),
        limit=limit,
        query_filter=query_filter,
        with_payload=True,
        # Lấy kèm vector dense để tầng trên tính được COSINE THẬT. Điểm RRF chỉ
        # phản ánh THỨ HẠNG nên truy vấn vô nghĩa vẫn có hạng 1 điểm cao —
        # không dùng làm ngưỡng "có dữ liệu hay không" được.
        with_vectors=[DENSE] if with_vectors else False,
    )
    return res.points
