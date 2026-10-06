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

TZ_VN = timezone(timedelta(hours=7))  # gắn +7 vào giờ đọc từ trang; storage đổi sang UTC
_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})(?:[^\d]+(\d{1,2}):(\d{2}))?")
_DUR_RE = re.compile(r"^\d{1,2}:\d{2}$")  # thời lượng video dạng "01:38" trên overlay
# Khớp "Theo Kyiv Post" hoặc "(Theo Reuters, AFP)". Tên nguồn buộc phải bắt đầu
# bằng chữ hoa, nhờ đó loại được "Theo dõi" của nút follow và "theo giới phân
# tích..." nằm giữa câu.
_THEO_RE = re.compile(r"^\(?\s*Theo\s+([A-ZĐ][^.()]{1,40}?)\s*\)?\.?$")
# Các nhãn chỉ trang chủ cần bỏ khỏi breadcrumb; mỗi báo dùng một biến thể khác nhau.
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
        """URL trang thứ N của một chuyên mục, hoặc None nếu báo không phân trang."""
        return base_url if page == 1 else None

    def more_page_urls(self, base_url: str, page1_html: str, max_pages: int) -> list[str]:
        """URL các trang 2..max_pages của một chuyên mục.

        Mặc định dựng từ `page_url()`. Lớp con ghi đè khi cần thông tin nằm trong
        HTML trang đầu, chẳng hạn zoneId của một timeline tải bằng AJAX.
        """
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
        """Bóc tách một trang bài viết thành `Article`, với `source` đã được gán."""

    def new_article(self, url: str) -> Article:
        return Article(
            url=url,
            url_hash=md5(url),
            source=self.name,
        )

    def fill_hierarchy(self, soup: BeautifulSoup, article: Article) -> None:
        """Điền `sub_category` và `topic` từ JSON-LD BreadcrumbList.

        Dùng BreadcrumbList vì đó là chuẩn schema.org và mọi báo trong dự án đều
        phát ra, nên không cần selector riêng cho từng nguồn. Sau khi loại các
        nhãn chỉ trang chủ, thứ tự các cấp là: [0] category — đã biết từ trang
        listing, [1] sub_category, [2] topic.

        Hàm cũng đánh dấu những bài cần loại để `main` bỏ qua, nhằm chỉ giữ bài
        dạng văn bản thường:

            crawl_status="special"   định dạng đặc biệt kiểu megastory hoặc
                                     e-magazine, nhận ra qua class chứa
                                     "magazine" hay "megastory"
            crawl_status="video"     breadcrumb có một cấp là "Video", ví dụ
                                     Trang chủ > Video > Giải trí
        """
        if soup.select_one("[class*=magazine], [class*=megastory]"):
            article.crawl_status = "special"
            return
        raw = breadcrumb_from_jsonld(soup)
        if any(c.strip().lower() == "video" for c in raw):
            article.crawl_status = "video"
            return
        crumbs = [c for c in raw if c.strip().lower() not in _HOME_TERMS]
        # Chỉ đặt sub_category khi nó khác category, tránh lặp lại cùng một tên.
        if len(crumbs) >= 2 and crumbs[1].strip().lower() != crumbs[0].strip().lower():
            article.sub_category = crumbs[1]
        if len(crumbs) >= 3:
            article.topic = crumbs[2]

    # --------------------------------------------------------------- helpers
    def links_by_pattern(
        self, html: str, seg_regex: str, exclude: tuple[str, ...] = (),
        skip_video: bool = True,
    ) -> list[str]:
        """Tìm link bài theo mẫu của segment cuối URL.

        Chỉ nhận link cùng domain và loại những URL chứa chuỗi trong `exclude`.
        Với `skip_video=True`, các card video cũng bị loại: chúng chỉ có clip nên
        parse ra nội dung rỗng.
        """
        soup = BeautifulSoup(html, "lxml")
        dom = urlparse(self.base_url).netloc
        rx = re.compile(seg_regex)

        # Lượt 1: thu thập các URL video. Một bài có nhiều thẻ <a> trỏ tới nó,
        # và chỉ thẻ bọc thumbnail nằm trong card video còn thẻ tiêu đề thì
        # không, nên phải nhận diện qua bất kỳ thẻ <a> nào trỏ tới URL đó.
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


