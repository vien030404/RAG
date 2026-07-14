"""Crawler đa nguồn: fetch -> lưu HTML thô (raw) -> parse -> Iceberg Bronze.

Phạm vi điều khiển qua env:
  CRAWL_SOURCES     "vnexpress,tuoitre"  (rỗng = tất cả nguồn)
  CRAWL_CATEGORIES  "Kinh doanh,Thế giới" (rỗng = tất cả category của nguồn)
  CRAWL_DATE        "2026-07-02"          (rỗng = HÔM QUA; "all" = không lọc ngày)

Lọc ngày: listing xếp mới->cũ, nên gặp bài CŨ hơn target_date thì DỪNG phân
trang category đó (bài mới hơn -> bỏ, đọc tiếp; đúng ngày -> giữ).
"""
import os
import re
import unicodedata
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()

from .config import MAX_PAGES, MIN_CONTENT_LEN
from .fetcher import fetch
from .article import Article
from .sources import SOURCES
from .sources.base import md5
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


def crawl_category(source, name: str, url: str, target_date: date | None) -> list[Article]:
    slug = _cat_slug(url)
    articles: list[Article] = []

    # Trang 1 (cần HTML để dựng URL các trang sau, vd zoneId của tuoitre/thanhnien)
    html1, _ = fetch(url)
    if not html1:
        print(f"[{source.name}/{name}] fetch listing lỗi.")
        return articles
    page_urls = [url] + source.more_page_urls(url, html1, MAX_PAGES)

    stop = False
    for pi, purl in enumerate(page_urls, 1):
        html = html1 if pi == 1 else fetch(purl)[0]
        if not html:
            break
        links = source.parse_links(html)   # không giới hạn số bài / trang
        if not links:
            print(f"[{source.name}/{name}] trang {pi}: hết bài, dừng.")
            break
        print(f"\n[{source.name}/{name}] Trang {pi}: {len(links)} link")

        for link in links:
            art_html, _ = fetch(link)
            if not art_html:
                print(f"  [skip] fetch lỗi: {link}")
                continue

            art = source.parse_article(art_html, link)
            art.category = slug  # category authoritative từ listing đang crawl

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

            # Lọc theo ngày (listing xếp mới->cũ):
            if target_date is not None:
                d = art.published_at.date() if art.published_at else None
                if d is None:
                    continue                      # không rõ ngày -> bỏ
                if d > target_date:
                    continue                      # mới hơn -> bỏ, đọc tiếp
                if d < target_date:
                    stop = True                   # cũ hơn (quá hạn) -> DỪNG
                    break

            # Qua mọi bộ lọc -> lưu raw (nén) + giữ bài
            try:
                raw_storage.save_raw_html(source.name, md5(link), art_html)
            except Exception as e:  # noqa: BLE001 - không để lỗi raw làm hỏng crawl
                print(f"  [raw] lỗi lưu {link}: {e}")
            articles.append(art)

        if stop:
            break

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
        source_articles = []
        for cat_name, url in cats.items():
            batch = crawl_category(source, cat_name, url, target_date)
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
        target = date.today() - timedelta(days=1)  # mặc định: HÔM QUA
    print(f"target_date = {target or 'ALL'}")

    run(sources=sources, categories=categories, target_date=target)
