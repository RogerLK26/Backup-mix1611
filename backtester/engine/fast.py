"""Đánh giá nhanh hàng trăm – hàng nghìn tổ hợp chốt lời / cắt lỗ / thời gian giữ lệnh.

Điểm vào lệnh không phụ thuộc TP, SL hay thời gian giữ lệnh. Vì vậy với mỗi tín hiệu ta tính MỘT lần:
  * thời điểm + giá giá chạm từng mức TP trong lưới,
  * thời điểm + giá giá chạm từng mức SL (hoặc giá thanh lý nếu đến trước),
  * giá đóng lệnh tại từng mốc hết thời gian giữ,
theo đúng quy tắc của `Simulator.exit` (tick tới mili-giây, thiếu tick thì dùng nến 1m).
Mỗi tổ hợp chỉ cần chọn sự kiện đến trước rồi chạy lại sổ lệnh (giới hạn rủi ro, số dư, thời gian nghỉ)
-> nhanh hơn chạy lại toàn bộ engine hàng trăm lần, kết quả lệnh trùng với engine chính
(chỉ không tính cờ "lệnh mơ hồ").
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from ..config import Config
from ..utils import DAY_MS, MIN_MS, now_ms
from .backtest import Backtester, Book
from .sim import EntryFill

BIG = 2 ** 62
R_TP, R_SL, R_LIQ, R_TO = 0, 1, 2, 3
REASONS = ("TP", "SL", "LIQ", "TIMEOUT")


@dataclass
class ExitAxis:
    """Các giá trị cần thử (đơn vị như trong cấu hình; None = không đặt)."""
    tps: list            # exit.take_profit
    sls: list            # exit.stop_loss
    holds: list          # exit.max_hold, đã quy đổi ra ms


class FastBook(Book):
    """Sổ lệnh dùng bộ nhớ đệm chung cho khoá ngày & điều kiện thời gian (giống hệt Book về logic)."""

    def __init__(self, cfg: Config, dk_cache: dict, tg_cache: dict):
        super().__init__(cfg)
        self._dk = dk_cache
        self._tg = tg_cache

    def dkey(self, t: int) -> str:
        # mọi múi giờ đều lệch bội số 15 phút -> khoá ngày không đổi trong mỗi khối 15 phút
        b = t // 900_000
        v = self._dk.get(b)
        if v is None:
            v = self._dk[b] = super().dkey(t)
        return v

    def time_gate(self, t: int) -> str | None:
        if t in self._tg:
            return self._tg[t]
        v = self._tg[t] = super().time_gate(t)
        return v


class FastEvaluator:
    """Tính trước sự kiện thoát lệnh cho một tập tín hiệu, rồi đánh giá từng tổ hợp (tp, sl, hold)."""

    def __init__(self, bt: Backtester, candidates: list, axis: ExitAxis):
        self.bt = bt
        self.cfg = cfg = bt.cfg
        self.axis = axis
        self.rejected = Counter()
        self._dk: dict = {}
        self._tg: dict = {}
        self._tg_key = None
        self._dk_key = None
        self.horizon = now_ms() - 2 * MIN_MS

        items = []
        candidates = bt.prepare_candidates(candidates, self.rejected)   # chờ giá hồi: xếp theo thời điểm hồi
        for c in candidates:
            ok, mc = bt._mc_pass(c.symbol, c.t, c.tf_open)
            if not ok:
                self.rejected["market_cap"] += 1
                continue
            c.mc = mc  # type: ignore[attr-defined]
            c.max_lev = None  # type: ignore[attr-defined]
            if cfg.min_max_leverage:   # giống Backtester.run: đòn bẩy tối đa sàn cho phép tại thời điểm tín hiệu
                lev, _ = bt.store.max_leverage(c.symbol, c.t)
                c.max_lev = lev  # type: ignore[attr-defined]
                if lev is not None and lev < cfg.min_max_leverage:
                    self.rejected["max_leverage"] += 1
                    continue
            e = bt.sim.entry(c)
            if e is None:
                self.rejected["no_data"] += 1
                continue
            items.append((e.signal_t, c.symbol, c, e))
        # đúng thứ tự xử lý của Backtester.run (theo thời điểm tín hiệu tới mili-giây, rồi theo coin)
        items.sort(key=lambda x: (x[0], x[1]))
        self.items = items
        tz = ZoneInfo(cfg.timezone)
        self.entry_hour = [datetime.fromtimestamp(e.fill_t / 1000, tz=tz).hour for _, _, _, e in items]
        n, ntp, nsl, nh = len(items), len(axis.tps), len(axis.sls), len(axis.holds)

        def mats(k):
            return (np.full((n, k), BIG, np.int64), np.full((n, k), BIG, np.int64), np.zeros((n, k), np.int64),
                    np.zeros((n, k)), np.zeros((n, k)), np.zeros((n, k)), np.zeros((n, k), bool))

        # order (so sánh cái nào trước), vkey (hợp lệ nếu < deadline), t, lãi thô / phí / funding trên 1 USDT
        # notional, có dùng tick không
        self.TPo, self.TPv, self.TPt, self.TPg, self.TPf, self.TPu, self.TPr = mats(ntp)
        self.STo, self.STv, self.STt, self.STg, self.STf, self.STu, self.STr = mats(nsl)
        self.STreason = np.full((n, nsl), R_SL, np.int8)
        _, _, self.TOt, self.TOg, self.TOf, self.TOu, self.TOr = mats(nh)
        # cho báo cáo chi tiết: giá thoát thực tế, mức TP, mức dừng lỗ (SL hoặc giá thanh lý), giá thanh lý
        self.TPx, self.STx, self.TOx = np.zeros((n, ntp)), np.zeros((n, nsl)), np.zeros((n, nh))
        self.TPl, self.STl = np.full((n, ntp), np.nan), np.full((n, nsl), np.nan)
        self.liq = np.full(n, np.nan)
        self.n_candidates = len(candidates)
        self.D = np.zeros((n, nh), np.int64)
        self.entry_px = np.zeros(n)
        self.entry_tick = np.zeros(n, bool)
        for idx, (_, _, c, e) in enumerate(items):
            self._precompute(idx, c, e)

    # ------------------------------------------------------------------ tính trước
    def _bars(self, sym: str, a: int, b: int):
        """Nến 1m có ts trong [a, b), ghép qua nhiều ngày."""
        parts = []
        d = a - a % DAY_MS
        st = self.bt.store
        while d < b:
            bars = st.day_bars(sym, d)
            i0 = np.searchsorted(bars.ts, a, "left")
            i1 = np.searchsorted(bars.ts, min(b, d + DAY_MS), "left")
            if i1 > i0:
                parts.append((bars.ts[i0:i1], bars.o[i0:i1], bars.h[i0:i1], bars.l[i0:i1], bars.c[i0:i1]))
            d += DAY_MS
        if not parts:
            e = np.zeros(0)
            return np.zeros(0, np.int64), e, e, e, e
        if len(parts) == 1:
            return parts[0]
        return tuple(np.concatenate([p[k] for p in parts]) for k in range(5))

    def _precompute(self, idx: int, c, e: EntryFill):
        cfg, sim, ax = self.cfg, self.bt.sim, self.axis
        sym, side = c.symbol, c.side
        lev = cfg.leverage
        unit_div = lev if cfg.tp_sl_unit == "roe" else 1.0
        entry_px = e.raw_px * (1 + side * cfg.entry_slippage_pct / 100.0)
        self.entry_px[idx] = entry_px
        self.entry_tick[idx] = e.resolution == "tick"
        mmr = cfg.maintenance_margin_pct / 100.0
        liq_px = entry_px * (1 - side * (1.0 / lev - mmr)) if cfg.liquidation else None
        if liq_px is not None:
            self.liq[idx] = liq_px
        tp_limit = cfg.tp_order == "limit"

        # deadline cho từng thời gian giữ lệnh (giống Backtester._open_trade)
        deadlines = []
        for hold in ax.holds:
            dl = e.fill_t + hold if hold else cfg.end_ms
            deadlines.append(min(max(dl, e.fill_t + 1), self.horizon))
        self.D[idx] = deadlines
        dmax = max(deadlines)

        # ---- dữ liệu dùng chung cho mọi mức giá
        t0 = e.fill_t
        m0 = t0 - t0 % MIN_MS
        m0_end = m0 + MIN_MS
        v1, pts1 = None, None
        if e.resolution == "tick":
            v1 = sim._ticks(sym, t0 + 1, m0_end)
        elif getattr(c, "setup_t", None) is not None:
            pass  # giống sim.exit: vào lệnh khi giá hồi, chỉ có nến 1m -> quét từ phút sau
        elif c.trigger == "touch":
            bar = sim._bar(sym, m0)
            if bar is not None:
                pts1 = sim._entry_bar_path(bar, side, c.dir_sign, c.threshold)
        else:
            m0_end = m0
        bts, bo, bh, bl, bc = self._bars(sym, m0_end, dmax)
        tick_cache: dict[int, tuple | None] = {}

        def bar_ticks(bt_):
            if bt_ not in tick_cache:
                tick_cache[bt_] = sim._ticks(sym, bt_, bt_ + MIN_MS)
            return tick_cache[bt_]

        def first_hit(level: float, up: bool, strict: bool, reason: str):
            """Lần chạm đầu tiên của MỘT mức giá -> (order, vkey, t, raw_px, tick?) hoặc None."""
            # 1) phần còn lại của phút vào lệnh
            if v1 is not None and len(v1[0]):
                ts, px = v1
                m = (px > level if strict else px >= level) if up else (px < level if strict else px <= level)
                if m.any():
                    i = int(np.argmax(m))
                    t = int(ts[i])
                    return t * 8, t, t, sim._exit_price(reason, level, float(px[i]), tp_limit), True
            elif pts1 is not None:
                for k, p in enumerate(pts1):
                    if (p >= level) if up else (p <= level):
                        t = t0 + (m0_end - t0) * (k + 1) // (len(pts1) + 1)
                        # sim.exit không kiểm tra deadline trong nến vào lệnh -> luôn hợp lệ
                        return t * 8 + k, -1, t, sim._exit_price(reason, level, level, tp_limit), False
            # 2) các nến 1m tiếp theo
            if len(bts) == 0:
                return None
            mask = bh >= level if up else bl <= level
            for i in np.flatnonzero(mask):
                bt_ = int(bts[i])
                v = bar_ticks(bt_)
                if v is not None and len(v[0]):
                    ts, px = v
                    m = (px > level if strict else px >= level) if up else (px < level if strict else px <= level)
                    if m.any():
                        j = int(np.argmax(m))
                        t = int(ts[j])
                        return t * 8, t, t, sim._exit_price(reason, level, float(px[j]), tp_limit), True
                    continue
                pts = sim._bar_path(bo[i], bh[i], bl[i], bc[i], side)
                for k, p in enumerate(pts):
                    if (p >= level) if up else (p <= level):
                        rpx = p if k == 0 else level      # mở nến đã vượt mức -> khớp tại giá mở (gap)
                        et = bt_ + (MIN_MS * k) // len(pts) if k else bt_
                        return et * 8 + k, bt_, et, sim._exit_price(reason, level, rpx, tp_limit), False
            return None

        tp_up = side == 1
        strict_tp = cfg.tp_fill == "through"
        for i, tp in enumerate(ax.tps):
            if not tp:
                continue
            level = entry_px * (1 + side * tp / unit_div / 100.0)
            self.TPl[idx, i] = level
            hit = first_hit(level, tp_up, strict_tp, "TP")
            if hit:
                self.TPx[idx, i] = self._store(self.TPo, self.TPv, self.TPt, self.TPg, self.TPf, self.TPu, self.TPr,
                                               idx, i, hit, R_TP, c, e, entry_px)
        for j, sl in enumerate(ax.sls):
            if side == 1:
                level = entry_px * (1 - sl / unit_div / 100.0) if sl else -math.inf
                if liq_px is not None and liq_px >= level:
                    level, reason = liq_px, R_LIQ
                else:
                    reason = R_SL
            else:
                level = entry_px * (1 + sl / unit_div / 100.0) if sl else math.inf
                if liq_px is not None and liq_px <= level:
                    level, reason = liq_px, R_LIQ
                else:
                    reason = R_SL
            if math.isinf(level):
                continue
            self.STl[idx, j] = level
            hit = first_hit(level, not tp_up, False, REASONS[reason])
            if hit:
                self.STreason[idx, j] = reason
                self.STx[idx, j] = self._store(self.STo, self.STv, self.STt, self.STg, self.STf, self.STu, self.STr,
                                               idx, j, hit, reason, c, e, entry_px)
        # 3) hết thời gian giữ lệnh -> đóng lệnh thị trường (giống bước 3 của Simulator.exit)
        for k, dl in enumerate(deadlines):
            nxt = sim._first_tick_at(sym, dl) if sim.use_ticks else None
            if nxt is not None:
                t, raw, tick = nxt[0], nxt[1], True
            else:
                tick = False
                bar = sim._bar(sym, dl - dl % MIN_MS)
                if bar is not None and dl % MIN_MS == 0:
                    t, raw = dl, float(bar[0])
                else:
                    px = sim._last_close_before(sym, dl - dl % MIN_MS + MIN_MS)
                    t, raw = dl, (entry_px if px is None else px)
            g, f, u, px = self._units(R_TO, raw, t, c, e, entry_px)
            self.TOt[idx, k], self.TOg[idx, k], self.TOf[idx, k], self.TOu[idx, k], self.TOr[idx, k] = t, g, f, u, tick
            self.TOx[idx, k] = px

    def _units(self, reason: int, raw: float, t: int, c, e: EntryFill, entry_px: float):
        """(lãi thô, phí thoát, funding) trên 1 USDT giá trị vị thế, kèm giá thoát sau trượt giá."""
        cfg = self.cfg
        side = c.side
        if reason == R_SL:
            px, rate = raw * (1 - side * cfg.sl_slippage_pct / 100.0), cfg.taker_pct
        elif reason == R_TP and cfg.tp_order == "limit":
            px, rate = raw, cfg.maker_pct
        elif reason == R_LIQ:
            px, rate = raw, 0.0
        else:
            px, rate = raw * (1 - side * cfg.exit_slippage_pct / 100.0), cfg.taker_pct
        g = -1.0 / cfg.leverage if reason == R_LIQ else side * (px / entry_px - 1.0)
        fee = px / entry_px * rate / 100.0
        fund = self.bt._funding(c.symbol, side, 1.0 / entry_px, e.fill_t, t) if cfg.funding else 0.0
        return g, fee, fund, px

    def _store(self, O, V, T, G, F, U, Rz, idx, col, hit, reason, c, e, entry_px):
        order, vkey, t, raw, tick = hit
        g, f, u, px = self._units(reason, raw, t, c, e, entry_px)
        O[idx, col], V[idx, col], T[idx, col], G[idx, col], F[idx, col], U[idx, col], Rz[idx, col] = \
            order, vkey, t, g, f, u, tick
        return px

    # ------------------------------------------------------------------ đánh giá một tổ hợp
    def run(self, cfg: Config, i: int, j: int, k: int, full: bool = False):
        """Chạy sổ lệnh cho tổ hợp (tp=i, sl=j, hold=k) với giới hạn rủi ro của `cfg`.
        full=True: bản ghi lệnh đủ các cột như Backtester (để xuất báo cáo chi tiết).
        Trả về (Book đã đóng hết lệnh, số tín hiệu bị bỏ)."""
        tg_key = (tuple(cfg.sessions), tuple(sorted(cfg.weekdays)), cfg.max_runtime_ms, cfg.start_ms, cfg.end_ms,
                  cfg.timezone)
        if tg_key != self._tg_key:
            self._tg, self._tg_key = {}, tg_key
        dk_key = (cfg.timezone, cfg.day_reset_hour)
        if dk_key != self._dk_key:
            self._dk, self._dk_key = {}, dk_key
        D = self.D[:, k]
        ot = np.where(self.TPv[:, i] < D, self.TPo[:, i], BIG)
        os_ = np.where(self.STv[:, j] < D, self.STo[:, j], BIG)
        use_tp = ot < os_
        use_st = ~use_tp & (os_ < BIG)
        reason = np.where(use_tp, R_TP, np.where(use_st, self.STreason[:, j], R_TO))
        ext = np.where(use_tp, self.TPt[:, i], np.where(use_st, self.STt[:, j], self.TOt[:, k])).tolist()
        g = np.where(use_tp, self.TPg[:, i], np.where(use_st, self.STg[:, j], self.TOg[:, k])).tolist()
        f = np.where(use_tp, self.TPf[:, i], np.where(use_st, self.STf[:, j], self.TOf[:, k])).tolist()
        u = np.where(use_tp, self.TPu[:, i], np.where(use_st, self.STu[:, j], self.TOu[:, k])).tolist()
        xt = np.where(use_tp, self.TPr[:, i], np.where(use_st, self.STr[:, j], self.TOr[:, k])).tolist()
        reason = reason.tolist()
        etick = self.entry_tick.tolist()
        if full:
            xp = np.where(use_tp, self.TPx[:, i], np.where(use_st, self.STx[:, j], self.TOx[:, k])).tolist()
            tpl, stl, liq = self.TPl[:, i].tolist(), self.STl[:, j].tolist(), self.liq.tolist()

        book = FastBook(cfg, self._dk, self._tg)
        rejected = Counter()
        lev, taker = cfg.leverage, cfg.taker_pct / 100.0
        for n, (sig_t, sym, c, e) in enumerate(self.items):
            book.settle(sig_t)
            r = book.gate(c, sig_t)
            if r:
                rejected[r] += 1
                continue
            margin = book.margin_needed()
            notional = margin * lev
            gross = notional * g[n]
            exit_fee = notional * f[n]
            funding = notional * u[n]
            entry_fee = notional * taker
            res = "tick" if (etick[n] and xt[n]) else ("mixed" if (etick[n] or xt[n]) else "1m")
            net = gross - entry_fee - exit_fee + funding
            rec = {
                "symbol": sym, "entry_t": e.fill_t, "exit_t": ext[n], "reason": REASONS[reason[n]],
                "margin": margin, "gross_pnl": gross, "entry_fee": entry_fee, "exit_fee": exit_fee,
                "funding": funding, "net_pnl": net,
                "hold_s": (ext[n] - e.fill_t) / 1000.0, "resolution": res, "hour": self.entry_hour[n],
            }
            if full:
                ep = float(self.entry_px[n])
                nan = lambda v: None if v != v else v  # noqa: E731
                rec.update({
                    "side": "LONG" if c.side == 1 else "SHORT", "rule": c.rule_idx, "trigger": c.trigger,
                    "move_pct": round(c.change_pct, 4), "candle_t": c.candle_ts, "signal_t": e.signal_t,
                    "setup_t": getattr(c, "setup_t", None) or e.signal_t,
                    "entry_px": ep, "tp_px": nan(tpl[n]), "sl_px": nan(stl[n]), "liq_px": nan(liq[n]),
                    "exit_px": xp[n], "notional": notional, "qty": notional / ep, "roe_pct": net / margin * 100.0,
                    "vol24": c.vol24, "mc": getattr(c, "mc", math.nan), "max_lev": getattr(c, "max_lev", None),
                    "ambiguous": False,   # engine nhanh không đánh dấu lệnh mơ hồ (chỉ xảy ra khi thiếu tick)
                })
            book.open(rec)
        book.settle_all()
        rejected.update(self.rejected)
        return book, rejected


def hour_vectors(closed: list[dict]):
    """Theo giờ vào lệnh 0–23: (lãi/lỗ, số lệnh, số lệnh lãi) – mảng 24 phần tử, gọn để lưu cho hàng nghìn chiến lược."""
    n = len(closed)
    if not n:
        return np.zeros(24, np.float32), np.zeros(24, np.int32), np.zeros(24, np.int32)
    h = np.fromiter((r["hour"] for r in closed), np.int64, n)
    net = np.fromiter((r["net_pnl"] for r in closed), float, n)
    return (np.bincount(h, weights=net, minlength=24).astype(np.float32),
            np.bincount(h, minlength=24).astype(np.int32),
            np.bincount(h, weights=(net > 0), minlength=24).astype(np.int32))


def hour_stats(hours, pnls) -> dict:
    """hours_pos: số giờ trong ngày (0–23, theo giờ vào lệnh) có tổng lãi > 0;
    top5h_pnl: tổng lãi/lỗ của 5 giờ có lãi cao nhất (trong các giờ có lệnh)."""
    by = {}
    for h, v in zip(hours, pnls):
        by[h] = by.get(h, 0.0) + v
    vals = sorted(by.values(), reverse=True)
    return {"hours_pos": sum(1 for v in vals if v > 0), "top5h_pnl": float(sum(vals[:5]))}


def quick_stats(closed: list[dict], initial: float, t_from: int | None = None, t_to: int | None = None) -> dict:
    """Chỉ số chính (cùng định nghĩa với stats.summarize) cho các lệnh có entry_t trong [t_from, t_to)."""
    if t_from is not None or t_to is not None:
        lo, hi = t_from or -BIG, t_to or BIG
        closed = [r for r in closed if lo <= r["entry_t"] < hi]
    n = len(closed)
    out = {"trades": n, "net_pnl": 0.0, "return_pct": 0.0, "win_rate_pct": 0.0, "profit_factor": 0.0,
           "max_drawdown": 0.0, "max_drawdown_pct": 0.0, "tp": 0, "sl": 0, "liquidations": 0, "timeouts": 0,
           "fees": 0.0, "expectancy_per_trade": 0.0, "avg_hold_s": 0.0, "wins": 0, "losses": 0,
           "hours_pos": 0, "top5h_pnl": 0.0}
    if not n:
        return out
    net = np.fromiter((r["net_pnl"] for r in closed), float, n)
    if t_from is None and t_to is None:
        # giống summarize: số dư thật sau mỗi lệnh đóng (đã trừ phí vào của các lệnh còn mở)
        eq = np.array([initial] + [r["balance_after"] for r in sorted(closed, key=lambda r: r["exit_t"])])
    else:
        # tập lệnh con: đường vốn tính lại từ vốn ban đầu
        eq = initial + np.concatenate([[0.0], np.cumsum(net)])
    peak = np.maximum.accumulate(eq)
    dd = peak - eq
    kk = int(np.argmax(dd))
    wins, losses = net[net > 0], net[net <= 0]
    reasons = Counter(r["reason"] for r in closed)
    out.update({
        "net_pnl": float(net.sum()),
        "return_pct": float(net.sum() / initial * 100) if initial else 0.0,
        "wins": int(len(wins)), "losses": int(len(losses)),
        "win_rate_pct": len(wins) / n * 100,
        "profit_factor": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else math.inf,
        "max_drawdown": float(dd[kk]),
        "max_drawdown_pct": float(dd[kk] / peak[kk] * 100) if peak[kk] > 0 else 0.0,
        "tp": reasons.get("TP", 0), "sl": reasons.get("SL", 0), "liquidations": reasons.get("LIQ", 0),
        "timeouts": reasons.get("TIMEOUT", 0),
        "fees": float(sum(r["entry_fee"] + r["exit_fee"] for r in closed)),
        "expectancy_per_trade": float(net.mean()),
        "avg_hold_s": float(sum(r["hold_s"] for r in closed) / n),
        **hour_stats([r["hour"] for r in closed], net.tolist()),
    })
    return out

COLUMNS = ["symbol", "side", "rule", "trigger", "move_pct", "candle_t", "setup_t", "signal_t", "entry_t", "entry_px", "tp_px",
           "sl_px", "liq_px", "exit_t", "exit_px", "reason", "hold_s", "margin", "notional", "qty", "gross_pnl",
           "entry_fee", "exit_fee", "funding", "net_pnl", "roe_pct", "vol24", "mc", "max_lev", "resolution", "ambiguous",
           "balance_after", "outcome"]


def fast_result(ev: FastEvaluator, cfg: Config, i: int, j: int, k: int, universe):
    """Kết quả đầy đủ (như Backtester.run) của một tổ hợp, dựng từ phần đã tính trước – dùng cho báo cáo chi tiết."""
    import pandas as pd
    from .backtest import Result
    book, rejected = ev.run(cfg, i, j, k, full=True)
    trades = pd.DataFrame(book.closed)
    if len(trades):
        trades = trades[COLUMNS].sort_values(["entry_t", "symbol"]).reset_index(drop=True)
    stats = {"tick_chunks_loaded": ev.bt.store.stats.get("tick_chunks_loaded", 0), "engine": "fast"}
    return Result(cfg, trades, rejected, book.equity, universe, ev.n_candidates, stats)
