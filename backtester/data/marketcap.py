"""Market cap từ CoinGecko — dùng cho điều kiện MC > X.

- supply (mặc định): MC(t) = lượng cung lưu hành hiện tại × giá trên sàn tại t. Chỉ ~8 request.
  Sai lệch chủ yếu khi coin có đợt unlock lớn trong giai đoạn test.
- coingecko: MC lịch sử thật theo ngày (1 request/coin, chậm nếu không có key).

- API công khai (không key): lịch sử tối đa 365 ngày, giới hạn ~5-15 request/phút.
- Demo key miễn phí (coingecko.com/en/api): nhanh hơn (30 request/phút).
- Ánh xạ ticker -> coin id: chọn coin có MC lớn nhất trong top ~2000 trùng ticker.
"""
from __future__ import annotations

import json
import math
import threading
from pathlib import Path

import numpy as np
import pandas as pd

from ..utils import DAY_MS, Http, data_path, log, now_ms
from .base import strip_multiplier


class MarketCap:
    def __init__(self, cache_dir: str | Path, api_key: str = "", pro: bool = False, overrides: dict | None = None):
        self.root = data_path(cache_dir) / "coingecko"
        self.root.mkdir(parents=True, exist_ok=True)
        if pro and api_key:
            self.base, headers, rate = "https://pro-api.coingecko.com/api/v3", {"x-cg-pro-api-key": api_key}, 5.0
        elif api_key:
            self.base, headers, rate = "https://api.coingecko.com/api/v3", {"x-cg-demo-api-key": api_key}, 0.45
        else:
            self.base, headers, rate = "https://api.coingecko.com/api/v3", {}, 0.08
        self.has_key = bool(api_key)
        self.http = Http(rate, headers=headers)
        # ticker hợp đồng khác ticker coin (bổ sung/ghi đè bằng universe.coingecko_ids)
        self.overrides = {"RAYSOL": "raydium", "RONIN": "ronin", "LUNA2": "terra-luna-2", "BEAMX": "beam-2",
                          "VELODROME": "velodrome-finance"}
        self.overrides.update({k.upper(): v for k, v in (overrides or {}).items()})
        self._map: dict[str, tuple[str, float]] | None = None
        self._supply: dict[str, float] | None = None
        self._hist: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- ticker -> id
    def _markets(self) -> list[dict]:
        p = self.root / "markets.json"
        if p.exists() and now_ms() - p.stat().st_mtime * 1000 < DAY_MS:
            rows = json.loads(p.read_text(encoding="utf-8"))
            if rows and "supply" in rows[0]:
                return rows
        rows = []
        log.info("Tải danh sách market cap / lượng cung lưu hành từ CoinGecko...")
        for page in range(1, 9):
            data = self.http.json(f"{self.base}/coins/markets", {
                "vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": page})
            if not data:
                break
            rows.extend({"id": d["id"], "symbol": d["symbol"], "mc": d.get("market_cap") or 0,
                         "supply": d.get("circulating_supply") or 0} for d in data)
        p.write_text(json.dumps(rows), encoding="utf-8")
        return rows

    def id_map(self) -> dict[str, tuple[str, float]]:
        if self._map is None:
            m: dict[str, tuple[str, float]] = {}
            for r in self._markets():
                s = r["symbol"].upper()
                if s not in m or (r["mc"] or 0) > m[s][1]:
                    m[s] = (r["id"], r["mc"] or 0)
            self._map = m
        return self._map

    def coin(self, base: str) -> tuple[str | None, float]:
        """(coingecko_id, hệ số nhân của hợp đồng) cho base coin của sàn (vd 1000PEPE)."""
        b = base.upper()
        if b in self.overrides:
            return self.overrides[b], strip_multiplier(b)[1]
        m = self.id_map()
        if b in m:
            return m[b][0], 1.0
        core, mult = strip_multiplier(b)
        if core in self.overrides:
            return self.overrides[core], mult
        if core in m:
            return m[core][0], mult
        return None, 1.0

    def supply(self, base: str) -> tuple[float, float]:
        """(lượng cung lưu hành hiện tại, hệ số nhân hợp đồng). MC(t) ≈ supply × giá_hợp_đồng(t) / hệ_số."""
        cid, mult = self.coin(base)
        if cid is None:
            return math.nan, mult
        if self._supply is None:
            self._supply = {r["id"]: float(r.get("supply") or 0) for r in self._markets()}
        s = self._supply.get(cid, 0.0)
        return (s if s > 0 else math.nan), mult

    def current(self, base: str) -> float:
        cid, _ = self.coin(base)
        if cid is None:
            return math.nan
        for s, (i, mc) in self.id_map().items():
            if i == cid:
                return float(mc)
        return math.nan

    # ---------------------------------------------------------------- lịch sử
    def history(self, coin_id: str, start: int, end: int) -> tuple[np.ndarray, np.ndarray]:
        with self._lock:
            if coin_id in self._hist:
                return self._hist[coin_id]
        p = self.root / f"{coin_id}.parquet"
        meta_p = p.with_suffix(".json")
        df = None
        if p.exists() and meta_p.exists():
            meta = json.loads(meta_p.read_text())
            if meta["start"] <= start and meta["end"] >= min(end, now_ms() - DAY_MS):
                df = pd.read_parquet(p)
        if df is None:
            days = math.ceil((now_ms() - start) / DAY_MS) + 2
            if days > 365 and not self.has_key:
                log.warning("CoinGecko không key chỉ cho 365 ngày lịch sử; MC trước đó sẽ không có")
                days = 365
            try:
                data = self.http.json(f"{self.base}/coins/{coin_id}/market_chart",
                                      {"vs_currency": "usd", "days": days, "interval": "daily"})
                rows = data.get("market_caps") or []
            except Exception as e:  # noqa: BLE001
                log.warning("CoinGecko lỗi với %s: %s", coin_id, e)
                rows = []
            df = pd.DataFrame(rows, columns=["ts", "mc"]).astype({"ts": "int64", "mc": "float64"})
            df = df[df.mc > 0]
            df.to_parquet(p, index=False)
            meta_p.write_text(json.dumps({"start": now_ms() - days * DAY_MS, "end": now_ms()}))
        arr = (df["ts"].to_numpy(np.int64), df["mc"].to_numpy(np.float64))
        with self._lock:
            self._hist[coin_id] = arr
        return arr

    def at(self, coin_id: str, ts: int, start: int, end: int) -> float:
        """MC gần nhất đã biết tại thời điểm ts (không nhìn tương lai)."""
        t, v = self.history(coin_id, start, end)
        i = np.searchsorted(t, ts, "right") - 1
        if i < 0:
            return math.nan
        if ts - t[i] > 3 * DAY_MS:
            return math.nan
        return float(v[i])
