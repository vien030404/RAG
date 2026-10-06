"""Sửa các cột nhãn bị bóc tách sai, bằng cách parse lại HTML thô trên MinIO.

Mỗi báo có cấu trúc trang riêng, nên heuristic bóc tách đôi khi lấy nhầm vùng.
Ba dạng sai quan sát được trên corpus:

    vnexpress    với bài dạng danh sách, tiêu đề phụ lọt vào `author_name`
    vietnamnet   tên tác giả dính liền chức danh, do span.name và span.position
                 nằm cạnh nhau và bị gộp mà không có dấu cách
    tuoitre,     hãng tin và bút danh toà soạn ("TTXVN", "PV", "Thông tin dịch
    dantri,      vụ") rơi vào `author_name` thay vì `source_attribution`
    thanhnien

Module không parse lại toàn bảng: chỉ xử lý những dòng bị đánh dấu nghi vấn, và
chỉ ghi đè bốn cột nhãn (`author_name`, `source_attribution`, `sub_category`,
`topic`). `content` giữ nguyên nên `content_hash` không đổi và embedding không
phải tính lại — đây là điều kiện để công cụ dùng được nhiều lần mà không tốn hạn
mức API.

    python -m newscrawler.repair              # chạy thử, liệt kê dòng sẽ sửa
    python -m newscrawler.repair --apply      # ghi thật
    python -m newscrawler.repair --check 40   # đối chứng trên những dòng đang đúng

Chỉ chạy khi không có crawl nào hoạt động, vì module ghi bằng `overwrite`.
"""
from __future__ import annotations

import argparse
import re

import pyarrow as pa

from . import raw_storage
from .sources import SOURCES
from .sources.base import demote_agency_author, split_sources
from .storage import SCHEMA, TABLE_IDENT, _catalog_conn

# Các cột được phép ghi đè. `content`, `title` và `url` nằm ngoài danh sách một
# cách có chủ đích: tầng trên đã dựa vào chúng, cụ thể là `content_hash` quyết
# định chunk nào phải embed lại.
LABEL_COLS = ("author_name", "source_attribution", "sub_category", "topic")

_AGENCY = {
    "ttxvn", "vna", "pv", "ctv", "nhóm pv", "nhóm phóng viên", "phóng viên",
    "dân trí", "tuổi trẻ", "thanh niên", "vnexpress", "vietnamnet",
    "thông tin dịch vụ", "tin tài trợ", "quảng cáo",
}


def _has_glued_words(s: str) -> bool:
    """Phát hiện chữ thường nằm liền ngay chữ hoa, dấu hiệu hai phần tử bị gộp.

    Ví dụ một ô tên tác giả chứa "<tên>Nhà báo" khi span tên và span chức danh
    nằm cạnh nhau mà không có dấu cách phân tách.

    Không dùng dải regex kiểu `[a-zà-ỹ][A-ZÀ-Ỹ]`: khoảng U+00C0–U+1EF8 chứa cả
    chữ thường có dấu của tiếng Việt, nên một tên viết đúng như "Hoàng" cũng
    khớp. Phải so sánh bằng `islower()` và `isupper()`.
    """
    return any(a.islower() and b.isupper() for a, b in zip(s, s[1:]))


def _has_lowercase_word(s: str) -> bool:
    """Phát hiện giá trị là một câu chứ không phải tên người.

    Tên người Việt viết hoa mọi từ, nên sự có mặt của một từ viết thường ở giữa
    gần như chắc chắn cho thấy ô này đã bị gán nhầm một đoạn văn.
    """
    return any(w[:1].islower() for w in s.split()[1:])


def is_suspect(author: str) -> bool:
    if not author:
        return False
    low = author.strip().lower()
    if low in _AGENCY or low.startswith("nguồn:"):
        return True
    return _has_glued_words(author) or _has_lowercase_word(author)


def _reparse(source_name: str, url: str, url_hash: str, crawled_at) -> dict | None:
    src = SOURCES.get(source_name)
    if src is None:
        return None
    html = raw_storage.load_raw_html(source_name, url_hash, crawled_at)
    if not html:
        return None
    art = src.parse_article(html, url)
    demote_agency_author(art)
    return {c: getattr(art, c) for c in LABEL_COLS}


