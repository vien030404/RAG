"""Cắt bài báo thành chunk để embed.

Kích cỡ chunk được chọn theo phân bố độ dài đo trên 3.727 bài của corpus:
p50 khoảng 950 token, 80% dưới 1.400 token, 5,5% trên 2.000 token.

Mỗi bài sinh ra:

  chunk 0, loại SUMMARY — title + description
      Luôn có, đúng một chunk mỗi bài. Phục vụ các câu hỏi ở mức tổng quát
      ("có tin gì về X?"), vốn khớp với ý chính của bài chứ không khớp riêng
      đoạn văn nào.

  chunk 1..n, loại BODY — thân bài
      - thân bài ngắn (≤ SINGLE_CHUNK_TOKENS): giữ nguyên thành một chunk.
      - thân bài dài: chia thành các phần có tổng token xấp xỉ bằng nhau, số
        phần bằng ceil(tổng token / MAX_TOKENS). Chia đều thay vì nhồi đầy lần
        lượt, để không sinh ra một chunk 1.200 token nằm cạnh một chunk 150
        token — mọi chunk nhận lượng ngữ cảnh tương đương nhau.
      - hai chunk liền kề chồng nhau một đoạn văn, giữ mạch ý không bị đứt.

Mọi điểm cắt đều nằm trên ranh giới đoạn văn (`content` nối các thẻ <p> bằng
"\n\n"), không bao giờ cắt giữa đoạn. Đoạn văn báo tiếng Việt trung bình 66–90
token, đủ nhỏ để cách chia đều theo đoạn vẫn bám sát kích cỡ mục tiêu.

Thân bài không dán thêm title/description vào đầu: các trường định danh đã nằm
trong payload và trong chunk summary. Nhân bản chúng vào mọi chunk vừa làm loãng
vector vừa khiến các chunk cùng một bài trở nên giống nhau.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass

# Ước lượng cho tiếng Việt: 1 token ≈ 3 ký tự. Chữ có dấu tốn nhiều token hơn
# tiếng Anh nên tỉ lệ này thấp hơn mức ~4 ký tự thường dùng cho văn bản Latin.
MAX_TOKENS = int(os.getenv("CHUNK_MAX_TOKENS", "1200"))
# Thân bài dưới ngưỡng này giữ nguyên làm một chunk.
SINGLE_CHUNK_TOKENS = int(os.getenv("CHUNK_SINGLE_TOKENS", "1400"))
CHARS_PER_TOKEN = 3

SUMMARY = "summary"
BODY = "body"

_WS_RE = re.compile(r"[ \t]+")


def est_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


@dataclass
class Chunk:
    text: str          # nội dung đem đi embed
    idx: int           # thứ tự trong bài; 0 luôn là chunk summary
    n_chunks: int      # tổng số chunk của bài, tính cả summary
    kind: str          # SUMMARY | BODY


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip())


def _paragraphs(content: str) -> list[str]:
    return [p for p in (_clean(x) for x in (content or "").split("\n\n")) if p]


def _equal_groups(paras: list[str], n: int) -> list[list[str]]:
    """Chia danh sách đoạn thành n nhóm có tổng token xấp xỉ bằng nhau.

    Nhóm được đóng khi đã đạt mức trung bình, nhưng chỉ nếu số đoạn còn lại vẫn
    đủ cho các nhóm phía sau; thiếu điều kiện này sẽ sinh ra nhóm rỗng ở cuối.
    """
    if n <= 1:
        return [paras]
    toks = [est_tokens(p) for p in paras]
    target = sum(toks) / n

    groups: list[list[str]] = []
    cur: list[str] = []
    cur_tok = 0
    for i, (p, t) in enumerate(zip(paras, toks)):
        cur.append(p)
        cur_tok += t
        remaining = len(paras) - i - 1
        groups_left = n - len(groups) - 1       # số nhóm còn phải tạo sau nhóm này
        if len(groups) < n - 1 and cur_tok >= target and remaining >= groups_left:
            groups.append(cur)
            cur, cur_tok = [], 0
    if cur:
        groups.append(cur)
    return groups


def split(title: str, description: str, content: str) -> list[Chunk]:
    """Cắt một bài thành danh sách Chunk; trả về [] nếu không có gì để index."""
    title, description = _clean(title), _clean(description)
    head = "\n".join(x for x in (title, description) if x)

    out: list[Chunk] = []
    if head:
        out.append(Chunk(head, 0, 0, SUMMARY))

    paras = _paragraphs(content)
    if paras:
        body = "\n\n".join(paras)
        if est_tokens(body) <= SINGLE_CHUNK_TOKENS:
            groups = [paras]
        else:
            n = max(2, math.ceil(est_tokens(body) / MAX_TOKENS))
            groups = _equal_groups(paras, n)
            # Chồng một đoạn: mỗi nhóm ngoài nhóm đầu mở bằng đoạn cuối của
            # nhóm liền trước.
            groups = [groups[0]] + [
                [groups[k - 1][-1]] + g for k, g in enumerate(groups[1:], 1)
            ]
        for g in groups:
            out.append(Chunk("\n\n".join(g), len(out), 0, BODY))

    n_total = len(out)
    for c in out:
        c.n_chunks = n_total
    return out
