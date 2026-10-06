"""Hỏi-đáp trên kho tin: truy vấn -> Qdrant -> Gemini trả lời kèm nguồn.

    python -m newscrawler.ask "Giá vàng hôm qua thế nào?"
    python -m newscrawler.ask --source vnexpress --days 3 "Tin thể thao nổi bật?"

Câu trả lời chỉ được dựng từ các đoạn trích truy hồi được. Ba cổng kiểm soát
chặn mô hình nói những điều kho không có, và quan trọng là cả ba đều cắt bằng
code chứ không chỉ bằng lời nhắc trong prompt:

    cổng 1   sàn cosine thực — kho không có gì đủ gần thì không gọi LLM
    cổng 2   chuỗi từ chối cố định — mô hình tự khai khi thiếu căn cứ
    cổng 3   bắt buộc trích dẫn — câu không dẫn được nguồn thì bị bỏ

Chi tiết từng cổng ở phần hằng số và ở `ask()` bên dưới.
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

# Dùng alias "-latest" thay vì ghim số phiên bản. Google có thể ngừng cấp một
# phiên bản cụ thể cho tài khoản mới trong khi `models.list()` vẫn liệt kê nó —
# khi đó mọi lệnh gọi trả 404 và cả đường hỏi-đáp ngừng hoạt động. Alias luôn
# trỏ sang bản flash hiện hành.
CHAT_MODEL = os.getenv("GEMINI_CHAT_MODEL", "gemini-flash-latest")
TOP_K = int(os.getenv("ASK_TOP_K", "8"))
TZ_VN = timezone(timedelta(hours=7))
_MAX_TRY = 4
# Hệ số lấy dư ở tầng chunk trước khi gộp về bài. Một bài dài chiếm nhiều chunk,
# nên lấy đúng `top_k` chunk sẽ gộp lại ra ít bài hơn yêu cầu.
OVERFETCH = int(os.getenv("ASK_OVERFETCH", "3"))

# --- Cách sắp xếp bài sau khi truy hồi ------------------------------------
SORT_RELEVANCE = "relevance"   # độ khớp trước, ngang điểm thì bài mới trước
SORT_RECENT = "recent"         # thuần theo thời gian
SORTS = (SORT_RELEVANCE, SORT_RECENT)

# Hai bài chênh điểm dưới ngưỡng này được coi là ngang nhau, và khi đó thời gian
# quyết định thứ tự. Thời gian chỉ phá thế hoà: nó không bao giờ đẩy một bài
# khớp tốt hơn xuống dưới một bài khớp kém hơn.
SCORE_EPS = float(os.getenv("ASK_SCORE_EPS", "0.02"))
# Chế độ sắp theo thời gian cần lấy dư rộng hơn: bài mới nhất của kho không chắc
# nằm trong nhóm khớp cao nhất, lấy hẹp thì "mới nhất" chỉ mới trong phạm vi hẹp
# đó chứ không mới trên toàn kho.
RECENT_OVERFETCH = int(os.getenv("ASK_RECENT_OVERFETCH", "8"))

# --- Ba cổng chống bịa -----------------------------------------------------
# Cổng 1: sàn cosine thực, không phải điểm RRF. Tìm kiếm vector luôn trả về thứ
# gần nhất nó có, nên nếu không đặt sàn thì một câu hỏi hoàn toàn ngoài phạm vi
# kho vẫn nhận đủ 8 bài và mô hình vẫn có nguyên liệu để suy diễn.
#
# Hiệu chuẩn trên chính corpus này:
#     "giá vàng hôm nay tăng hay giảm"          -> 0.771   (kho có)
#     "công thức làm bánh chưng nhân sầu riêng" -> 0.627   (kho không có)
#     "cách nuôi cá koi trong bể xi măng"       -> 0.617   (kho không có)
#
# Khoảng trống giữa hai nhóm khá rộng, nhưng số mẫu hiệu chuẩn còn ít nên ngưỡng
# để cấu hình được qua biến môi trường.
MIN_COSINE = float(os.getenv("ASK_MIN_COSINE", "0.70"))
# Giới hạn chờ hạn mức trên đường tương tác: hết thời gian thì báo lỗi rõ ràng
# thay vì để giao diện treo.
MAX_WAIT = float(os.getenv("ASK_MAX_WAIT", "20"))
# Cổng 3 nhận cả dạng "[1][2]" và dạng "[1, 2, 3]". Mô hình dùng xen kẽ hai
# dạng, nên nếu chỉ bắt một dạng thì những câu trích dẫn hợp lệ ở dạng còn lại
# sẽ bị loại oan.
_CITE_RE = re.compile(r"\[\s*(\d+(?:\s*[,;]\s*\d+)*)\s*\]")

# Cổng 2 dựa trên một chuỗi từ chối cố định thay vì câu tự do, để tầng trên nhận
# ra bằng so khớp chuỗi rồi ẩn hẳn khối trả lời và chỉ hiện danh sách bài.
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

    # `task_type` ở đây là RETRIEVAL_QUERY, khác RETRIEVAL_DOCUMENT dùng lúc
    # index. Gemini tối ưu riêng cho từng vai, nên đặt sai làm giảm rõ chất
    # lượng truy hồi dù vector vẫn cùng số chiều.
    qvec = embed_texts(gclient, [query], "RETRIEVAL_QUERY")[0]
    sidx, sval = bm25.query_vector(query)

    # Các trường định danh được lọc bằng so khớp chính xác trên payload chứ không
    # dựa vào khớp mặt chữ trong text, để không nhầm tác giả của bài với người
    # cùng tên được nhắc trong nội dung.
    must = []
    if source:
        must.append(qm.FieldCondition(key="source", match=qm.MatchValue(value=source)))
    if category:
        must.append(qm.FieldCondition(key="category", match=qm.MatchValue(value=category)))
    if author:
        must.append(qm.FieldCondition(key="author_name", match=qm.MatchValue(value=author)))
    if tag:
        # `tags` là mảng; MatchValue khớp khi bất kỳ phần tử nào bằng giá trị.
        must.append(qm.FieldCondition(key="tags", match=qm.MatchValue(value=tag)))
    if attribution:
        # Cũng là mảng. Nhờ đã tách sẵn từ "AFP, Reuters" thành hai phần tử, lọc
        # theo "Reuters" bắt được cả những bài dẫn nhiều hãng cùng lúc.
        must.append(qm.FieldCondition(key="source_attribution",
                                      match=qm.MatchValue(value=attribution)))
    if days:
        since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
        must.append(qm.FieldCondition(key="published_ts", range=qm.Range(gte=since)))

    # Truy hồi hybrid: nhánh dense bắt ngữ nghĩa, nhánh BM25 bắt khớp đúng mặt
    # chữ với tên riêng và số liệu; hai bảng xếp hạng hợp nhất bằng RRF trên
    # server.
    points = vs.hybrid_search(
        qc, qvec, sidx, sval, limit=top_k,
        query_filter=qm.Filter(must=must) if must else None,
        with_vectors=True,
    )
    # Cả hai vector đều đã chuẩn hoá L2 nên cosine bằng đúng tích vô hướng.
    # Giá trị này được gắn vào payload để cổng 1 dùng làm ngưỡng; điểm RRF không
    # thay thế được vì nó chỉ cho biết thứ hạng, không cho biết mức tương đồng.
    for p in points:
        vec = (p.vector or {}).get(vs.DENSE) if isinstance(p.vector, dict) else None
        if vec is not None and p.payload is not None:
            p.payload["_cos"] = sum(a * b for a, b in zip(qvec, vec))
    return points


def group_articles(points: list, limit: int, sort: str = SORT_RELEVANCE) -> list[dict]:
    """Gộp chunk về bài báo, tức đơn vị mà người đọc thực sự quan tâm.

    Thứ tự giữa hai bước là quan trọng: truy hồi lấy dư ở tầng chunk, rồi cắt
    `limit` ở tầng bài. Nếu cắt top-k ngay ở tầng chunk thì một bài dài bốn
    chunk có thể giữ bốn trong tám chỗ nhưng hiển thị thành một nguồn duy nhất,
    khiến yêu cầu tám bài chỉ trả về năm.

    Điểm của một bài lấy theo chunk khớp mạnh nhất, không cộng dồn các chunk:
    cộng dồn sẽ thiên vị bài dài chỉ vì nó được cắt thành nhiều phần hơn.
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
        # Lượng tử điểm thành bậc, mỗi bậc rộng SCORE_EPS. RRF thường cho ra
        # những điểm trùng khít nhau, và khi điểm bằng nhau thì thứ tự chỉ còn
        # phụ thuộc thứ tự chèn vào dict — tức tuỳ tiện. Chia bậc biến thế hoà
        # đó thành một quy tắc tường minh: cùng bậc thì bài mới đứng trước.
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
    """Dựng khối đoạn trích cho LLM.

    Số [n] trùng khớp với thứ tự thẻ bài trên giao diện, nhờ đó trích dẫn trong
    câu trả lời chỉ đúng vào bài người đọc đang nhìn thấy.
    """
    return "\n\n---\n\n".join(
        f"[{a['n']}] {a['title']}\n"
        f"Nguồn: {a['source']} | Ngày: {a['date']} | Chuyên mục: {a['category']}\n"
        + "\n".join(a["texts"])
        for a in articles
    )


