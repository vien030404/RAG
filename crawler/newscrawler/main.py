"""Crawler đa nguồn: fetch -> lưu HTML thô -> parse -> Iceberg Bronze.

Phạm vi crawl điều khiển qua biến môi trường:

    CRAWL_SOURCES     "vnexpress,tuoitre"    rỗng là lấy mọi nguồn
    CRAWL_CATEGORIES  "Kinh doanh,Thế giới"  rỗng là lấy mọi chuyên mục
    CRAWL_DATE        "2026-07-02"           rỗng là ngày hôm trước theo giờ VN
                                             tại thời điểm chạy; "all" là không
                                             lọc theo ngày

Cách lọc theo ngày
------------------
Chỉ giữ bài đúng `target_date`, bỏ cả bài mới hơn và cũ hơn.

Trang listing xếp từ mới đến cũ, nhưng thường chèn lẫn bài "nổi bật" của những
ngày trước. Vì vậy một bài lẻ nằm ngoài ngày không được coi là dấu hiệu đã đi
quá: nó bị bỏ qua, còn vòng phân trang chỉ dừng khi cả một trang không còn bài
nào mới bằng `target_date`.
"""
import os
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from .config import MAX_PAGES, MIN_CONTENT_LEN
from .fetcher import fetch
from .article import Article
from .sources import SOURCES
from .sources.base import demote_agency_author, md5
from . import raw_storage, storage


def _cat_slug(url: str) -> str:
    """https://tuoitre.vn/the-gioi.htm -> the_gioi"""
    seg = url.rstrip("/").split("/")[-1]
    seg = re.sub(r"\.html?$", "", seg)
    return seg.replace("-", "_")


def _slugify(text: str) -> str:
    """"Nhịp sống số" -> "nhip_song_so" (bỏ dấu) để so với category slug."""
    text = text.replace("Đ", "D").replace("đ", "d")
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()


def crawl_category(source, name: str, url: str, target_date: date | None,
                   known: dict[str, date] | None = None) -> list[Article]:
    slug = _cat_slug(url)
    articles: list[Article] = []
    known = known or {}
    n_known = 0          # số bài bỏ qua vì đã có trong Bronze

    # Trang 1 (cần HTML để dựng URL các trang sau, vd zoneId của tuoitre/thanhnien)
    html1, _ = fetch(url)
    if not html1:
        print(f"[{source.name}/{name}] fetch listing lỗi.")
        return articles
    page_urls = [url] + source.more_page_urls(url, html1, MAX_PAGES)

    # Các trang listing chồng lấn nhau (đo được: tuoitre trang 3 trùng 9/20 link với
    # trang 2) -> nhớ link đã xử lý trong cả category để không fetch lại bài.
    # Nhớ kèm ngày để link trùng vẫn tính được vào newest_on_page (xem cuối vòng lặp).
    seen: dict[str, date | None] = {}

    stop = False
    for pi, purl in enumerate(page_urls, 1):
        html = html1 if pi == 1 else fetch(purl)[0]
        if not html:
            break
        page_links = source.parse_links(html)   # không giới hạn số bài / trang
        if not page_links:
            print(f"[{source.name}/{name}] trang {pi}: hết bài, dừng.")
            break
        links = [l for l in page_links if l not in seen]
        dup = len(page_links) - len(links)
        if not links:
            print(f"[{source.name}/{name}] trang {pi}: {dup} link đều đã xử lý, dừng.")
            break
        print(
            f"\n[{source.name}/{name}] Trang {pi}: {len(links)} link"
            + (f" (bỏ {dup} trùng trang trước)" if dup else "")
        )

        for link in links:
            # Bài đã có trong Bronze, chẳng hạn khi DAG chạy lại cùng một ngày:
            # không fetch lại và không ghi trùng. Ngày đã biết vẫn phải đưa vào
            # `seen` để logic dừng phân trang tính đúng.
            h = md5(link)
            if h in known:
                seen[link] = known[h]
                n_known += 1
                continue
            seen[link] = None                 # đã xử lý; ngày điền bên dưới nếu parse ra
            art_html, _ = fetch(link)
            if not art_html:
                print(f"  [skip] fetch lỗi: {link}")
                continue

            art = source.parse_article(art_html, link)
            # `category` giữ đúng tên chuyên mục của chính báo đó, có dấu, cũng
            # là khoá trong registry: "Thời sự", "Khoa học công nghệ", "Nhịp
            # sống số". Không chuẩn hoá tên chéo giữa các nguồn, vì cùng một tên
            # chưa chắc cùng nội dung — "Nhịp sống số" của Tuổi Trẻ không tương
            # đương "Công nghệ" của VietnamNet. `slug` chỉ dùng nội bộ để so khớp.
            art.category = name
            demote_agency_author(art)

            # Bỏ `sub_category` hoặc `topic` nếu chúng trùng với `category`.
            # Ví dụ: cào chuyên mục nhip-song-so nhưng breadcrumb của bài là
            # "Công nghệ > Nhịp sống số", khiến sub_category lặp lại chính
            # category.
            if _slugify(art.sub_category) == slug:
                art.sub_category, art.topic = art.topic, ""   # đôn cấp sâu hơn lên
            if _slugify(art.topic) == slug:
                art.topic = ""

            # Chỉ giữ bài dạng văn bản thường: loại các trang được đánh dấu là
            # video, megastory hay e-magazine, và các trang parse ra nội dung rỗng.
            if art.crawl_status != "success" or len(art.content) < MIN_CONTENT_LEN:
                print(f"  [skip] {art.crawl_status} / nội dung ngắn: {link}")
                continue

            # Lọc theo ngày. Một bài lẻ nằm ngoài ngày chỉ bị bỏ qua, không làm
            # dừng vòng lặp, vì listing thường chèn lẫn bài "nổi bật" của những
            # ngày trước. Quyết định dừng phân trang nằm ở cuối mỗi trang, dựa
            # trên `newest_on_page` bên dưới.
            if target_date is not None:
                d = art.published_at.date() if art.published_at else None
                seen[link] = d
                if d is None:
                    continue                      # không xác định được ngày
                if d != target_date:
                    continue                      # ngoài ngày cần lấy, đọc tiếp

            # Qua mọi bộ lọc: lưu HTML thô rồi giữ bài lại cho lượt append.
            try:
                raw_storage.save_raw_html(source.name, md5(link), art_html)
            except Exception as e:  # noqa: BLE001 — lỗi lưu raw không làm hỏng crawl
                print(f"  [raw] lỗi lưu {link}: {e}")
            articles.append(art)

        # Khi cả trang không còn bài nào mới bằng `target_date`, vòng phân trang
        # đã đi qua hẳn ngày cần lấy và có thể dừng. Điều kiện xét trên bài mới
        # nhất của trang nên vẫn chịu được những bài "nổi bật" ngày cũ nằm lẫn.
        #
        # Phép xét lấy mọi link trên trang, kể cả link đã xử lý ở trang trước và
        # lấy ngày từ `seen`. Nếu chỉ xét link mới thì việc bỏ trùng có thể làm
        # trang trông như không còn bài nào trong ngày và gây dừng sớm.
        page_dates = [seen.get(l) for l in page_links]
        page_dates = [d for d in page_dates if d is not None]
        newest_on_page = max(page_dates) if page_dates else None
        if target_date is not None and newest_on_page is not None and newest_on_page < target_date:
            print(f"[{source.name}/{name}] trang {pi}: đã qua {target_date}, dừng.")
            stop = True
        if stop:
            break
    else:
        # Duyệt hết `page_urls` mà chưa lần nào đi qua `target_date` nghĩa là kết
        # quả có thể bị cắt cụt: số bài mới hơn ngày cần lấy nhiều tới mức lấp
        # kín MAX_PAGES trang trước khi phân trang tới hết ngày đó. In cảnh báo
        # để trường hợp thiếu bài không diễn ra âm thầm.
        if target_date is not None and not stop:
            print(
                f"[{source.name}/{name}] ⚠ HẾT {len(page_urls)} trang mà chưa qua "
                f"{target_date} (giữ {len(articles)} bài). Có thể thiếu — tăng MAX_PAGES."
            )

    if n_known:
        print(f"[{source.name}/{name}] bỏ qua {n_known} bài đã có trong Bronze")
    return articles


