"""Sửa lại NHÃN của những dòng ghi sai cột — parse lại từ HTML thô trên MinIO.

Vì sao cần: mỗi báo có cấu trúc trang khác nhau, nên heuristic bóc tách đôi khi
lấy nhầm — đo được trên dữ liệu thật:

  * vnexpress  : tiêu đề phụ của bài dạng danh sách lọt vào `author_name`
                 ("Việt quất", "Hay quên, khó tập trung kéo dài")
  * vietnamnet : tên dính chức danh ("Đàm ĐệNhà báo") do gộp span.name + span.position
  * tuoitre/dantri/thanhnien : hãng tin & bút danh tòa soạn ("TTXVN", "PV",
                 "Thông tin dịch vụ") nằm ở `author_name` thay vì `source_attribution`

Công cụ này KHÔNG parse lại toàn bảng — chỉ những dòng bị đánh dấu nghi vấn, và
chỉ ghi đè các cột NHÃN (author_name, source_attribution, sub_category, topic).
`content` giữ nguyên nên `content_hash` không đổi, embedding không phải làm lại.

    python -m newscrawler.repair              # thử: liệt kê dòng sẽ sửa
    python -m newscrawler.repair --apply      # ghi thật
    python -m newscrawler.repair --check 40   # đối chứng trên dòng ĐANG ĐÚNG

Chạy khi không có crawl nào đang chạy (ghi bằng `overwrite`).
"""
from __future__ import annotations

import argparse
import re

import pyarrow as pa

from . import raw_storage
from .sources import SOURCES
from .sources.base import demote_agency_author, split_sources
from .storage import SCHEMA, TABLE_IDENT, _catalog_conn

# Cột được phép ghi đè. Cố ý KHÔNG đụng content/title/url để không làm hỏng
# những thứ tầng trên đã dựa vào (content_hash -> embedding).
LABEL_COLS = ("author_name", "source_attribution", "sub_category", "topic")

_AGENCY = {
    "ttxvn", "vna", "pv", "ctv", "nhóm pv", "nhóm phóng viên", "phóng viên",
    "dân trí", "tuổi trẻ", "thanh niên", "vnexpress", "vietnamnet",
    "thông tin dịch vụ", "tin tài trợ", "quảng cáo",
}


def _has_glued_words(s: str) -> bool:
    """"Đàm ĐệNhà báo" — chữ thường dính ngay chữ hoa, dấu hiệu gộp 2 phần tử.

    KHÔNG dùng dải regex kiểu [a-zà-ỹ][A-ZÀ-Ỹ]: dải À-Ỹ (U+00C0–U+1EF8) bao luôn
    chữ THƯỜNG tiếng Việt, nên "Hoàng" cũng khớp. Phải dùng islower/isupper.
    """
    return any(a.islower() and b.isupper() for a, b in zip(s, s[1:]))


def _has_lowercase_word(s: str) -> bool:
    """Tên người Việt viết hoa mọi từ; có từ viết thường -> gần như chắc là câu."""
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
    # Dữ liệu chưa qua `compact` còn là chuỗi -> đưa về mảng, nếu không mọi dòng
    # đều "khác" so với parser mới chỉ vì lệch kiểu.
    cols["source_attribution"] = [split_sources(v) for v in cols["source_attribution"]]

    idxs = [i for i in range(tb.num_rows) if is_suspect(cols["author_name"][i])]
    if check:
        # Đối chứng: lấy dòng ĐANG ĐÚNG, parse lại, xem parser mới có làm đổi không.
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
    """Parse lại TOÀN VĂN từ HTML thô — dùng khi vá lỗi bóc tách văn bản.

    Khác `run()` ở trên: hàm kia cố ý KHÔNG đụng `content` để giữ `content_hash`.
    Ở đây thì ngược lại — chính `content` mới là thứ sai, nên phải ghi lại và
    chấp nhận embed lại những chunk bị đổi.

    Lý do tồn tại: `get_text(strip=True)` nối text các thẻ con mà không chèn dấu
    cách, nên `<p>thông tấn<b>TASS</b>của Nga</p>` ra "thông tấnTASScủa Nga".
    Đo được 85% số bài dính ít nhất một chỗ. Đã vá ở `sources.base.inline_text`.

    KHÔNG cào lại: HTML thô trên MinIO còn nguyên thẻ, độ phủ 100%.
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
