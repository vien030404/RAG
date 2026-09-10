"""Bản ghi bài báo thống nhất cho mọi nguồn (schema chung của Bronze).

Mọi parser theo báo đều trả về `Article` này, nên bảng Iceberg
`bronze.articles` có cấu trúc đồng nhất, phân biệt nguồn qua cột `source`.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Article:
    # Identity
    url: str
    url_hash: str = ""
    article_id: int | None = None

    # Category
    category: str = ""
    sub_category: str = ""
    topic: str = ""
    tags: list[str] = field(default_factory=list)

    # Content
    title: str = ""
    description: str = ""
    content: str = ""
    word_count: int = 0
    thumbnail_url: str = ""

    # Author
    author_name: str = ""
    # Mảng, KHÔNG phải chuỗi: 1 bài thường dẫn nhiều hãng ("AFP, Reuters").
    # Gán qua `sources.base.split_sources` để tách sẵn thành phần.
    source_attribution: list[str] = field(default_factory=list)

    # Time
    published_at: datetime | None = None
    crawled_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Meta
    source: str = ""           # "vnexpress" | "vietnamnet" | "tuoitre" | ...
    crawl_status: str = "success"
