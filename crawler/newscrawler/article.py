"""Bản ghi bài báo thống nhất cho mọi nguồn (schema chung của Bronze).

Mọi parser theo báo đều trả về `Article` này, nên bảng Iceberg
`bronze.articles` có cấu trúc đồng nhất, phân biệt nguồn qua cột `source`.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Article:
    # Định danh
    url: str
    url_hash: str = ""
    article_id: int | None = None

    # Phân loại
    category: str = ""
    sub_category: str = ""
    topic: str = ""
    tags: list[str] = field(default_factory=list)

    # Nội dung
    title: str = ""
    description: str = ""
    content: str = ""
    word_count: int = 0
    thumbnail_url: str = ""

    # Tác giả
    author_name: str = ""
    # Kiểu mảng chứ không phải chuỗi: một bài thường dẫn nhiều hãng cùng lúc
    # ("AFP, Reuters"), và để nguyên chuỗi thì lọc theo từng hãng sẽ trượt.
    # Gán qua `sources.base.split_sources` để tách sẵn thành phần.
    source_attribution: list[str] = field(default_factory=list)

    # Thời gian
    published_at: datetime | None = None
    crawled_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Siêu dữ liệu
    source: str = ""           # "vnexpress" | "vietnamnet" | "tuoitre" | ...
    crawl_status: str = "success"
