"""Giao diện tìm tin viết bằng Streamlit, trình bày theo thẻ bài báo.

Giao diện cố ý không phải khung hội thoại. Phần chính luôn là danh sách bài thực
có trong kho — ảnh, tiêu đề, sapo, tác giả, link — còn đoạn tóm tắt do LLM sinh
ra chỉ là phần phụ và bị ẩn hoàn toàn khi mô hình không đủ căn cứ. Với dữ liệu
tin tức, không có tóm tắt là trạng thái tốt hơn một tóm tắt không kiểm chứng
được.

Module không chứa logic truy hồi nào: nó gọi thẳng `newscrawler.ask`, nhờ đó
giao diện và CLI luôn trả về cùng kết quả cho cùng câu hỏi. File nằm ngoài
package vì `streamlit run` nạp nó như `__main__`.

    docker compose --profile chat up -d chatbot     # http://localhost:8501
"""
from __future__ import annotations

import streamlit as st

from newscrawler.ask import (MIN_COSINE, SCORE_EPS, SORT_RECENT,
                             SORT_RELEVANCE, TOP_K, ask)
from newscrawler.embed import BRONZE_IDENT, _catalog

st.set_page_config(page_title="Tìm tin", page_icon="📰", layout="wide")

st.markdown("""<style>
.card-desc { color:#666; font-size:0.9rem; line-height:1.45; margin:.25rem 0 .35rem; }
.card-meta { color:#888; font-size:0.8rem; }
.card-title a { text-decoration:none; }
</style>""", unsafe_allow_html=True)


@st.cache_data(ttl=600, show_spinner=False)
def vocabulary() -> tuple[list[str], list[str]]:
    """Danh sách nguồn và chuyên mục thực có trong kho.

    Dropdown dựng từ dữ liệu thật nên không bao giờ đưa ra một lựa chọn chắc
    chắn trả về 0 kết quả. Chỉ đọc hai cột nên chi phí không đáng kể.
    """
    try:
        tb = (_catalog().load_table(BRONZE_IDENT)
              .scan(selected_fields=("source", "category")).to_arrow())
        return (sorted({s for s in tb.column("source").to_pylist() if s}),
                sorted({c for c in tb.column("category").to_pylist() if c}))
    except Exception:                      # Bronze chưa tồn tại hoặc chưa kết nối được
        return [], []


def render_card(a: dict) -> None:
    img, txt = st.columns([1, 4], vertical_alignment="top")
    with img:
        if a["thumbnail_url"]:
            # Phiên bản Streamlit ghim trong requirements là 1.39, nơi tham số
            # còn tên `use_column_width`; `use_container_width` chỉ xuất hiện từ
            # 1.40 trở đi.
            st.image(a["thumbnail_url"], use_column_width=True)
        else:
            st.caption("(không có ảnh)")
    with txt:
        st.markdown(
            f"<div class='card-title'><b>[{a['n']}] "
            f"<a href='{a['url']}' target='_blank'>{a['title'] or '(không có tiêu đề)'}</a></b></div>",
            unsafe_allow_html=True,
        )
        if a["description"]:
            st.markdown(f"<div class='card-desc'>{a['description']}</div>",
                        unsafe_allow_html=True)
        # Ưu tiên hiện tên người viết; không có thì hiện hãng tin được dẫn lại.
        who = a["author_name"] or ", ".join(a["source_attribution"])
        meta = [f"**{a['source']}**", a["date"] or "—", a["category"] or "—"]
        if who:
            meta.append(f"✍ {who}")
        if a["source_attribution"] and a["author_name"]:
            meta.append("theo " + ", ".join(a["source_attribution"]))
        st.markdown(f"<div class='card-meta'>{' · '.join(meta)} · "
                    f"khớp {a['score']} · giống {a['cos']} · "
                    f"chunk {', '.join(a['chunks'])}</div>",
                    unsafe_allow_html=True)
        st.caption(a["url"])
    st.divider()


