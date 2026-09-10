"""Bể API key Gemini — xoay vòng khi một key hết quota.

CẢNH BÁO: quota Gemini tính theo PROJECT chứ không theo key. Nhiều key cùng một
project sẽ dùng chung hạn mức và xoay vòng hoàn toàn vô ích. Chỉ cộng dồn được
khi các key thuộc project / tài khoản khác nhau.

Cấu hình (.env):
    GEMINI_API_KEYS=key1,key2,key3      # ưu tiên; phân tách bằng dấu phẩy
    GEMINI_API_KEY=key1                 # tương thích ngược, dùng khi trên trống

Cơ chế: NGÂN SÁCH TOKEN THEO PHÚT, không phải "cứ gửi rồi dính 429 mới đổi key".
Ràng buộc thật của free tier là token/phút (TPM), nên mỗi key giữ một cửa sổ
trượt 60 giây ghi lại đã tiêu bao nhiêu token; `acquire(tokens)` chỉ trả về key
còn đủ chỗ cho lô sắp gửi, không có key nào đủ thì CHỜ tới lúc cửa sổ nhả ra.
Nhờ vậy đường đi bình thường không còn sinh 429 nào. `penalize()` chỉ còn là
lưới an toàn cho trường hợp ước lượng token hụt so với cách Gemini đếm.

Đo trên chính dữ liệu này: 1 lô 50 chunk ≈ 26.000 token qua được, nhưng lô thứ
hai cùng key trong cùng phút thì 429 -> trần rơi vào khoảng 30.000 token/phút.

CÓ HAI TRẦN, và cái thứ hai KHÔNG lách được bằng nhịp gửi:

    quotaId : EmbedContentRequestsPerDayPerUserPerProjectPerModel-FreeTier
    limit   : 1000 / ngày / project   (model gemini-embedding-1.0)

Bộ đếm này tăng theo TỪNG `contents` item, không phải theo mỗi lần gọi HTTP —
suy ra từ số đo: 8.830 chunk chia đều 8 project ≈ 1.104/project, vừa vượt 1.000,
trong khi tổng số lần gọi HTTP chỉ ~500 (≈62/project, còn xa 1.000). Nghĩa là
gộp lô KHÔNG kéo dài được ngân sách ngày; sức chứa thật là

    số project × 1000 chunk/ngày

Ngân sách token bên dưới chỉ giải quyết trần TPM (theo phút). Hết trần ngày thì
phải đợi reset, không có cách nào khác ngoài tăng số project hoặc trả phí.
"""
from __future__ import annotations

import os
import time
from collections import deque

from google import genai

COOLDOWN = int(os.getenv("GEMINI_KEY_COOLDOWN", "60"))

WINDOW = 60.0
# Trần TPM SUY RA TỪ QUAN SÁT (Google không công bố con số cho từng model), nên
# để chỉnh được từ .env thay vì đóng cứng.
TPM_LIMIT = int(os.getenv("GEMINI_TPM_LIMIT", "30000"))
# Chỉ tiêu một phần trần: `est_tokens` là ước lượng (ký tự / 3) nên có thể hụt so
# với bộ đếm thật của Gemini; phần dư này chính là biên an toàn.
TPM_SAFETY = float(os.getenv("GEMINI_TPM_SAFETY", "0.8"))
RPM_LIMIT = int(os.getenv("GEMINI_RPM_LIMIT", "100"))

_BUDGET = TPM_LIMIT * TPM_SAFETY

# Chuỗi nhận diện lỗi hết quota / rate-limit (google-genai không phơi mã lỗi
# riêng cho trường hợp này nên phải soi thông điệp).
_QUOTA_MARKS = ("429", "RESOURCE_EXHAUSTED", "quota")


def is_quota_error(e: Exception) -> bool:
    msg = str(e)
    return any(m in msg for m in _QUOTA_MARKS)


def _read_keys() -> list[str]:
    raw = os.getenv("GEMINI_API_KEYS", "").strip() or os.getenv("GEMINI_API_KEY", "").strip()
    seen, out = set(), []
    for k in (x.strip() for x in raw.split(",")):
        if k and k not in seen:      # bỏ key lặp -> tránh ảo tưởng có nhiều quota
            seen.add(k)
            out.append(k)
    return out


class KeyPool:
    """Vòng xoay client Gemini. Không bao giờ in key ra log, chỉ in số thứ tự."""

    def __init__(self, keys: list[str] | None = None, max_wait: float | None = None):
        # `max_wait`: chờ tối đa bao nhiêu giây cho một key rảnh trước khi BỎ CUỘC.
        # None = chờ bao lâu cũng được (job embed chạy nền, chờ là đúng).
        # Đường TƯƠNG TÁC (giao diện, CLI hỏi đáp) phải đặt số nhỏ: thà báo lỗi
        # ngay còn hơn để người dùng nhìn spinner quay vài phút.
        self.max_wait = max_wait
        self.keys = _read_keys() if keys is None else keys
        if not self.keys:
            raise SystemExit("Thiếu GEMINI_API_KEYS / GEMINI_API_KEY (đặt trong .env).")
        self.clients = [genai.Client(api_key=k) for k in self.keys]
        self.ready_at = [0.0] * len(self.keys)   # thời điểm key hết "nghỉ" (sau 429)
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
        """Trả về key CÒN ĐỦ NGÂN SÁCH cho lô `tokens`, chờ nếu chưa có.

        Đặt chỗ trước thay vì gửi bừa: mỗi lượt gọi ghi luôn `tokens` vào cửa sổ
        của key được chọn, nên hai lô liên tiếp không bao giờ dồn quá trần trong
        cùng một phút. Xoay vòng từ key kế tiếp để tải trải đều, không dồn hết
        vào key đầu rồi mới nhảy sang key sau.
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

    # Giữ tên cũ cho chỗ nào chỉ cần một key, không quan tâm ngân sách.
    def current(self) -> tuple[int, genai.Client]:
        return self.acquire()

    def penalize(self, seconds: float = COOLDOWN) -> None:
        """Key hiện tại dính 429 -> cho nghỉ. Việc chọn key kế do `acquire` lo."""
        self.ready_at[self.i] = time.time() + seconds
        print(f"  [gemini] key #{self.i + 1} hết quota, nghỉ {seconds:.0f}s")
