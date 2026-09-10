"""Hỏi-đáp trên kho tin: truy vấn -> Qdrant -> Gemini trả lời kèm nguồn.

    python -m newscrawler.ask "Giá vàng hôm qua thế nào?"
    python -m newscrawler.ask --source vnexpress --days 3 "Tin thể thao nổi bật?"

Chống bịa: chỉ trả lời dựa trên đoạn trích lấy được; không đủ căn cứ thì nói
không tìm thấy, không suy đoán.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from google import genai
from google.genai import types
from qdrant_client import models as qm

from . import bm25, vectorstore as vs
from .embed import embed_texts
from .gemini import KeyPool, is_quota_error

# Dùng ALIAS "-latest" chứ không ghim số phiên bản: 2026-09-07 Google gỡ
# `gemini-2.5-flash` với tài khoản mới (404 "no longer available to new users")
# và cả pipeline hỏi-đáp chết câm. Alias tự trỏ sang bản flash hiện hành.
CHAT_MODEL = os.getenv("GEMINI_CHAT_MODEL", "gemini-flash-latest")
TOP_K = int(os.getenv("ASK_TOP_K", "8"))
TZ_VN = timezone(timedelta(hours=7))
_MAX_TRY = 4
# Lấy dư bao nhiêu lần ở tầng chunk trước khi gộp về bài. Bài dài chiếm nhiều
# chunk nên không lấy dư sẽ hụt số bài trả về.
OVERFETCH = int(os.getenv("ASK_OVERFETCH", "3"))

# --- Cách sắp xếp bài sau khi truy hồi ------------------------------------
SORT_RELEVANCE = "relevance"   # độ khớp trước, NGANG ĐIỂM thì bài mới trước (mặc định)
SORT_RECENT = "recent"         # thuần mới nhất
SORTS = (SORT_RELEVANCE, SORT_RECENT)

# Hai điểm chênh nhau dưới ngưỡng này coi như NGANG NHAU -> để ngày quyết định.
# Thời gian chỉ phá thế hoà, KHÔNG bao giờ hạ điểm một bài đang khớp tốt hơn.
SCORE_EPS = float(os.getenv("ASK_SCORE_EPS", "0.02"))
# Sắp theo "mới nhất" cần lấy dư nhiều hơn: bài mới nhất của kho chưa chắc lọt
# vào nhóm khớp cao nhất, không lấy rộng thì "mới nhất" chỉ mới trong phạm vi hẹp.
RECENT_OVERFETCH = int(os.getenv("ASK_RECENT_OVERFETCH", "8"))

# --- Gác cổng chống bịa ----------------------------------------------------
# Sàn COSINE THẬT (không phải điểm RRF) để coi là "kho có dữ liệu về việc này".
# Đo trên chính kho này:
#     "giá vàng hôm nay tăng hay giảm"          -> 0.771  (có dữ liệu)
#     "công thức làm bánh chưng nhân sầu riêng" -> 0.627  (không có)
#     "cách nuôi cá koi trong bể xi măng"       -> 0.617  (không có)
# Vector luôn trả về thứ "gần nhất", nên không có sàn thì câu hỏi vô nghĩa vẫn
# ra 8 bài và LLM vẫn có cái để bịa. Hiệu chuẩn trên ít mẫu -> chỉnh được qua env.
MIN_COSINE = float(os.getenv("ASK_MIN_COSINE", "0.70"))
# Đường tương tác: chờ quota tối đa ngần này giây rồi báo lỗi, không treo.
MAX_WAIT = float(os.getenv("ASK_MAX_WAIT", "20"))
# Chấp nhận cả "[1][2]" lẫn "[1, 2, 3]" — model dùng cả hai kiểu, và bắt cứng
# một kiểu sẽ vứt oan câu ĐANG trích dẫn đúng.
_CITE_RE = re.compile(r"\[\s*(\d+(?:\s*[,;]\s*\d+)*)\s*\]")

# Câu "từ chối" phải là một chuỗi CỐ ĐỊNH, không phải câu tự do — để tầng trên
# nhận ra bằng so khớp chuỗi và ẩn hẳn khối trả lời đi, chỉ hiện danh sách bài.
NO_DATA = "KHÔNG ĐỦ DỮ LIỆU"

SYSTEM = f"""Bạn là bộ tóm tắt tin tức tiếng Việt. Bạn KHÔNG phải trợ lý trò chuyện.

