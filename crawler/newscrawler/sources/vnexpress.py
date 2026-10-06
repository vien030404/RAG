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
    split_sources,
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


# Chức danh đứng trước tên trong byline bài tư vấn ("Bác sĩ CKI Lê Ngọc Vinh").
_TITLE_PREFIX_RE = re.compile(
    r"^(?:(?:ths|thạc sĩ|ts|tiến sĩ|bs|bác sĩ|pgs|gs|cki|ckii|dược sĩ|ds)[.,\s]+)+",
    re.IGNORECASE,
)


def _looks_like_name(text: str) -> bool:
    """Byline VnExpress là tên người: 2-6 từ, mọi từ viết hoa đầu.

    Loại được tiêu đề phụ ("Hay quên, khó tập trung kéo dài", "Việt quất",
    "Đeo kính bảo vệ khi ra ngoài") vốn có từ viết thường.
    """
    core = _TITLE_PREFIX_RE.sub("", text).strip()
    words = core.split()
    if not 2 <= len(words) <= 6:
        return False
    return all(w[:1].isupper() for w in words)


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
        # Không khoá cứng vào thẻ h3: mỗi chuyên mục dùng một cấp heading khác
        # nhau — đo trên chuyên mục Khoa học công nghệ thấy 44 link trong h4 và 7
        # link trong h3, còn Thời sự chỉ dùng h3. Bám vào class .title-news phủ
        # được mọi biến thể layout.
        for tag in soup.select(".title-news a"):
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

        # Byline chuẩn (hiếm gặp trên bài thường).
        author_tag = soup.select_one("p.author_mail strong")
        byline_tag = None
        if not author_tag and body is not None:
            # Dạng byline thực tế của VnExpress nằm ở đoạn cuối bài và căn phải:
            #   <p class="Normal" style="text-align:right"><strong>Tên</strong> (Theo X)</p>
            #
            # Điều kiện nhận dạng bám vào `text-align:right` chứ không bám vào
            # "thẻ <strong> cuối cùng trong bài": ở những bài dạng danh sách, các
            # tiêu đề phụ cũng là <strong> nhưng căn trái, và cách nhận dạng theo
            # vị trí sẽ lấy nhầm chúng làm tên tác giả.
            for tag in reversed(body.find_all("p")):
                style = (tag.get("style") or "").replace(" ", "").lower()
                if "text-align:right" not in style:
                    continue
                strong = tag.find("strong")
                if strong is not None and strong.find_parent("a") is None \
                        and _looks_like_name(txt(strong)):
                    author_tag = strong
                    break
                # Không có <strong> hợp lệ thì lấy phần đứng trước dấu "(" của
                # đoạn byline. Dạng gặp trong dữ liệu là "<Tên> (Tổng hợp) Ảnh:
                # <tên khác>", trong đó phần sau là credit ảnh chứ không phải
                # tác giả bài.
                lead = re.split(r"[(\[]|Ảnh\s*:", tag.get_text(" ", strip=True))[0].strip()
                if _looks_like_name(lead):
                    a.author_name = lead
                    byline_tag = tag
                    break
        if author_tag is not None:
            a.author_name = txt(author_tag)
        byline = byline_tag or (author_tag.find_parent("p") if author_tag is not None else None)
        if byline is not None:
            m = re.search(r"\(\s*Theo\s+(.*?)\s*\)", byline.get_text(" ", strip=True), re.IGNORECASE)
            if m:
                a.source_attribution = split_sources(m.group(1))

        pub = meta(soup, name="pubdate", prop="article:published_time")
        a.published_at = parse_iso(pub)

        return a
