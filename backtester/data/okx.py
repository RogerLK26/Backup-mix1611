"""OKX: file dump hằng ngày của OKX tính theo ngày UTC+8 (file ngày D = [D-1 16:00, D 16:00) UTC)."""
from __future__ import annotations

import io

import pandas as pd

from ..utils import DAY_MS, HOUR_MS, Http, day_floor, now_ms, unzip_single, utc_date
from .base import Exchange, SymbolInfo, empty_klines, klines_from_rows, norm_klines, norm_ticks

API = "https://www.okx.com"
DUMP = "https://static.okx.com/cdn/okex/traderecords"
TZ8 = 8 * HOUR_MS


class OKX(Exchange):
    name = "okx"
    tick_history_days = None
    tick_publish_lag_ms = 12 * HOUR_MS
    tick_chunk_offset_ms = -TZ8
    default_fees = (0.02, 0.05)
    rest_per_sec = 8.0

    def __init__(self):
        super().__init__()
        self.dump = Http(10.0, timeout=180)

    def _get(self, path, params=None):
        d = self.http.json(f"{API}{path}", params)
        if str(d.get("code")) != "0":
            raise RuntimeError(f"OKX {path} {params}: {d.get('msg')}")
        return d["data"]

    def list_symbols(self):
        out = []
        for s in self._get("/api/v5/public/instruments", {"instType": "SWAP"}):
            if s.get("settleCcy") != "USDT" or s.get("ctType") != "linear":
                continue
            base = s["instId"].split("-")[0]
            out.append(SymbolInfo(f"{base}USDT", s["instId"], base, float(s.get("ctVal") or 1), s.get("state", "")))
        return out

    @staticmethod
    def _d8(chunk_start_utc8_midnight: int) -> str:
        return utc_date(chunk_start_utc8_midnight + TZ8)

    def _rest_klines(self, inst, bar, start, end):
        frames, after = [], end
        while after > start:
            rows = self._get("/api/v5/market/history-candles", {"instId": inst, "bar": bar, "after": after, "limit": 300 if bar == "1m" else 100})
            if not rows:
                break
            frames.append(klines_from_rows(rows, 0, 1, 2, 3, 4, 7))
            oldest = min(int(r[0]) for r in rows)
            if oldest >= after:
                break
            after = oldest
        return norm_klines(pd.concat(frames) if frames else empty_klines(), start, end)

    def fetch_klines(self, sym, start, end):
        frames = []
        # các ngày UTC+8 phủ [start, end)
        d8 = day_floor(start + TZ8)
        while d8 - TZ8 < end:
            a, b = d8 - TZ8, d8 - TZ8 + DAY_MS
            df = None
            if b + self.tick_publish_lag_ms < now_ms():
                ds = utc_date(d8)
                raw = self.dump.content(f"{DUMP}/candlesticks/daily/{ds.replace('-', '')}/{sym.native}-candlesticks-{ds}.zip")
                if raw is not None:
                    df = pd.read_csv(io.BytesIO(unzip_single(raw)))
                    df = df.rename(columns={"open_time": "ts", "open": "o", "high": "h", "low": "l", "close": "c", "vol_quote": "qv"})
                    df = norm_klines(df)
            if df is None:
                df = self._rest_klines(sym.native, "1m", max(a, start), min(b, end))
            frames.append(df)
            d8 += DAY_MS
        return norm_klines(pd.concat(frames) if frames else empty_klines(), start, end)

    def fetch_daily_qvol(self, sym, start, end):
        return self._rest_klines(sym.native, "1Dutc", start, end)[["ts", "qv", "c"]]

    def fetch_ticks_chunk(self, sym, chunk_start):
        ds = self._d8(chunk_start)
        raw = self.dump.content(f"{DUMP}/trades/daily/{ds.replace('-', '')}/{sym.native}-trades-{ds}.zip")
        if raw is None:
            return None
        df = pd.read_csv(io.BytesIO(unzip_single(raw)), usecols=["price", "created_time"])
        df = df.rename(columns={"created_time": "ts", "price": "px"})
        return norm_ticks(df)

    def fetch_max_leverage(self):
        out = {}
        for s in self._get("/api/v5/public/instruments", {"instType": "SWAP"}):
            if s.get("settleCcy") == "USDT" and s.get("lever"):
                out[f"{s['instId'].split('-')[0]}USDT"] = (float(s["lever"]), None)
        return out

    def fetch_funding(self, sym, start, end):
        out, after = [], end
        while after > start:
            rows = self._get("/api/v5/public/funding-rate-history", {"instId": sym.native, "after": after, "limit": 100})
            if not rows:
                break
            out.extend((int(r["fundingTime"]), float(r.get("realizedRate") or r["fundingRate"])) for r in rows)
            oldest = min(int(r["fundingTime"]) for r in rows)
            if oldest >= after:
                break
            after = oldest
        df = pd.DataFrame(out, columns=["ts", "rate"]).astype({"ts": "int64", "rate": "float64"})
        df = df[(df.ts >= start) & (df.ts < end)]
        return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
