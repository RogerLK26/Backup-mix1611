from __future__ import annotations

import gzip
import io

import pandas as pd

from ..utils import DAY_MS, MIN_MS, Http, utc_date
from .base import Exchange, SymbolInfo, empty_klines, klines_from_rows, norm_klines, norm_ticks

API = "https://api.bybit.com"
DUMP = "https://public.bybit.com/trading"


class Bybit(Exchange):
    name = "bybit"
    tick_history_days = None
    tick_publish_lag_ms = 6 * 3_600_000
    default_fees = (0.02, 0.055)
    rest_per_sec = 10.0

    def __init__(self):
        super().__init__()
        self.dump = Http(10.0, timeout=180)

    def _get(self, path, params):
        d = self.http.json(f"{API}{path}", params)
        if d.get("retCode") != 0:
            raise RuntimeError(f"Bybit {path} {params}: {d.get('retMsg')}")
        return d["result"]

    def list_symbols(self):
        out, cursor = [], ""
        while True:
            p = {"category": "linear", "limit": 1000}
            if cursor:
                p["cursor"] = cursor
            r = self._get("/v5/market/instruments-info", p)
            for s in r["list"]:
                if s.get("contractType") == "LinearPerpetual" and s.get("quoteCoin") == "USDT":
                    out.append(SymbolInfo(s["symbol"], s["symbol"], s["baseCoin"], 1.0, s.get("status", "")))
            cursor = r.get("nextPageCursor")
            if not cursor:
                break
        return out

    def _klines(self, sym, interval, start, end, step_ms):
        frames = []
        t = start
        while t < end:
            e = min(end, t + 1000 * step_ms)
            r = self._get("/v5/market/kline", {"category": "linear", "symbol": sym, "interval": interval,
                                               "start": t, "end": e - 1, "limit": 1000})
            rows = r["list"]
            if rows:
                frames.append(klines_from_rows(rows, 0, 1, 2, 3, 4, 6))
            t = e
        return norm_klines(pd.concat(frames) if frames else empty_klines(), start, end)

    def fetch_klines(self, sym, start, end):
        return self._klines(sym.native, "1", start, end, MIN_MS)

    def fetch_daily_qvol(self, sym, start, end):
        return self._klines(sym.native, "D", start, end, DAY_MS)[["ts", "qv", "c"]]

    def fetch_ticks_chunk(self, sym, chunk_start):
        s = sym.native
        raw = self.dump.content(f"{DUMP}/{s}/{s}{utc_date(chunk_start)}.csv.gz")
        if raw is None:
            return None
        df = pd.read_csv(io.BytesIO(gzip.decompress(raw)), usecols=["timestamp", "price"])
        # timestamp dạng giây (float) -> ms
        df["ts"] = (df["timestamp"].astype("float64") * 1000).round().astype("int64")
        df["px"] = df["price"]
        return norm_ticks(df)

    def fetch_max_leverage(self):
        out, cursor = {}, ""
        while True:
            p = {"category": "linear", "limit": 1000}
            if cursor:
                p["cursor"] = cursor
            r = self._get("/v5/market/instruments-info", p)
            for s in r["list"]:
                if s.get("quoteCoin") == "USDT":
                    out[s["symbol"]] = (float(s["leverageFilter"]["maxLeverage"]), None)
            cursor = r.get("nextPageCursor")
            if not cursor:
                return out

    def fetch_funding(self, sym, start, end):
        out = []
        e = end
        while e > start:
            r = self._get("/v5/market/funding/history", {"category": "linear", "symbol": sym.native,
                                                         "startTime": start, "endTime": e - 1, "limit": 200})
            rows = r["list"]
            if not rows:
                break
            out.extend((int(x["fundingRateTimestamp"]), float(x["fundingRate"])) for x in rows)
            oldest = min(int(x["fundingRateTimestamp"]) for x in rows)
            if len(rows) < 200 or oldest >= e:
                break
            e = oldest
        df = pd.DataFrame(out, columns=["ts", "rate"]).astype({"ts": "int64", "rate": "float64"})
        return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
