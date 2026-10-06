"""Parser VietnamNet (https://vietnamnet.vn).

Hai điểm khác biệt so với các nguồn còn lại. Thứ nhất, trang không có meta
`article:published_time`, nên ngày giờ phải parse từ text của `div.publish-date`
ở dạng "07/07/2026 09:00 (GMT+07:00)". Thứ hai, `meta[name=author]` chỉ chứa tên
toà soạn chứ không phải tên người, nên tác giả thật lấy từ
`div.article-detail-author__main`.
"""
from __future__ import annotations

from bs4 import BeautifulSoup

from ..article import Article
from .base import (
    BaseSource,
    extract_id,
    join_paragraphs,
    meta,
    parse_vn_datetime,
    split_keywords,
    txt,
)

_BASE = "https://vietnamnet.vn"


class VietnamNetSource(BaseSource):
    name = "vietnamnet"
    base_url = _BASE
    categories = {
        "Thời sự": f"{_BASE}/thoi-su",
        "Kinh doanh": f"{_BASE}/kinh-doanh",
        "Thế giới": f"{_BASE}/the-gioi",
        "Giải trí": f"{_BASE}/giai-tri",
        "Thể thao": f"{_BASE}/the-thao",
        "Giáo dục": f"{_BASE}/giao-duc",
        "Sức khỏe": f"{_BASE}/suc-khoe",
        "Công nghệ": f"{_BASE}/cong-nghe",
    }

    def page_url(self, base_url: str, page: int) -> str | None:
        return base_url if page == 1 else f"{base_url}-page{page}"

    def parse_links(self, html: str) -> list[str]:
        return self.links_by_pattern(html, r"-\d+\.html?$")

    def parse_article(self, html: str, url: str) -> Article:
        soup = BeautifulSoup(html, "lxml")
        a = self.new_article(url)
        a.article_id = extract_id(url, r"-(\d+)\.html?$")

        a.title = txt(soup.select_one("h1.content-detail-title"))
        a.description = txt(soup.select_one("h2.content-detail-sapo"))

        body = soup.select_one("div.maincontent.main-content") or soup.select_one("#maincontent")
        a.content, a.word_count = join_paragraphs(body)

        a.thumbnail_url = meta(soup, prop="og:image")
        a.tags = split_keywords(meta(soup, name="keywords"))
        self.fill_hierarchy(soup, a)

        # Khối tác giả gồm <span class="name"> đi liền <span class="position">,
        # trong đó position là chức danh như "Nhà báo" hoặc một mô tả dài hơn.
        # Lấy text của cả khối sẽ nối tên với chức danh thành một chuỗi dính
        # liền, nên chỉ đọc span.name — chức danh không phải phần của tên.
        block = (soup.select_one("div.article-detail-author__main")
                 or soup.select_one("p.article-detail-author__info"))
        if block is not None:
            a.author_name = txt(block.select_one("span.name")) or block.get_text(" ", strip=True)

        pub = soup.select_one("div.publish-date") or soup.select_one("div.bread-crumb-detail__time")
        a.published_at = parse_vn_datetime(txt(pub))

        return a
