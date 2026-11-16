"""Bộ điều phối backtest: chuẩn bị dữ liệu, quét tín hiệu, áp dụng giới hạn rủi ro, mô phỏng lệnh."""
from __future__ import annotations

import heapq
import math
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ..config import Config
from ..data.marketcap import MarketCap
from ..data.store import DataStore
from ..progress import PROGRESS
from ..utils import DAY_MS, MIN_MS, days_between, fmt_ms, local_day_key, log, now_ms, utc_date
from .signals import Candidate, scan_symbol
from .sim import EntryFill, Simulator, make_levels


# ============================================================================ chuẩn bị dữ liệu
@dataclass
class Universe:
    symbols: list[str]
    days: dict[str, set[int]]                  # ngày được phép giao dịch (đã qua lọc sơ bộ)
    coin_ids: dict[str, str | None] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    supply: dict[str, tuple[float, float]] = field(default_factory=dict)   # (lượng cung, hệ số hợp đồng)


def prepare_universe(cfg: Config, store: DataStore, mcap: MarketCap | None) -> Universe:
    syms = store.symbols()
    notes = []
    if cfg.symbols:
        chosen = []
        for s in cfg.symbols:
            si = store.resolve(s)
            if si is None:
                notes.append(f"Không tìm thấy {s} trên {cfg.exchange}")
            else:
                chosen.append(si.symbol)
    else:
        chosen = list(syms)
    excl = {store.resolve(x).symbol for x in cfg.exclude if store.resolve(x)}
    if cfg.symbols:
        # coin được chỉ định rõ ở "Chỉ test các coin" được ưu tiên hơn danh sách loại trừ
        kept = sorted(set(chosen) & excl)
        if kept:
            notes.append(f"{', '.join(kept)} nằm trong danh sách loại trừ nhưng được chọn ở 'Chỉ test các coin' – vẫn được test")
        chosen = sorted(set(chosen))
    else:
        chosen = sorted(set(chosen) - excl)
    days = days_between(cfg.start_ms, cfg.end_ms)
    allowed: dict[str, set[int]] = {s: set(days) for s in chosen}
    log.info("Universe ban đầu: %d coin, %d ngày", len(chosen), len(days))

    need_mc = bool(cfg.min_market_cap or cfg.max_market_cap) and cfg.market_cap_source != "none" and mcap is not None
    supply_mode = need_mc and cfg.market_cap_source == "supply"

    # --- dữ liệu ngày (volume USDT + giá đóng cửa) cho lọc sơ bộ
    daily: dict[str, dict[int, tuple[float, float]]] = {}
    if cfg.min_volume_24h or supply_mode:
        def job(s):
            try:
                df = store.daily_qvol(syms[s], days[0] - 2 * DAY_MS, days[-1] + 2 * DAY_MS)
            except Exception as e:  # noqa: BLE001 - coin lỗi/ngừng giao dịch: bỏ qua
                log.debug("Bỏ qua %s (dữ liệu ngày lỗi: %s)", s, e)
                return s, {}
            return s, {int(t): (float(q), float(c)) for t, q, c in zip(df["ts"], df["qv"], df["c"])}

        log.info("Tải volume ngày cho %d coin...", len(chosen))
        PROGRESS.set_stage("Tải volume ngày", len(chosen))
        cnt = [0]

        def job2(s):
            if PROGRESS.cancel.is_set():
                return s, {}
            r = job(s)
            cnt[0] += 1
            PROGRESS.done = cnt[0]
            return r

        with ThreadPoolExecutor(cfg.workers) as pool:
            daily = dict(pool.map(job2, chosen))
        PROGRESS.check()

    # --- lọc sơ bộ theo volume: vol24h tại mọi thời điểm của ngày d <= vol(d-1)+vol(d)(+vol(d+1) nếu lệch múi giờ)
    if cfg.min_volume_24h:
        for s in chosen:
            dq = daily.get(s, {})
            allowed[s] = {d for d in days if sum(dq.get(d + k * DAY_MS, (0, 0))[0] for k in (-1, 0, 1))
                          >= cfg.min_volume_24h}
        log.info("Sau lọc volume sơ bộ: %d coin còn ngày hợp lệ", sum(1 for s in chosen if allowed[s]))

    # --- market cap
    coin_ids: dict[str, str | None] = {}
    supply: dict[str, tuple[float, float]] = {}
    if need_mc:
        unknown = []
        for s in chosen:
            if not allowed[s]:
                continue
            cid, _ = mcap.coin(syms[s].base)
            coin_ids[s] = cid
            if supply_mode and cid is not None:
                supply[s] = mcap.supply(syms[s].base)
                if math.isnan(supply[s][0]):
                    cid = None
            if cid is None:
                unknown.append(s)
                if cfg.unknown_market_cap == "exclude":
                    allowed[s] = set()
        if unknown:
            notes.append(f"{len(unknown)} coin không xác định được market cap ({cfg.unknown_market_cap}): "
                         + ", ".join(unknown[:30]) + (" ..." if len(unknown) > 30 else ""))
        if supply_mode:
            # lọc lỏng (x2) theo giá đóng cửa ngày; kiểm tra chính xác tại từng tín hiệu
            for s in chosen:
                if not allowed[s] or s not in supply:
                    continue
                sup, mult = supply[s]
                dq = daily.get(s, {})
                keep = set()
                for d in allowed[s]:
                    px = [dq[x][1] for x in (d - DAY_MS, d) if x in dq and dq[x][1] > 0]
                    if not px:
                        keep.add(d)
                        continue
                    lo, hi = sup * min(px) / mult / 2, sup * max(px) / mult * 2
                    if (not cfg.min_market_cap or hi >= cfg.min_market_cap) and                             (not cfg.max_market_cap or lo <= cfg.max_market_cap):
                        keep.add(d)
                allowed[s] = keep
        elif cfg.market_cap_source == "coingecko":
            todo = [s for s in chosen if allowed[s] and coin_ids.get(s)]
            log.info("Tải market cap lịch sử cho %d coin (CoinGecko, có thể chậm nếu không có API key)...", len(todo))
            PROGRESS.set_stage("Tải market cap (CoinGecko)", len(todo))
            for k, s in enumerate(todo, 1):
                PROGRESS.step(k - 1)
                cid = coin_ids[s]
                keep = set()
                for d in allowed[s]:
                    vals = [mcap.at(cid, t, cfg.start_ms, cfg.end_ms) for t in (d, d + DAY_MS - 1)]
                    vals = [v for v in vals if not math.isnan(v)]
                    if not vals:
                        if cfg.unknown_market_cap == "include":
                            keep.add(d)
                        continue
                    if any(_mc_ok(cfg, v) for v in vals):
                        keep.add(d)
                allowed[s] = keep
                if k % 20 == 0:
                    log.info("  market cap: %d/%d", k, len(todo))
        else:  # current
            for s in chosen:
                if allowed[s] and coin_ids.get(s):
                    v = mcap.current(syms[s].base)
                    if math.isnan(v) or not _mc_ok(cfg, v):
                        allowed[s] = set()
        log.info("Sau lọc market cap sơ bộ: %d coin", sum(1 for s in chosen if allowed[s]))

    # --- đòn bẩy tối đa sàn cho phép (sàn chỉ công bố mức hiện tại -> lưu bản ghi theo ngày)
    if cfg.min_max_leverage:
        snaps = store.leverage_snapshots()
        if not snaps:
            notes.append("Không lấy được đòn bẩy tối đa từ sàn – bỏ qua điều kiện đòn bẩy")
        else:
            low, unknown = [], []
            for s in chosen:
                if not allowed[s]:
                    continue
                vals = [d[s][0] for _, d in snaps if s in d]
                if not vals:
                    unknown.append(s)
                elif max(vals) < cfg.min_max_leverage:
                    low.append(s)
                    allowed[s] = set()
            days = ", ".join(sorted({utc_date(d) for d, _ in snaps}))
            notes.append(f"Đòn bẩy tối đa lấy theo dữ liệu sàn ngày {days} (sàn không công bố lịch sử; từ nay mỗi ngày "
                         f"chạy sẽ lưu thêm 1 bản ghi). Loại {len(low)} coin < x{cfg.min_max_leverage:g}"
                         + (": " + ", ".join(low[:30]) + (" ..." if len(low) > 30 else "") if low else ""))
            if unknown:
                notes.append(f"{len(unknown)} coin không có dữ liệu đòn bẩy (vẫn giao dịch): " + ", ".join(unknown[:20]))
            log.info("Sau lọc đòn bẩy tối đa >= x%g: %d coin", cfg.min_max_leverage,
                     sum(1 for s in chosen if allowed[s]))

    final = [s for s in chosen if allowed[s]]
    return Universe(final, {s: allowed[s] for s in final}, coin_ids, notes, supply)


