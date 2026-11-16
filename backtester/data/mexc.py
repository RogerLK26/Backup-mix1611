"""MEXC USDT futures: API công khai chỉ có ~30 ngày nến 1m gần nhất và không có lịch sử tick.
Backtest trên MEXC luôn dùng mô phỏng trong nến 1m (độ chính xác thấp hơn)."""
from __future__ import annotations

import pandas as pd

from ..utils import DAY_MS, MIN_MS
from .base import Exchange, SymbolInfo, empty_klines, norm_klines

API = "https://contract.mexc.com/api/v1/contract"


class MEXC(Exchange):
    name = "mexc"
    tick_history_days = 0
    default_fees = (0.0, 0.02)
    rest_per_sec = 8.0

    def _get(self, path, params=None):
        d = self.http.json(f"{API}{path}", params)
        if not d.get("success", False):
            raise RuntimeError(f"MEXC {path} {params}: {d}")
        return d["data"]

    def list_symbols(self):
        out = []
        for s in self._get("/detail"):
            if s.get("quoteCoin") != "USDT" or s.get("settleCoin") != "USDT":
                continue
            out.append(SymbolInfo(f"{s['baseCoin']}USDT", s["symbol"], s["baseCoin"], float(s.get("contractSize") or 1),
                                  str(s.get("state", ""))))
        return out

    def _klines(self, sym, interval, start, end, step_ms):
        frames = []
        t = start
        while t < end:
            e = min(end, t + 1999 * step_ms)
            d = self._get(f"/kline/{sym}", {"interval": interval, "start": t // 1000, "end": (e - 1) // 1000})
            if d.get("time"):
                frames.append(pd.DataFrame({"ts": [int(x) * 1000 for x in d["time"]], "o": d["open"], "h": d["high"],
                                            "l": d["low"], "c": d["close"], "qv": d["amount"]}))
            t = e
        return norm_klines(pd.concat(frames) if frames else empty_klines(), start, end)

    def fetch_klines(self, sym, start, end):
        return self._klines(sym.native, "Min1", start, end, MIN_MS)

    def fetch_daily_qvol(self, sym, start, end):
        return self._klines(sym.native, "Day1", start, end, DAY_MS)[["ts", "qv", "c"]]

    def fetch_ticks_chunk(self, sym, chunk_start):
        return None

    def fetch_max_leverage(self):
        return {f"{s['baseCoin']}USDT": (float(s["maxLeverage"]), None)
                for s in self._get("/detail") if s.get("quoteCoin") == "USDT" and s.get("maxLeverage")}

    def fetch_funding(self, sym, start, end):
        out, page = [], 1
        while True:
            d = self._get("/funding_rate/history", {"symbol": sym.native, "page_num": page, "page_size": 100})
            rows = d.get("resultList") or []
            if not rows:
                break
            out.extend((int(r["settleTime"]), float(r["fundingRate"])) for r in rows)
            if min(int(r["settleTime"]) for r in rows) < start or page >= int(d.get("totalPage", 1)) or page > 200:
                break
            page += 1
        df = pd.DataFrame(out, columns=["ts", "rate"]).astype({"ts": "int64", "rate": "float64"})
        df = df[(df.ts >= start) & (df.ts < end)]
        return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
