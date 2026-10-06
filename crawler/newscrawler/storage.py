"""Ghi bài báo trực tiếp vào bảng Iceberg tầng Bronze (thống nhất mọi nguồn).

Bảng: <BRONZE_NAMESPACE>.<BRONZE_TABLE> (mặc định bronze.articles),
lưu trong warehouse trên MinIO, quản lý qua Iceberg REST Catalog. Mỗi lần
crawl 1 chuyên mục (1 nguồn) → 1 lần append (1 snapshot) vào bảng; phân biệt
nguồn qua cột `source`.
"""
import os
import time
from datetime import date, datetime, timezone

import pyarrow as pa
from pyiceberg.catalog.rest import RestCatalog
from pyiceberg.exceptions import (
    CommitFailedException,
    NamespaceAlreadyExistsError,
    NoSuchTableError,
    TableAlreadyExistsError,
)
from pyiceberg.expressions import EqualTo

from .article import Article

_COMMIT_RETRIES = 5

BRONZE_NAMESPACE = os.getenv("BRONZE_NAMESPACE", "bronze")
BRONZE_TABLE = os.getenv("BRONZE_TABLE", "articles")
TABLE_IDENT = f"{BRONZE_NAMESPACE}.{BRONZE_TABLE}"

# Khai báo schema Arrow tường minh để schema Iceberg ổn định, không phụ thuộc
# vào nội dung của lô dữ liệu đầu tiên ghi vào bảng.
# (avoids null-type inference when a column happens to be all-null in a batch).
SCHEMA = pa.schema([
    ("url_hash", pa.string()),
    ("url", pa.string()),
    ("article_id", pa.int64()),
    ("category", pa.string()),
    ("sub_category", pa.string()),
    ("topic", pa.string()),
    ("tags", pa.list_(pa.string())),
    ("title", pa.string()),
    ("description", pa.string()),
    ("content", pa.string()),
    ("word_count", pa.int64()),
    ("thumbnail_url", pa.string()),
    ("author_name", pa.string()),
    # mảng như `tags`: 1 bài hay dẫn nhiều hãng ("AFP, Reuters") -> lọc theo phần tử
    ("source_attribution", pa.list_(pa.string())),
    ("published_at", pa.timestamp("us", tz="UTC")),
    ("crawled_at", pa.timestamp("us", tz="UTC")),
    ("source", pa.string()),
    ("crawl_status", pa.string()),
    # --- cột dẫn xuất, tính luôn lúc ghi ---
    ("published_date", pa.date32()),
    ("content_len", pa.int64()),
])

_catalog = None


def _catalog_conn() -> RestCatalog:
    global _catalog
    if _catalog is None:
        _catalog = RestCatalog(
            name="rest",
            **{
                "uri": os.getenv("ICEBERG_REST_URI", "http://iceberg-rest:8181"),
                "warehouse": os.getenv("ICEBERG_WAREHOUSE", "s3://warehouse/"),
                # Dùng FileIO của pyarrow để không cần thêm fsspec hay s3fs.
                "py-io-impl": "pyiceberg.io.pyarrow.PyArrowFileIO",
                "s3.endpoint": os.getenv("MINIO_ENDPOINT", "http://minio:9000"),
                "s3.access-key-id": os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
                "s3.secret-access-key": os.getenv("MINIO_SECRET_KEY", "minioadmin"),
                "s3.path-style-access": "true",
                "s3.region": os.getenv("AWS_REGION", "us-east-1"),
            },
        )
    return _catalog


def _ensure_table(catalog: RestCatalog):
    try:
        catalog.create_namespace(BRONZE_NAMESPACE)
    except NamespaceAlreadyExistsError:
        pass
    try:
        return catalog.load_table(TABLE_IDENT)
    except NoSuchTableError:
        try:
            return catalog.create_table(TABLE_IDENT, schema=SCHEMA)
        except TableAlreadyExistsError:
            # 1 task nguồn song song vừa tạo bảng trước -> load lại
            return catalog.load_table(TABLE_IDENT)


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_row(a: Article) -> dict:
    pub = _utc(a.published_at)
    return {
        "url_hash": a.url_hash,
        "url": a.url,
        "article_id": a.article_id,
        "category": a.category,
        "sub_category": a.sub_category,
        "topic": a.topic,
        "tags": a.tags or [],
        "title": a.title,
        "description": a.description,
        "content": a.content,
        "word_count": a.word_count,
        "thumbnail_url": a.thumbnail_url,
        "author_name": a.author_name,
        "source_attribution": a.source_attribution or [],
        "published_at": pub,
        "crawled_at": _utc(a.crawled_at),
        "source": a.source,
        "crawl_status": a.crawl_status,
        "published_date": pub.date() if pub else None,
        "content_len": len(a.content or ""),
    }


def known_published_dates(source_name: str) -> dict[str, str]:
    """Trả về {url_hash: ngày xuất bản} của những bài đã có trong Bronze.

    Đây là trạng thái chống trùng của crawler, và nó phải nằm trong bảng chứ
    không nằm trong bộ nhớ tiến trình: mỗi lượt crawl chạy trong một container
    mới, nên trạng thái trong RAM sẽ mất và bài đã cào bị cào lại từ đầu mỗi khi
    DAG chạy lại cùng một ngày — lúc run bị dồn, lúc retry, hay khi trigger tay.

    Hàm trả về cả ngày xuất bản chứ không chỉ tập `url_hash`, để logic dừng phân
    trang vẫn tính được `newest_on_page` cho những link đã bỏ qua. Thiếu thông
    tin đó, vòng lặp sẽ dừng sai thời điểm.

    Kết quả lọc theo `source`, nên các task của từng nguồn chạy song song không
    ảnh hưởng lẫn nhau.
    """
    try:
        table = _catalog_conn().load_table(TABLE_IDENT)
    except NoSuchTableError:
        return {}                     # lần chạy đầu, chưa có bảng
    tb = table.scan(
        row_filter=EqualTo("source", source_name),
        selected_fields=("url_hash", "published_at"),
    ).to_arrow()
    if tb.num_rows == 0:
        return {}
    hashes = tb.column("url_hash").to_pylist()
    pubs = tb.column("published_at").to_pylist()
    return {h: p.date() for h, p in zip(hashes, pubs) if h and p is not None}


def save_articles(
    articles: list[Article],
    category_slug: str | None = None,
    target_date: date | None = None,
) -> int:
    if not articles:
        return 0

    # Khử trùng theo `article_id` trong phạm vi lô: cùng một bài có thể xuất
    # hiện dưới nhiều URL khác nhau trên các trang listing.
    seen: set[int] = set()
    rows: list[dict] = []
    for a in articles:
        if a.article_id is not None:
            if a.article_id in seen:
                continue
            seen.add(a.article_id)
        rows.append(_to_row(a))

    arrow_table = pa.Table.from_pylist(rows, schema=SCHEMA)

    catalog = _catalog_conn()
    _ensure_table(catalog)  # đảm bảo namespace + bảng tồn tại

    # Các task của nhiều nguồn append song song vào cùng một bảng, nên commit có
    # thể xung đột — REST catalog dùng compare-and-swap trên con trỏ metadata.
    # Cách xử lý là nạp lại metadata mới nhất rồi thử lại.
    last_err: Exception | None = None
    for attempt in range(_COMMIT_RETRIES):
        try:
            table = catalog.load_table(TABLE_IDENT)
            table.append(arrow_table)
            return len(rows)
        except CommitFailedException as e:
            last_err = e
            time.sleep(0.5 * (attempt + 1))
    raise last_err  # hết lượt retry vẫn xung đột
