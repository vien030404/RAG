"""Khung chung cho parser theo báo.

Mỗi báo là 1 lớp con `BaseSource`, chỉ cần cài đặt `parse_links` +
`parse_article` với selector riêng. Các thao tác lặp lại (trích meta,
parse ngày, ghép đoạn văn, tìm link theo pattern URL) nằm ở đây.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..article import Article

TZ_VN = timezone(timedelta(hours=7))  # published_at lưu UTC; gắn +7 rồi để storage đổi UTC
_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})(?:[^\d]+(\d{1,2}):(\d{2}))?")
_DUR_RE = re.compile(r"^\d{1,2}:\d{2}$")  # thời lượng video "01:38" (overlay nút play)
# "Theo Kyiv Post" / "(Theo Reuters, AFP)" — nguồn bắt đầu chữ HOA để loại
# "Theo dõi" (nút follow) và "theo giới phân tích..." (giữa câu).
_THEO_RE = re.compile(r"^\(?\s*Theo\s+([A-ZĐ][^.()]{1,40}?)\s*\)?\.?$")
# Các nhãn "trang chủ" cần bỏ khỏi breadcrumb (nhiều biến thể giữa các báo).
_HOME_TERMS = {"trang chủ", "trang chu", "home", "tin tức mới nhất", "dantri.com.vn"}


class BaseSource(ABC):
    name: str = ""
    base_url: str = ""
    categories: dict[str, str] = {}

    # ------------------------------------------------------------------ links
    @abstractmethod
    def parse_links(self, html: str) -> list[str]:
        """Trả về danh sách URL bài viết từ trang listing."""

    def page_url(self, base_url: str, page: int) -> str | None:
        """URL trang thứ N của 1 category, hoặc None nếu không hỗ trợ phân trang."""
        return base_url if page == 1 else None

    def more_page_urls(self, base_url: str, page1_html: str, max_pages: int) -> list[str]:
        """URL các trang 2..max_pages của listing. Mặc định dựng từ page_url();
        override khi cần thông tin trong page1_html (vd zoneId cho AJAX timeline)."""
        urls: list[str] = []
        for page in range(2, max_pages + 1):
            u = self.page_url(base_url, page)
            if not u:
                break
            urls.append(u)
        return urls

    # ---------------------------------------------------------------- article
    @abstractmethod
    def parse_article(self, html: str, url: str) -> Article:
        """Bóc tách 1 trang bài viết thành Article (đã gán self.name vào source)."""

    def new_article(self, url: str) -> Article:
        return Article(
            url=url,
            url_hash=md5(url),
            source=self.name,
        )

    def fill_hierarchy(self, soup: BeautifulSoup, article: Article) -> None:
        """Điền sub_category / topic từ JSON-LD BreadcrumbList (chuẩn schema.org,
        đồng nhất mọi báo). Sau khi bỏ nhãn "Trang chủ": [0]=category (đã có từ
        listing), [1]=sub_category, [2]=topic.

        Đồng thời đánh dấu bài cần BỎ (main sẽ skip), chỉ giữ bài text thường:
        - Định dạng đặc biệt (megastory tuoitre / e-magazine dantri): class
          chứa 'magazine'/'megastory' -> crawl_status="special".
        - Video: breadcrumb có cấp "Video" (vd Trang chủ > Video > Giải trí)
          -> crawl_status="video"."""
        if soup.select_one("[class*=magazine], [class*=megastory]"):
            article.crawl_status = "special"
            return
        raw = breadcrumb_from_jsonld(soup)
        if any(c.strip().lower() == "video" for c in raw):
            article.crawl_status = "video"
            return
        crumbs = [c for c in raw if c.strip().lower() not in _HOME_TERMS]
        # sub_category chỉ đặt khi KHÁC category (tránh trùng lặp "Giải trí")
        if len(crumbs) >= 2 and crumbs[1].strip().lower() != crumbs[0].strip().lower():
            article.sub_category = crumbs[1]
        if len(crumbs) >= 3:
            article.topic = crumbs[2]

    # --------------------------------------------------------------- helpers
    def links_by_pattern(
        self, html: str, seg_regex: str, exclude: tuple[str, ...] = (),
        skip_video: bool = True,
    ) -> list[str]:
        """Tìm link bài theo pattern segment cuối URL (cùng domain, bỏ `exclude`).
        skip_video=True: bỏ bài video (card có class ...video...) — chỉ có clip,
        không có nội dung text."""
        soup = BeautifulSoup(html, "lxml")
        dom = urlparse(self.base_url).netloc
        rx = re.compile(seg_regex)

        # Lượt 1: đánh dấu URL video (1 bài có nhiều <a>: thumb nằm trong card
        # video, title thì không -> phải gom URL video qua BẤT KỲ <a> nào).
        video_urls: set[str] = set()
        if skip_video:
            for a in soup.find_all("a", href=True):
                if _is_video_card(a):
                    video_urls.add(urljoin(self.base_url, a["href"]))

        out: list[str] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            full = urljoin(self.base_url, a["href"])
            if urlparse(full).netloc != dom:
                continue
            if any(x in full for x in exclude):
                continue
            if full in video_urls:
                continue
            seg = full.rstrip("/").split("/")[-1]
            if rx.search(seg) and full not in seen:
                seen.add(full)
                out.append(full)
        return out


# ------------------------- module-level pure helpers -------------------------
def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def _is_video_card(a) -> bool:
    """True nếu <a> là link video (loại bài chỉ có clip, vd Dân trí trộn video
    vào listing tin). Dấu hiệu: text của <a> là thời lượng "01:38" (overlay nút
    play), hoặc tổ tiên có class chứa 'video'."""
    if _DUR_RE.match(a.get_text(strip=True)):
        return True
    node = a
    for _ in range(5):
        node = node.parent
        if node is None:
            break
        if re.search(r"video", " ".join(node.get("class", []) or []), re.I):
            return True
    return False


# Khoảng trắng "lạ" hay gặp trong HTML báo: nbsp, zero-width space.
_WS_RUN = re.compile(r"[ \t ​‌﻿]+")
# Dấu cách vừa chèn vào SAI CHỖ: trước dấu đóng câu, sau dấu mở.
_SP_BEFORE = re.compile(r"\s+([,.;:!?%…)\]}»”’])")
_SP_AFTER = re.compile(r"([(\[{«“‘])\s+")


def inline_text(el) -> str:
    """Text của một thẻ, CÓ dấu cách giữa các thẻ con.

    `get_text(strip=True)` nối text của các con mà không chèn gì, nên
    `<p>thông tấn<b>TASS</b>của Nga</p>` ra "thông tấnTASScủa Nga" — dính chữ.
    Đo trên 5.033 bài: 68% số bài có ít nhất một chỗ dính, trung bình 3,5 chỗ.
    Hậu quả: BM25 tách "tấnTASScủa" thành MỘT token nên tra "TASS" không ra.

    Chèn dấu cách rồi dọn lại, vì chèn thô sẽ đẻ lỗi mới:
      "Nga<b>,</b> nói"  ->  "Nga , nói"   -> phải thành "Nga, nói"
      "(<b>Ảnh</b>: X)"  ->  "( Ảnh : X)"  -> phải thành "(Ảnh: X)"
    """
    if el is None:
        return ""
    t = el.get_text(" ", strip=True)
    t = _WS_RUN.sub(" ", t)
    t = _SP_BEFORE.sub(r"\1", t)
    t = _SP_AFTER.sub(r"\1", t)
    return t.strip()


def txt(el) -> str:
    return inline_text(el)


def meta(soup: BeautifulSoup, *, name: str | None = None, prop: str | None = None) -> str:
    tag = None
    if name:
        tag = soup.find("meta", attrs={"name": name})
    if tag is None and prop:
        tag = soup.find("meta", attrs={"property": prop})
    return (tag.get("content") or "").strip() if tag else ""


def split_keywords(s: str) -> list[str]:
    return [t.strip() for t in (s or "").split(",") if t.strip()]


def extract_id(url: str, pattern: str) -> int | None:
    m = re.search(pattern, url)
    return int(m.group(1)) if m else None


def parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def parse_vn_datetime(value: str) -> datetime | None:
    """"Thứ ba, 07/07/2026 - 06:01" | "07/07/2026 09:00 (GMT+07:00)" -> datetime(+7)."""
    if not value:
        return None
    m = _DATE_RE.search(value)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh = int(m.group(4)) if m.group(4) else 0
    mm = int(m.group(5)) if m.group(5) else 0
    try:
        return datetime(y, mo, d, hh, mm, tzinfo=TZ_VN)
    except ValueError:
        return None


def join_paragraphs(container) -> tuple[str, int]:
    """Ghép các <p> thành nội dung. Bỏ <p class=description> và <p> là caption
    ảnh (nằm trong <figure>/<figcaption> — mọi báo đặt chú thích "Ảnh: ..." ở đó)."""
    if container is None:
        return "", 0
    parts = []
    for p in container.find_all("p"):
        classes = p.get("class") or []
        if "description" in classes:
            continue
        if p.find_parent(["figure", "figcaption"]):   # caption ảnh -> bỏ
            continue
        t = inline_text(p)          # chèn dấu cách giữa <b>/<i>/<a> rồi dọn
        if t:
            parts.append(t)
    content = "\n\n".join(parts)
    return content, len(content.split())


def _iter_jsonld(data):
    """Duyệt mọi dict trong JSON-LD (dict / list / lồng trong @graph)."""
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(cur)
        elif isinstance(cur, dict):
            yield cur
            if isinstance(cur.get("@graph"), list):
                stack.extend(cur["@graph"])


def breadcrumb_from_jsonld(soup: BeautifulSoup) -> list[str]:
    """Trích danh sách tên từ <script type=application/ld+json> BreadcrumbList."""
    for sc in soup.find_all("script", type="application/ld+json"):
        raw = sc.string or sc.get_text() or ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for node in _iter_jsonld(data):
            if node.get("@type") != "BreadcrumbList":
                continue
            names: list[str] = []
            for e in node.get("itemListElement", []):
                if isinstance(e, list):        # Dân trí bọc mỗi item trong 1 list con
                    e = e[0] if e else None
                if not isinstance(e, dict):
                    continue
                nm = e.get("name")
                if not nm and isinstance(e.get("item"), dict):
                    nm = e["item"].get("name")
                if nm:
                    names.append(html.unescape(str(nm)).strip())
            if names:
                return names
    return []


def _jsonld_nodes(soup: BeautifulSoup):
    for sc in soup.find_all("script", type="application/ld+json"):
        raw = sc.string or sc.get_text() or ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        yield from _iter_jsonld(data)


def _author_names(au) -> list[str]:
    out: list[str] = []
    if isinstance(au, dict):
        nm = au.get("name")
        if nm:
            out.append(str(nm).strip())
    elif isinstance(au, list):
        for x in au:
            out.extend(_author_names(x))
    elif isinstance(au, str) and au.strip():
        out.append(au.strip())
    return out


def author_from_jsonld(soup: BeautifulSoup) -> str:
    """Tên tác giả từ JSON-LD NewsArticle/Article `author` (Person) — chuẩn, sạch."""
    for node in _jsonld_nodes(soup):
        if node.get("@type") in ("NewsArticle", "Article", "Report", "BlogPosting"):
            names = [n for n in _author_names(node.get("author")) if n.lower() != "dân trí"]
            if names:
                return ", ".join(dict.fromkeys(names))
    return ""


def theo_source(soup: BeautifulSoup) -> str:
    """Nguồn gốc bài từ dòng "Theo <Nguồn>" cuối bài (bài dịch/quốc tế).
    Trả về phần sau "Theo" (vd "Kyiv Post"), lấy cái xuất hiện sau cùng."""
    found = ""
    for el in soup.find_all(["p", "div", "span"]):
        if el.find(["p", "div"]):  # chỉ xét leaf ngắn
            continue
        m = _THEO_RE.match(el.get_text(" ", strip=True))
        if m:
            found = m.group(1).strip()
    return found


def tightest_body(soup: BeautifulSoup):
    """Fallback khi không có selector ổn định: div/article chứa nhiều text <p> nhất."""
    best = None
    best_len = 0
    for el in soup.find_all(["div", "article"]):
        ps = el.find_all("p")
        if len(ps) < 5:
            continue
        tl = sum(len(p.get_text(strip=True)) for p in ps)
        if tl > best_len:
            best, best_len = el, tl
    return best


# Bút danh tòa soạn / hãng tin / nhãn nội dung tài trợ — KHÔNG phải tên tác giả.
# Đo trên dữ liệu thật: tuoitre ghi "TTXVN", dantri ghi "PV"/"Dân trí",
# thanhnien ghi "Thông tin dịch vụ"/"Nguồn: Bệnh viện FV".
_NOT_A_PERSON = {
    "ttxvn", "vna", "pv", "ctv", "nhóm pv", "nhóm phóng viên", "phóng viên",
    "dân trí", "tuổi trẻ", "thanh niên", "vnexpress", "vietnamnet",
    "thông tin dịch vụ", "tin tài trợ", "quảng cáo",
}


_SRC_SPLIT_RE = re.compile(r"\s*[,/]\s*")


def split_sources(raw) -> list[str]:
    """"AFP, Reuters" -> ["AFP", "Reuters"] · "Health 2.0/TVBS" -> [...].

    Vì sao phải tách: đo trên dữ liệu thật, 404/605 giá trị chứa nhiều hãng
    trong MỘT chuỗi (tối đa 5), sinh ra hàng trăm chuỗi khác nhau cho vài chục
    hãng có thật. Lọc `source_attribution = 'Reuters'` vì thế trượt hết những
    bài ghi "AFP, Reuters". Thành mảng thì khớp theo PHẦN TỬ, giống `tags`.

    Chỉ tách `,` và `/` — dữ liệu thật không dùng dấu phân cách nào khác, và
    tách rộng hơn sẽ vỡ những tên vốn có dấu ("L'Équipe", "Health 2.0").
    """
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        raw = ", ".join(str(x) for x in raw)
    out: list[str] = []
    seen: set[str] = set()
    for part in _SRC_SPLIT_RE.split(str(raw).strip()):
        p = part.strip(" .;-")
        key = p.lower()
        if p and key not in seen:      # "Reuters, AFP, Reuters" -> 2 phần tử
            seen.add(key)
            out.append(p)
    return out


def demote_agency_author(a: Article) -> None:
    """Chuyển hãng tin / bút danh tòa soạn từ `author_name` sang `source_attribution`.

    Hai cột này mang ngữ nghĩa khác nhau: `author_name` để lọc "bài của nhà báo X",
    `source_attribution` để biết bài dẫn lại từ đâu. Trộn lẫn thì cả hai bộ lọc
    đều sai. Gọi sau parse_article, áp cho mọi nguồn.
    """
    name = (a.author_name or "").strip()
    if not name:
        return
    low = name.lower()
    if low in _NOT_A_PERSON or low.startswith("nguồn:"):
        a.author_name = ""
        src = name[6:].strip() if low.startswith("nguồn:") else name
        if src and not a.source_attribution:
            a.source_attribution = split_sources(src)
