"""Bể API key Gemini với hạn mức token theo phút.

Hạn mức Gemini tính trên từng project, không trên từng key: nhiều key thuộc cùng
một project chia nhau một hạn mức, nên xoay vòng giữa chúng không tăng thông
lượng. Chỉ cộng dồn được khi mỗi key thuộc một project khác nhau.

Cấu hình (.env):
    GEMINI_API_KEYS=key1,key2,key3      # ưu tiên; phân tách bằng dấu phẩy
    GEMINI_API_KEY=key1                 # dùng khi biến trên để trống

Đặt chỗ theo ngân sách token
----------------------------
Ràng buộc thực tế của free tier là token mỗi phút (TPM), không phải số lần gọi.
Mỗi key vì vậy giữ một cửa sổ trượt 60 giây ghi lại lượng token đã tiêu;
`acquire(tokens)` chỉ trả về key còn đủ chỗ cho lô sắp gửi và chờ nếu chưa key
nào đủ. Cách này giữ cho đường đi bình thường không sinh 429, để `penalize()`
còn lại đúng vai trò lưới an toàn khi ước lượng token thấp hơn bộ đếm của Gemini.

Hai trần tách biệt
------------------
TPM, đo trên tập dữ liệu của dự án: một lô 50 chunk (khoảng 26.000 token) qua
được, nhưng lô thứ hai trên cùng key trong cùng phút bị từ chối — trần nằm quanh
30.000 token/phút. Ngân sách bên dưới xử lý trần này.

RPD, `EmbedContentRequestsPerDayPerUserPerProjectPerModel-FreeTier`: 1.000 mỗi
ngày mỗi project (model gemini-embedding-1.0). Bộ đếm tăng theo từng phần tử
`contents` chứ không theo lần gọi HTTP — 8.830 chunk chia cho 8 project (khoảng
1.104 mỗi project) vượt trần, trong khi tổng số lần gọi HTTP chỉ khoảng 500
(khoảng 62 mỗi project). Gộp lô do đó không nới được trần ngày; sức chứa thực là

    số project × 1.000 chunk/ngày

Khi hết trần ngày, nhịp gửi không còn ý nghĩa: chỉ còn cách đợi reset, thêm
project, hoặc chuyển sang gói trả phí.
"""
from __future__ import annotations

import os
import time
from collections import deque

from google import genai

COOLDOWN = int(os.getenv("GEMINI_KEY_COOLDOWN", "60"))

WINDOW = 60.0
# Google không công bố TPM cho từng model; giá trị dưới đây suy ra từ đo đạc nên
# để cấu hình được qua .env thay vì đóng cứng.
TPM_LIMIT = int(os.getenv("GEMINI_TPM_LIMIT", "30000"))
# Chỉ tiêu một phần trần. `est_tokens` là ước lượng (ký tự / 3) nên có thể thấp
# hơn bộ đếm của Gemini; phần dư là biên an toàn.
TPM_SAFETY = float(os.getenv("GEMINI_TPM_SAFETY", "0.8"))
RPM_LIMIT = int(os.getenv("GEMINI_RPM_LIMIT", "100"))

_BUDGET = TPM_LIMIT * TPM_SAFETY

# Nhận diện lỗi hết hạn mức / rate-limit. google-genai không phơi mã lỗi riêng
# cho trường hợp này, nên phải đối chiếu thông điệp.
_QUOTA_MARKS = ("429", "RESOURCE_EXHAUSTED", "quota")


def is_quota_error(e: Exception) -> bool:
    msg = str(e)
    return any(m in msg for m in _QUOTA_MARKS)


def _read_keys() -> list[str]:
    raw = os.getenv("GEMINI_API_KEYS", "").strip() or os.getenv("GEMINI_API_KEY", "").strip()
    seen, out = set(), []
    for k in (x.strip() for x in raw.split(",")):
        if k and k not in seen:      # key trùng không cộng thêm hạn mức nào
            seen.add(k)
            out.append(k)
    return out


