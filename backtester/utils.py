"""Tiện ích dùng chung: thời gian, HTTP có retry + rate limit."""
from __future__ import annotations

import io
import logging
import os
import random
import re
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("bt")

# Thư mục lưu dữ liệu (cache, kết quả, cấu hình người dùng). Trên Railway đặt DATA_DIR = đường dẫn volume;
# không đặt thì tự dùng volume đang gắn vào service (Railway cung cấp RAILWAY_VOLUME_MOUNT_PATH).
DATA_DIR = Path(os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or ".")


def data_path(p) -> Path:
    p = Path(p)
    return p if p.is_absolute() else DATA_DIR / p

SEC_MS = 1_000
MIN_MS = 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000

_DUR_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d|w)?\s*$", re.I)
_DUR_UNIT = {"ms": 1, "s": SEC_MS, "m": MIN_MS, "h": HOUR_MS, "d": DAY_MS, "w": 7 * DAY_MS}


def parse_duration(v) -> int | None:
    """'30s', '15m', '2h', '1d', 90 (phút) -> mili giây. None/0/'' -> None."""
    if v is None or v == "" or v is False:
        return None
    if isinstance(v, (int, float)):
        return int(v * MIN_MS) if v > 0 else None
    m = _DUR_RE.match(str(v))
    if not m:
        raise ValueError(f"Không hiểu khoảng thời gian: {v!r} (ví dụ hợp lệ: 30s, 15m, 2h, 1d)")
    val = float(m.group(1)) * _DUR_UNIT[(m.group(2) or "m").lower()]
    return int(val) if val > 0 else None


def parse_time(s, tz: str) -> int:
    """'2026-09-01' hoặc '2026-09-01 10:30' theo múi giờ tz -> epoch ms UTC."""
    if isinstance(s, (int, float)):
        return int(s)
    if isinstance(s, datetime):
        dt = s
    else:
        s = str(s).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"Không hiểu thời gian: {s!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return int(dt.timestamp() * 1000)


def fmt_ms(ms: int | None, tz: str = "UTC", with_ms: bool = False) -> str:
    if ms is None:
        return ""
    dt = datetime.fromtimestamp(ms / 1000, tz=ZoneInfo(tz))
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if with_ms else dt.strftime("%Y-%m-%d %H:%M:%S")


def day_floor(ms: int) -> int:
    return ms - ms % DAY_MS


def utc_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def date_to_ms(d: str) -> int:
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def days_between(start_ms: int, end_ms: int) -> list[int]:
    """Danh sách mốc đầu ngày UTC phủ [start, end)."""
    d = day_floor(start_ms)
    out = []
    while d < end_ms:
        out.append(d)
        d += DAY_MS
    return out


def now_ms() -> int:
    return int(time.time() * 1000)


class RateLimiter:
    """Giới hạn số request/giây (token bucket đơn giản, thread-safe)."""

    def __init__(self, per_sec: float):
        self.interval = 1.0 / per_sec
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            t = max(now, self._next)
            self._next = t + self.interval
        delay = t - now
        if delay > 0:
            time.sleep(delay)


class Http:
    def __init__(self, per_sec: float = 5.0, timeout: float = 30.0, headers: dict | None = None):
        self.rl = RateLimiter(per_sec)
        self.timeout = timeout
        self._local = threading.local()
        self.headers = {"User-Agent": "backtester/1.0", **(headers or {})}

    @property
    def s(self) -> requests.Session:
        if not hasattr(self._local, "s"):
            self._local.s = requests.Session()
            self._local.s.headers.update(self.headers)
        return self._local.s

    def _get(self, url, params=None, stream=False, allow_404=False, retries=6):
        last = None
        for attempt in range(retries):
            self.rl.wait()
            try:
                r = self.s.get(url, params=params, timeout=self.timeout, stream=stream)
            except requests.RequestException as e:
                last = e
                time.sleep(min(30, 1.5 * 2 ** attempt) + random.random())
                continue
            if r.status_code == 451 or (r.status_code == 403 and "country" in r.text.lower()):
                raise RuntimeError(
                    f"Sàn chặn truy cập từ khu vực/IP của máy chủ (HTTP {r.status_code}, {url.split('/')[2]}). "
                    "Nếu chạy trên Railway/VPS: chọn region không bị chặn (vd Singapore/Tokyo) hoặc chạy trên máy cá nhân.")
            if r.status_code in (404, 403) and allow_404:
                return None
            if r.status_code in (418, 429) or r.status_code >= 500:
                wait = float(r.headers.get("Retry-After", 0) or 0) or min(60, 2 * 2 ** attempt)
                log.debug("HTTP %s %s -> chờ %.1fs", r.status_code, url, wait)
                last = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                time.sleep(wait + random.random())
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code} {url} {params}: {r.text[:300]}")
            return r
        raise RuntimeError(f"Request thất bại sau {retries} lần: {url} {params}: {last}")

    def json(self, url, params=None, allow_404=False):
        r = self._get(url, params=params, allow_404=allow_404)
        return None if r is None else r.json()

    def content(self, url, allow_404=True) -> bytes | None:
        r = self._get(url, allow_404=allow_404)
        return None if r is None else r.content


def unzip_single(data: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        return z.read(names[0])


def setup_logging(verbose: bool = False):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def local_day_key(ms: int, tz: str, reset_hour: int = 0) -> str:
    """Ngày giao dịch theo múi giờ (dùng cho bộ đếm theo ngày, có giờ reset tùy chỉnh)."""
    dt = datetime.fromtimestamp(ms / 1000, tz=ZoneInfo(tz)) - timedelta(hours=reset_hour)
    return dt.strftime("%Y-%m-%d")


def mem_usage() -> int | None:
    """RAM (RSS) của tiến trình, byte – Linux (Railway) đọc /proc, Windows dùng psapi; không đọc được thì None."""
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), wintypes.DWORD]
        if k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return int(pmc.WorkingSetSize)
    except Exception:  # noqa: BLE001
        pass
    return None


def release_memory() -> tuple[int | None, int | None]:
    """Trả RAM không dùng nữa về hệ điều hành (sau mỗi lượt chạy hoặc khi bấm nút). Trả về (trước, sau)."""
    import gc
    before = mem_usage()
    gc.collect()
    try:
        import pyarrow as pa
        pa.default_memory_pool().release_unused()
    except Exception:  # noqa: BLE001
        pass
    try:  # glibc giữ lại vùng nhớ đã free -> trả lại cho hệ điều hành (Linux)
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # noqa: BLE001
        pass
    return before, mem_usage()
