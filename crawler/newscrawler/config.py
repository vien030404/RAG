"""Cấu hình chung cho crawler đa nguồn.

Danh mục (categories) của từng báo nằm trong lớp source tương ứng ở
`newscrawler/sources/`. File này chỉ giữ thiết lập dùng chung.
"""

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

REQUEST_DELAY = 1.0    # giây giãn cách giữa hai request tới cùng một nguồn
REQUEST_TIMEOUT = 15   # giây
MAX_RETRIES = 3

# Phạm vi crawl
#
# Trần số trang listing duyệt cho mỗi chuyên mục. `crawl_category` dừng sớm khi
# một trang không còn bài nào thuộc `target_date` (so theo ngày mới nhất trên
# trang), nên trần đặt cao chỉ để phủ hết những ngày nhiều tin.
MAX_PAGES = 20

# Ngưỡng loại trang không phải bài viết: trang video và trang chuyên mục parse
# ra nội dung rỗng, trong khi bài thật luôn dài hơn vài trăm ký tự.
MIN_CONTENT_LEN = 100
