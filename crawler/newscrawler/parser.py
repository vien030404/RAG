import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from bs4 import BeautifulSoup


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
    author_email: str = ""
    source_attribution: str = ""   # nguồn gốc bài, vd "Reuters, AFP" từ "(Theo Reuters, AFP)"

    # Time
    published_at: datetime | None = None
    updated_at: datetime | None = None
    crawled_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_date: date | None = None

    # Meta
    source: str = "vnexpress"
    crawl_status: str = "success"
    http_status: int = 200


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def _extract_article_id(url: str) -> int | None:
    m = re.search(r"-(\d+)\.html", url)
    return int(m.group(1)) if m else None


def _parse_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _is_ad(tag) -> bool:
    node = tag
    for _ in range(6):
        node = node.parent
        if node is None:
            break
        if "item-ads" in node.get("class", []):
            return True
    return False


def parse_article_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    links: list[str] = []

    for tag in soup.select("h3.title-news a, h2.title-news a"):
        href = tag.get("href", "")
        if href.startswith("https://vnexpress.net") and href not in seen and not _is_ad(tag):
            seen.add(href)
            links.append(href)

    return links


def parse_article(html: str, url: str, http_status: int = 200) -> Article:
    soup = BeautifulSoup(html, "lxml")

    # Gỡ dateline địa điểm (vd <span class="location-stamp">MỸ</span>) để khỏi lẫn vào
    # description/content.
    for stamp in soup.select("span.location-stamp"):
        stamp.decompose()

    article = Article(
        url=url,
        url_hash=_md5(url),
        article_id=_extract_article_id(url),
        http_status=http_status,
    )

    # --- title ---
    title_tag = soup.select_one("h1.title-detail, h1.title_page_detail")
    if title_tag:
        article.title = title_tag.get_text(strip=True)

    # --- description ---
    desc_tag = soup.select_one("p.description, p.lead")
    if desc_tag:
        article.description = desc_tag.get_text(strip=True)

    # --- category hierarchy from breadcrumb ---
    # Breadcrumb sâu nhất chứa full path, vd: /khoa-hoc-cong-nghe/chuyen-doi-so/nhip-song-so
    # → segments[0]=cấp 1, [1]=cấp 2 (sub_category), [2]=cấp 3 (topic)
    breadcrumbs = soup.select("ul.breadcrumb li a")
    if breadcrumbs:
        deepest_href = breadcrumbs[-1].get("href", "")
        segments = [s.replace("-", "_") for s in deepest_href.strip("/").split("/") if s]
        if len(segments) >= 1:
            article.category = segments[0]
        if len(segments) >= 2:
            article.sub_category = segments[1]
        if len(segments) >= 3:
            article.topic = segments[2]

    # --- tags from meta keywords ---
    meta_kw = soup.find("meta", attrs={"name": "keywords"})
    if meta_kw and meta_kw.get("content"):
        article.tags = [t.strip() for t in meta_kw["content"].split(",") if t.strip()]

    # --- content (joined paragraphs from fck_detail) ---
    body = soup.select_one("article.fck_detail, div.fck_detail")
    if body:
        paragraphs = [
            p.get_text(strip=True)
            for p in body.find_all("p", class_=lambda c: c != "description")
            if p.get_text(strip=True)
        ]
        article.content = "\n\n".join(paragraphs)
        article.word_count = len(article.content.split())

    # --- thumbnail from og:image ---
    og_img = soup.find("meta", property="og:image")
    if og_img:
        article.thumbnail_url = og_img.get("content", "")

    # --- author ---
    author_tag = soup.select_one("p.author_mail strong")
    if not author_tag:
        # Fallback: <strong> cuối trong p.Normal có thể là byline tên tác giả.
        # Loại link điều hướng (">>"), câu hỏi đố vui, trích dẫn, và fragment
        # ngắn — tên tác giả VnExpress luôn có ≥ 2 từ và không chứa các ký tự này.
        for tag in reversed(soup.select("p.Normal strong")):
            text = tag.get_text(strip=True)
            if not text or len(text) > 50 or "\n" in text:
                continue
            if tag.find_parent("a") is not None:          # nằm trong link
                continue
            if text.startswith(">>") or "?" in text:       # điều hướng / câu hỏi
                continue
            if any(q in text for q in ('"', '“', '”')):    # câu trích dẫn
                continue
            if " " not in text:                            # không phải tên (vd "Hi")
                continue
            author_tag = tag
            break
    if author_tag:
        article.author_name = author_tag.get_text(strip=True)
        # Nguồn gốc bài trong ngoặc cạnh tên: "(Theo Reuters, AFP)" → "Reuters, AFP"
        byline = author_tag.find_parent("p")
        if byline:
            m = re.search(r"\(\s*Theo\s+(.*?)\s*\)", byline.get_text(" ", strip=True), re.IGNORECASE)
            if m:
                article.source_attribution = m.group(1).strip()

    email_tag = soup.select_one('p.author_mail a[href^="mailto:"]')
    if email_tag:
        article.author_email = email_tag.get("href", "").replace("mailto:", "")

    # --- published_at / updated_at from meta ---
    pub_meta = (
        soup.find("meta", attrs={"name": "pubdate"})
        or soup.find("meta", property="article:published_time")
    )
    if pub_meta:
        article.published_at = _parse_datetime(pub_meta.get("content", ""))

    upd_meta = (
        soup.find("meta", attrs={"name": "article_updatetime"})
        or soup.find("meta", property="article:modified_time")
    )
    if upd_meta:
        content = upd_meta.get("content", "")
        if content.isdigit():
            article.updated_at = datetime.fromtimestamp(int(content), tz=timezone.utc)
        else:
            article.updated_at = _parse_datetime(content)

    # --- raw_date ---
    if article.published_at:
        article.raw_date = article.published_at.date()
    else:
        article.raw_date = article.crawled_at.date()

    return article
