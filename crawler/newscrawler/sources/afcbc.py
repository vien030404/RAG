"""Parser dùng chung cho Tuổi Trẻ + Thanh Niên (cùng CMS "afcbc").

Cả 2 báo cùng cấu trúc: `h1.detail-title`, `[data-role=sapo]`,
`[data-role=content].afcbc-body`, meta `article:published_time` (ISO),
`[data-role=author]`. Nên 1 lớp tham số hóa xử lý được cả hai.
"""
from __future__ import annotations

from bs4 import BeautifulSoup

from ..article import Article
from .base import (
    BaseSource,
    extract_id,
    join_paragraphs,
    meta,
    parse_iso,
    split_keywords,
    txt,
)


class AfcbcSource(BaseSource):

    def __init__(self, name: str, base_url: str, categories: dict[str, str]):
        self.name = name
        self.base_url = base_url
        self.categories = categories

    def parse_links(self, html: str) -> list[str]:
        # bài viết: segment cuối có id số dài trước .htm (bỏ trang chuyên mục xxx.htm)
        return self.links_by_pattern(html, r"-\d{5,}\.htm$")

    def more_page_urls(self, base_url: str, page1_html: str, max_pages: int) -> list[str]:
        # tuoitre/thanhnien load-more bằng AJAX (endpoint /timeline/{zoneId}/...),
        # nhưng zoneId của category KHÔNG trích được ổn định từ HTML tĩnh (nạp qua
        # JS API) — ép sẽ dễ lấy nhầm category. -> chỉ crawl trang 1 cho an toàn.
        return []

    def parse_article(self, html: str, url: str) -> Article:
        soup = BeautifulSoup(html, "lxml")
        a = self.new_article(url)
        a.article_id = extract_id(url, r"-(\d{5,})\.htm$")

        a.title = txt(soup.select_one("h1.detail-title") or soup.find("h1"))
        a.description = txt(soup.select_one("[data-role=sapo]"))

        body = soup.select_one("[data-role=content]") or soup.select_one("div.detail-content")
        a.content, a.word_count = join_paragraphs(body)

        a.thumbnail_url = meta(soup, prop="og:image")
        a.tags = split_keywords(meta(soup, name="keywords"))
        self.fill_hierarchy(soup, a)

        a.published_at = parse_iso(meta(soup, prop="article:published_time"))

        # author name: ưu tiên meta article:author (Thanh Niên = tên sạch);
        # else gom các <.author-name> (Tuổi Trẻ), dedupe (khối [data-role=author]
        # lẫn text UI "và N tác giả khác" + bản sao ẩn -> không dùng text khối).
        author_meta = meta(soup, prop="article:author")
        if author_meta and "http" not in author_meta and "@" not in author_meta:
            a.author_name = author_meta
        else:
            names: list[str] = []
            for el in soup.select("div.author-info a.name, div.author-item-name a.name"):
                t = el.get_text(strip=True)
                if t and t not in names:
                    names.append(t)
            a.author_name = ", ".join(names)

        return a


# Hai instance dùng chung lớp trên
_TT = "https://tuoitre.vn"
_TN = "https://thanhnien.vn"

TuoiTreSource = AfcbcSource(
    "tuoitre", _TT,
    {
        "Thời sự": f"{_TT}/thoi-su.htm",
        "Thế giới": f"{_TT}/the-gioi.htm",
        "Kinh doanh": f"{_TT}/kinh-doanh.htm",
        "Giải trí": f"{_TT}/giai-tri.htm",
        "Thể thao": f"{_TT}/the-thao.htm",
        "Giáo dục": f"{_TT}/giao-duc.htm",
        "Sức khỏe": f"{_TT}/suc-khoe.htm",
        "Công nghệ": f"{_TT}/nhip-song-so.htm",
    },
)

ThanhNienSource = AfcbcSource(
    "thanhnien", _TN,
    {
        "Thời sự": f"{_TN}/thoi-su.htm",
        "Thế giới": f"{_TN}/the-gioi.htm",
        "Kinh tế": f"{_TN}/kinh-te.htm",
        "Giải trí": f"{_TN}/giai-tri.htm",
        "Thể thao": f"{_TN}/the-thao.htm",
        "Giáo dục": f"{_TN}/giao-duc.htm",
        "Sức khỏe": f"{_TN}/suc-khoe.htm",
        "Công nghệ": f"{_TN}/cong-nghe.htm",
    },
)
