from __future__ import annotations

import io
from datetime import datetime, timezone

import pandas as pd

from ..utils import DAY_MS, MIN_MS, Http, day_floor, now_ms, unzip_single, utc_date
from .base import Exchange, SymbolInfo, empty_klines, klines_from_rows, norm_klines, norm_ticks

API = "https://fapi.binance.com"
VISION = "https://data.binance.vision/data/futures/um"


def _read_csv(raw: bytes, names: list[str]) -> pd.DataFrame:
    has_header = not raw[:1].isdigit()
    return pd.read_csv(io.BytesIO(raw), header=0 if has_header else None, names=names)


KL_NAMES = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume",
            "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore"]
AGG_NAMES = ["agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id", "transact_time", "is_buyer_maker"]


class Binance(Exchange):
    name = "binance"
    tick_history_days = None
    tick_publish_lag_ms = 6 * 3_600_000
    default_fees = (0.02, 0.05)
    rest_per_sec = 4.0  # klines limit 1500 = weight 10, giới hạn 2400 weight/phút

    def __init__(self):
        super().__init__()
        self.vision = Http(20.0, timeout=120)

    def list_symbols(self):
        d = self.http.json(f"{API}/fapi/v1/exchangeInfo")
        out = []
        for s in d["symbols"]:
            if s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != "USDT":
                continue
            out.append(SymbolInfo(s["symbol"], s["symbol"], s["baseAsset"], 1.0, s.get("status", "")))
        return out

    # ---------- klines ----------
    def _vision_klines(self, url: str) -> pd.DataFrame | None:
        raw = self.vision.content(url)
        if raw is None:
            return None
        df = _read_csv(unzip_single(raw), KL_NAMES)
        df = df.rename(columns={"open_time": "ts", "open": "o", "high": "h", "low": "l", "close": "c", "quote_volume": "qv"})
        return norm_klines(df)

    def _rest_klines(self, sym: str, start: int, end: int) -> pd.DataFrame:
        frames = []
        t = start
        while t < end:
            rows = self.http.json(f"{API}/fapi/v1/klines", {"symbol": sym, "interval": "1m", "startTime": t,
                                                            "endTime": end - 1, "limit": 1500})
            if not rows:
                break
            frames.append(klines_from_rows(rows, 0, 1, 2, 3, 4, 7))
            last = int(rows[-1][0])
            if last + MIN_MS <= t:
                break
            t = last + MIN_MS
        return norm_klines(pd.concat(frames) if frames else empty_klines(), start, end)

    def fetch_klines(self, sym: SymbolInfo, start: int, end: int) -> pd.DataFrame:
        s = sym.native
        frames = []
        published_before = day_floor(now_ms() - self.tick_publish_lag_ms)  # ngày đã có file vision
        d = day_floor(start)
        while d < end:
            dt = datetime.fromtimestamp(d / 1000, tz=timezone.utc)
            month_start = int(datetime(dt.year, dt.month, 1, tzinfo=timezone.utc).timestamp() * 1000)
            nm = datetime(dt.year + (dt.month == 12), dt.month % 12 + 1, 1, tzinfo=timezone.utc)
            month_end = int(nm.timestamp() * 1000)
            # cả tháng nằm trong khoảng cần lấy & đã công bố -> dùng file tháng
            if d == month_start and month_end <= end and month_end <= published_before - DAY_MS:
                df = self._vision_klines(f"{VISION}/monthly/klines/{s}/1m/{s}-1m-{dt.strftime('%Y-%m')}.zip")
                if df is not None:
                    frames.append(df)
                    d = month_end
                    continue
            df = None
            if d + DAY_MS <= published_before:
                df = self._vision_klines(f"{VISION}/daily/klines/{s}/1m/{s}-1m-{utc_date(d)}.zip")
            if df is None:
                df = self._rest_klines(s, d, min(d + DAY_MS, end))
            frames.append(df)
            d += DAY_MS
        return norm_klines(pd.concat(frames) if frames else empty_klines(), start, end)

    def fetch_daily_qvol(self, sym, start, end):
        rows = self.http.json(f"{API}/fapi/v1/klines", {"symbol": sym.native, "interval": "1d",
                                                        "startTime": start, "endTime": end - 1, "limit": 1500})
        df = klines_from_rows(rows, 0, 1, 2, 3, 4, 7)
        return df[["ts", "qv", "c"]]

    # ---------- ticks ----------
    def fetch_ticks_chunk(self, sym, chunk_start):
        s = sym.native
        raw = self.vision.content(f"{VISION}/daily/aggTrades/{s}/{s}-aggTrades-{utc_date(chunk_start)}.zip")
        if raw is None:
            return None
        df = _read_csv(unzip_single(raw), AGG_NAMES)
        df = df.rename(columns={"transact_time": "ts", "price": "px"})
        return norm_ticks(df)

    # ---------- đòn bẩy tối đa ----------
    def fetch_max_leverage(self):
        # API chính thức (leverageBracket) cần API key; endpoint công khai của web Binance trả cùng dữ liệu,
        # kèm thời điểm cập nhật bậc đòn bẩy (updateTime) của từng coin
        import requests
        r = requests.post("https://www.binance.com/bapi/futures/v1/friendly/future/common/brackets", json={},
                          timeout=30, headers={"User-Agent": "backtester/1.0"})
        r.raise_for_status()
        out = {}
        for b in (r.json().get("data") or {}).get("brackets") or []:
            rb = b.get("riskBrackets") or []
            if rb:
                out[b["symbol"]] = (float(rb[0]["maxOpenPosLeverage"]), int(b.get("updateTime") or 0) or None)
        return out

    # ---------- funding ----------
    def fetch_funding(self, sym, start, end):
        out = []
        t = start
        while t < end:
            rows = self.http.json(f"{API}/fapi/v1/fundingRate", {"symbol": sym.native, "startTime": t,
                                                                 "endTime": end - 1, "limit": 1000})
            if not rows:
                break
            out.extend((int(r["fundingTime"]), float(r["fundingRate"])) for r in rows)
            if len(rows) < 1000:
                break
            t = int(rows[-1]["fundingTime"]) + 1
        return pd.DataFrame(out, columns=["ts", "rate"]).astype({"ts": "int64", "rate": "float64"})