def _generate(pool: KeyPool, prompt: str) -> str:
    """Sinh câu trả lời từ prompt đã dựng.

    Nhận vào `KeyPool` chứ không nhận client: client phải lấy qua `acquire()` ở
    từng lần thử, vì key được chọn có thể đổi giữa các lần.

    Hai dạng lỗi xử lý khác nhau:

      * 429 — key đó cạn hạn mức; cho nghỉ rồi `acquire()` lấy key khác.
      * 503 hoặc UNAVAILABLE — model quá tải nhất thời, quan sát được với
        `gemini-flash-latest`. Chờ luỹ tiến rồi thử lại trên cùng key, vì lỗi
        không nằm ở key.
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
    """Truy hồi rồi trả về danh sách bài đã khử trùng, không phải danh sách chunk.

    Lấy dư ở tầng chunk để sau khi gộp vẫn còn đủ `top_k` bài khác nhau. Chế độ
    sắp theo thời gian lấy dư rộng hơn đáng kể, vì bài mới nhất của kho không
    nhất thiết nằm trong nhóm khớp cao nhất.
    """
    mult = RECENT_OVERFETCH if sort == SORT_RECENT else OVERFETCH
    points = search(query, top_k=top_k * mult, **kw)
    return group_articles(points, top_k, sort=sort)


def _enforce_citations(answer: str, n_articles: int) -> str | None:
    """Cổng 3 — chỉ giữ những câu có trích dẫn [n] hợp lệ.

    Prompt chỉ là lời dặn, mô hình vẫn có thể viết câu không dẫn nguồn hoặc dẫn
    [9] khi danh sách chỉ có 5 bài. Việc cắt vì vậy làm bằng code: dòng nào
    không mang được [n] trong khoảng 1..n_articles thì bỏ. Không còn dòng nào
    nghĩa là không có câu trả lời nào đứng vững.
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

    `articles` luôn là phần chính của kết quả: kho có gì thì trả về đúng thứ đó.
    `answer` là phần phụ và chỉ tồn tại khi qua được cả ba cổng:

        1. cosine thực của bài tốt nhất đạt MIN_COSINE — kho thực sự có dữ liệu
        2. mô hình không tự khai thiếu căn cứ
        3. còn lại ít nhất một câu mang trích dẫn [n] hợp lệ
    """
    articles = search_articles(query, top_k=top_k, sort=sort, **kw)
    best_cos = max((a["cos"] for a in articles), default=0.0)
    grounded = best_cos >= MIN_COSINE
    out = {"articles": articles, "answer": None,
           "grounded": grounded, "best_cos": round(best_cos, 3)}

    # Cổng 1: kho không có gì đủ gần thì không gọi LLM. Vừa chặn bịa ở gốc, vừa
    # tiết kiệm hạn mức.
    if not articles or not generate or not grounded:
        return out

    today = datetime.now(TZ_VN).strftime("%d/%m/%Y")
    prompt = (
        f"Hôm nay là {today}.\n\nCÁC ĐOẠN TRÍCH:\n\n{_prompt_blocks(articles)}\n\n"
        f"---\nCÂU HỎI: {query}"
    )
    answer = (_generate(KeyPool(max_wait=MAX_WAIT), prompt) or "").strip()
    if NO_DATA in answer or not answer:      # cổng 2: mô hình tự khai thiếu căn cứ
        return out
    out["answer"] = _enforce_citations(answer, len(articles))   # cổng 3
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
