"""Sparse vector kiểu BM25 cho tiếng Việt, để hybrid search cùng dense vector.

Chỉ sinh phần **term frequency** đã bão hòa theo BM25; phần IDF do Qdrant tự
tính trên toàn collection (`Modifier.IDF`) — nhờ vậy không phải giữ thống kê
corpus ở phía client, và IDF luôn đúng khi kho tin lớn dần.

Tiếng Việt viết rời từng âm tiết ("giá vàng" = 2 token) nên unigram thôi thì
"giá" và "vàng" tách rời, mất nghĩa. Vì vậy sinh cả **bigram** — cụm 2 âm tiết
liền nhau — vốn là dạng từ phổ biến nhất trong tiếng Việt.
"""
from __future__ import annotations

import os
import re
import unicodedata
import zlib
from collections import Counter

# BM25: k1 điều tiết mức bão hòa tần suất, b mức chuẩn hóa theo độ dài.
K1 = float(os.getenv("BM25_K1", "1.2"))
B = float(os.getenv("BM25_B", "0.75"))
# Độ dài trung bình 1 chunk sau khi tách token — đo trên 5416 chunk của corpus
# Silver (mean=645, p50=695). Chunk được khống chế kích thước nên ít biến thiên;
# đo lại nếu đổi CHUNK_MAX_TOKENS.
AVG_DL = float(os.getenv("BM25_AVGDL", "645"))
USE_BIGRAM = os.getenv("BM25_BIGRAM", "1").lower() not in ("0", "false", "no")

_TOKEN_RE = re.compile(r"[0-9a-zà-ỹ]+", re.IGNORECASE)

# Hư từ tiếng Việt: xuất hiện ở hầu hết bài nên IDF ~0, bỏ đi cho vector nhẹ.
STOPWORDS = {
    "và", "của", "là", "có", "được", "cho", "trong", "với", "các", "những",
    "một", "này", "đó", "khi", "đã", "sẽ", "không", "cũng", "để", "từ", "tại",
    "về", "như", "ra", "vào", "lên", "xuống", "đến", "theo", "trên", "dưới",
    "nhưng", "mà", "thì", "nếu", "vì", "do", "bởi", "nên", "còn", "đang",
    "rằng", "hay", "hoặc", "chỉ", "rất", "quá", "lắm", "hơn", "nhất", "cả",
    "người", "việc", "sau", "trước", "nữa", "đây", "kia", "ấy", "ai", "gì",
    "thế", "vậy", "ở", "bị", "phải", "làm", "nhiều", "ít", "the", "of", "and",
}


def _segment_tokens(seg: str) -> list[str]:
    seg = unicodedata.normalize("NFC", seg).lower()
    return [t for t in _TOKEN_RE.findall(seg) if t not in STOPWORDS and len(t) > 1]


def tokenize(text: str) -> list[str]:
    """Chuẩn hóa NFC (tiếng Việt có dạng tổ hợp/dựng sẵn) -> thường -> tách âm tiết.

    Bigram KHÔNG bao giờ bắc qua ranh giới dòng: tiêu đề / sapo / từng đoạn văn /
    từng tag là các đoạn độc lập. Nếu ghép qua ranh giới sẽ sinh term rác — ví dụ
    2 tag rời nhau "Iran" + "Tin nóng" tạo ra "iran_tin", vô nghĩa nhưng vẫn được
    tính điểm.
    """
    out: list[str] = []
    for seg in (text or "").split("\n"):
        toks = _segment_tokens(seg)
        if not toks:
            continue
        out += toks
        if USE_BIGRAM:
            out += [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
    return out


def _tid(token: str) -> int:
    """Token -> id u32 (Qdrant yêu cầu chỉ số nguyên không âm). crc32 ổn định
    giữa các tiến trình, khác `hash()` của Python vốn bị randomize."""
    return zlib.crc32(token.encode("utf-8")) & 0x7FFFFFFF


def keyword_text(tags=None) -> str:
    """`tags` -> các dòng độc lập để nối vào sparse vector (mỗi tag 1 dòng nên
    bigram không bắc qua giữa 2 tag).

    CHỈ nhận `tags` — đó là từ khóa CHỦ ĐỀ do tòa soạn gán, cùng bản chất với nội
    dung nên hòa vào text là hợp lý. Các trường ĐỊNH DANH (`author_name`,
    `source_attribution`, `sub_category`, `topic`) cố ý KHÔNG đưa vào: trộn vào
    text làm chúng mất tính định vị — "Ngọc Ánh" tác giả sẽ lẫn với "Ngọc Ánh"
    được nhắc trong thân bài. Chúng nằm ở payload có index, lọc bằng so khớp
    CHÍNH XÁC (xem vectorstore.ensure_collection).
    """
    return "\n".join(t for t in (tags or []) if t)


def doc_vector(text: str, keywords: str = "") -> tuple[list[int], list[float]]:
    """Sparse vector cho 1 chunk khi index: TF đã bão hòa theo BM25.

    `keywords` (xem keyword_text) được nối thêm vào phần text của chunk.
    """
    if keywords:
        text = f"{text}\n{keywords}"
    toks = tokenize(text)
    if not toks:
        return [], []
    dl = len(toks)
    denom_len = K1 * (1 - B + B * dl / AVG_DL)
    # Gộp theo id (hiếm khi đụng độ crc32, nhưng nếu có thì cộng dồn cho an toàn)
    acc: dict[int, float] = {}
    for tok, tf in Counter(toks).items():
        acc[_tid(tok)] = acc.get(_tid(tok), 0.0) + tf * (K1 + 1) / (tf + denom_len)
    return list(acc.keys()), list(acc.values())


def query_vector(text: str) -> tuple[list[int], list[float]]:
    """Sparse vector cho truy vấn: trọng số = tần suất trong câu hỏi (IDF do
    Qdrant nhân vào)."""
    toks = tokenize(text)
    if not toks:
        return [], []
    acc: dict[int, float] = {}
    for tok, tf in Counter(toks).items():
        acc[_tid(tok)] = acc.get(_tid(tok), 0.0) + float(tf)
    return list(acc.keys()), list(acc.values())
