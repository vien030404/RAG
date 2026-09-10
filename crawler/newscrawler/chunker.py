"""Cắt bài báo thành chunk để embed.

Chiến lược (đo trên 3.727 bài thật: p50 ≈ 950 token, 80% dưới 1.400, 5,5% trên 2.000):

  chunk 0  SUMMARY  = title + description
           Một chunk/bài, luôn có. Bắt câu hỏi TỔNG QUÁT ("có tin gì về X?")
           vốn khớp với ý chính của bài chứ không khớp đoạn văn cụ thể nào.

  chunk 1..n  BODY   = thân bài
           - bài vừa (≤ SINGLE_CHUNK_TOKENS)  -> đúng 1 chunk, trọn thân bài
           - bài dài                          -> chia THÀNH CÁC PHẦN BẰNG NHAU,
             số phần = ceil(tổng token / MAX_TOKENS). Chia đều (thay vì nhồi đầy
             rồi để phần cuối cụt) để mọi chunk có lượng ngữ cảnh tương đương,
             không có chunk 1.200 token cạnh chunk 150 token.
           - overlap 1 đoạn giữa hai chunk liền kề để không đứt mạch ý.

Ranh giới cắt LUÔN là ranh giới đoạn văn (`content` nối các <p> bằng "\n\n");
không bao giờ cắt giữa đoạn. Đoạn văn báo VN trung bình 66–90 token nên chia đều
theo đoạn vẫn bám rất sát mục tiêu.

Thân bài KHÔNG dán title/description vào đầu: các trường định danh nằm ở payload
và ở chunk summary, nhân bản vào mọi chunk vừa làm loãng vector vừa khiến mọi
chunk cùng bài giống nhau.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass

# Ước lượng tiếng Việt: ~1 token ≈ 3 ký tự (dấu tốn token hơn tiếng Anh).
MAX_TOKENS = int(os.getenv("CHUNK_MAX_TOKENS", "1200"))
# Thân bài dưới ngưỡng này -> 1 chunk, không cắt.
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
    idx: int           # thứ tự trong bài; 0 luôn là summary
    n_chunks: int      # tổng số chunk của bài (kể cả summary)
    kind: str          # SUMMARY | BODY


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip())


def _paragraphs(content: str) -> list[str]:
    return [p for p in (_clean(x) for x in (content or "").split("\n\n")) if p]


def _equal_groups(paras: list[str], n: int) -> list[list[str]]:
    """Chia danh sách đoạn thành n nhóm có tổng token gần bằng nhau.

    Đóng nhóm khi đã đạt mức trung bình, nhưng chỉ khi số đoạn còn lại vẫn đủ
    cho các nhóm phía sau — nếu không sẽ ra nhóm rỗng ở cuối.
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
        con_lai = len(paras) - i - 1
        can_them = n - len(groups) - 1          # số nhóm còn phải tạo sau nhóm này
        if len(groups) < n - 1 and cur_tok >= target and con_lai >= can_them:
            groups.append(cur)
            cur, cur_tok = [], 0
    if cur:
        groups.append(cur)
    return groups


def split(title: str, description: str, content: str) -> list[Chunk]:
    """Cắt 1 bài thành các Chunk. Trả [] nếu không có gì để index."""
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
            # Overlap 1 đoạn: mỗi nhóm (trừ nhóm đầu) mở bằng đoạn cuối nhóm trước.
            groups = [groups[0]] + [
                [groups[k - 1][-1]] + g for k, g in enumerate(groups[1:], 1)
            ]
        for g in groups:
            out.append(Chunk("\n\n".join(g), len(out), 0, BODY))

    n_total = len(out)
    for c in out:
        c.n_chunks = n_total
    return out
