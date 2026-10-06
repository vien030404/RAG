"""Parser dùng chung cho Tuổi Trẻ và Thanh Niên, hai báo chạy cùng CMS "afcbc".

Vì cùng CMS, hai trang dùng chung bộ điểm neo: `h1.detail-title`,
`[data-role=sapo]`, `[data-role=content].afcbc-body`, meta
`article:published_time` ở dạng ISO, và `[data-role=author]`. Một lớp tham số
hoá vì vậy phục vụ được cả hai, thay vì viết hai parser gần như giống nhau.
"""
from __future__ import annotations

from bs4 import BeautifulSoup

from ..article import Article
from .base import (
    BaseSource,
    inline_text,
    extract_id,
    join_paragraphs,
    meta,
    parse_iso,
    split_keywords,
    txt,
)


class AfcbcSource(BaseSource):

    def __init__(
        self,
        name: str,
        base_url: str,
        categories: dict[str, str],
        timeline_tmpl: str | None = None,
    ):
        self.name = name
        self.base_url = base_url
        self.categories = categories
        # URL template cho các trang 2..N (load-more AJAX). Có 2 placeholder:
        #   {zone} = zoneId của category (lấy từ <input#hdZoneId> trên trang 1)
        #   {n}    = số trang
        self.timeline_tmpl = timeline_tmpl

    # Khối danh sách bài chính của trang chuyên mục. Các sidebar kiểu "đọc
    # nhiều" hay "tin liên quan" bị loại vì chúng trộn bài cũ vào, phá thứ tự từ
    # mới đến cũ mà logic dừng phân trang theo ngày đang dựa vào.
    _STREAM_SEL = '[data-marked-zoneid="tto_cate_stream"], [data-marked-zoneid="tn_cate_stream"]'

    def parse_links(self, html: str) -> list[str]:
        # Link bài nhận ra qua segment cuối: một dãy số dài đứng trước ".htm",
        # nhờ đó loại được các trang chuyên mục vốn không có phần id này.
        #
        # Với trang 1, tức HTML chuyên mục đầy đủ, ưu tiên quét trong khối stream
        # để bỏ link sidebar. Khi khối đó rỗng hoặc không tồn tại — trang đổ nội
        # dung bằng JS, hoặc đây là fragment timeline của trang 2 trở đi — thì
        # quét toàn bộ tài liệu.
        rx = r"-\d{5,}\.htm$"
        stream = BeautifulSoup(html, "lxml").select_one(self._STREAM_SEL)
        if stream is not None:
            scoped = self.links_by_pattern(str(stream), rx)
            if scoped:
                return scoped
        return self.links_by_pattern(html, rx)

    def more_page_urls(self, base_url: str, page1_html: str, max_pages: int) -> list[str]:
        # Hai báo này không phân trang bằng hậu tố URL: nút "Xem thêm" gọi một
        # endpoint AJAX dạng /timeline/{zoneId}/... . zoneId nằm ngay trong
        # <input id="hdZoneId" value="..."> của chính trang chuyên mục, nên trích
        # từ đó vừa ổn định vừa chắc chắn khớp đúng chuyên mục đang cào.
        if not self.timeline_tmpl or max_pages < 2:
            return []
        tag = BeautifulSoup(page1_html, "lxml").select_one("#hdZoneId")
        zone = (tag.get("value") if tag else "") or ""
        if not zone.isdigit():
            print(f"[{self.name}] không tìm thấy #hdZoneId ở {base_url} -> chỉ trang 1")
            return []
        return [
            self.timeline_tmpl.format(zone=zone, n=n)
            for n in range(2, max_pages + 1)
        ]

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

        # Tên tác giả: ưu tiên thẻ meta article:author, nơi Thanh Niên ghi tên
        # đã sạch. Không có thì gom các phần tử .author-name của Tuổi Trẻ rồi khử
        # trùng. Không lấy text của cả khối [data-role=author] vì khối đó còn
        # chứa chuỗi giao diện "và N tác giả khác" cùng một bản sao ẩn của chính
        # danh sách tên.
        author_meta = meta(soup, prop="article:author")
        if author_meta and "http" not in author_meta and "@" not in author_meta:
            a.author_name = author_meta
        else:
            names: list[str] = []
            for el in soup.select("div.author-info a.name, div.author-item-name a.name"):
                t = inline_text(el)
                if t and t not in names:
                    names.append(t)
            a.author_name = ", ".join(names)

        return a


# Hai thực thể cấu hình khác nhau trên cùng lớp parser ở trên.
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
        "Nhịp sống số": f"{_TT}/nhip-song-so.htm",   # tên chuyên mục của chính Tuổi Trẻ
    },
    timeline_tmpl=f"{_TT}/timeline/{{zone}}/trang-{{n}}.htm",
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
    timeline_tmpl=f"{_TN}/timeline/{{zone}}/{{n}}.htm",
)
