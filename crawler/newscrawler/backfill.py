"""Cào bù những ngày còn thiếu trong Bronze.

Crawler lấy ngày hôm trước tính tại thời điểm container chạy, nên mỗi lượt phủ
đúng một ngày. Nếu scheduler ngừng vài hôm thì những ngày ở giữa không được cào,
và `catchup=False` khiến Airflow bỏ qua chúng thay vì chạy bù.

Module này lấp đúng khoảng trống đó: xác định các ngày thiếu rồi gọi `main.run()`
cho từng ngày.

    python -m newscrawler.backfill                    # khoảng giữa 2 lượt cào gần nhất
    python -m newscrawler.backfill --missing          # mọi ngày thiếu trong kho
    python -m newscrawler.backfill --from 2026-09-01 --to 2026-09-05
    python -m newscrawler.backfill --missing --apply  # bỏ --apply để chỉ liệt kê

Chạy lại nhiều lần an toàn: crawl đã idempotent nhờ
`storage.known_published_dates`, và bài đã có bị loại trước khi fetch nên cào bù
không sinh dòng trùng, cũng không tốn request.

Giới hạn cần biết: trang listing của báo chỉ lùi được `MAX_PAGES` trang. Ngày
càng xa thì càng phải lật nhiều trang mới tới, nên những ngày cách đây vài tuần
thường không còn cào lại được — khi đó `crawl_category` in cảnh báo rằng kết quả
có thể thiếu.
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta

from pyiceberg.exceptions import NoSuchTableError

from . import main as crawler
from .storage import TABLE_IDENT, _catalog_conn


def _bronze_days() -> tuple[set[date], set[date]]:
    """Đọc một lượt từ Bronze, trả về (tập ngày đăng đã có, tập ngày đã cào)."""
    try:
        table = _catalog_conn().load_table(TABLE_IDENT)
    except NoSuchTableError:
        return set(), set()
    tb = table.scan(selected_fields=("published_date", "crawled_at")).to_arrow()
    pub = {d for d in tb.column("published_date").to_pylist() if d}
    crawled = {t.date() for t in tb.column("crawled_at").to_pylist() if t}
    return pub, crawled


def gap_between_last_runs() -> list[date]:
    """Các ngày đăng rơi vào khoảng hở giữa hai lượt cào gần nhất.

    Mỗi lượt cào phủ đúng ngày liền trước nó, nên hai lượt chạy ở D1 và D2 để hở
    các ngày đăng từ D1 đến D2 - 2. Ví dụ cào ngày 09/09 rồi ngày 11/09: lượt
    đầu phủ 08/09, lượt sau phủ 10/09, còn 09/09 không ai phủ.
    """
    pub, crawled = _bronze_days()
    if len(crawled) < 2:
        return []
    d2, d1 = sorted(crawled)[-1], sorted(crawled)[-2]
    ngay = [d1 + timedelta(days=i) for i in range((d2 - d1).days - 1)]
    return [d for d in ngay if d not in pub]


def missing_days(start: date | None = None, end: date | None = None) -> list[date]:
    """Những ngày không có bài nào, trong khoảng [start, end].

    Mặc định khoảng xét chạy từ ngày đăng cũ nhất tới ngày đăng mới nhất đang có
    trong Bronze. Nghĩa là hàm chỉ tìm lỗ hổng bên trong phạm vi đã cào, không
    đề xuất mở rộng kho về quá khứ.
    """
    pub, _ = _bronze_days()
    if not pub:
        return []
    start = start or min(pub)
    end = end or max(pub)
    n = (end - start).days + 1
    return [d for i in range(n) if (d := start + timedelta(days=i)) not in pub]


def run(days: list[date], sources=None, categories=None) -> dict[date, int]:
    """Cào từng ngày, trả về {ngày: số bài ghi được}."""
    ket_qua: dict[date, int] = {}
    for i, d in enumerate(days, 1):
        print(f"\n{'=' * 62}\n[backfill] {i}/{len(days)} — target_date = {d}\n{'=' * 62}")
        try:
            ket_qua[d] = crawler.run(sources=sources, categories=categories,
                                     target_date=d)
        except Exception as e:  # noqa: BLE001 — một ngày lỗi không dừng cả lượt
            print(f"[backfill] ⚠ {d} lỗi: {type(e).__name__}: {e}")
            ket_qua[d] = -1
    return ket_qua


def main() -> None:
    ap = argparse.ArgumentParser(description="Cào bù ngày thiếu trong Bronze")
    ap.add_argument("--from", dest="start", metavar="YYYY-MM-DD",
                    help="ngày bắt đầu (mặc định: suy từ Bronze)")
    ap.add_argument("--to", dest="end", metavar="YYYY-MM-DD", help="ngày kết thúc")
    ap.add_argument("--missing", action="store_true",
                    help="mọi ngày THIẾU trong kho, thay vì chỉ khoảng giữa 2 lượt cào")
    ap.add_argument("--sources", help="giới hạn nguồn, phẩy ngăn cách")
    ap.add_argument("--categories", help="giới hạn chuyên mục, phẩy ngăn cách")
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                    help="chỉ cào N ngày MỚI NHẤT trong danh sách (0 = tất cả)")
    ap.add_argument("--apply", action="store_true",
                    help="cào thật (mặc định chỉ liệt kê ngày sẽ cào)")
    a = ap.parse_args()

    start = date.fromisoformat(a.start) if a.start else None
    end = date.fromisoformat(a.end) if a.end else None

    if start and end:
        n = (end - start).days + 1
        days = [start + timedelta(days=i) for i in range(n)]
        nguon_goc = f"khoảng {start} → {end}"
    elif a.missing:
        days = missing_days(start, end)
        nguon_goc = "ngày thiếu trong kho"
    else:
        days = gap_between_last_runs()
        nguon_goc = "khoảng giữa 2 lượt cào gần nhất"

    # Xử lý ngày mới trước: càng gần hiện tại thì listing càng còn lùi tới được,
    # nên khi `--limit` cắt bớt danh sách, phần giữ lại là phần có khả năng
    # thành công cao nhất.
    days.sort(reverse=True)
    if a.limit:
        days = days[: a.limit]
    days.sort()

    if not days:
        print(f"[backfill] Không có ngày nào cần cào ({nguon_goc}).")
        return

    print(f"[backfill] {nguon_goc}: {len(days)} ngày")
    print("           " + ", ".join(str(d) for d in days))
    cu_nhat = (date.today() - days[0]).days
    if cu_nhat > 14:
        print(f"[backfill] ⚠ ngày cũ nhất cách đây {cu_nhat} ngày — listing của báo "
              f"có thể không lùi tới nơi trong MAX_PAGES trang.")
    if not a.apply:
        print("[backfill] chạy thử. Thêm --apply để cào thật.")
        return

    sources = [s.strip() for s in (a.sources or "").split(",") if s.strip()] or None
    cats = [c.strip() for c in (a.categories or "").split(",") if c.strip()] or None
    ket_qua = run(days, sources=sources, categories=cats)

    print(f"\n{'=' * 62}\n[backfill] TỔNG KẾT")
    for d, n in sorted(ket_qua.items()):
        print(f"  {d}  {'LỖI' if n < 0 else f'{n:4d} bài'}")
    tot = sum(n for n in ket_qua.values() if n > 0)
    print(f"  {'-' * 22}\n  tổng   {tot} bài vào Bronze")


if __name__ == "__main__":
    main()