# --------------------------------------------------------------------------
srcs, cats = vocabulary()
with st.sidebar:
    st.header("Bộ lọc")
    st.caption("Lọc CỨNG: bài không khớp bị loại **trước khi** chấm điểm.")
    f_source = st.selectbox("Báo", ["(tất cả)"] + srcs)
    f_cat = st.selectbox("Chuyên mục", ["(tất cả)"] + cats)
    f_days = st.slider("Trong vòng N ngày", 0, 60, 0, help="0 = không giới hạn")
    f_author = st.text_input("Tác giả", placeholder="khớp CHÍNH XÁC, có dấu")
    f_tag = st.text_input("Tag")
    f_attr = st.text_input("Dẫn lại từ", placeholder="Reuters, AFP, ...")
    st.divider()
    top_k = st.number_input("Số bài hiển thị", min_value=1, max_value=50,
                            value=TOP_K, step=1)
    sort_label = st.radio(
        "Sắp xếp",
        ["Liên quan nhất", "Mới nhất trước"],
        help=f"'Liên quan nhất' vẫn ưu tiên bài mới: hai bài chênh điểm dưới "
             f"{SCORE_EPS} coi như ngang nhau, khi đó bài mới đứng trên.",
    )
    sort = SORT_RECENT if sort_label == "Mới nhất trước" else SORT_RELEVANCE

    st.divider()
    want_summary = st.toggle(
        "Kèm tóm tắt của AI", value=False,
        help="Tắt = chỉ liệt kê bài, không gọi model. Bật cũng chỉ hiện tóm tắt "
             "khi model bám được vào bài cụ thể.",
    )

kw = dict(
    top_k=int(top_k),
    sort=sort,
    source=None if f_source == "(tất cả)" else f_source,
    category=None if f_cat == "(tất cả)" else f_cat,
    days=f_days or None,
    author=f_author.strip() or None,
    tag=f_tag.strip() or None,
    attribution=f_attr.strip() or None,
)
dang_loc = {k: v for k, v in kw.items() if v and k not in ("top_k", "sort")}

st.title("📰 Tìm tin")
st.caption("Truy hồi trên kho tin đã crawl · hybrid dense + BM25, hợp nhất bằng RRF")

q = st.text_input("Bạn muốn tìm gì?", placeholder="ví dụ: giá vàng tuần này",
                  label_visibility="collapsed")

if q:
    if dang_loc:
        st.caption("đang lọc: " + " · ".join(f"`{k}={v}`" for k, v in dang_loc.items()))
    try:
        with st.spinner("Đang tìm…"):
            out = ask(q, generate=want_summary, **kw)
    # `embed_texts` và `_generate` ném SystemExit khi mọi key đã cạn hạn mức.
    # Không bắt thì Streamlit dừng cả script và người dùng chỉ thấy trang trắng
    # thay vì một thông báo giải thích được.
    except SystemExit as e:
        st.error(f"Hết hạn ngạch Gemini — thử lại sau khi quota reset.\n\n```\n{e}\n```")
        st.stop()
    except Exception as e:  # noqa: BLE001
        st.error(f"Lỗi: `{type(e).__name__}: {e}`")
        st.stop()

    articles = out["articles"]
    if not articles:
        st.warning("Không có bài nào khớp." +
                   (" Thử nới bộ lọc bên trái." if dang_loc else ""))
        st.stop()

    if not out["grounded"]:
        st.warning(
            f"Kho tin **không có dữ liệu** đủ gần với câu hỏi này "
            f"(độ giống cao nhất {out['best_cos']} < ngưỡng {MIN_COSINE}). "
            "Các bài dưới đây chỉ là những bài **gần nhất tìm được** — "
            "không có tóm tắt, để tránh suy đoán."
        )
    elif out["answer"]:
        st.info(out["answer"])
    elif want_summary:
        st.caption("⚠️ Có bài liên quan nhưng không đủ căn cứ trả lời thẳng "
                   "câu hỏi — chỉ hiển thị bài, không suy đoán.")

    xep = "mới nhất trước" if sort == SORT_RECENT else "liên quan nhất, ngang điểm thì bài mới trước"
    st.subheader(f"{len(articles)} bài · {xep}")
    for a in articles:
        render_card(a)
