"""浏览器代操作层出口。"""

from .manager import BrowserError, BrowserHandle, BrowserManager
from .session import BrowserSession
from .snapshot import compact_snapshot, format_snapshot

__all__ = [
    "BrowserManager",
    "BrowserHandle",
    "BrowserSession",
    "BrowserError",
    "format_snapshot",
    "compact_snapshot",
]
