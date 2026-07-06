BASE_URL = "https://vnexpress.net"

CATEGORIES = {
    "Thời sự":              f"{BASE_URL}/thoi-su",
    "Thế giới":             f"{BASE_URL}/the-gioi",
    "Kinh doanh":           f"{BASE_URL}/kinh-doanh",
    "Khoa học công nghệ":   f"{BASE_URL}/khoa-hoc",
    "Bất động sản":         f"{BASE_URL}/bat-dong-san",
    "Sức khỏe":             f"{BASE_URL}/suc-khoe",
    "Giải trí":             f"{BASE_URL}/giai-tri",
    "Thể thao":             f"{BASE_URL}/the-thao",
    "Pháp luật":            f"{BASE_URL}/phap-luat",
    "Giáo dục":             f"{BASE_URL}/giao-duc",
    "Đời sống":             f"{BASE_URL}/doi-song",
    "Xe":                   f"{BASE_URL}/oto-xe-may",
    "Du lịch":              f"{BASE_URL}/du-lich",
    "Tâm sự":               f"{BASE_URL}/tam-su",
    "Thư giãn":             f"{BASE_URL}/thu-gian",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

REQUEST_DELAY = 1.5   # seconds between requests
REQUEST_TIMEOUT = 15  # seconds
MAX_RETRIES = 3
MAX_PAGES = 10        # giới hạn an toàn số trang mỗi category