# ------------------- hàm thuần, dùng chung ở cấp module ----------------------
def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def _is_video_card(a) -> bool:
    """Cho biết một thẻ <a> có trỏ tới bài chỉ có video hay không.

    Nhiều báo trộn thẳng video vào trang listing tin. Hai dấu hiệu nhận ra:
    text của <a> là thời lượng dạng "01:38" do overlay nút play sinh ra, hoặc
    một thẻ tổ tiên mang class chứa "video".
    """
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


# Các loại khoảng trắng bất thường thường gặp trong HTML báo: nbsp và
# zero-width space.
_WS_RUN = re.compile(r"[ \t ​‌﻿]+")
# Dấu cách nằm sai chỗ sau khi chèn: ngay trước dấu đóng câu, hoặc ngay
# sau dấu mở.
_SP_BEFORE = re.compile(r"\s+([,.;:!?%…)\]}»”’])")
_SP_AFTER = re.compile(r"([(\[{«“‘])\s+")


def inline_text(el) -> str:
    """Lấy text của một thẻ, có chèn dấu cách giữa các thẻ con.

    `get_text(strip=True)` nối text của các thẻ con mà không chèn gì vào giữa,
    nên `<p>thông tấn<b>TASS</b>của Nga</p>` cho ra "thông tấnTASScủa Nga". Đo
    trên 5.033 bài thì 68% số bài có ít nhất một chỗ dính như vậy, trung bình
    3,5 chỗ mỗi bài. Hệ quả nằm ở tầng truy hồi: BM25 coi "tấnTASScủa" là một
    token duy nhất, nên tìm "TASS" không trả về bài đó.

    Chèn dấu cách là cần thiết nhưng chèn thô lại sinh lỗi mới, vì vậy phải dọn
    lại ngay sau đó:

        "Nga<b>,</b> nói"  ->  "Nga , nói"   ->  "Nga, nói"
        "(<b>Ảnh</b>: X)"  ->  "( Ảnh : X)"  ->  "(Ảnh: X)"
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
    """Parse các dạng ngày giờ mà báo tiếng Việt dùng, trả về datetime múi +7.

        "Thứ ba, 07/07/2026 - 06:01"
        "07/07/2026 09:00 (GMT+07:00)"
    """
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
    """Ghép các thẻ <p> thành nội dung bài.

    Loại <p class=description> vì đó là sapo, đã nằm ở cột riêng, và loại các <p>
    là chú thích ảnh — mọi báo đặt chúng trong <figure> hoặc <figcaption>.
    """
    if container is None:
        return "", 0
    parts = []
    for p in container.find_all("p"):
        classes = p.get("class") or []
        if "description" in classes:
            continue
        if p.find_parent(["figure", "figcaption"]):   # chú thích ảnh, bỏ
            continue
        t = inline_text(p)          # chèn dấu cách giữa các thẻ inline rồi dọn
        if t:
            parts.append(t)
    content = "\n\n".join(parts)
    return content, len(content.split())


def _iter_jsonld(data):
    """Duyệt mọi dict trong JSON-LD, kể cả dạng list và dạng lồng trong @graph."""
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
    """Trích danh sách tên các cấp breadcrumb từ khối JSON-LD BreadcrumbList."""
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
                if isinstance(e, list):        # một số báo bọc mỗi item trong list con
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
    """Tên tác giả lấy từ trường `author` kiểu Person trong JSON-LD NewsArticle.

    Ưu tiên nguồn này vì nó đã được chuẩn hoá sẵn, không lẫn chức danh hay nhãn.
    """
    for node in _jsonld_nodes(soup):
        if node.get("@type") in ("NewsArticle", "Article", "Report", "BlogPosting"):
            names = [n for n in _author_names(node.get("author")) if n.lower() != "dân trí"]
            if names:
                return ", ".join(dict.fromkeys(names))
    return ""


def theo_source(soup: BeautifulSoup) -> str:
    """Nguồn gốc bài, lấy từ dòng "Theo <Nguồn>" ở cuối những bài dịch.

    Trả về phần nằm sau chữ "Theo", ví dụ "Kyiv Post". Khi có nhiều dòng khớp
    thì lấy dòng xuất hiện sau cùng, vì đó là vị trí ghi nguồn thật.
    """
    found = ""
    for el in soup.find_all(["p", "div", "span"]):
        if el.find(["p", "div"]):  # chỉ xét node lá, bỏ khối lồng nhau
            continue
        m = _THEO_RE.match(el.get_text(" ", strip=True))
        if m:
            found = m.group(1).strip()
    return found


def tightest_body(soup: BeautifulSoup):
    """Phương án dự phòng khi nguồn không có selector ổn định.

    Chọn thẻ div hoặc article chứa nhiều text trong các <p> nhất, dựa trên giả
    định rằng khối nội dung chính luôn dài hơn mọi khối phụ.
    """
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


# Bút danh toà soạn, tên hãng tin và nhãn nội dung tài trợ. Chúng xuất hiện ở
# đúng vị trí của tên tác giả trên trang nhưng không phải tên người. Quan sát
# trên corpus: Tuổi Trẻ ghi "TTXVN", Dân trí ghi "PV" hoặc "Dân trí", Thanh Niên
# ghi "Thông tin dịch vụ" hoặc "Nguồn: Bệnh viện FV".
_NOT_A_PERSON = {
    "ttxvn", "vna", "pv", "ctv", "nhóm pv", "nhóm phóng viên", "phóng viên",
    "dân trí", "tuổi trẻ", "thanh niên", "vnexpress", "vietnamnet",
    "thông tin dịch vụ", "tin tài trợ", "quảng cáo",
}


_SRC_SPLIT_RE = re.compile(r"\s*[,/]\s*")


def split_sources(raw) -> list[str]:
    """Tách chuỗi nguồn dẫn thành danh sách tên hãng.

        "AFP, Reuters"      ->  ["AFP", "Reuters"]
        "Health 2.0/TVBS"   ->  ["Health 2.0", "TVBS"]

    Lý do phải tách: trên corpus có 404 trong 605 giá trị chứa nhiều hãng gộp
    trong một chuỗi, nhiều nhất là 5 hãng, sinh ra hàng trăm chuỗi khác nhau cho
    chỉ vài chục hãng thực sự tồn tại. Khi đó điều kiện lọc
    `source_attribution = 'Reuters'` bỏ sót toàn bộ những bài ghi "AFP, Reuters".
    Chuyển sang mảng thì phép lọc khớp theo từng phần tử, giống cách `tags` hoạt
    động.

    Chỉ tách ở `,` và `/`. Corpus không dùng dấu phân cách nào khác, còn tách
    rộng hơn sẽ làm vỡ những tên vốn chứa dấu như "L'Équipe" hay "Health 2.0".
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
        if p and key not in seen:      # "Reuters, AFP, Reuters" còn 2 phần tử
            seen.add(key)
            out.append(p)
    return out


def demote_agency_author(a: Article) -> None:
    """Dời hãng tin và bút danh toà soạn từ `author_name` sang `source_attribution`.

    Hai cột mang ngữ nghĩa khác nhau: `author_name` phục vụ câu hỏi "bài của nhà
    báo X", còn `source_attribution` cho biết bài được dẫn lại từ đâu. Để lẫn thì
    cả hai phép lọc đều cho kết quả sai.

    Gọi sau `parse_article` và áp dụng cho mọi nguồn.
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
