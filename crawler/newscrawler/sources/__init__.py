"""Registry các nguồn báo. Thêm báo mới = thêm 1 file + 1 dòng vào đây."""
from .afcbc import ThanhNienSource, TuoiTreSource
from .base import BaseSource
from .dantri import DanTriSource
from .vietnamnet import VietnamNetSource
from .vnexpress import VnExpressSource

SOURCES: dict[str, BaseSource] = {
    s.name: s
    for s in [
        VnExpressSource(),
        VietnamNetSource(),
        TuoiTreSource,     # đã là instance (AfcbcSource)
        ThanhNienSource,   # đã là instance (AfcbcSource)
        DanTriSource(),
    ]
}

__all__ = ["SOURCES", "BaseSource"]
