"""Parser VnExpress (https://vnexpress.net)."""
from __future__ import annotations

import re

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

_BASE = "https://vnexpress.net"


def _is_ad(tag) -> bool:
    node = tag
    for _ in range(6):
        node = node.parent
        if node is None:
            break
        if "item-ads" in (node.get("class", []) or []):
            return True
    return False


class VnExpressSource(BaseSource):
    name = "vnexpress"
    base_url = _BASE
    categories = {
        "Thời sự": f"{_BASE}/thoi-su",
        "Thế giới": f"{_BASE}/the-gioi",
        "Kinh doanh": f"{_BASE}/kinh-doanh",
        "Khoa học công nghệ": f"{_BASE}/khoa-hoc",
        "Sức khỏe": f"{_BASE}/suc-khoe",
        "Giải trí": f"{_BASE}/giai-tri",
        "Thể thao": f"{_BASE}/the-thao",
        "Giáo dục": f"{_BASE}/giao-duc",
    }

    def page_url(self, base_url: str, page: int) -> str | None:
        return base_url if page == 1 else f"{base_url}-p{page}"

    def parse_links(self, html: str) -> list[str]:
        soup = BeautifulSoup(html, "lxml")
        seen: set[str] = set()
        links: list[str] = []
        for tag in soup.select("h3.title-news a, h2.title-news a"):
            href = tag.get("href", "")
            if href.startswith(_BASE) and href not in seen and not _is_ad(tag):
                seen.add(href)
                links.append(href)
        return links

    def parse_article(self, html: str, url: str) -> Article:
        soup = BeautifulSoup(html, "lxml")
        for stamp in soup.select("span.location-stamp"):
            stamp.decompose()

        a = self.new_article(url)
        a.article_id = extract_id(url, r"-(\d+)\.html")

        a.title = txt(soup.select_one("h1.title-detail, h1.title_page_detail"))
        a.description = txt(soup.select_one("p.description, p.lead"))

        # sub_category / topic từ JSON-LD BreadcrumbList (chung mọi nguồn)
        self.fill_hierarchy(soup, a)

        a.tags = split_keywords(meta(soup, name="keywords"))

        body = soup.select_one("article.fck_detail, div.fck_detail")
        a.content, a.word_count = join_paragraphs(body)

        a.thumbnail_url = meta(soup, prop="og:image")

        author_tag = soup.select_one("p.author_mail strong")
        if not author_tag:
            for tag in reversed(soup.select("p.Normal strong")):
                text = tag.get_text(strip=True)
                if not text or len(text) > 50 or "\n" in text:
                    continue
                if tag.find_parent("a") is not None:
                    continue
                if text.startswith(">>") or "?" in text:
                    continue
                if any(q in text for q in ('"', "“", "”")):
                    continue
                if " " not in text:
                    continue
                author_tag = tag
                break
        if author_tag:
            a.author_name = author_tag.get_text(strip=True)
            byline = author_tag.find_parent("p")
            if byline:
                m = re.search(r"\(\s*Theo\s+(.*?)\s*\)", byline.get_text(" ", strip=True), re.IGNORECASE)
                if m:
                    a.source_attribution = m.group(1).strip()

        pub = meta(soup, name="pubdate", prop="article:published_time")
        a.published_at = parse_iso(pub)

        return a
