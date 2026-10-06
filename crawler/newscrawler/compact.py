"""Dọn và chuẩn hoá `bronze.articles` tại chỗ. Chạy thủ công, không nằm trong DAG.

Vì sao không có tầng Silver
---------------------------
Pipeline chỉ có một bảng dữ liệu. Ba việc mà một tầng trung gian thường đảm nhận
đều đã được xử lý ở nơi khác:

    khử trùng       crawl đã idempotent nhờ `storage.known_published_dates`
    lọc chất lượng  `main.py` lọc ngay lúc cào — đo lại thì không còn dòng nào
                    bị loại ở bước sau
    cột dẫn xuất    tập nguồn và chuyên mục cố định nên ánh xạ không đổi, tính
                    luôn trong `storage._to_row`

Một bảng Silver trong hoàn cảnh đó chỉ là bản sao thứ hai phải giữ đồng bộ.

Vai trò của module này
----------------------
Công cụ sửa hồi tố cho toàn bảng: dùng khi đã vá parser và cần parse lại HTML
thô, hoặc khi cần áp lại luật lọc cho dữ liệu đã cào từ trước.

Lưu ý khi chạy: module ghi bằng `overwrite`, nên chỉ được chạy lúc không có
crawl nào hoạt động. Bronze nhận append song song từ nhiều task nguồn, và một
lệnh overwrite chen vào giữa sẽ tranh chấp commit.

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


# Những tên chuyên mục từng bị ghi sai trong dữ liệu cũ, tra theo cặp (nguồn,
# slug hiện tại). Bảng này chỉ phục vụ việc chuẩn hoá dữ liệu đã có; crawl hiện
# tại lấy thẳng khoá registry nên không đi qua đây.
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
        # Tra theo cặp (nguồn, slug) chứ không chỉ theo giá trị, vì cùng một tên
        # chuyên mục có thể hợp lệ ở báo này mà sai ở báo khác: "Công nghệ" là
        # tên đúng ở một số nguồn, nhưng ở Tuổi Trẻ chuyên mục tương ứng mang
        # tên "Nhịp sống số".
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
    # `.column_names` liệt kê cả tên lồng như 'tags.element', nên chỉ xét các
    # trường cấp một; nếu không, `delete_column` sẽ cố xoá element type của list
    # và thất bại.
    stale = [f.name for f in table.schema().fields if f.name not in SCHEMA.names]

    # Iceberg không cho đổi kiểu một cột từ string sang list, nên phải xoá cột
    # rồi thêm lại với kiểu mới. Thao tác an toàn ở đây vì `tb` đã đọc toàn bộ
    # dữ liệu vào bộ nhớ và ngay sau đó ghi đè lại cả bảng.
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
