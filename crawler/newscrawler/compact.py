"""Dọn & chuẩn hóa `bronze.articles` TẠI CHỖ. Chạy tay, KHÔNG nằm trong DAG.

Trước đây việc này nằm ở một bảng riêng (`silver.articles`). Đã gộp vào Bronze vì
cả ba lý do tồn tại của nó đều đã hết:

  * khử trùng      -> crawl đã idempotent (`storage.known_published_dates`)
  * lọc chất lượng -> `main.py` đã lọc ngay lúc cào (đo được: 0 dòng bị loại)
  * cột dẫn xuất   -> nguồn/chuyên mục cố định nên `CATEGORY_MAP` không đổi,
                      tính luôn trong `storage._to_row`

Giữ lại script này làm CÔNG CỤ SỬA HỒI TỐ: khi vá parser rồi parse lại HTML thô,
hoặc khi cần áp lại luật lọc cho dữ liệu cũ, đây là chỗ ghi lại toàn bảng.

QUAN TRỌNG: ghi bằng `overwrite` nên phải chạy khi KHÔNG có crawl nào đang chạy
(Bronze có 4 task append song song; overwrite cùng lúc sẽ tranh chấp commit).

    docker compose run --rm --no-deps crawler -m newscrawler.compact
"""
from __future__ import annotations

import pyarrow as pa
from pyiceberg.types import ListType

from .config import MIN_CONTENT_LEN
from .main import _cat_slug, _slugify
from .sources import SOURCES
from .sources.base import split_sources
from .storage import SCHEMA, TABLE_IDENT, _catalog_conn

DERIVED = ("published_date", "content_len")


# Tên chuyên mục ĐÃ GHI SAI ở dữ liệu cũ, tra theo (nguồn, slug hiện tại).
# Chỉ để migrate; crawl mới lấy thẳng khóa registry nên không dùng tới.
_LEGACY_ALIAS = {
    ("tuoitre", "cong_nghe"): "Nhịp sống số",
}


def _legacy_slug_map() -> dict[tuple[str, str], str]:
    """(source, slug cũ) -> tên chuyên mục có dấu, dựng từ chính registry SOURCES.

    Dữ liệu cũ lưu `category` dạng slug ASCII sinh từ URL ("nhip_song_so"). Nay
    `category` là tên hiển thị của báo ("Nhịp sống số"). Bảng tra này dựng lại
    đúng ánh xạ đó nên không phải gõ tay, và tự đúng khi registry đổi.
    """
    out: dict[tuple[str, str], str] = {}
    for src_name, source in SOURCES.items():
        for name, url in source.categories.items():
            out[(src_name, _cat_slug(url))] = name
            out[(src_name, _slugify(name))] = name
    return out


def _to_display_category(tb: pa.Table) -> pa.Array:
    """Đổi cột `category` sang tên có dấu; giá trị không tra được thì giữ nguyên."""
    m = _legacy_slug_map()
    valid = {n: set(so.categories) for n, so in SOURCES.items()}
    src = tb.column("source").to_pylist()
    cat = tb.column("category").to_pylist()
    out, miss = [], set()
    for s, c in zip(src, cat):
        # Tra theo (nguồn, slug) — KHÔNG chỉ theo giá trị, vì cùng một tên có thể
        # hợp lệ ở báo này mà sai ở báo kia (Tuổi Trẻ từng bị gán "Công nghệ"
        # trong khi chuyên mục của họ tên là "Nhịp sống số").
        name = _LEGACY_ALIAS.get((s, _slugify(c))) or m.get((s, _slugify(c))) or m.get((s, c))
        if name is None:
            if c in valid.get(s, ()):     # đã đúng tên của chính nguồn đó
                name = c
            else:
                miss.add((s, c))
                name = c
        out.append(name)
    if miss:
        print(f"[compact] ⚠ không tra được {len(miss)} cặp (source, category): "
              + ", ".join(f"{s}/{c}" for s, c in sorted(miss)[:8]))
    return pa.array(out, pa.string())


def _keep_indices(tb: pa.Table) -> list[int]:
    """Mỗi bài 1 dòng, giữ bản `crawled_at` mới nhất.

    Khóa = (source, article_id); article_id null -> (source, url_hash).
    Ghép `source` vào khóa vì id số của các báo độc lập nhau, có thể trùng.
    """
    source = tb.column("source").to_pylist()
    aid = tb.column("article_id").to_pylist()
    uh = tb.column("url_hash").to_pylist()
    crawled = tb.column("crawled_at").to_pylist()

    best: dict[tuple, tuple] = {}  # key -> (crawled_at, index)
    for i in range(tb.num_rows):
        key = (source[i], aid[i]) if aid[i] is not None else (source[i], uh[i])
        prev = best.get(key)
        if prev is None or crawled[i] > prev[0]:
            best[key] = (crawled[i], i)
    return sorted(idx for _, idx in best.values())


