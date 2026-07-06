"""Ghi bài báo trực tiếp vào bảng Iceberg tầng Bronze.

Bảng: <BRONZE_NAMESPACE>.<BRONZE_TABLE> (mặc định bronze.vnexpress_articles),
lưu trong warehouse trên MinIO, quản lý qua Iceberg REST Catalog. Mỗi lần
crawl 1 chuyên mục → 1 lần append (1 snapshot) vào bảng.
"""
import os
from datetime import date, datetime, timezone

import pyarrow as pa
from pyiceberg.catalog.rest import RestCatalog
from pyiceberg.exceptions import NamespaceAlreadyExistsError, NoSuchTableError

from .parser import Article

BRONZE_NAMESPACE = os.getenv("BRONZE_NAMESPACE", "bronze")
BRONZE_TABLE = os.getenv("BRONZE_TABLE", "vnexpress_articles")
TABLE_IDENT = f"{BRONZE_NAMESPACE}.{BRONZE_TABLE}"

# Explicit Arrow schema → stable Iceberg schema, independent of batch contents
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
    ("author_email", pa.string()),
    ("source_attribution", pa.string()),
    ("published_at", pa.timestamp("us", tz="UTC")),
    ("updated_at", pa.timestamp("us", tz="UTC")),
    ("crawled_at", pa.timestamp("us", tz="UTC")),
    ("raw_date", pa.date32()),
    ("source", pa.string()),
    ("crawl_status", pa.string()),
    ("http_status", pa.int64()),
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
                # Use pyarrow FileIO so no extra fsspec/s3fs deps are needed.
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
        return catalog.create_table(TABLE_IDENT, schema=SCHEMA)


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_row(a: Article) -> dict:
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
        "author_email": a.author_email,
        "source_attribution": a.source_attribution,
        "published_at": _utc(a.published_at),
        "updated_at": _utc(a.updated_at),
        "crawled_at": _utc(a.crawled_at),
        "raw_date": a.raw_date,
        "source": a.source,
        "crawl_status": a.crawl_status,
        "http_status": a.http_status,
    }


def save_articles(
    articles: list[Article],
    category_slug: str | None = None,
    target_date: date | None = None,
) -> int:
    if not articles:
        return 0

    # Dedup theo article_id trong batch (cùng bài, nhiều URL)
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
    table = _ensure_table(catalog)
    table.append(arrow_table)
    return len(rows)