Bạn chỉ được dùng các ĐOẠN TRÍCH cho sẵn. Bạn không có kiến thức nào khác.

Quy tắc tuyệt đối:
- Mọi câu bạn viết PHẢI truy được về một đoạn trích cụ thể, và phải gắn [số] của đoạn đó.
- Câu nào không gắn được [số] thì XOÁ câu đó đi, đừng viết.
- Nếu các đoạn trích KHÔNG trả lời được câu hỏi, in ra ĐÚNG một dòng: {NO_DATA}
  Không giải thích, không xin lỗi, không gợi ý, không viết gì thêm.
- Đoạn trích chỉ liên quan mơ hồ tới câu hỏi cũng tính là KHÔNG trả lời được -> in {NO_DATA}
- Không suy luận bắc cầu, không cộng dồn thông tin từ nhiều bài để tạo ra kết
  luận mới mà không bài nào nói.
- Không dùng "có thể", "dường như", "theo tôi" — chỉ nêu điều đoạn trích ghi rõ.
- Nếu các nguồn mâu thuẫn, nêu cả hai kèm [số] của từng bên.

Viết tối đa 4 gạch đầu dòng, mỗi dòng một ý, tiếng Việt, không mở bài kết bài."""


def search(query: str, top_k: int = TOP_K, source: str | None = None,
           category: str | None = None, days: int | None = None,
           author: str | None = None, tag: str | None = None,
           attribution: str | None = None) -> list:
    gclient = KeyPool(max_wait=MAX_WAIT)
    qc = vs.client()

    # task_type khác lúc index (RETRIEVAL_QUERY vs RETRIEVAL_DOCUMENT) — Gemini
    # tối ưu riêng cho từng vai, dùng sai làm giảm rõ chất lượng truy hồi.
    qvec = embed_texts(gclient, [query], "RETRIEVAL_QUERY")[0]
    sidx, sval = bm25.query_vector(query)

    # Trường định danh -> so khớp CHÍNH XÁC ở payload, không dựa vào lexical match
    # trong text (tránh nhầm tác giả với người được nhắc trong bài).
    must = []
    if source:
        must.append(qm.FieldCondition(key="source", match=qm.MatchValue(value=source)))
    if category:
        must.append(qm.FieldCondition(key="category", match=qm.MatchValue(value=category)))
    if author:
        must.append(qm.FieldCondition(key="author_name", match=qm.MatchValue(value=author)))
    if tag:
        # `tags` là mảng -> MatchValue khớp nếu BẤT KỲ phần tử nào bằng giá trị
        must.append(qm.FieldCondition(key="tags", match=qm.MatchValue(value=tag)))
    if attribution:
        # cũng là mảng: "AFP, Reuters" đã tách nên lọc 'Reuters' bắt được cả bài đó
        must.append(qm.FieldCondition(key="source_attribution",
                                      match=qm.MatchValue(value=attribution)))
    if days:
        since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
        must.append(qm.FieldCondition(key="published_ts", range=qm.Range(gte=since)))

    # Hybrid: dense (ngữ nghĩa) + BM25 (khớp chính xác tên riêng/số liệu), hợp
    # nhất bằng RRF trên server.
    points = vs.hybrid_search(
        qc, qvec, sidx, sval, limit=top_k,
        query_filter=qm.Filter(must=must) if must else None,
        with_vectors=True,
    )
    # Cosine thật = tích vô hướng, vì cả hai vector đều đã chuẩn hoá L2.
    # Gắn vào payload để tầng trên dùng làm ngưỡng; điểm RRF không làm được
    # việc này (nó chỉ nói "hạng mấy", không nói "có giống không").
    for p in points:
        vec = (p.vector or {}).get(vs.DENSE) if isinstance(p.vector, dict) else None
        if vec is not None and p.payload is not None:
            p.payload["_cos"] = sum(a * b for a, b in zip(qvec, vec))
    return points


def group_articles(points: list, limit: int, sort: str = SORT_RELEVANCE) -> list[dict]:
    """Gộp chunk về BÀI BÁO — đơn vị người dùng quan tâm.

    Trước đây việc gộp diễn ra SAU khi đã cắt top-k, nên một bài dài 4 chunk có
    thể chiếm 4/8 chỗ rồi hiện ra như một nguồn duy nhất: người dùng xin 8 bài
    mà chỉ nhận được 5. Nay lấy dư ở tầng chunk rồi mới cắt ở tầng bài.

    Điểm của bài = điểm của chunk khớp mạnh nhất (không cộng dồn — cộng dồn sẽ
    thiên vị bài dài chỉ vì nó có nhiều chunk).
    """
    by: dict = {}
    for p in points:
        pl = p.payload or {}
        key = (pl.get("source"), pl.get("article_id"))
        a = by.get(key)
        if a is None:
            a = by[key] = {"payload": pl, "texts": [], "chunks": [],
                           "score": p.score, "cos": 0.0}
        a["texts"].append(pl.get("text", ""))
        a["chunks"].append(f"{pl.get('chunk_idx')}({pl.get('chunk_type','')})")
        a["score"] = max(a["score"], p.score)
        a["cos"] = max(a["cos"], pl.get("_cos") or 0.0)

    for a in by.values():
        a["ts"] = a["payload"].get("published_ts") or 0
        # Gom điểm thành bậc: chênh dưới SCORE_EPS là "ngang nhau". RRF vốn hay
        # ra điểm trùng khít (0.5333 lặp lại), và khi đó thứ tự trước đây phụ
        # thuộc thứ tự dict — tuỳ tiện. Nay ngang bậc thì bài mới đứng trước.
        a["tier"] = round(a["score"] / SCORE_EPS)

    if sort == SORT_RECENT:
        rank = lambda x: (-x["ts"], -x["score"])          # noqa: E731
    else:
        rank = lambda x: (-x["tier"], -x["ts"])           # noqa: E731

    out = []
    for n, a in enumerate(sorted(by.values(), key=rank)[:limit], 1):
        pl = a["payload"]
        out.append({
            "n": n,
            "title": pl.get("title", ""),
            "description": pl.get("description", ""),
            "author_name": pl.get("author_name", ""),
            "source_attribution": pl.get("source_attribution") or [],
            "url": pl.get("url", ""),
            "thumbnail_url": pl.get("thumbnail_url", ""),
            "source": pl.get("source", ""),
            "category": pl.get("category", ""),
            "date": (pl.get("published_at") or "")[:10],
            "score": round(a["score"], 4),
            "cos": round(a["cos"], 3),
            "tier": a["tier"],
            "chunks": a["chunks"],
            "texts": a["texts"],
        })
    return out


def _prompt_blocks(articles: list[dict]) -> str:
    """Đoạn trích đưa cho LLM. Số [n] khớp đúng với thứ tự thẻ bài hiển thị."""
    return "\n\n---\n\n".join(
        f"[{a['n']}] {a['title']}\n"
        f"Nguồn: {a['source']} | Ngày: {a['date']} | Chuyên mục: {a['category']}\n"
        + "\n".join(a["texts"])
        for a in articles
    )


def _generate(pool: KeyPool, prompt: str) -> str:
    """Sinh câu trả lời. Hai chế độ hỏng, xử lý khác nhau:

      * 429 -> key đó cạn hạn ngạch, cho nghỉ rồi `acquire()` lấy key khác
      * 503 -> model quá tải nhất thời (đo được với `gemini-flash-latest`),
               chờ lũy tiến rồi thử lại; KHÔNG đổi key vì key không có lỗi

    `KeyPool` là BỂ key, không phải client — phải `acquire()` mới có client.
    """
    cfg = types.GenerateContentConfig(system_instruction=SYSTEM, temperature=0.2)
    last: Exception | None = None
    for attempt in range(_MAX_TRY):
        idx, client = pool.acquire()
        try:
            return client.models.generate_content(
                model=CHAT_MODEL, contents=prompt, config=cfg).text
        except Exception as e:  # noqa: BLE001
            last = e
            if is_quota_error(e):
                pool.penalize()
            elif "503" in str(e) or "UNAVAILABLE" in str(e):
                time.sleep(2 ** attempt)
            else:
                raise
    raise SystemExit(f"Không gọi được {CHAT_MODEL} sau {_MAX_TRY} lần: {last}")


def search_articles(query: str, top_k: int = TOP_K,
                    sort: str = SORT_RELEVANCE, **kw) -> list[dict]:
    """Truy hồi rồi trả về DANH SÁCH BÀI (đã khử trùng), không phải chunk.

    Lấy dư ở tầng chunk để sau khi gộp vẫn còn đủ `top_k` bài KHÁC NHAU. Chế độ
    "mới nhất" lấy dư rộng hơn hẳn, vì bài mới nhất của kho không nhất thiết nằm
    trong nhóm khớp cao nhất.
    """
    mult = RECENT_OVERFETCH if sort == SORT_RECENT else OVERFETCH
    points = search(query, top_k=top_k * mult, **kw)
    return group_articles(points, top_k, sort=sort)


def _enforce_citations(answer: str, n_articles: int) -> str | None:
    """CỔNG 2 — chỉ giữ lại câu có trích dẫn [n] HỢP LỆ.

    Prompt chỉ là lời dặn; model vẫn có thể viết câu không nguồn hoặc trích [9]
    khi chỉ có 5 bài. Ở đây cắt bằng code: dòng nào không gắn được [n] trong
    khoảng 1..n_articles thì bỏ. Không còn dòng nào -> không có câu trả lời.
    """
    keep = []
    for line in answer.splitlines():
        if not line.strip():
            continue
        nums = [int(x) for g in _CITE_RE.findall(line)
                for x in re.split(r"[,;]", g)]
        if nums and all(1 <= x <= n_articles for x in nums):
            keep.append(line)
    return "\n".join(keep) or None


def ask(query: str, top_k: int = TOP_K, generate: bool = True,
        sort: str = SORT_RELEVANCE, **kw) -> dict:
    """Trả về {"articles", "answer", "grounded", "best_cos"}.

    `articles` LUÔN là phần chính — có gì trong kho thì hiện nấy.
    `answer` chỉ tồn tại khi qua được CẢ BA cổng:
        1. cosine thật của bài tốt nhất >= MIN_COSINE  (kho thực sự có dữ liệu)
        2. model không tự khai KHÔNG ĐỦ DỮ LIỆU
        3. còn ít nhất một câu có trích dẫn [n] hợp lệ
    """
    articles = search_articles(query, top_k=top_k, sort=sort, **kw)
    best_cos = max((a["cos"] for a in articles), default=0.0)
    grounded = best_cos >= MIN_COSINE
    out = {"articles": articles, "answer": None,
           "grounded": grounded, "best_cos": round(best_cos, 3)}

    # CỔNG 1: kho không có gì đủ gần -> không gọi LLM. Vừa chặn bịa, vừa đỡ quota.
    if not articles or not generate or not grounded:
        return out

    today = datetime.now(TZ_VN).strftime("%d/%m/%Y")
    prompt = (
        f"Hôm nay là {today}.\n\nCÁC ĐOẠN TRÍCH:\n\n{_prompt_blocks(articles)}\n\n"
        f"---\nCÂU HỎI: {query}"
    )
    answer = (_generate(KeyPool(max_wait=MAX_WAIT), prompt) or "").strip()
    if NO_DATA in answer or not answer:      # CỔNG 2: model tự khai không đủ căn cứ
        return out
    out["answer"] = _enforce_citations(answer, len(articles))   # CỔNG 3
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Hỏi-đáp trên kho tin đã crawl")
    ap.add_argument("query", nargs="+", help="câu hỏi")
    ap.add_argument("--source", help="lọc theo báo (vnexpress, tuoitre, ...)")
    ap.add_argument("--category", help="tên chuyên mục CỦA BÁO ĐÓ, có dấu "
                                   "('Thời sự', 'Nhịp sống số', ...)")
    ap.add_argument("--days", type=int, help="chỉ lấy bài trong N ngày gần đây")
    ap.add_argument("--author", help="lọc CHÍNH XÁC theo tên tác giả")
    ap.add_argument("--tag", help="lọc bài có tag này (khớp 1 phần tử của mảng tags)")
    ap.add_argument("--attribution", metavar="HANG",
                    help="lọc bài dẫn lại từ hãng này (Reuters, AFP, ...)")
    ap.add_argument("--top-k", type=int, default=TOP_K)
    ap.add_argument("--sort", choices=SORTS, default=SORT_RELEVANCE,
                    help="relevance = khớp trước, ngang điểm thì bài mới trước")
    ap.add_argument("--no-llm", action="store_true",
                    help="chỉ liệt kê bài, không gọi model sinh câu trả lời")
    args = ap.parse_args()


    out = ask(" ".join(args.query), top_k=args.top_k, source=args.source,
              category=args.category, days=args.days,
              author=args.author, tag=args.tag,
              attribution=args.attribution)
    print("\n" + out["answer"] + "\n")
    if out["sources"]:
        print("NGUỒN:")
        for s in out["sources"]:
            print(f"  [{s['n']}] ({s['score']}) {s['source']} {s['date']} — {s['title']}")
            print(f"      {s['url']}")


if __name__ == "__main__":
    main()