def _mc_ok(cfg: Config, v: float) -> bool:
    if cfg.min_market_cap and v < cfg.min_market_cap:
        return False
    if cfg.max_market_cap and v > cfg.max_market_cap:
        return False
    return True


def load_klines(cfg: Config, store: DataStore, uni: Universe):
    """Tải nến 1m cho ngày hợp lệ + 1 ngày trước đó (để tính volume 24h)."""
    plan = {}
    for s in uni.symbols:
        ds = set()
        for d in uni.days[s]:
            ds.add(d)
            ds.add(d - DAY_MS)
        plan[s] = sorted(ds)
    total = sum(len(v) for v in plan.values())
    log.info("Tải nến 1m: %d coin, %d coin-ngày (có cache sẽ bỏ qua)", len(plan), total)
    store.ensure_klines(plan)


def scan_candidates(cfg: Config, store: DataStore, uni: Universe) -> list[Candidate]:
    PROGRESS.set_stage("Quét tín hiệu")
    out: list[Candidate] = []
    for s in uni.symbols:
        days = store.loaded_days(s)
        if not days:
            continue
        # các dải ngày liên tục
        runs, cur = [], [days[0]]
        for d in days[1:]:
            if d == cur[-1] + DAY_MS:
                cur.append(d)
            else:
                runs.append(cur)
                cur = [d]
        runs.append(cur)
        for run in runs:
            parts = [store.day_bars(s, d) for d in run]
            ts = np.concatenate([p.ts for p in parts])
            if len(ts) == 0:
                continue
            arr = [np.concatenate([getattr(p, k) for p in parts]) for k in ("o", "h", "l", "c", "qv")]
            out.extend(scan_symbol(s, ts, *arr, cfg.signals, cfg.start_ms, cfg.end_ms, uni.days[s]))
    # lọc volume 24h chính xác tại thời điểm tín hiệu
    res = []
    for c in out:
        if cfg.min_volume_24h and c.vol24 < cfg.min_volume_24h:
            continue
        if cfg.max_volume_24h and c.vol24 > cfg.max_volume_24h:
            continue
        res.append(c)
    res.sort(key=lambda c: (c.t, c.symbol, c.rule_idx))
    return res