def build() -> int:
    catalog = _catalog_conn()
    table = catalog.load_table(TABLE_IDENT)
    tb = table.scan().to_arrow()
    n0 = tb.num_rows
    print(f"[compact] Bronze: {n0} dòng")

    # --- 1. Khử trùng (dữ liệu cũ, trước khi crawl thành idempotent) ----------
    tb = tb.take(_keep_indices(tb))
    if tb.num_rows < n0:
        print(f"[compact] khử trùng: bỏ {n0 - tb.num_rows} dòng")

    # --- 2. Lọc chất lượng (áp lại luật hiện hành cho dữ liệu cũ) -------------
    status = tb.column("crawl_status").to_pylist()
    pub = tb.column("published_at").to_pylist()
    content = tb.column("content").to_pylist()
    keep = [
        i for i in range(tb.num_rows)
        if status[i] == "success"
        and pub[i] is not None
        and content[i]
        and len(content[i]) >= MIN_CONTENT_LEN
    ]
    if len(keep) < tb.num_rows:
        print(f"[compact] lọc chất lượng: bỏ {tb.num_rows - len(keep)} dòng")
        tb = tb.take(keep)
        pub = [pub[i] for i in keep]
        content = [content[i] for i in keep]

    # --- 3a. Chuẩn hóa `category` sang tên có dấu (migration dữ liệu cũ) ---
    ci = tb.schema.get_field_index('category')
    tb = tb.set_column(ci, 'category', _to_display_category(tb))

    # --- 3a'. `source_attribution`: chuỗi -> mảng (migration dữ liệu cũ) ------
    # "AFP, Reuters" là 2 hãng nhưng nằm chung 1 chuỗi nên lọc = 'Reuters' trượt.
    si = tb.schema.get_field_index("source_attribution")
    # Hỏi "chưa phải list?" chứ không hỏi "có phải string?": pyiceberg đọc ra
    # `large_string`, `is_string()` trả False và nhánh này lặng lẽ bị bỏ qua.
    _t = tb.schema.field(si).type if si >= 0 else None
    if si >= 0 and not (pa.types.is_list(_t) or pa.types.is_large_list(_t)):
        old = tb.column("source_attribution").to_pylist()
        new = [split_sources(v) for v in old]
        n_multi = sum(1 for v in new if len(v) > 1)
        print(f"[compact] source_attribution -> mảng: "
              f"{sum(1 for v in new if v)} dòng có giá trị, {n_multi} dòng nhiều hãng")
        tb = tb.set_column(si, "source_attribution",
                           pa.array(new, pa.list_(pa.string())))

    # --- 3b. Tính lại cột dẫn xuất --------------------------------------------
    # Bỏ bản cũ (có thể null với dòng ghi trước khi thêm cột) rồi tính lại hết.
    base = [f.name for f in SCHEMA if f.name not in DERIVED]
    tb = tb.select([n for n in base if n in tb.schema.names])
    tb = tb.append_column(
        "published_date", pa.array([p.date() for p in pub], pa.date32())
    ).append_column(
        "content_len", pa.array([len(c) for c in content], pa.int64())
    )
    tb = tb.select([f.name for f in SCHEMA]).cast(SCHEMA)

    # --- 4. Mở rộng schema bảng (dòng cũ chưa có 3 cột này) rồi ghi đè --------
    # .column_names gồm cả tên lồng ('tags.element') -> chỉ xét trường cấp 1,
    # nếu không delete_column sẽ đòi xóa element type của list và vỡ.
    stale = [f.name for f in table.schema().fields if f.name not in SCHEMA.names]

    # Iceberg KHÔNG cho đổi kiểu cột (string -> list): phải xóa rồi thêm lại.
    # An toàn vì `tb` đã đọc hết vào RAM và ngay sau đây ghi đè toàn bảng.
    fld = next((f for f in table.schema().fields
                if f.name == "source_attribution"), None)
    if fld is not None and not isinstance(fld.field_type, ListType):
        print("[compact] đổi kiểu source_attribution: string -> list<string>")
        stale.append("source_attribution")

    if stale:
        print(f"[compact] xóa cột không còn trong schema: {', '.join(stale)}")
        with table.update_schema() as us:
            for c in stale:
                us.delete_column(c)
        table = catalog.load_table(TABLE_IDENT)

    missing = [f.name for f in SCHEMA if f.name not in {g.name for g in table.schema().fields}]
    if missing:
        print(f"[compact] thêm cột vào schema bảng: {', '.join(missing)}")
        with table.update_schema() as us:
            us.union_by_name(SCHEMA)
        table = catalog.load_table(TABLE_IDENT)

    table.overwrite(tb)
    print(f"[compact] Đã ghi {tb.num_rows} dòng vào {TABLE_IDENT}.")
    return tb.num_rows


if __name__ == "__main__":
    build()
