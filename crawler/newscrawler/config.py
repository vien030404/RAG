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

REQUEST_DELAY = 1.0    # giây nghỉ giữa các request (lịch sự với server)
REQUEST_TIMEOUT = 15   # giây
MAX_RETRIES = 3

# Phạm vi crawl
MAX_PAGES = 20   # TRẦN số trang listing / category. crawl_category tự dừng sớm
                 # khi 1 trang không còn bài nào của target_date (xét ngày MAX
                 # trên trang) — trần cao chỉ để phủ hết ngày tin nhiều.
                 # (0 hoặc None = không cào; >=1)
# Không giới hạn số bài mỗi category (lấy hết bài trên mỗi trang listing).

# Bỏ bài không có nội dung text (video / trang không phải bài viết).
# Bài thật luôn > vài trăm ký tự; video parse ra content rỗng.
MIN_CONTENT_LEN = 100