# ============================================================================ trạng thái rủi ro
@dataclass(order=True)
class OpenPos:
    exit_t: int
    seq: int
    rec: dict = field(compare=False)


class Book:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.balance = cfg.initial_balance
        self.locked = 0.0
        self.heap: list[OpenPos] = []
        self.open_by_coin: Counter = Counter()
        self.day = defaultdict(lambda: {"trades": 0, "tp": 0, "sl": 0, "pnl": 0.0})
        self.coin_day = defaultdict(lambda: {"tp": 0, "sl": 0})
        self.coin_cd: dict[str, int] = {}
        self.pause_until = 0
        self.pause_reason = ""
        self.consec_sl = 0
        self.closed: list[dict] = []
        self.equity: list[tuple[int, float]] = [(cfg.start_ms, cfg.initial_balance)]
        self._seq = 0

    def dkey(self, t: int) -> str:
        return local_day_key(t, self.cfg.timezone, self.cfg.day_reset_hour)

    # ---- đóng các lệnh có exit_t <= t
    def settle(self, t: int):
        while self.heap and self.heap[0].exit_t <= t:
            p = heapq.heappop(self.heap)
            self._close(p.rec)

    def settle_all(self):
        self.settle(2 ** 62)

    def _close(self, r: dict):
        cfg = self.cfg
        self.locked -= r["margin"]
        self.open_by_coin[r["symbol"]] -= 1
        # phí vào lệnh đã trừ lúc mở; giờ cộng PnL thô - phí thoát + funding
        self.balance += r["gross_pnl"] - r["exit_fee"] + r["funding"]
        r["balance_after"] = self.balance
        self.closed.append(r)
        self.equity.append((r["exit_t"], self.balance))

        t = r["exit_t"]
        dk = self.dkey(t)
        self.day[dk]["pnl"] += r["net_pnl"]
        reason = r["reason"]
        win = r["net_pnl"] > 0
        if reason in ("SL", "LIQ"):
            outcome = "sl"
        elif reason == "TP":
            outcome = "tp"
        elif cfg.timeout_counts_as == "pnl":
            outcome = "tp" if win else "sl"
        else:
            outcome = None
        r["outcome"] = outcome or ""
        sym = r["symbol"]
        if outcome == "tp":
            self.day[dk]["tp"] += 1
            self.coin_day[(dk, sym)]["tp"] += 1
            self.consec_sl = 0
            if cfg.cooldown_after_tp_coin_ms:
                self.coin_cd[sym] = max(self.coin_cd.get(sym, 0), t + cfg.cooldown_after_tp_coin_ms)
        elif outcome == "sl":
            self.day[dk]["sl"] += 1
            self.coin_day[(dk, sym)]["sl"] += 1
            self.consec_sl += 1
            if cfg.cooldown_after_sl_coin_ms:
                self.coin_cd[sym] = max(self.coin_cd.get(sym, 0), t + cfg.cooldown_after_sl_coin_ms)
            if cfg.cooldown_after_sl_global_ms and t + cfg.cooldown_after_sl_global_ms > self.pause_until:
                self.pause_until = t + cfg.cooldown_after_sl_global_ms
                self.pause_reason = "global_sl_cooldown"
            if cfg.max_consecutive_sl and self.consec_sl >= cfg.max_consecutive_sl:
                self.consec_sl = 0
                if cfg.pause_after_consecutive_sl_ms:
                    until = t + cfg.pause_after_consecutive_sl_ms
                    if until > self.pause_until:
                        self.pause_until = until
                        self.pause_reason = "consecutive_sl_pause"
        if cfg.cooldown_after_trade_coin_ms:
            self.coin_cd[sym] = max(self.coin_cd.get(sym, 0), t + cfg.cooldown_after_trade_coin_ms)

    # ---- kiểm tra điều kiện được phép vào lệnh tại thời điểm t
    def gate(self, c: Candidate, t: int) -> str | None:
        return self.time_gate(t) or self.state_gate(c, t)

    def time_gate(self, t: int) -> str | None:
        """Điều kiện chỉ phụ thuộc thời điểm (thời gian backtest, lịch giao dịch)."""
        cfg = self.cfg
        if t < cfg.start_ms or t >= cfg.end_ms:
            return "outside_period"
        if cfg.max_runtime_ms and t >= cfg.start_ms + cfg.max_runtime_ms:
            return "max_runtime"
        lt = datetime.fromtimestamp(t / 1000, tz=ZoneInfo(cfg.timezone))
        if lt.weekday() not in cfg.weekdays:
            return "weekday"
        mod = lt.hour * 60 + lt.minute
        if not any((a <= mod < b) if a <= b else (mod >= a or mod < b) for a, b in cfg.sessions):
            return "session"
        return None

    def state_gate(self, c: Candidate, t: int) -> str | None:
        """Điều kiện phụ thuộc trạng thái sổ lệnh (lệnh mở, bộ đếm, thời gian nghỉ, số dư)."""
        cfg = self.cfg
        if t < self.pause_until:
            return self.pause_reason
        dk = self.dkey(t)
        day = self.day[dk]
        if cfg.max_trades_per_day is not None and day["trades"] >= cfg.max_trades_per_day:
            return "daily_max_trades"
        if cfg.max_tp_per_day is not None and day["tp"] >= cfg.max_tp_per_day:
            return "daily_max_tp"
        if cfg.max_sl_per_day is not None and day["sl"] >= cfg.max_sl_per_day:
            return "daily_max_sl"
        if cfg.daily_loss_limit is not None and day["pnl"] <= -abs(cfg.daily_loss_limit):
            return "daily_loss_limit"
        sym = c.symbol
        if self.coin_cd.get(sym, 0) > t:
            return "coin_cooldown"
        cd = self.coin_day[(dk, sym)]
        if cfg.max_tp_per_coin_per_day is not None and cd["tp"] >= cfg.max_tp_per_coin_per_day:
            return "coin_max_tp"
        if cfg.max_sl_per_coin_per_day is not None and cd["sl"] >= cfg.max_sl_per_coin_per_day:
            return "coin_max_sl"
        if self.open_by_coin[sym] >= cfg.max_open_per_coin:
            return "coin_position_open"
        if cfg.max_open_positions is not None and len(self.heap) >= cfg.max_open_positions:
            return "max_open_positions"
        if self.margin_needed() > self.balance - self.locked + 1e-12 or self.margin_needed() <= 0:
            return "insufficient_balance"
        return None

    def margin_needed(self) -> float:
        cfg = self.cfg
        if cfg.margin_mode == "percent":
            return self.balance * cfg.margin / 100.0
        return cfg.margin

    def may_unblock(self, sym: str, t0: int, t1: int) -> bool:
        """Trạng thái có thể nới lỏng trong (t0, t1)? (lệnh đóng / hết cooldown)"""
        if self.heap and self.heap[0].exit_t < t1:
            return True
        if t0 < self.pause_until < t1:
            return True
        cd = self.coin_cd.get(sym, 0)
        return t0 < cd < t1

    def open(self, rec: dict):
        self.locked += rec["margin"]
        self.balance -= rec["entry_fee"]
        self.open_by_coin[rec["symbol"]] += 1
        self.day[self.dkey(rec["entry_t"])]["trades"] += 1
        self._seq += 1
        heapq.heappush(self.heap, OpenPos(rec["exit_t"], self._seq, rec))


