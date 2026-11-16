"""Cache dữ liệu trên đĩa (parquet) + trong RAM cho nến 1m, tick, funding, volume ngày."""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from ..progress import PROGRESS, Cancelled
from ..utils import DAY_MS, HOUR_MS, MIN_MS, data_path, day_floor, fmt_ms, log, now_ms, utc_date
from .base import Exchange, SymbolInfo, empty_klines, norm_klines


class Bars:
    """Mảng numpy nến 1m của 1 coin trong 1 ngày UTC."""
    __slots__ = ("ts", "o", "h", "l", "c", "qv")

    def __init__(self, df: pd.DataFrame):
        self.ts = df["ts"].to_numpy(np.int64)
        self.o = df["o"].to_numpy(np.float64)
        self.h = df["h"].to_numpy(np.float64)
        self.l = df["l"].to_numpy(np.float64)
        self.c = df["c"].to_numpy(np.float64)
        self.qv = df["qv"].to_numpy(np.float64)

    def __len__(self):
        return len(self.ts)


class DataStore:
    def __init__(self, ex: Exchange, cache_dir: str | Path = "cache", workers: int = 8,
                 tick_ram_limit: int | None = None):
        import os
        self.ex = ex
        self.root = data_path(cache_dir) / ex.name
        # giới hạn số tick giữ trong RAM (~16 byte/tick); máy chủ ít RAM nên đặt TICK_RAM_LIMIT nhỏ hơn
        tick_ram_limit = tick_ram_limit or int(os.environ.get("TICK_RAM_LIMIT", 30_000_000))
        self.root.mkdir(parents=True, exist_ok=True)
        self.workers = workers
        self._bars: dict[str, dict[int, Bars]] = {}
        self._bars_lock = threading.Lock()
        # nến 1m trong RAM: tối đa BARS_RAM_LIMIT coin-ngày (~70 KB/coin-ngày); bỏ bớt ngày ít dùng nhất,
        # cần lại thì đọc từ cache trên đĩa. Ngày chưa có file cache (hôm nay) luôn giữ trong RAM.
        self._bars_limit = int(os.environ.get("BARS_RAM_LIMIT", 12_000))
        self._bars_lru: OrderedDict[tuple[str, int], None] = OrderedDict()
        self._lru_lock = threading.Lock()
        self._avail: dict[str, set[int]] = {}   # các ngày đã có dữ liệu (trong RAM hoặc trên đĩa)
        self._ticks: OrderedDict[tuple[str, int], tuple[np.ndarray, np.ndarray] | None] = OrderedDict()
        self._tick_count = 0
        self._tick_limit = tick_ram_limit
        self._tick_lock = threading.Lock()
        self._funding: dict[str, pd.DataFrame] = {}
        self._sym: dict[str, SymbolInfo] | None = None
        self.stats = {"tick_chunks_loaded": 0, "tick_chunks_missing": 0}

    # ------------------------------------------------------------------ symbols
    def symbols(self) -> dict[str, SymbolInfo]:
        if self._sym is not None:
            return self._sym
        p = self.root / "symbols.json"
        if p.exists() and now_ms() - p.stat().st_mtime * 1000 < DAY_MS:
            data = json.loads(p.read_text(encoding="utf-8"))
            self._sym = {d["symbol"]: SymbolInfo(**d) for d in data}
        else:
            lst = self.ex.list_symbols()
            p.write_text(json.dumps([asdict(s) for s in lst]), encoding="utf-8")
            self._sym = {s.symbol: s for s in lst}
        return self._sym

    def resolve(self, name: str) -> SymbolInfo | None:
        syms = self.symbols()
        key = name.upper().replace("-", "").replace("_", "").replace("/", "")
        if key.endswith("SWAP"):
            key = key[:-4]
        if not key.endswith("USDT"):
            key += "USDT"
        if key in syms:
            return syms[key]
        for s in syms.values():
            if s.native.upper() == name.upper():
                return s
        # PEPE -> 1000PEPEUSDT, SHIB -> SHIB1000USDT ...
        from .base import strip_multiplier
        core = strip_multiplier(key[:-4])[0]
        for s in syms.values():
            if strip_multiplier(s.base)[0] == core:
                return s
        return None

    # ------------------------------------------------------------------ daily volume
    def daily_qvol(self, sym: SymbolInfo, start: int, end: int) -> pd.DataFrame:
        p = self.root / "daily" / f"{sym.symbol}.parquet"
        meta_p = p.with_suffix(".json")
        if p.exists() and meta_p.exists():
            meta = json.loads(meta_p.read_text())
            if meta["start"] <= start and meta["end"] >= end:
                df = pd.read_parquet(p)
                if "c" in df.columns:
                    return df[(df.ts >= start) & (df.ts < end)].reset_index(drop=True)
        df = self.ex.fetch_daily_qvol(sym, start, end)
        complete_end = min(end, day_floor(now_ms()))  # ngày hiện tại chưa đủ -> không cache
        p.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(p, index=False)
        meta_p.write_text(json.dumps({"start": start, "end": complete_end}))
        return df

    # ------------------------------------------------------------------ klines
    def _kpath(self, sym: str, day: int) -> Path:
        return self.root / "klines" / sym / f"{utc_date(day)}.parquet"

    def _day_complete(self, day: int) -> bool:
        return day + DAY_MS + 2 * HOUR_MS < now_ms()

    def _put(self, symbol: str, day: int, bars: Bars, on_disk: bool):
        self._bars.setdefault(symbol, {})[day] = bars
        self._avail.setdefault(symbol, set()).add(day)
        if not on_disk:
            return
        with self._lru_lock:
            self._bars_lru[(symbol, day)] = None
            self._bars_lru.move_to_end((symbol, day))
            while len(self._bars_lru) > self._bars_limit:
                (s2, d2), _ = self._bars_lru.popitem(last=False)
                self._bars.get(s2, {}).pop(d2, None)

    def _load_days(self, sym: SymbolInfo, days: list[int], load_cached: bool = True):
        """Nạp các ngày vào RAM: đọc cache, thiếu thì tải theo dải liên tục.
        load_cached=False: ngày đã có file cache chỉ được ghi nhận (đọc vào RAM khi thật sự cần)."""
        have = self._bars.setdefault(sym.symbol, {})
        missing = []
        for d in days:
            if d in have:
                continue
            p = self._kpath(sym.symbol, d)
            if p.exists() and self._day_complete(d):
                if load_cached:
                    self._put(sym.symbol, d, Bars(pd.read_parquet(p)), True)
                else:
                    self._avail.setdefault(sym.symbol, set()).add(d)
            else:
                missing.append(d)
        if not missing:
            return
        # gom thành các dải liên tục
        runs, cur = [], [missing[0]]
        for d in missing[1:]:
            if d == cur[-1] + DAY_MS:
                cur.append(d)
            else:
                runs.append(cur)
                cur = [d]
        runs.append(cur)
        for run in runs:
            a, b = run[0], min(run[-1] + DAY_MS, now_ms() - now_ms() % MIN_MS)
            if b <= a:
                for d in run:
                    self._put(sym.symbol, d, Bars(empty_klines()), False)
                continue
            df = self.ex.fetch_klines(sym, a, b)
            dayidx = (df["ts"].to_numpy() // DAY_MS) * DAY_MS
            for d in run:
                part = norm_klines(df[dayidx == d])
                on_disk = self._day_complete(d)
                if on_disk:
                    p = self._kpath(sym.symbol, d)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    part.to_parquet(p, index=False)
                if load_cached or not on_disk:
                    self._put(sym.symbol, d, Bars(part), on_disk)
                else:
                    self._avail.setdefault(sym.symbol, set()).add(d)

    def ensure_klines(self, plan: dict[str, list[int]], progress: bool = True):
        """plan: {symbol: [day_ms,...]} -> tải song song."""
        syms = self.symbols()
        todo = [(s, sorted(set(days))) for s, days in plan.items() if days]
        total = len(todo)
        done = 0
        errors = []
        PROGRESS.set_stage("Tải nến 1m", total)
        with ThreadPoolExecutor(self.workers) as pool:
            futs = {pool.submit(self._load_days, syms[s], days, False): s for s, days in todo}
            for f in as_completed(futs):
                done += 1
                if PROGRESS.cancel.is_set():
                    for x in futs:
                        x.cancel()
                    raise Cancelled("Đã huỷ")
                PROGRESS.step(done)
                try:
                    f.result()
                except Exception as e:  # noqa: BLE001
                    errors.append((futs[f], e))
                    log.error("Lỗi tải nến %s: %s", futs[f], e)
                if progress and (done % 20 == 0 or done == total):
                    log.info("  nến 1m: %d/%d coin", done, total)
        return errors

    def day_bars(self, symbol: str, day: int) -> Bars:
        d = self._bars.get(symbol, {}).get(day)
        if d is None:
            with self._bars_lock:
                d = self._bars.get(symbol, {}).get(day)
                if d is None:
                    self._load_days(self.symbols()[symbol], [day])
                    d = self._bars[symbol][day]
        elif (symbol, day) in self._bars_lru:
            with self._lru_lock:
                if (symbol, day) in self._bars_lru:
                    self._bars_lru.move_to_end((symbol, day))
        return d

    def bars_range(self, symbol: str, start: int, end: int) -> pd.DataFrame:
        frames = []
        for d in range(day_floor(start), end, DAY_MS):
            b = self.day_bars(symbol, d)
            frames.append(pd.DataFrame({"ts": b.ts, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "qv": b.qv}))
        df = pd.concat(frames) if frames else empty_klines()
        return df[(df.ts >= start) & (df.ts < end)].reset_index(drop=True)

    def loaded_days(self, symbol: str) -> list[int]:
        return sorted(self._avail.get(symbol, ()))

    def ram_stats(self) -> dict:
        return {"bar_days": sum(len(v) for v in self._bars.values()), "bar_limit": self._bars_limit,
                "ticks": self._tick_count, "tick_limit": self._tick_limit}

    # ------------------------------------------------------------------ ticks
    def _tpath(self, sym: str, chunk: int) -> Path:
        name = fmt_ms(chunk, "UTC").replace(" ", "_").replace(":", "")
        return self.root / "ticks" / sym / f"{name}.parquet"

    def _tick_chunk(self, sym: SymbolInfo, chunk: int):
        key = (sym.symbol, chunk)
        with self._tick_lock:
            if key in self._ticks:
                self._ticks.move_to_end(key)
                return self._ticks[key]
        val = None
        if self.ex.tick_available(chunk):
            p = self._tpath(sym.symbol, chunk)
            df = None
            if p.exists():
                df = pd.read_parquet(p)
            else:
                try:
                    df = self.ex.fetch_ticks_chunk(sym, chunk)
                except Exception as e:  # noqa: BLE001
                    log.warning("Không tải được tick %s %s: %s", sym.symbol, fmt_ms(chunk), e)
                    df = None
                if df is not None:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    df.to_parquet(p, index=False)
            if df is not None:
                val = (df["ts"].to_numpy(np.int64), df["px"].to_numpy(np.float64))
                self.stats["tick_chunks_loaded"] += 1
        if val is None:
            self.stats["tick_chunks_missing"] += 1
        with self._tick_lock:
            self._ticks[key] = val
            self._tick_count += 0 if val is None else len(val[0])
            while self._tick_count > self._tick_limit and len(self._ticks) > 1:
                _, old = self._ticks.popitem(last=False)
                self._tick_count -= 0 if old is None else len(old[0])
        return val

    def ticks(self, symbol: str, a: int, b: int):
        """Tick trong [a, b). Trả về (ts, px) hoặc None nếu không có dữ liệu tick cho khoảng này."""
        sym = self.symbols()[symbol]
        chunk = self.ex.tick_chunk_start(a)
        parts_ts, parts_px = [], []
        while chunk < b:
            v = self._tick_chunk(sym, chunk)
            if v is None:
                return None
            ts, px = v
            i, j = np.searchsorted(ts, a, "left"), np.searchsorted(ts, b, "left")
            parts_ts.append(ts[i:j])
            parts_px.append(px[i:j])
            chunk += self.ex.tick_chunk_ms
        if len(parts_ts) == 1:
            return parts_ts[0], parts_px[0]
        return np.concatenate(parts_ts), np.concatenate(parts_px)

    def prefetch_ticks(self, items: list[tuple[str, int]]):
        """Tải trước song song các khối tick (symbol, ts)."""
        syms = self.symbols()
        keys = sorted({(s, self.ex.tick_chunk_start(t)) for s, t in items})
        keys = [k for k in keys if self.ex.tick_available(k[1]) and not self._tpath(k[0], k[1]).exists()]
        if not keys:
            return
        log.info("Tải trước %d khối tick...", len(keys))
        done = 0

        def job(k):
            s, c = k
            try:
                df = self.ex.fetch_ticks_chunk(syms[s], c)
            except Exception as e:  # noqa: BLE001
                log.warning("Không tải được tick %s %s: %s", s, fmt_ms(c), e)
                return
            if df is not None:
                p = self._tpath(s, c)
                p.parent.mkdir(parents=True, exist_ok=True)
                df.to_parquet(p, index=False)

        PROGRESS.set_stage("Tải dữ liệu tick", len(keys))
        with ThreadPoolExecutor(self.workers) as pool:
            futs = [pool.submit(job, k) for k in keys]
            for _ in as_completed(futs):
                done += 1
                if PROGRESS.cancel.is_set():
                    for x in futs:
                        x.cancel()
                    raise Cancelled("Đã huỷ")
                PROGRESS.step(done)
                if done % 25 == 0 or done == len(keys):
                    log.info("  tick: %d/%d", done, len(keys))

    # ------------------------------------------------------------------ đòn bẩy tối đa
    def leverage_snapshots(self) -> list[tuple[int, dict]]:
        """Các bản ghi đòn bẩy tối đa theo ngày [(day_ms, {symbol: [lev, update_ms|None]})], cũ -> mới.
        Sàn chỉ công bố mức hiện tại nên mỗi ngày chạy sẽ lưu thêm 1 bản ghi để dần có lịch sử."""
        if getattr(self, "_lev", None) is not None:
            return self._lev
        d = self.root / "leverage"
        today = utc_date(now_ms())
        p = d / f"{today}.json"
        if not p.exists():
            try:
                data = self.ex.fetch_max_leverage()
            except Exception as e:  # noqa: BLE001
                log.warning("Không tải được đòn bẩy tối đa của %s: %s", self.ex.name, e)
                data = {}
            if data:
                d.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({k: list(v) for k, v in data.items()}), encoding="utf-8")
        snaps = []
        for f in sorted(d.glob("*.json")) if d.exists() else []:
            try:
                from ..utils import date_to_ms
                snaps.append((date_to_ms(f.stem), json.loads(f.read_text(encoding="utf-8"))))
            except (ValueError, OSError):
                continue
        self._lev = snaps
        return snaps

    def max_leverage(self, symbol: str, t: int) -> tuple[float | None, bool]:
        """(đòn bẩy tối đa tại thời điểm t, có chắc chắn đúng tại t không).
        Dùng bản ghi gần nhất trước t; nếu chưa có thì dùng bản ghi sớm nhất sau t (ước lượng).
        Binance có thời điểm cập nhật bậc đòn bẩy: nếu cập nhật trước t thì mức này chắc chắn đúng tại t."""
        snaps = self.leverage_snapshots()
        best = None
        for day, data in snaps:
            if symbol not in data:
                continue
            if day <= t:
                best = (day, data[symbol])
            elif best is None:
                best = (day, data[symbol])
                break
            else:
                break
        if best is None:
            return None, False
        day, (lev, upd) = best
        exact = day <= t < day + DAY_MS or (upd is not None and upd <= t)
        return float(lev), bool(exact)

    # ------------------------------------------------------------------ funding
    def funding(self, symbol: str, start: int, end: int) -> pd.DataFrame:
        if symbol in self._funding:
            return self._funding[symbol]
        p = self.root / "funding" / f"{symbol}.parquet"
        meta_p = p.with_suffix(".json")
        df = None
        if p.exists() and meta_p.exists():
            meta = json.loads(meta_p.read_text())
            if meta["start"] <= start and meta["end"] >= end:
                df = pd.read_parquet(p)
        if df is None:
            try:
                df = self.ex.fetch_funding(self.symbols()[symbol], start, end)
            except Exception as e:  # noqa: BLE001
                log.warning("Không tải được funding %s: %s", symbol, e)
                df = pd.DataFrame({"ts": pd.Series(dtype="int64"), "rate": pd.Series(dtype="float64")})
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                df.to_parquet(p, index=False)
                meta_p.write_text(json.dumps({"start": start, "end": min(end, now_ms() - HOUR_MS)}))
        self._funding[symbol] = df
        return df
