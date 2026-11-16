"""Gate.io USDT futures.

- Nến 1m: dump theo tháng (download.gatedata.org). Tháng hiện tại chưa có dump -> REST,
  nhưng REST chỉ trả 10.000 nến gần nhất (~6,9 ngày).
- Tick: REST /futures/usdt/trades, tối đa 180 ngày gần nhất.
"""
from __future__ import annotations

import gzip
import io
from datetime import datetime, timezone

import pandas as pd

from ..utils import DAY_MS, HOUR_MS, MIN_MS, Http, log, now_ms
from .base import Exchange, SymbolInfo, empty_klines, norm_klines, norm_ticks

API = "https://api.gateio.ws/api/v4"
DUMP = "https://download.gatedata.org/futures_usdt"


class Gate(Exchange):
    name = "gate"
    tick_history_days = 179
    tick_chunk_ms = HOUR_MS
    default_fees = (0.02, 0.05)
    rest_per_sec = 8.0

    def __init__(self):
        super().__init__()
        self.dump = Http(5.0, timeout=180)
        self._warned = False

    def list_symbols(self):
        out = []
        for s in self.http.json(f"{API}/futures/usdt/contracts"):
            base = s["name"].split("_")[0]
            out.append(SymbolInfo(f"{base}USDT", s["name"], base, float(s.get("quanto_multiplier") or 1),
                                  s.get("status", "")))
        return out

    def _dump_month(self, sym: SymbolInfo, ym: str) -> pd.DataFrame | None:
        raw = self.dump.content(f"{DUMP}/candlesticks_1m/{ym}/{sym.native}-{ym}.csv.gz")
        if raw is None:
            return None
        df = pd.read_csv(io.BytesIO(gzip.decompress(raw)), header=None, names=["t", "v", "c", "h", "l", "o"])
        df["ts"] = df["t"].astype("int64") * 1000
        df["qv"] = df["v"].astype("float64") * sym.contract_size * df["c"].astype("float64")
        return norm_klines(df)

    def _rest(self, sym: SymbolInfo, interval: str, start: int, end: int, step_ms: int) -> pd.DataFrame:
        earliest = now_ms() - 9_990 * step_ms
        if start < earliest:
            if interval == "1m" and not self._warned:
                log.warning("Gate: REST chỉ có ~10.000 nến 1m gần nhất; dữ liệu trước %s của tháng hiện tại bị thiếu",
                            datetime.fromtimestamp(earliest / 1000, tz=timezone.utc))
                self._warned = True
            start = max(start, earliest - earliest % step_ms + step_ms)
        frames = []
        t = start
        while t < end:
            e = min(end, t + 1999 * step_ms)
            rows = self.http.json(f"{API}/futures/usdt/candlesticks", {
                "contract": sym.native, "interval": interval, "from": t // 1000, "to": (e - 1) // 1000})
            if rows:
                frames.append(pd.DataFrame({"ts": [int(r["t"]) * 1000 for r in rows],
                                            "o": [r["o"] for r in rows], "h": [r["h"] for r in rows],
                                            "l": [r["l"] for r in rows], "c": [r["c"] for r in rows],
                                            "qv": [r.get("sum", 0) for r in rows]}))
            t = e
        return norm_klines(pd.concat(frames) if frames else empty_klines(), start, end)

    def fetch_klines(self, sym, start, end):
        frames = []
        cur = datetime.fromtimestamp(start / 1000, tz=timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while int(cur.timestamp() * 1000) < end:
            nxt = cur.replace(year=cur.year + (cur.month == 12), month=cur.month % 12 + 1)
            a, b = int(cur.timestamp() * 1000), int(nxt.timestamp() * 1000)
            df = self._dump_month(sym, cur.strftime("%Y%m")) if b + DAY_MS < now_ms() else None
            if df is None:
                df = self._rest(sym, "1m", max(a, start), min(b, end), MIN_MS)
            frames.append(df)
            cur = nxt
        return norm_klines(pd.concat(frames) if frames else empty_klines(), start, end)

    def fetch_daily_qvol(self, sym, start, end):
        return self._rest(sym, "1d", start, end, DAY_MS)[["ts", "qv", "c"]]

    def fetch_ticks_chunk(self, sym, chunk_start):
        def window(a, b):
            fs, ts_ = a // 1000, (b - 1) // 1000
            out, offset = [], 0
            while True:
                rows = self.http.json(f"{API}/futures/usdt/trades", {
                    "contract": sym.native, "from": fs, "to": ts_, "limit": 1000, "offset": offset})
                out.extend((int(round(float(r["create_time_ms"]) * 1000)), float(r["price"]), int(r["id"])) for r in rows)
                # cửa sổ ~1-2 giây vẫn đầy -> phân trang bằng offset
                if len(rows) < 1000 or b - a > 2000:
                    break
                offset += 1000
            return out

        rows = self._bisect_trades(window, chunk_start, chunk_start + self.tick_chunk_ms, 1000, 2000)
        df = pd.DataFrame(rows, columns=["ts", "px", "id"]).drop_duplicates("id").sort_values(["ts", "id"])
        return norm_ticks(df, chunk_start, chunk_start + self.tick_chunk_ms)

    def fetch_max_leverage(self):
        return {f"{s['name'].split('_')[0]}USDT": (float(s["leverage_max"]), None)
                for s in self.http.json(f"{API}/futures/usdt/contracts") if s.get("leverage_max")}

    def fetch_funding(self, sym, start, end):
        out = []
        t = start
        while t < end:
            rows = self.http.json(f"{API}/futures/usdt/funding_rate", {
                "contract": sym.native, "from": t // 1000, "to": (end - 1) // 1000, "limit": 1000})
            if not rows:
                break
            out.extend((int(r["t"]) * 1000, float(r["r"])) for r in rows)
            if len(rows) < 1000:
                break
            t = max(int(r["t"]) for r in rows) * 1000 + 1000
        df = pd.DataFrame(out, columns=["ts", "rate"]).astype({"ts": "int64", "rate": "float64"})
        df = df[(df.ts >= start) & (df.ts < end)]
        return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