# ============================================================================ backtest
@dataclass
class Result:
    cfg: Config
    trades: pd.DataFrame
    rejected: Counter
    equity: list
    universe: Universe
    n_candidates: int
    sim_stats: dict


class Backtester:
    def __init__(self, cfg: Config, store: DataStore, mcap: MarketCap | None, universe: Universe):
        self.cfg = cfg
        self.store = store
        self.mcap = mcap
        self.uni = universe
        self.sim = Simulator(store, use_ticks=cfg.resolution != "1m", intrabar=cfg.intrabar,
                             latency_ms=cfg.latency_ms, tp_through=cfg.tp_fill == "through", rules=cfg.signals)
        self._mc_cache: dict[tuple[str, int], float] = {}

    def _mc(self, sym: str, t: int, px: float) -> float:
        cfg = self.cfg
        if self.mcap is None or cfg.market_cap_source == "none":
            return math.nan
        if cfg.market_cap_source == "supply":
            sup, mult = self.uni.supply.get(sym, (math.nan, 1.0))
            return sup * px / mult
        cid = self.uni.coin_ids.get(sym)
        if cid is None:
            return math.nan
        key = (sym, t - t % DAY_MS)
        if key not in self._mc_cache:
            if cfg.market_cap_source == "current":
                self._mc_cache[key] = self.mcap.current(self.store.symbols()[sym].base)
            else:
                self._mc_cache[key] = self.mcap.at(cid, t, cfg.start_ms, cfg.end_ms)
        return self._mc_cache[key]

    def _mc_pass(self, sym: str, t: int, px: float) -> tuple[bool, float]:
        cfg = self.cfg
        if not (cfg.min_market_cap or cfg.max_market_cap) or cfg.market_cap_source == "none":
            return True, math.nan
        v = self._mc(sym, t, px)
        if math.isnan(v):
            return cfg.unknown_market_cap == "include", v
        return _mc_ok(cfg, v), v

    def prepare_candidates(self, candidates: list[Candidate], rejected: Counter) -> list[Candidate]:
        """Tín hiệu có 'chờ giá hồi': xác định trước thời điểm giá hồi (chỉ phụ thuộc đường giá) và xếp lại ứng viên
        theo thời điểm đó, để giới hạn rủi ro được xét đúng thứ tự thời gian vào lệnh thật."""
        rules = self.cfg.signals
        if not any(r.pullback_pct for r in rules):
            return candidates
        out = []
        for c in candidates:
            if rules[c.rule_idx].pullback_pct:
                e = self.sim.entry(c)
                if e is None:
                    rejected["no_pullback" if getattr(c, "no_pullback", False) else "no_data"] += 1
                    continue
                c.t = e.signal_t
            out.append(c)
        out.sort(key=lambda c: (c.t, c.symbol, c.rule_idx))
        return out

    def run(self, candidates: list[Candidate]) -> Result:
        cfg = self.cfg
        book = Book(cfg)
        rejected: Counter = Counter()
        n_signals = len(candidates)
        candidates = self.prepare_candidates(candidates, rejected)
        lev = cfg.leverage
        tp_pct = cfg.take_profit / (lev if cfg.tp_sl_unit == "roe" else 1) if cfg.take_profit else None
        sl_pct = cfg.stop_loss / (lev if cfg.tp_sl_unit == "roe" else 1) if cfg.stop_loss else None
        mmr = cfg.maintenance_margin_pct / 100.0
        horizon = now_ms() - 2 * MIN_MS

        # nhóm ứng viên theo phút để xử lý đúng thứ tự thời gian chính xác trong phút
        groups: dict[int, list[Candidate]] = defaultdict(list)
        for c in candidates:
            groups[c.t - c.t % MIN_MS].append(c)

        n_done = 0
        PROGRESS.set_stage("Mô phỏng lệnh", len(groups))
        for gi, minute in enumerate(sorted(groups)):
            if gi % 20 == 0:
                PROGRESS.step(gi)
            grp = groups[minute]
            book.settle(minute)
            ready: list[tuple[int, Candidate, EntryFill]] = []
            for c in grp:
                ok_mc, mc = self._mc_pass(c.symbol, c.t, c.tf_open)
                if not ok_mc:
                    rejected["market_cap"] += 1
                    continue
                c.mc = mc  # type: ignore[attr-defined]
                c.max_lev = None  # type: ignore[attr-defined]
                if cfg.min_max_leverage:
                    lev, _ = self.store.max_leverage(c.symbol, c.t)
                    c.max_lev = lev  # type: ignore[attr-defined]
                    if lev is not None and lev < cfg.min_max_leverage:
                        rejected["max_leverage"] += 1
                        continue
                reason = book.gate(c, max(c.t, cfg.start_ms))
                if reason and not book.may_unblock(c.symbol, minute, minute + MIN_MS):
                    rejected[reason] += 1
                    continue
                e = self.sim.entry(c)
                if e is None:
                    rejected["no_data"] += 1
                    continue
                ready.append((e.signal_t, c, e))
            ready.sort(key=lambda x: (x[0], x[1].symbol))
            for sig_t, c, e in ready:
                book.settle(sig_t)
                reason = book.gate(c, sig_t)
                if reason:
                    rejected[reason] += 1
                    continue
                rec = self._open_trade(book, c, e, tp_pct, sl_pct, mmr, horizon)
                book.open(rec)
                n_done += 1
                if n_done % 200 == 0:
                    log.info("  đã mô phỏng %d lệnh (%s)", n_done, fmt_ms(sig_t, cfg.timezone))
        book.settle_all()

        trades = pd.DataFrame(book.closed)
        if len(trades):
            trades = trades.sort_values(["entry_t", "symbol"]).reset_index(drop=True)
        stats = {"tick_chunks_loaded": self.store.stats["tick_chunks_loaded"],
                 "tick_missing_lookups": self.sim.tick_missing}
        return Result(cfg, trades, rejected, book.equity, self.uni, n_signals, stats)

    def _open_trade(self, book: Book, c: Candidate, e: EntryFill, tp_pct, sl_pct, mmr, horizon) -> dict:
        cfg = self.cfg
        side = c.side
        slip = cfg.entry_slippage_pct / 100.0
        entry_px = e.raw_px * (1 + side * slip)
        margin = book.margin_needed()
        notional = margin * cfg.leverage
        qty = notional / entry_px
        liq_px = None
        if cfg.liquidation:
            liq_px = entry_px * (1 - side * (1.0 / cfg.leverage - mmr))
        lv = make_levels(side, entry_px, tp_pct, sl_pct, liq_px)
        deadline = e.fill_t + cfg.max_hold_ms if cfg.max_hold_ms else cfg.end_ms
        deadline = min(max(deadline, e.fill_t + 1), horizon)
        ex = self.sim.exit(c.symbol, side, e, entry_px, lv, deadline, c, cfg.tp_order == "limit")

        # giá thoát sau trượt giá
        if ex.reason == "SL":
            exit_px = ex.raw_px * (1 - side * cfg.sl_slippage_pct / 100.0)
            fee_rate = cfg.taker_pct
        elif ex.reason == "TP" and cfg.tp_order == "limit":
            exit_px = ex.raw_px
            fee_rate = cfg.maker_pct
        elif ex.reason == "LIQ":
            exit_px = ex.raw_px
            fee_rate = 0.0
        else:
            exit_px = ex.raw_px * (1 - side * cfg.exit_slippage_pct / 100.0)
            fee_rate = cfg.taker_pct
        if ex.reason == "LIQ":
            gross = -margin  # mất toàn bộ ký quỹ (isolated)
        else:
            gross = side * qty * (exit_px - entry_px)
        entry_fee = notional * cfg.taker_pct / 100.0
        exit_fee = qty * exit_px * fee_rate / 100.0
        funding = self._funding(c.symbol, side, qty, e.fill_t, ex.t) if cfg.funding else 0.0
        net = gross - entry_fee - exit_fee + funding
        resolution = "tick" if (e.resolution == "tick" and ex.resolution == "tick") else (
            "mixed" if "tick" in (e.resolution, ex.resolution) else "1m")
        return {
            "symbol": c.symbol,
            "side": "LONG" if side == 1 else "SHORT",
            "rule": c.rule_idx,
            "trigger": c.trigger,
            "move_pct": round(c.change_pct, 4),
            "candle_t": c.candle_ts,
            "setup_t": getattr(c, "setup_t", None) or e.signal_t,
            "signal_t": e.signal_t,
            "entry_t": e.fill_t,
            "entry_px": entry_px,
            "tp_px": None if math.isinf(lv.up if side == 1 else lv.down) else (lv.up if side == 1 else lv.down),
            "sl_px": (lv.down if side == 1 else lv.up) if not math.isinf(lv.down if side == 1 else lv.up) else None,
            "liq_px": liq_px,
            "exit_t": ex.t,
            "exit_px": exit_px,
            "reason": ex.reason,
            "hold_s": (ex.t - e.fill_t) / 1000.0,
            "margin": margin,
            "notional": notional,
            "qty": qty,
            "gross_pnl": gross,
            "entry_fee": entry_fee,
            "exit_fee": exit_fee,
            "funding": funding,
            "net_pnl": net,
            "roe_pct": net / margin * 100.0,
            "vol24": c.vol24,
            "mc": getattr(c, "mc", math.nan),
            "max_lev": getattr(c, "max_lev", None),
            "resolution": resolution,
            "ambiguous": ex.ambiguous,
        }

    def _funding(self, sym: str, side: int, qty: float, t0: int, t1: int) -> float:
        cfg = self.cfg
        if t0 // 3_600_000 == t1 // 3_600_000:
            return 0.0  # không qua mốc tròn giờ nào -> không thể có funding
        df = self.store.funding(sym, cfg.start_ms - DAY_MS, cfg.end_ms + 3 * DAY_MS)
        if df is None or len(df) == 0:
            return 0.0
        ts = df["ts"].to_numpy()
        # mốc funding của sàn có thể lệch vài ms -> làm tròn về giây
        tsr = ts - ts % 1000
        i, j = np.searchsorted(tsr, t0, "right"), np.searchsorted(tsr, t1, "right")
        total = 0.0
        for k in range(i, j):
            px = self.sim._last_close_before(sym, int(tsr[k]))
            if px is None:
                continue
            total += -side * qty * px * float(df["rate"].iloc[k])
        return total
