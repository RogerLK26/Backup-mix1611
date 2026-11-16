"""Giao diện chung cho adapter dữ liệu của từng sàn (USDT perpetual futures).

Chuẩn dữ liệu:
  - Nến 1m  : DataFrame cột ts (ms, mở nến, UTC), o, h, l, c, qv (khối lượng quote = USDT)
  - Tick     : DataFrame cột ts (ms), px  (giá khớp từng giao dịch, đã sắp xếp theo thời gian)
  - Funding  : DataFrame cột ts (ms), rate
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..utils import DAY_MS, HOUR_MS, MIN_MS, Http, log, now_ms

KLINE_COLS = ["ts", "o", "h", "l", "c", "qv"]


def empty_klines() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="int64" if c == "ts" else "float64") for c in KLINE_COLS})


def empty_ticks() -> pd.DataFrame:
    return pd.DataFrame({"ts": pd.Series(dtype="int64"), "px": pd.Series(dtype="float64")})


def norm_klines(df: pd.DataFrame, start: int | None = None, end: int | None = None) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return empty_klines()
    df = df[KLINE_COLS].copy()
    df["ts"] = df["ts"].astype("int64")
    for c in KLINE_COLS[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    df = df.dropna()
    if start is not None:
        df = df[df["ts"] >= start]
    if end is not None:
        df = df[df["ts"] < end]
    df = df.drop_duplicates("ts", keep="last").sort_values("ts").reset_index(drop=True)
    return df


def norm_ticks(df: pd.DataFrame, start: int | None = None, end: int | None = None) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return empty_ticks()
    df = df[["ts", "px"]].copy()
    df["ts"] = df["ts"].astype("int64")
    df["px"] = df["px"].astype("float64")
    if start is not None:
        df = df[df["ts"] >= start]
    if end is not None:
        df = df[df["ts"] < end]
    # sort ổn định để giữ thứ tự khớp lệnh trong cùng 1 ms
    df = df.sort_values("ts", kind="stable").reset_index(drop=True)
    return df


@dataclass
class SymbolInfo:
    symbol: str          # tên chuẩn hoá: BASEUSDT (vd PEPEUSDT, 1000PEPEUSDT)
    native: str          # id gốc của sàn (vd PEPE-USDT-SWAP, PEPE_USDT)
    base: str            # base coin theo sàn (vd 1000PEPE, PEPE)
    contract_size: float = 1.0
    status: str = ""


class Exchange:
    name = "base"
    # Lịch sử tick: None = toàn bộ lịch sử, số = chỉ N ngày gần nhất, 0 = không có
    tick_history_days: int | None = None
    # Dữ liệu dump công bố trễ bao lâu sau khi hết ngày
    tick_publish_lag_ms: int = 0
    # Chia nhỏ tick theo khối (cache theo khối)
    tick_chunk_ms: int = DAY_MS
    tick_chunk_offset_ms: int = 0
    default_fees = (0.02, 0.05)  # (maker %, taker %)
    rest_per_sec = 5.0

    def __init__(self):
        self.http = Http(self.rest_per_sec)
        self._symbols: dict[str, SymbolInfo] | None = None

    # ---- danh sách coin ----
    def list_symbols(self) -> list[SymbolInfo]:
        raise NotImplementedError

    def symbols(self) -> dict[str, SymbolInfo]:
        if self._symbols is None:
            self._symbols = {s.symbol: s for s in self.list_symbols()}
        return self._symbols

    # ---- nến ----
    def fetch_klines(self, sym: SymbolInfo, start: int, end: int) -> pd.DataFrame:
        """Nến 1m trong [start, end)."""
        raise NotImplementedError

    def fetch_daily_qvol(self, sym: SymbolInfo, start: int, end: int) -> pd.DataFrame:
        """Volume (USDT) theo ngày UTC: cột ts, qv."""
        raise NotImplementedError

    # ---- tick ----
    def tick_chunk_start(self, ts: int) -> int:
        off = self.tick_chunk_offset_ms
        return (ts - off) - (ts - off) % self.tick_chunk_ms + off

    def tick_available(self, chunk_start: int) -> bool:
        if self.tick_history_days == 0:
            return False
        now = now_ms()
        if chunk_start + self.tick_chunk_ms + self.tick_publish_lag_ms > now:
            return False
        if self.tick_history_days is not None and chunk_start < now - self.tick_history_days * DAY_MS + HOUR_MS:
            return False
        return True

    def fetch_ticks_chunk(self, sym: SymbolInfo, chunk_start: int) -> pd.DataFrame | None:
        """Toàn bộ giao dịch trong khối [chunk_start, chunk_start + tick_chunk_ms). None = không lấy được."""
        raise NotImplementedError

    # ---- đòn bẩy tối đa (mức hiện tại; sàn không công bố lịch sử) ----
    def fetch_max_leverage(self) -> dict[str, tuple[float, int | None]]:
        """{symbol: (đòn bẩy tối đa ở bậc vị thế nhỏ nhất, thời điểm sàn cập nhật mức này (ms) hoặc None)}."""
        return {}

    # ---- funding ----
    def fetch_funding(self, sym: SymbolInfo, start: int, end: int) -> pd.DataFrame:
        return pd.DataFrame({"ts": pd.Series(dtype="int64"), "rate": pd.Series(dtype="float64")})

    # ---- tiện ích phân trang tick REST theo chia đôi khoảng thời gian ----
    def _bisect_trades(self, fetch_window, start: int, end: int, limit: int, min_span: int) -> list:
        """fetch_window(a, b) trả về list (ts, px, id) trong [a, b). Nếu đầy `limit` thì chia đôi."""
        out = []
        stack = [(start, end)]
        while stack:
            a, b = stack.pop()
            rows = fetch_window(a, b)
            if len(rows) >= limit and b - a > min_span:
                mid = a + (b - a) // 2
                stack.append((mid, b))
                stack.append((a, mid))
                continue
            if len(rows) >= limit:
                log.warning("%s: >%d giao dịch trong %d ms, có thể thiếu tick", self.name, limit, b - a)
            out.extend(rows)
        return out


def klines_from_rows(rows, idx_ts=0, idx_o=1, idx_h=2, idx_l=3, idx_c=4, idx_qv=6) -> pd.DataFrame:
    if not rows:
        return empty_klines()
    a = np.asarray(rows, dtype=object)
    return pd.DataFrame({
        "ts": a[:, idx_ts].astype("int64"),
        "o": a[:, idx_o].astype("float64"),
        "h": a[:, idx_h].astype("float64"),
        "l": a[:, idx_l].astype("float64"),
        "c": a[:, idx_c].astype("float64"),
        "qv": a[:, idx_qv].astype("float64"),
    })


def strip_multiplier(base: str) -> tuple[str, float]:
    """1000PEPE -> (PEPE, 1000); SHIB1000 -> (SHIB, 1000); 1MBABYDOGE -> (BABYDOGE, 1e6)."""
    import re
    b = base.upper()
    m = re.match(r"^(1000000|100000|10000|1000|100)([A-Z].*)$", b)
    if m:
        return m.group(2), float(m.group(1))
    m = re.match(r"^1M([A-Z].*)$", b)
    if m:
        return m.group(1), 1e6
    m = re.match(r"^([A-Z].*?)(1000000|10000|1000)$", b)
    if m:
        return m.group(1), float(m.group(2))
    return b, 1.0


MINUTES_PER_DAY = DAY_MS // MIN_MS
