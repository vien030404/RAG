"""Crawler đa nguồn: fetch -> lưu HTML thô (raw) -> parse -> Iceberg Bronze.

Phạm vi điều khiển qua env:
  CRAWL_SOURCES     "vnexpress,tuoitre"  (rỗng = tất cả nguồn)
  CRAWL_CATEGORIES  "Kinh doanh,Thế giới" (rỗng = tất cả category của nguồn)
  CRAWL_DATE        "2026-07-02"          (rỗng = HÔM QUA theo giờ VN lúc chạy;
                                          "all" = không lọc ngày)

Lọc ngày: chỉ giữ bài ĐÚNG target_date (mới/cũ hơn -> bỏ). Listing xếp mới->cũ
nhưng hay lẫn bài "nổi bật" ngày cũ, nên KHÔNG dừng vì 1 bài lẻ: chỉ dừng phân
trang khi cả 1 trang không còn bài nào mới bằng target_date.
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
            # Đã có trong Bronze (DAG chạy lại cùng ngày) -> khỏi fetch, khỏi
            # ghi trùng. Vẫn ghi ngày đã biết vào `seen` để logic dừng đúng.
            h = md5(link)
            if h in known:
                seen[link] = known[h]
                n_known += 1
                continue
            seen[link] = None                 # đã xử lý; ngày điền bên dưới nếu parse được
            art_html, _ = fetch(link)
            if not art_html:
                print(f"  [skip] fetch lỗi: {link}")
                continue

            art = source.parse_article(art_html, link)
            # `category` = ĐÚNG TÊN chuyên mục của báo đó (khóa registry, có dấu):
            # "Thời sự", "Khoa học công nghệ", "Nhịp sống số". Không chuẩn hóa chéo
            # nguồn vì cùng một tên chưa chắc cùng nội dung (Nhịp sống số của Tuổi
            # Trẻ ≠ Công nghệ của VietnamNet). `slug` chỉ dùng nội bộ để so khớp.
            art.category = name
            demote_agency_author(art)

            # Bỏ sub_category/topic TRÙNG category (vd crawl nhip-song-so nhưng
            # breadcrumb "Công nghệ > Nhịp sống số" -> sub = chính category slug).
            if _slugify(art.sub_category) == slug:
                art.sub_category, art.topic = art.topic, ""   # đôn cấp sâu hơn lên
            if _slugify(art.topic) == slug:
                art.topic = ""

            # Chỉ giữ bài text thường: bỏ bài bị đánh dấu (video/megastory/
            # e-magazine) hoặc không có nội dung text.
            if art.crawl_status != "success" or len(art.content) < MIN_CONTENT_LEN:
                print(f"  [skip] {art.crawl_status} / nội dung ngắn: {link}")
                continue

            # Lọc theo ngày. Listing xếp mới->cũ NHƯNG hay lẫn bài "nổi bật" ngày
            # cũ -> KHÔNG dừng vì 1 bài lẻ; chỉ bỏ qua bài ngoài ngày. Việc dừng
            # phân trang quyết định ở cuối trang (xem newest_on_page bên dưới).
            if target_date is not None:
                d = art.published_at.date() if art.published_at else None
                seen[link] = d
                if d is None:
                    continue                      # không rõ ngày -> bỏ
                if d != target_date:
                    continue                      # mới/cũ hơn -> bỏ, đọc tiếp

            # Qua mọi bộ lọc -> lưu raw (nén) + giữ bài
            try:
                raw_storage.save_raw_html(source.name, md5(link), art_html)
            except Exception as e:  # noqa: BLE001 - không để lỗi raw làm hỏng crawl
                print(f"  [raw] lỗi lưu {link}: {e}")
            articles.append(art)

        # Cả trang không còn bài nào mới bằng target_date -> đã phân trang qua hẳn
        # ngày cần lấy, dừng (chịu được bài "nổi bật" ngày cũ lẻ tẻ vì xét MAX).
        # Xét MỌI link trên trang (kể cả link trùng đã xử lý ở trang trước, ngày
        # lấy từ `seen`) để việc bỏ trùng không gây dừng sớm nhầm.
        page_dates = [seen.get(l) for l in page_links]
        page_dates = [d for d in page_dates if d is not None]
        newest_on_page = max(page_dates) if page_dates else None
        if target_date is not None and newest_on_page is not None and newest_on_page < target_date:
            print(f"[{source.name}/{name}] trang {pi}: đã qua {target_date}, dừng.")
            stop = True
        if stop:
            break
    else:
        # Vòng lặp hết page_urls mà CHƯA từng "đã qua target" -> có thể bị cắt cụt:
        # bài mới hơn target (vd hôm nay) nhiều tới mức lấp kín MAX_PAGES trước khi
        # phân trang tới hết ngày target. Cảnh báo để không âm thầm thiếu bài.
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
        # Gom cả nguồn thành 1 batch -> 1 append (1 snapshot) -> ít xung đột
        # commit khi nhiều task nguồn ghi song song vào cùng bảng.
        # Đọc 1 lần những gì đã có trong Bronze cho nguồn này -> chống trùng
        # CHÉO lần chạy (trạng thái trong RAM chỉ chống trùng trong 1 lần chạy).
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
            print(f"[{src_name}] Saved {saved} articles (1 snapshot)")

    print(f"\nDone. Tổng: {total} articles vào bronze.{storage.BRONZE_TABLE}.")
    return total


if __name__ == "__main__":
    src_env = os.getenv("CRAWL_SOURCES", "").strip()
    sources = [s.strip() for s in src_env.split(",") if s.strip()] or None

    cats_env = os.getenv("CRAWL_CATEGORIES", "").strip()
    categories = [c.strip() for c in cats_env.split(",") if c.strip()] or None

    date_env = os.getenv("CRAWL_DATE", "").strip()
    if date_env.lower() in ("all", "none"):
        target = None                              # không lọc ngày (crawl hết)
    elif date_env:
        target = date.fromisoformat(date_env)
    else:
        # Mặc định: NGÀY HÔM QUA theo giờ VN tại thời điểm container chạy
        # (không phụ thuộc logical date của Airflow). VN không có DST -> +7 cố định.
        _TZ_VN = timezone(timedelta(hours=7))
        target = (datetime.now(_TZ_VN) - timedelta(days=1)).date()
    print(f"target_date = {target or 'ALL'}")

    run(sources=sources, categories=categories, target_date=target)
