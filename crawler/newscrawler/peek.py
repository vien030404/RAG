"""Xem nhanh bảng Iceberg dưới dạng bảng / xuất CSV.

Không đọc thẳng file Parquet trên MinIO: Silver ghi bằng `overwrite` nên Parquet
của snapshot cũ vẫn nằm đó — đọc thô sẽ ra dữ liệu trùng/cũ. Đi qua PyIceberg để
nó giải đúng snapshot hiện hành.

    # 10 bài mới nhất
    docker compose run --rm crawler -m newscrawler.peek

    # lọc + chọn cột
    docker compose run --rm crawler -m newscrawler.peek -n 30 --source tuoitre \\
        --date 2026-08-26 --cols title,author_name,published_at

    # thống kê theo nguồn / chuyên mục / ngày
    docker compose run --rm crawler -m newscrawler.peek --stats

    # đọc trọn 1 bài
    docker compose run --rm crawler -m newscrawler.peek --id 10026082617134521

    # xuất CSV ra thư mục đã mount (mở bằng Excel)
    docker compose run --rm -v "$PWD/out:/out" crawler -m newscrawler.peek \\
        --csv /out/tin.csv --n 0
"""
from __future__ import annotations

import argparse
import collections
import os
import sys

import pyarrow as pa
import pyarrow.csv as pacsv
from pyiceberg.catalog.rest import RestCatalog

DEFAULT_COLS = ["published_at", "source", "category", "title", "author_name", "word_count"]


def _catalog() -> RestCatalog:
    return RestCatalog(
        name="rest",
        **{
            "uri": os.getenv("ICEBERG_REST_URI", "http://iceberg-rest:8181"),
            "warehouse": os.getenv("ICEBERG_WAREHOUSE", "s3://warehouse/"),
            "py-io-impl": "pyiceberg.io.pyarrow.PyArrowFileIO",
            "s3.endpoint": os.getenv("MINIO_ENDPOINT", "http://minio:9000"),
            "s3.access-key-id": os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
            "s3.secret-access-key": os.getenv("MINIO_SECRET_KEY", "minioadmin"),
            "s3.path-style-access": "true",
            "s3.region": os.getenv("AWS_REGION", "us-east-1"),
        },
    )


def _cell(v, width: int) -> str:
    if v is None:
        return "—".ljust(width)
    s = str(v).replace("\n", " ").replace("\t", " ")
    if isinstance(v, list):
        s = ", ".join(str(x) for x in v)
    if len(s) > width:
        s = s[: width - 1] + "…"
    return s.ljust(width)


def _print_table(rows: list[dict], cols: list[str], widths: dict[str, int]) -> None:
    line = "─" * (sum(widths[c] for c in cols) + 3 * (len(cols) - 1))
    print("  ".join(_cell(c.upper(), widths[c]) for c in cols))
    print(line)
    for r in rows:
        print("  ".join(_cell(r.get(c), widths[c]) for c in cols))


def _stats(tb: pa.Table) -> None:
    src = tb.column("source").to_pylist()
    day = [str(d) for d in tb.column("published_date").to_pylist()] \
        if "published_date" in tb.schema.names else []
    cat = tb.column("category").to_pylist() \
        if "category" in tb.schema.names else tb.column("category").to_pylist()

    print(f"Tổng: {tb.num_rows} dòng\n")
    print("THEO NGUỒN")
    for k, v in sorted(collections.Counter(src).items()):
        print(f"  {k:<14}{v:>7}")
    print("\nTHEO CHUYÊN MỤC")
    for k, v in sorted(collections.Counter(cat).items(), key=lambda x: -x[1]):
        print(f"  {k:<14}{v:>7}")
    if day:
        print("\nTHEO NGÀY XUẤT BẢN (15 gần nhất)")
        for k, v in sorted(collections.Counter(day).items())[-15:]:
            print(f"  {k:<14}{v:>7}")
    print("\nNGUỒN × NGÀY (5 ngày gần nhất)")
    if day:
        days = sorted(set(day))[-5:]
        srcs = sorted(set(src))
        pair = collections.Counter(zip(src, day))
        print("  " + "nguồn".ljust(14) + "".join(d.ljust(13) for d in days))
        for s in srcs:
            print("  " + s.ljust(14) + "".join(str(pair[(s, d)]).ljust(13) for d in days))


