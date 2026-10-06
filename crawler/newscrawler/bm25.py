"""Sparse vector theo BM25 cho tiếng Việt, dùng song song với dense vector.

Module chỉ sinh phần term frequency đã bão hoà theo công thức BM25; phần IDF để
Qdrant tự tính trên toàn collection qua `Modifier.IDF`. Nhờ vậy phía client không
phải giữ thống kê corpus, và IDF vẫn đúng khi kho tin lớn dần.

Tiếng Việt viết rời từng âm tiết — "giá vàng" là hai token — nên nếu chỉ dùng
unigram thì "giá" và "vàng" đứng tách rời và mất nghĩa. Vì vậy vector gồm cả
bigram, tức cụm hai âm tiết liền nhau, dạng từ phổ biến nhất trong tiếng Việt.
"""
from __future__ import annotations

import os
import re
import unicodedata
import zlib
from collections import Counter

# BM25: k1 điều tiết mức bão hoà tần suất, b mức chuẩn hoá theo độ dài.
K1 = float(os.getenv("BM25_K1", "1.2"))
B = float(os.getenv("BM25_B", "0.75"))
# Độ dài trung bình của một chunk sau khi tách token, dùng cho phần chuẩn hoá
# theo độ dài của BM25. Chunk đã bị khống chế kích thước nên đại lượng này ít
# biến thiên, và sai lệch vừa phải chỉ làm điểm dịch đều chứ không đảo thứ tự.
#
# Giá trị mặc định lấy từ một lần đo trên corpus; cần đo lại sau mỗi lần đổi
# CHUNK_MAX_TOKENS hoặc CHUNK_SINGLE_TOKENS, vì hai tham số đó quyết định trực
# tiếp độ dài chunk.
AVG_DL = float(os.getenv("BM25_AVGDL", "645"))
USE_BIGRAM = os.getenv("BM25_BIGRAM", "1").lower() not in ("0", "false", "no")

_TOKEN_RE = re.compile(r"[0-9a-zà-ỹ]+", re.IGNORECASE)

# Hư từ tiếng Việt. Xuất hiện ở gần như mọi bài nên IDF xấp xỉ 0; loại bỏ để
# vector thưa hơn mà không mất khả năng phân biệt.
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
    """Chuẩn hoá NFC, hạ chữ thường, rồi tách thành âm tiết.

    Bước NFC là bắt buộc vì tiếng Việt tồn tại song song dạng tổ hợp và dạng
    dựng sẵn; hai dạng trông giống nhau nhưng khác chuỗi byte.

    Bigram không bao giờ bắc qua ranh giới dòng. Tiêu đề, sapo, từng đoạn văn và
    từng tag là những đơn vị độc lập, ghép qua ranh giới sẽ sinh term vô nghĩa
    nhưng vẫn được tính điểm — hai tag rời nhau "Iran" và "Tin nóng" sẽ tạo ra
    term "iran_tin".
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
    """Ánh xạ token sang id nguyên không âm theo yêu cầu của Qdrant.

    Dùng crc32 chứ không dùng `hash()`: `hash()` của Python được randomize theo
    từng tiến trình, nên cùng một token sẽ nhận id khác nhau giữa lần index và
    lần truy vấn.
    """
    return zlib.crc32(token.encode("utf-8")) & 0x7FFFFFFF


def keyword_text(tags=None) -> str:
    """Trải `tags` thành các dòng độc lập để nối vào sparse vector.

    Mỗi tag chiếm một dòng riêng nên bigram không bắc qua giữa hai tag.

    Hàm chỉ nhận `tags`, vì đó là từ khoá chủ đề do toà soạn gán — cùng bản chất
    với nội dung bài nên hoà vào text là hợp lý. Các trường định danh
    (`author_name`, `source_attribution`, `sub_category`, `topic`) được giữ
    ngoài một cách có chủ đích: trộn chúng vào text sẽ làm mất tính định vị, khi
    đó tên một tác giả không còn phân biệt được với chính tên đó xuất hiện trong
    thân bài. Những trường này nằm ở payload có index và được lọc bằng so khớp
    chính xác (xem `vectorstore.ensure_collection`).
    """
    return "\n".join(t for t in (tags or []) if t)


def doc_vector(text: str, keywords: str = "") -> tuple[list[int], list[float]]:
    """Sparse vector của một chunk khi index: TF đã bão hoà theo BM25.

    `keywords` do `keyword_text` sinh ra, được nối thêm vào phần text của chunk.
    """
    if keywords:
        text = f"{text}\n{keywords}"
    toks = tokenize(text)
    if not toks:
        return [], []
    dl = len(toks)
    denom_len = K1 * (1 - B + B * dl / AVG_DL)
    # Gộp theo id. Đụng độ crc32 rất hiếm, nhưng khi xảy ra thì cộng dồn trọng
    # số vẫn an toàn hơn là ghi đè.
    acc: dict[int, float] = {}
    for tok, tf in Counter(toks).items():
        acc[_tid(tok)] = acc.get(_tid(tok), 0.0) + tf * (K1 + 1) / (tf + denom_len)
    return list(acc.keys()), list(acc.values())


def query_vector(text: str) -> tuple[list[int], list[float]]:
    """Sparse vector của truy vấn: trọng số là tần suất term trong câu hỏi.

    Không bão hoà và không chuẩn hoá độ dài như phía document — phần IDF do
    Qdrant nhân vào lúc chấm điểm.
    """
    toks = tokenize(text)
    if not toks:
        return [], []
    acc: dict[int, float] = {}
    for tok, tf in Counter(toks).items():
        acc[_tid(tok)] = acc.get(_tid(tok), 0.0) + float(tf)
    return list(acc.keys()), list(acc.values())
