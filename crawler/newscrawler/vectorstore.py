"""Lớp Qdrant: collection hybrid, index payload và truy vấn.

Dùng chung cho `embed.py` ở chiều ghi và `ask.py` ở chiều đọc.

Collection khai báo hai vector đặt tên trên cùng một point:

    dense   embedding Gemini, khoảng cách cosine. Bắt tương đồng ngữ nghĩa,
            ví dụ "xe điện" khớp với "ô tô chạy pin".
    bm25    sparse term-frequency, IDF do Qdrant tính. Bắt khớp đúng mặt chữ —
            tên riêng, số liệu, mã hiệu — vốn là điểm yếu của dense vector.

Một truy vấn chạy cả hai nhánh rồi hợp nhất hai bảng xếp hạng bằng RRF ngay trên
server, nên chỉ tốn một request.
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

# Namespace cố định khiến point id sinh ra ổn định giữa các lần chạy: chạy lại
# embed ghi đè đúng point cũ thay vì tạo bản trùng, nên thao tác idempotent.
_NS = uuid.UUID("6f1d4a5e-1c2b-4f3a-9d8e-7a6b5c4d3e2f")


def point_id(source: str, article_id, chunk_idx: int) -> str:
    return str(uuid.uuid5(_NS, f"{source}:{article_id}:{chunk_idx}"))


def client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=120)


def ensure_collection(qc: QdrantClient, dim: int = EMBED_DIM) -> None:
    """Tạo collection và index cho các trường dùng để lọc. An toàn khi gọi lại."""
    if not qc.collection_exists(COLLECTION):
        qc.create_collection(
            collection_name=COLLECTION,
            vectors_config={DENSE: qm.VectorParams(size=dim, distance=qm.Distance.COSINE)},
            sparse_vectors_config={
                # IDF tính ở server: client chỉ gửi term frequency, và IDF luôn
                # phản ánh đúng corpus hiện tại khi kho tin lớn dần.
                SPARSE: qm.SparseVectorParams(modifier=qm.Modifier.IDF),
            },
        )
        print(f"[qdrant] tạo collection {COLLECTION} (dense={dim} cosine + sparse bm25/IDF)")

    # Index cho các trường định danh. Chúng được lọc bằng so khớp chính xác chứ
    # không trộn vào text, để không mất tính định vị: tên một tác giả phải phân
    # biệt được với chính tên đó khi xuất hiện trong thân bài.
    #
    # Mọi trường xuất hiện trong bộ lọc đều cần index. Thiếu index thì Qdrant
    # vẫn trả kết quả đúng nhưng phải quét toàn collection — đo trên collection
    # hiện tại là 337 ms so với khoảng 5 ms khi có index.
    for field, schema in (
        ("source", qm.PayloadSchemaType.KEYWORD),
        ("category", qm.PayloadSchemaType.KEYWORD),
        ("chunk_type", qm.PayloadSchemaType.KEYWORD),   # summary | body
        ("sub_category", qm.PayloadSchemaType.KEYWORD),
        ("topic", qm.PayloadSchemaType.KEYWORD),
        ("author_name", qm.PayloadSchemaType.KEYWORD),
        ("source_attribution", qm.PayloadSchemaType.KEYWORD),  # mảng, như `tags`
        ("tags", qm.PayloadSchemaType.KEYWORD),   # mảng: khớp bất kỳ phần tử
        ("published_ts", qm.PayloadSchemaType.INTEGER),
        ("published_date", qm.PayloadSchemaType.KEYWORD),
        ("article_id", qm.PayloadSchemaType.INTEGER),
    ):
        try:
            qc.create_payload_index(COLLECTION, field_name=field, field_schema=schema)
        except Exception:  # noqa: BLE001 — index đã tồn tại
            pass


def existing_texts(qc: QdrantClient, ids: list[str]) -> dict[str, str]:
    """Trả về {point_id: sha256(text đang lưu)} cho những point đã tồn tại.

    Băm lại tại chỗ từ trường `text` trong payload thay vì đọc `content_hash`
    đã lưu. `content_hash` phụ thuộc vào cách băm của phiên bản code đã ghi nó,
    nên mỗi lần thay đổi phạm vi băm là toàn bộ point cũ bị coi là đã đổi và
    phải embed lại dù văn bản y nguyên — đo trên corpus thì so bằng
    `content_hash` chỉ bỏ qua được 615 trong 12.302 chunk, còn so bằng chính văn
    bản cho ra đúng số chunk thực sự thay đổi. Cách này không bị ảnh hưởng bởi
    những lần đổi cách băm về sau.
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
    """Xoá những point không còn thuộc bài nào trong Bronze.

    Point id được dẫn từ `chunk_idx`, nên khi số chunk của một bài giảm, các
    point ở chỉ số lớn hơn mang id không còn được sinh lại và do đó không bị ghi
    đè. Chúng trở thành mồ côi nhưng vẫn tham gia truy hồi và trả về văn bản cũ.
    Ba tình huống dẫn tới trạng thái đó:

      * báo sửa bài ngắn lại, lần crawl sau sinh ít chunk hơn
      * đổi CHUNK_MAX_TOKENS hoặc CHUNK_SINGLE_TOKENS, số chunk đổi hàng loạt
      * `compact` khử trùng và bỏ hẳn một dòng khỏi Bronze

    Hàm quét toàn collection nhưng chỉ lấy id, không lấy payload hay vector, nên
    chi phí thấp và không tốn request embedding nào.
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
    """Chạy hai nhánh truy hồi rồi hợp nhất bằng RRF (Reciprocal Rank Fusion).

    RRF chỉ dựa trên thứ hạng nên không cần chuẩn hoá điểm giữa cosine và BM25,
    hai thang điểm vốn không so sánh trực tiếp được với nhau.

    Mỗi nhánh lấy dư `candidates` ứng viên để RRF còn đủ dữ liệu xếp lại. Bộ lọc
    payload được áp trong từng nhánh Prefetch, tức trước khi chấm điểm, nên
    `limit` luôn đếm trên các point đã thoả điều kiện.
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
        # Lấy kèm vector dense để tầng trên tính được cosine thực. Điểm RRF chỉ
        # phản ánh thứ hạng, nên một truy vấn không liên quan gì tới corpus vẫn
        # có kết quả hạng nhất với điểm cao; không thể dùng nó làm ngưỡng phán
        # đoán "có dữ liệu hay không".
        with_vectors=[DENSE] if with_vectors else False,
    )
    return res.points
