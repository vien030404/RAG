"""Lưu HTML thô (nén gzip) vào bucket `raw` trên MinIO — tầng raw của lakehouse.

Mỗi trang bài -> 1 object: raw/source=<src>/dt=<YYYY-MM-DD>/<url_hash>.html.gz
Đây là object thường (không phải Iceberg), cho phép re-parse lại về sau mà
không cần crawl lại.
"""
import gzip
import os
from datetime import datetime, timezone

import boto3
from botocore.config import Config

RAW_BUCKET = os.getenv("RAW_BUCKET", "raw")

_s3 = None


def _client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client(
            "s3",
            endpoint_url=os.getenv("MINIO_ENDPOINT", "http://minio:9000"),
            aws_access_key_id=os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
            aws_secret_access_key=os.getenv("MINIO_SECRET_KEY", "minioadmin"),
            region_name=os.getenv("AWS_REGION", "us-east-1"),
            # MinIO cần path-style (bucket trong path, không phải subdomain)
            config=Config(s3={"addressing_style": "path"}),
        )
    return _s3


def save_raw_html(source: str, url_hash: str, html: str, when: datetime | None = None) -> str:
    """Nén và ghi HTML thô. Trả về object key."""
    when = when or datetime.now(timezone.utc)
    key = f"source={source}/dt={when:%Y-%m-%d}/{url_hash}.html.gz"
    body = gzip.compress(html.encode("utf-8"))
    _client().put_object(
        Bucket=RAW_BUCKET,
        Key=key,
        Body=body,
        ContentType="text/html; charset=utf-8",
        ContentEncoding="gzip",
    )
    return key


def load_raw_html(source: str, url_hash: str, when: datetime) -> str | None:
    """Đọc lại HTML thô đã lưu. `when` là ngày CRAWL (crawled_at), không phải ngày đăng.

    Trả None nếu không có object — bài cào trước khi bật lưu raw, hoặc lệch ngày.
    """
    key = f"source={source}/dt={when:%Y-%m-%d}/{url_hash}.html.gz"
    try:
        obj = _client().get_object(Bucket=RAW_BUCKET, Key=key)
    except Exception:
        return None
    return gzip.decompress(obj["Body"].read()).decode("utf-8", errors="replace")