def main() -> None:
    ap = argparse.ArgumentParser(description="Xem bảng Iceberg (Bronze/Silver)")
    ap.add_argument("--table", default="bronze.articles", help="mặc định bronze.articles")
    ap.add_argument("-n", "--limit", dest="n", type=int, default=10,
                    help="số dòng (0 = tất cả)")
    ap.add_argument("--cols", help="danh sách cột, phẩy ngăn cách")
    ap.add_argument("--source", help="lọc theo báo")
    ap.add_argument("--category", help="lọc theo category")
    ap.add_argument("--date", help="lọc theo ngày xuất bản YYYY-MM-DD")
    ap.add_argument("--id", type=int, help="in TOÀN VĂN bài có article_id này")
    ap.add_argument("--stats", action="store_true", help="thống kê thay vì liệt kê")
    ap.add_argument("--schema", action="store_true", help="in schema rồi thoát")
    ap.add_argument("--csv", help="xuất ra file CSV (dùng với -v để mount thư mục)")
    args = ap.parse_args()

    catalog = _catalog()
    try:
        table = catalog.load_table(args.table)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Không mở được bảng {args.table}: {e}\n"
                 f"Bảng có sẵn: {['.'.join(t) for ns in catalog.list_namespaces() for t in catalog.list_tables(ns)]}")

    if args.schema:
        print(f"{args.table} — {len(table.schema().fields)} cột")
        for f in table.schema().fields:
            print(f"  {f.name:<22}{str(f.field_type):<28}{'' if f.required else 'nullable'}")
        return

    tb = table.scan().to_arrow()

    # --- lọc ---------------------------------------------------------------
    names = tb.schema.names
    keep = range(tb.num_rows)
    def _filter(col, want, conv=str):
        nonlocal keep
        if want is None or col not in names:
            return
        vals = tb.column(col).to_pylist()
        keep = [i for i in keep if conv(vals[i]) == want]
    _filter("source", args.source)
    _filter("category", args.category)
    _filter("published_date", args.date)
    if args.id is not None:
        aid = tb.column("article_id").to_pylist()
        keep = [i for i in keep if aid[i] == args.id]
    keep = list(keep)
    if len(keep) != tb.num_rows:
        tb = tb.take(keep)

    if tb.num_rows == 0:
        sys.exit("Không có dòng nào khớp bộ lọc.")

    # --- toàn văn 1 bài ----------------------------------------------------
    if args.id is not None:
        r = {c: tb.column(c).to_pylist()[0] for c in names}
        print(f"{r.get('title','')}\n{'=' * 70}")
        for k in ("source", "url", "author_name", "source_attribution",
                  "published_at", "category", "sub_category", "topic",
                  "tags", "word_count"):
            if k in r and r[k] not in (None, "", []):
                print(f"{k:<20}{r[k]}")
        print(f"\n{r.get('description','')}\n\n{'-' * 70}\n{r.get('content','')}")
        return

    # --- sắp xếp mới -> cũ --------------------------------------------------
    # Sắp bằng pyarrow, không dùng sorted() với key đọc cột — gọi to_pylist()
    # trong key function sẽ materialize lại cả cột ở MỖI lần so sánh.
    if "published_at" in names:
        tb = tb.sort_by([("published_at", "descending")])

    if args.stats:
        _stats(tb)
        return

    # --- xuất CSV -----------------------------------------------------------
    if args.csv:
        out = tb if args.n == 0 else tb.slice(0, args.n)
        if args.cols:
            out = out.select([c.strip() for c in args.cols.split(",")])
        # CSV không ghi được cột mảng (vd `tags`) -> nối thành chuỗi
        for i, field in enumerate(out.schema):
            if pa.types.is_list(field.type) or pa.types.is_large_list(field.type):
                joined = ["; ".join(str(x) for x in (v or []))
                          for v in out.column(field.name).to_pylist()]
                out = out.set_column(i, field.name, pa.array(joined, pa.string()))
        pacsv.write_csv(out, args.csv)
        print(f"Đã ghi {out.num_rows} dòng x {len(out.schema.names)} cột -> {args.csv}")
        return

    # --- in bảng ------------------------------------------------------------
    cols = [c.strip() for c in args.cols.split(",")] if args.cols else \
           [c for c in DEFAULT_COLS if c in names]
    missing = [c for c in cols if c not in names]
    if missing:
        sys.exit(f"Không có cột {missing}. Cột hợp lệ: {names}")

    limit = tb.num_rows if args.n == 0 else min(args.n, tb.num_rows)
    data = {c: tb.column(c).to_pylist()[:limit] for c in cols}
    rows = [{c: data[c][i] for c in cols} for i in range(limit)]

    # cột text dài thì cắt bớt, cột ngắn thì vừa nội dung
    widths = {}
    for c in cols:
        longest = max((len(str(r.get(c, ""))) for r in rows), default=0)
        widths[c] = max(len(c), min(longest, 60 if c in ("title", "description", "url") else 22))

    print(f"{args.table} — hiện {limit}/{tb.num_rows} dòng\n")
    _print_table(rows, cols, widths)


if __name__ == "__main__":
    main()
