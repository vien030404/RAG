import hashlib
import os
from datetime import date

from dotenv import load_dotenv

load_dotenv()

from .config import CATEGORIES, MAX_PAGES
from .fetcher import fetch
from .parser import Article, parse_article, parse_article_links
from . import storage


def _category_slug(url: str) -> str:
    """https://vnexpress.net/kinh-doanh → kinh_doanh"""
    return url.rstrip("/").split("/")[-1].replace("-", "_")


def _page_url(base_url: str, page: int) -> str:
    """https://vnexpress.net/kinh-doanh, 2 → https://vnexpress.net/kinh-doanh-p2"""
    if page == 1:
        return base_url
    return f"{base_url}-p{page}"


def crawl_category(name: str, url: str, target_date: date | None = None) -> list[Article]:
    target_date = target_date or date.today()
    slug = _category_slug(url)
    articles: list[Article] = []

    for page in range(1, MAX_PAGES + 1):
        page_url = _page_url(url, page)
        print(f"\n[{name}] Page {page}: {page_url}")

        html, _ = fetch(page_url)
        if not html:
            print(f"[{name}] Failed to fetch page {page}, stopping.")
            break

        links = parse_article_links(html)
        if not links:
            print(f"[{name}] No links found on page {page}, stopping.")
            break

        stop_pagination = False

        for i, link in enumerate(links, 1):
            article_html, status = fetch(link)

            if not article_html:
                articles.append(Article(
                    url=link,
                    url_hash=hashlib.md5(link.encode()).hexdigest(),
                    category=slug,
                    crawl_status="error",
                    http_status=status,
                ))
                continue

            article = parse_article(article_html, link, http_status=status)
            article.category = slug

            article_date = article.published_at.date() if article.published_at else None

            # Bài mới hơn ngày mục tiêu (vd hôm nay) → bỏ qua, đọc tiếp xuống dưới
            if article_date is not None and article_date > target_date:
                print(f"  [skip] Article {i} date={article_date} > target={target_date}, bỏ qua (bài mới hơn).")
                continue

            # Không xác định được ngày → bỏ qua, không dừng phân trang
            if article_date is None:
                print(f"  [skip] Article {i} không có ngày, bỏ qua.")
                continue

            # Bài cũ hơn ngày mục tiêu → dừng (danh sách sắp xếp mới→cũ)
            if article_date < target_date:
                print(f"  [stop] Article {i} date={article_date} < target={target_date}, dừng phân trang.")
                stop_pagination = True
                break

            articles.append(article)
            print(f"  [{i}] {article.title[:70] if article.title else link}")

        if stop_pagination:
            break

    return articles


def run(categories: list[str] | None = None, target_date: date | None = None) -> list[Article]:
    targets = (
        {k: v for k, v in CATEGORIES.items() if k in categories}
        if categories
        else CATEGORIES
    )

    all_articles: list[Article] = []
    for name, url in targets.items():
        batch = crawl_category(name, url, target_date)
        if batch:
            saved = storage.save_articles(batch, target_date=target_date)
            print(f"[{name}] Saved {saved} articles")
        all_articles.extend(batch)

    print(f"\nDone. Total: {len(all_articles)} articles.")
    return all_articles


if __name__ == "__main__":
    # Scope is controllable via env so the container / Airflow can crawl a
    # subset instead of every category. Defaults preserve the original
    # behaviour (all categories, today).
    cats_env = os.getenv("CRAWL_CATEGORIES", "").strip()
    categories = [c.strip() for c in cats_env.split(",") if c.strip()] or None

    date_env = os.getenv("CRAWL_DATE", "").strip()
    target = date.fromisoformat(date_env) if date_env else None

    run(categories=categories, target_date=target)