def run(apply: bool = False, check: int = 0) -> int:
    catalog = _catalog_conn()
    table = catalog.load_table(TABLE_IDENT)
    tb = table.scan().to_arrow()
    cols = {n: tb.column(n).to_pylist() for n in tb.schema.names}
    # Dữ liệu chưa qua `compact` còn giữ kiểu chuỗi, nên phải đưa về mảng trước
    # khi so sánh; nếu không thì mọi dòng đều khác kết quả của parser mới chỉ vì
    # lệch kiểu, chứ không phải vì nội dung sai.
    cols["source_attribution"] = [split_sources(v) for v in cols["source_attribution"]]

    idxs = [i for i in range(tb.num_rows) if is_suspect(cols["author_name"][i])]
    if check:
        # Phép đối chứng: lấy những dòng hiện đang đúng, parse lại, rồi xem
        # parser mới có làm chúng đổi không. Giúp phân biệt "sửa được lỗi" với
        # "gây ra lỗi mới ở nơi vốn ổn".
        ok = [i for i in range(tb.num_rows) if cols["author_name"][i]
              and not is_suspect(cols["author_name"][i])]
        step = max(1, len(ok) // check)
        idxs = ok[::step][:check]
        print(f"[repair] ĐỐI CHỨNG {len(idxs)} dòng đang đúng\n")

    print(f"[repair] {len(idxs)} dòng cần xem\n")
    changed, no_raw, same = 0, 0, 0
    for i in idxs:
        new = _reparse(cols["source"][i], cols["url"][i], cols["url_hash"][i],
                       cols["crawled_at"][i])
        if new is None:
            no_raw += 1
            continue
        diff = {c: (cols[c][i], new[c]) for c in LABEL_COLS if cols[c][i] != new[c]}
        if not diff:
            same += 1
            continue
        changed += 1
        print(f"  {cols['source'][i]:<11} {cols['title'][i][:46]}")
        for c, (o, n) in diff.items():
            print(f"      {c:<20} {o!r}  ->  {n!r}")
        if apply and not check:
            for c in LABEL_COLS:
                cols[c][i] = new[c]

    print(f"\n[repair] đổi {changed} · giữ nguyên {same} · thiếu HTML thô {no_raw}")
    if check:
        print("[repair] (đối chứng, không ghi)" if changed == 0
              else "[repair] ⚠ parser mới làm ĐỔI dòng đang đúng — xem lại trước khi apply")
        return changed
    if not apply:
        print("[repair] chạy thử. Thêm --apply để ghi.")
        return changed
    if not changed:
        return 0

    new_tb = pa.table({n: pa.array(cols[n], SCHEMA.field(n).type) for n in SCHEMA.names},
                      schema=SCHEMA)
    table.overwrite(new_tb)
    print(f"[repair] đã ghi {new_tb.num_rows} dòng vào {TABLE_IDENT}.")
    return changed


TEXT_COLS = ("title", "description", "content")


def run_content(apply: bool = False) -> int:
    """Parse lại toàn văn từ HTML thô, dùng khi chính nội dung bài bị bóc sai.

    Ngược với `run()` ở trên: hàm đó giữ nguyên `content` để `content_hash` không
    đổi, còn ở đây `content` chính là thứ sai nên phải ghi lại, và chấp nhận
    embed lại những chunk thay đổi theo.

    Lỗi cần sửa: `get_text(strip=True)` nối text của các thẻ con mà không chèn
    dấu cách, nên `<p>thông tấn<b>TASS</b>của Nga</p>` cho ra "thông tấnTASScủa
    Nga". Đo trên corpus thì 85% số bài có ít nhất một chỗ như vậy. Nguyên nhân
    đã được xử lý ở `sources.base.inline_text`; hàm này chữa phần dữ liệu đã ghi.

    Không cần cào lại các nguồn: HTML thô trên MinIO còn nguyên thẻ và phủ 100%
    số bài, nên toàn bộ việc sửa diễn ra trên dữ liệu đã có.
    """
    catalog = _catalog_conn()
    table = catalog.load_table(TABLE_IDENT)
    tb = table.scan().to_arrow()
    cols = {n: tb.column(n).to_pylist() for n in tb.schema.names}
    cols["source_attribution"] = [split_sources(v) for v in cols["source_attribution"]]

    changed = no_raw = 0
    per_col = {c: 0 for c in TEXT_COLS}
    for i in range(tb.num_rows):
        src = SOURCES.get(cols["source"][i])
        html = raw_storage.load_raw_html(
            cols["source"][i], cols["url_hash"][i], cols["crawled_at"][i])
        if src is None or not html:
            no_raw += 1
            continue
        art = src.parse_article(html, cols["url"][i])
        diff = [c for c in TEXT_COLS if (getattr(art, c) or "") != (cols[c][i] or "")]
        if not diff:
            continue
        changed += 1
        for c in diff:
            per_col[c] += 1
        if apply:
            for c in TEXT_COLS:
                cols[c][i] = getattr(art, c) or ""
            cols["word_count"][i] = art.word_count
            cols["content_len"][i] = len(cols["content"][i])   # cột dẫn xuất

    print(f"[repair] {changed}/{tb.num_rows} dòng đổi "
          f"({', '.join(f'{c}={n}' for c, n in per_col.items())})"
          f" · thiếu HTML thô {no_raw}")
    if not apply:
        print("[repair] chạy thử. Thêm --apply để ghi.")
        return changed
    if not changed:
        return 0
    new_tb = pa.table({n: pa.array(cols[n], SCHEMA.field(n).type) for n in SCHEMA.names},
                      schema=SCHEMA)
    table.overwrite(new_tb)
    print(f"[repair] đã ghi {new_tb.num_rows} dòng vào {TABLE_IDENT}.")
    return changed


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Sửa nhãn sai, parse lại từ HTML thô")
    ap.add_argument("--apply", action="store_true", help="ghi thật (mặc định chạy thử)")
    ap.add_argument("--check", type=int, default=0, metavar="N",
                    help="đối chứng trên N dòng ĐANG ĐÚNG, không ghi")
    ap.add_argument("--content", action="store_true",
                    help="parse lại TOÀN VĂN (title/description/content) thay vì chỉ nhãn")
    a = ap.parse_args()
    if a.content:
        run_content(apply=a.apply)
    else:
        run(apply=a.apply, check=a.check)