def run(sources=None, categories=None, target_date=None) -> int:
    selected = (
        {k: v for k, v in SOURCES.items() if k in sources}
        if sources else SOURCES
    )
    if not selected:
        print(f"Không có nguồn khớp {sources}. Có sẵn: {list(SOURCES)}")
        return 0

    total = 0
    for src_name, source in selected.items():
        cats = source.categories
        if categories:
            cats = {k: v for k, v in cats.items() if k in categories}
        print(f"\n{'='*60}\nNGUỒN: {src_name}  ({len(cats)} category)\n{'='*60}")
        # Gom toàn bộ một nguồn thành một lô rồi append một lần, tạo một snapshot
        # duy nhất. Càng ít lần commit thì càng ít xung đột khi các task của
        # nhiều nguồn ghi song song vào cùng bảng.
        #
        # Đọc một lần danh sách bài đã có trong Bronze cho nguồn này, để chống
        # trùng được cả giữa các lượt chạy — trạng thái trong bộ nhớ tiến trình
        # chỉ chống trùng trong phạm vi một lượt.
        known = storage.known_published_dates(src_name)
        if known:
            print(f"[{src_name}] Bronze đã có {len(known)} bài -> sẽ bỏ qua khi gặp lại")
        source_articles = []
        for cat_name, url in cats.items():
            batch = crawl_category(source, cat_name, url, target_date, known)
            source_articles.extend(batch)
            print(f"[{src_name}/{cat_name}] {len(batch)} bài")
        if source_articles:
            saved = storage.save_articles(source_articles)
            total += saved
            print(f"[{src_name}] đã ghi {saved} bài (1 snapshot)")

    print(f"\nXong. Tổng {total} bài vào bronze.{storage.BRONZE_TABLE}.")
    return total


if __name__ == "__main__":
    src_env = os.getenv("CRAWL_SOURCES", "").strip()
    sources = [s.strip() for s in src_env.split(",") if s.strip()] or None

    cats_env = os.getenv("CRAWL_CATEGORIES", "").strip()
    categories = [c.strip() for c in cats_env.split(",") if c.strip()] or None

    date_env = os.getenv("CRAWL_DATE", "").strip()
    if date_env.lower() in ("all", "none"):
        target = None                              # không lọc ngày, cào toàn bộ
    elif date_env:
        target = date.fromisoformat(date_env)
    else:
        # Mặc định lấy ngày hôm trước theo giờ Việt Nam tại thời điểm container
        # chạy, độc lập với logical date của Airflow. Việt Nam không áp dụng giờ
        # mùa hè nên độ lệch +7 là cố định.
        _TZ_VN = timezone(timedelta(hours=7))
        target = (datetime.now(_TZ_VN) - timedelta(days=1)).date()
    print(f"target_date = {target or 'ALL'}")

    run(sources=sources, categories=categories, target_date=target)