class KeyPool:
    """Vòng xoay client Gemini. Log chỉ in số thứ tự key, không in giá trị key."""

    def __init__(self, keys: list[str] | None = None, max_wait: float | None = None):
        # `max_wait`: số giây tối đa chờ một key rảnh trước khi bỏ cuộc.
        # None — chờ không giới hạn, phù hợp với job embed chạy nền.
        # Đường tương tác (giao diện, CLI hỏi đáp) nên đặt giá trị nhỏ để báo lỗi
        # sớm thay vì giữ người dùng trước spinner.
        self.max_wait = max_wait
        self.keys = _read_keys() if keys is None else keys
        if not self.keys:
            raise SystemExit("Thiếu GEMINI_API_KEYS / GEMINI_API_KEY (đặt trong .env).")
        self.clients = [genai.Client(api_key=k) for k in self.keys]
        self.ready_at = [0.0] * len(self.keys)   # thời điểm key hết nghỉ sau 429
        # Cửa sổ trượt 60s: (thời điểm gửi, số token) của từng request theo key.
        self.window: list[deque[tuple[float, int]]] = [deque() for _ in self.keys]
        self.i = -1
        print(f"[gemini] {len(self.keys)} key trong bể · ngân sách "
              f"{int(_BUDGET):,} token/phút/key (trần {TPM_LIMIT:,})")

    def __len__(self) -> int:
        return len(self.keys)

    def _prune(self, j: int, now: float) -> int:
        """Bỏ các request đã rơi khỏi cửa sổ 60s, trả về token còn tính."""
        w = self.window[j]
        while w and w[0][0] <= now - WINDOW:
            w.popleft()
        return sum(t for _, t in w)

    def _free_at(self, j: int, now: float, tokens: int) -> float:
        """Thời điểm key j giải phóng đủ chỗ cho lô `tokens`."""
        t0 = max(self.ready_at[j], now)
        used = sum(t for _, t in self.window[j])
        if _BUDGET - used >= tokens and len(self.window[j]) < RPM_LIMIT:
            return t0
        for ts, tok in self.window[j]:          # mục cũ nhất rơi ra trước
            used -= tok
            if _BUDGET - used >= tokens:
                return max(t0, ts + WINDOW)
        return max(t0, now + WINDOW)

    def acquire(self, tokens: int = 0) -> tuple[int, genai.Client]:
        """Trả về key còn đủ ngân sách cho lô `tokens`, chờ nếu chưa key nào đủ.

        Token được ghi vào cửa sổ của key ngay lúc cấp phát, nên hai lô liên
        tiếp không thể cùng vượt trần trong một phút. Vòng quét bắt đầu từ key
        kế tiếp để tải trải đều thay vì dồn vào key đầu danh sách.
        """
        n = len(self.keys)
        waited = 0.0
        if tokens > _BUDGET:
            print(f"  [gemini] ⚠ lô {tokens:,} token vượt ngân sách "
                  f"{int(_BUDGET):,}/phút — giảm EMBED_BATCH để tránh 429")
            tokens = int(_BUDGET)
        while True:
            now = time.time()
            best_j, best_free = None, -1.0
            for step in range(n):
                j = (self.i + 1 + step) % n
                if self.ready_at[j] > now:
                    continue
                used = self._prune(j, now)
                if len(self.window[j]) >= RPM_LIMIT:
                    continue
                free = _BUDGET - used
                if free >= tokens and free > best_free:
                    best_j, best_free = j, free
            if best_j is not None:
                self.i = best_j
                self.window[best_j].append((now, tokens))
                return best_j, self.clients[best_j]

            soonest = min(self._free_at(j, now, tokens) for j in range(n))
            wait = max(1.0, min(WINDOW, soonest - now))
            waited += wait
            if self.max_wait is not None and waited > self.max_wait:
                raise SystemExit(
                    f"Cả {n} key đều hết hạn ngạch, phải chờ hơn "
                    f"{self.max_wait:.0f}s. Thử lại sau khi quota reset."
                )
            print(f"  [gemini] {n} key đều đầy tải, chờ {wait:.0f}s")
            time.sleep(wait)

    # Lối gọi rút gọn cho nơi chỉ cần một client, không ràng buộc ngân sách.
    def current(self) -> tuple[int, genai.Client]:
        return self.acquire()

    def penalize(self, seconds: float = COOLDOWN) -> None:
        """Cho key hiện tại nghỉ sau khi dính 429; `acquire` chọn key kế tiếp."""
        self.ready_at[self.i] = time.time() + seconds
        print(f"  [gemini] key #{self.i + 1} hết quota, nghỉ {seconds:.0f}s")
