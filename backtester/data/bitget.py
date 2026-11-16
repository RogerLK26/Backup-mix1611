from __future__ import annotations

import pandas as pd

from ..utils import DAY_MS, HOUR_MS, MIN_MS
from .base import Exchange, SymbolInfo, empty_klines, klines_from_rows, norm_klines, norm_ticks

API = "https://api.bitget.com"
PT = "usdt-futures"


class Bitget(Exchange):
    name = "bitget"
    tick_history_days = 89          # REST fills-history chỉ cho 90 ngày gần nhất
    tick_chunk_ms = HOUR_MS
    default_fees = (0.02, 0.06)
    rest_per_sec = 9.0

    def _get(self, path, params):
        d = self.http.json(f"{API}{path}", params)
        if str(d.get("code")) != "00000":
            raise RuntimeError(f"Bitget {path} {params}: {d.get('msg')}")
        return d["data"]

    def list_symbols(self):
        out = []
        for s in self._get("/api/v2/mix/market/contracts", {"productType": PT}):
            if s.get("quoteCoin") != "USDT" or s.get("symbolType") != "perpetual":
                continue
            out.append(SymbolInfo(s["symbol"], s["symbol"], s["baseCoin"], 1.0, s.get("symbolStatus", "")))
        return out

    def _klines(self, sym, gran, start, end, step_ms, per_req):
        frames = []
        t = start
        while t < end:
            e = min(end, t + per_req * step_ms)
            rows = self._get("/api/v2/mix/market/history-candles", {
                "symbol": sym, "productType": PT, "granularity": gran,
                "startTime": t, "endTime": e - 1, "limit": per_req})
            if rows:
                frames.append(klines_from_rows(rows, 0, 1, 2, 3, 4, 6))
            t = e
        return norm_klines(pd.concat(frames) if frames else empty_klines(), start, end)

    def fetch_klines(self, sym, start, end):
        return self._klines(sym.native, "1m", start, end, MIN_MS, 200)

    def fetch_daily_qvol(self, sym, start, end):
        return self._klines(sym.native, "1Dutc", start, end, DAY_MS, 80)[["ts", "qv", "c"]]

    def fetch_ticks_chunk(self, sym, chunk_start):
        def window(a, b):
            rows = self._get("/api/v2/mix/market/fills-history", {
                "symbol": sym.native, "productType": PT, "startTime": a, "endTime": b - 1, "limit": 1000})
            return [(int(r["ts"]), float(r["price"]), r["tradeId"]) for r in rows]

        rows = self._bisect_trades(window, chunk_start, chunk_start + self.tick_chunk_ms, 1000, 1)
        df = pd.DataFrame(rows, columns=["ts", "px", "id"]).drop_duplicates("id")
        # id tăng dần theo thứ tự khớp
        df["id"] = df["id"].astype("int64") if len(df) else df["id"]
        df = df.sort_values(["ts", "id"]) if len(df) else df
        return norm_ticks(df, chunk_start, chunk_start + self.tick_chunk_ms)

    def fetch_max_leverage(self):
        return {s["symbol"]: (float(s["maxLever"]), None)
                for s in self._get("/api/v2/mix/market/contracts", {"productType": PT}) if s.get("maxLever")}

    def fetch_funding(self, sym, start, end):
        out, page = [], 1
        while True:
            rows = self._get("/api/v2/mix/market/history-fund-rate", {
                "symbol": sym.native, "productType": PT, "pageSize": 100, "pageNo": page})
            if not rows:
                break
            out.extend((int(r["fundingTime"]), float(r["fundingRate"])) for r in rows)
            if min(int(r["fundingTime"]) for r in rows) < start or len(rows) < 100 or page > 200:
                break
            page += 1
        df = pd.DataFrame(out, columns=["ts", "rate"]).astype({"ts": "int64", "rate": "float64"})
        df = df[(df.ts >= start) & (df.ts < end)]
        return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
