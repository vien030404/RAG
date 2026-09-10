"""Parser Dân trí (https://dantri.com.vn).

Khó nhất: class Tailwind `dt-*` KHÔNG ổn định, có 2 layout (thường +
e-magazine), KHÔNG có meta published_time. Chiến lược: dựa vào meta chuẩn
(og:image, keywords), thẻ <time> ("Thứ ba, 07/07/2026 - 06:01"), wrapper
ngữ nghĩa còn sót (e-magazine__body), và fallback mật độ <p>.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..article import Article
from .base import (
    BaseSource,
    author_from_jsonld,
    extract_id,
    join_paragraphs,
    meta,
    parse_vn_datetime,
    split_keywords,
    split_sources,
    theo_source,
    tightest_body,
    txt,
)

_BASE = "https://dantri.com.vn"
_PREFIX_RE = re.compile(r"^\s*\(Dân trí\)\s*-\s*")


class DanTriSource(BaseSource):
    name = "dantri"
    base_url = _BASE
    categories = {
        "Xã hội": f"{_BASE}/xa-hoi.htm",
        "Thế giới": f"{_BASE}/the-gioi.htm",
        "Kinh doanh": f"{_BASE}/kinh-doanh.htm",
        "Giải trí": f"{_BASE}/giai-tri.htm",
        "Thể thao": f"{_BASE}/the-thao.htm",
        "Giáo dục": f"{_BASE}/giao-duc.htm",
        "Sức khỏe": f"{_BASE}/suc-khoe.htm",
        "Công nghệ": f"{_BASE}/cong-nghe.htm",
    }

    def page_url(self, base_url: str, page: int) -> str | None:
        if page == 1:
            return base_url
        # xa-hoi.htm -> xa-hoi/trang-2.htm
        return f"{base_url[:-4]}/trang-{page}.htm"

    def parse_links(self, html: str) -> list[str]:
        # bài: id timestamp dài (>=12 số) trước .htm; bỏ trang chủ đề /event/
        return self.links_by_pattern(html, r"-\d{12,}\.htm$", exclude=("/event/",))

    def parse_article(self, html: str, url: str) -> Article:
        soup = BeautifulSoup(html, "lxml")
        a = self.new_article(url)
        a.article_id = extract_id(url, r"-(\d{12,})\.htm$")

        a.title = txt(soup.select_one("h1.e-magazine__title") or soup.find("h1"))

        sapo = soup.select_one("h2.e-magazine__sapo") or soup.find("h2")
        a.description = _PREFIX_RE.sub("", txt(sapo))

        body = (
            soup.select_one("div.e-magazine__body")
            or soup.select_one("article.singular-container")
            or soup.select_one("div.singular-content")
            or tightest_body(soup)
        )
        a.content, a.word_count = join_paragraphs(body)

        a.thumbnail_url = meta(soup, prop="og:image")
        a.tags = split_keywords(meta(soup, name="keywords"))
        self.fill_hierarchy(soup, a)

        # published: thẻ <time> đầu tiên có ngày
        for tel in soup.find_all("time"):
            dt = parse_vn_datetime(txt(tel))
            if dt:
                a.published_at = dt
                break

        # author: JSON-LD NewsArticle.author (chuẩn, sạch), fallback link byline
        # /tac-gia/. (Heuristic <b> cuối bài cũ bắt nhầm tiêu đề/URL nên đã bỏ.)
        a.author_name = author_from_jsonld(soup) or txt(soup.select_one("a[href*='/tac-gia/']"))

        # source_attribution: bài dịch/quốc tế kết bằng "Theo <Nguồn>" (hiếm)
        a.source_attribution = split_sources(theo_source(soup))

        return a
