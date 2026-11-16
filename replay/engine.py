"""Chạy lại các lệnh thật với nhiều cấu hình thoát lệnh (TP / SL / thời gian giữ) trên cùng điểm vào lệnh.

Mỗi lệnh thật = 1 điểm vào lệnh (coin, chiều, đúng mili-giây, đúng giá). Với mỗi cấu hình, tìm tick đầu tiên chạm TP
hoặc SL (từ đường giá tick thật, xem paths.py), nếu không chạm trước hạn giữ lệnh thì đóng thị trường ở tick đầu tiên
sau hạn. Sau đó áp dụng chế độ vị thế:

  * single : mỗi coin chỉ 1 lệnh mở tại một thời điểm. Lệnh thật tiếp theo trên coin đó bị BỎ nếu lệnh (theo cấu hình
             mới) trước đó chưa đóng – đúng như bot thật.
  * multi  : cho phép nhiều lệnh mở đồng thời trên 1 coin (tối đa `max_per_coin`, 0 = không giới hạn) – mỗi tín hiệu
             đều được vào.

Lưu ý: tập điểm vào lệnh = các lệnh bot THẬT đã vào. Khi bot thật đang giữ lệnh trên 1 coin, các tín hiệu khác trên
coin đó không được ghi lại – nên cấu hình đóng lệnh nhanh hơn không thể "có thêm" những lệnh đó.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from .bt import MIN_MS
from .paths import PricePath

INF = np.int64(2 ** 62)
R_TP, R_SL, R_LIQ, R_TIMEOUT, R_OPEN, R_NA = 0, 1, 2, 3, 4, 5
REASONS = ["TP", "SL", "LIQ", "TIMEOUT", "OPEN", "NA"]


@dataclass
class Settings:
    tps: list[float] = field(default_factory=lambda: [1.0])          # % giá
    sls: list[float] = field(default_factory=lambda: [10.0])         # % giá, 0 = không SL
    holds: list[int] = field(default_factory=lambda: [120])          # phút, 0 = không giới hạn (tới hết cửa sổ)
    modes: list[str] = field(default_factory=lambda: ["single"])     # single | multi
    max_per_coin: int = 0            # chế độ multi: tối đa N lệnh mở / coin (0 = không giới hạn)
    max_total: int = 0               # tối đa N lệnh mở cùng lúc toàn tài khoản (0 = không giới hạn)
    cooldown_min: float = 0.0        # nghỉ coin sau khi đóng lệnh (phút)
    tp_order: str = "limit"          # limit (đúng giá TP, phí maker) | trigger (đúng giá TP, phí taker) | market
    tp_fill: str = "touch"           # touch (chạm là khớp) | through (giá phải vượt qua)
    entry_slip_pct: float = 0.0
    sl_slip_pct: float = 0.0
    exit_slip_pct: float = 0.0
    maker_pct: float = 0.02
    taker_pct: float = 0.055
    funding: bool = True
    sizing: str = "fixed"            # fixed (notional cố định) | actual (ký quỹ × đòn bẩy trong file)
    notional: float = 40.0           # USDT mỗi lệnh (fixed)
    leverage: float = 20.0           # dùng tính ký quỹ / ROE / thanh lý (fixed)
    liquidation: bool = False        # isolated: chạm giá thanh lý -> mất toàn bộ ký quỹ
    mmr_pct: float = 0.5
    skip_1m: bool = False            # bỏ lệnh không có tick (chỉ mô phỏng bằng nến 1m)
    auto_slip: bool = False          # tự ước lượng trượt giá SL / đóng hết giờ từ lệnh thật (calibrate)
    direction: str = "same"          # same (cùng hướng bot thật) | reverse (đảo Long <-> Short, cùng điểm & giá vào)

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        s = cls()
        for k, v in (d or {}).items():
            if hasattr(s, k) and v is not None:
                cur = getattr(s, k)
                if isinstance(cur, bool):
                    v = bool(v)
                elif isinstance(cur, float):
                    v = float(v)
                elif isinstance(cur, int) and not isinstance(cur, bool):
                    v = int(v)
                setattr(s, k, v)
        s.tps = sorted({float(x) for x in s.tps if float(x) > 0}) or [1.0]
        s.sls = sorted({float(x) for x in s.sls if float(x) >= 0}) or [0.0]
        s.holds = sorted({int(x) for x in s.holds if int(x) >= 0}) or [0]
        s.modes = [m for m in ("single", "multi") if m in s.modes] or ["single"]
        s.direction = "reverse" if s.direction == "reverse" else "same"
        return s

    def window_min(self, cap: int) -> int:
        return max([h for h in self.holds if h > 0] + ([cap] if 0 in self.holds else []) + [1])


# ============================================================================ mô phỏng 1 lệnh
def _first_up(px: np.ndarray, levels: np.ndarray, strict: bool) -> np.ndarray:
    """px tăng dần (đỉnh mới): chỉ số đầu tiên có px >= level (strict: >)."""
    return np.searchsorted(px, levels, "right" if strict else "left")


def _first_down(px: np.ndarray, levels: np.ndarray, strict: bool) -> np.ndarray:
    """px giảm dần (đáy mới): chỉ số đầu tiên có px <= level (strict: <)."""
    return np.searchsorted(-px, -levels, "right" if strict else "left")


@dataclass
class TradeSim:
    reason: np.ndarray      # (T,S,H) int8
    exit_t: np.ndarray      # (T,S,H) int64 (ms, tuyệt đối)
    exit_px: np.ndarray     # (T,S,H) float64
    pnl: np.ndarray         # (T,S,H) float64 (USDT, sau phí & funding)
    fee: np.ndarray
    funding: np.ndarray


def simulate_trade(p: PricePath, notional: float, margin: float, st: Settings,
                   tps: np.ndarray, sls: np.ndarray, holds: np.ndarray, window_min: int) -> TradeSim:
    T, S, H = len(tps), len(sls), len(holds)
    shape = (T, S, H)
    if not p.ok or not math.isfinite(p.entry_px) or p.entry_px <= 0:
        z = np.zeros(shape)
        return TradeSim(np.full(shape, R_NA, np.int8), np.full(shape, p.entry_t, np.int64), z, z, z, z)
    s = p.side
    e = p.entry_px * (1 + s * st.entry_slip_pct / 100.0)
    qty = notional / e
    strict = st.tp_fill == "through"

    # ---- TP: lần chạm đầu tiên của từng mức
    if s == -1:
        lt = e * (1 - tps / 100.0)
        i = _first_down(p.lo_px, lt, strict)
        rec_t, rec_px, rec_q = p.lo_t, p.lo_px, p.lo_q
    else:
        lt = e * (1 + tps / 100.0)
        i = _first_up(p.hi_px, lt, strict)
        rec_t, rec_px, rec_q = p.hi_t, p.hi_px, p.hi_q
    n = len(rec_t)
    hit = i < n
    ic = np.minimum(i, max(n - 1, 0))
    tp_t = np.where(hit, p.entry_t + rec_t[ic].astype(np.int64) if n else 0, INF)
    tp_q = np.where(hit, rec_q[ic] if n else 0, INF)
    tp_tick = np.where(hit, rec_px[ic] if n else np.nan, np.nan)

    # ---- SL (+ thanh lý nếu isolated): mức bất lợi
    adv_px, adv_t, adv_q = (p.hi_px, p.hi_t, p.hi_q) if s == -1 else (p.lo_px, p.lo_t, p.lo_q)
    na = len(adv_t)
    finder = _first_up if s == -1 else _first_down

    def first_adverse(levels):
        j = finder(adv_px, levels, False)
        ok = j < na
        jc = np.minimum(j, max(na - 1, 0))
        return (np.where(ok, p.entry_t + adv_t[jc].astype(np.int64) if na else 0, INF), np.where(ok, adv_q[jc] if na else 0, INF),
                np.where(ok, adv_px[jc] if na else np.nan, np.nan))

    has_sl = sls > 0
    ls = e * (1 - s * sls / 100.0)
    sl_t, sl_q, sl_tick = first_adverse(np.where(has_sl, ls, np.inf * -s))
    sl_t = np.where(has_sl, sl_t, INF)
    sl_q = np.where(has_sl, sl_q, INF)
    liq_lvl = None
    is_liq = np.zeros(S, bool)
    if st.liquidation:
        lev = notional / margin if margin > 0 else st.leverage
        liq_lvl = e * (1 - s * (1.0 / lev - st.mmr_pct / 100.0))
        lq_t, lq_q, _ = first_adverse(np.array([liq_lvl]))
        is_liq = lq_q[0] <= sl_q          # thanh lý trước (hoặc cùng tick) SL
        sl_t = np.where(is_liq, lq_t[0], sl_t)
        sl_q = np.where(is_liq, lq_q[0], sl_q)

    # ---- hạn giữ lệnh
    hk = np.where(holds > 0, holds, window_min).astype(np.int64)
    dl = p.entry_t + hk * MIN_MS                                 # (H,)
    tp_t3, tp_q3 = tp_t[:, None, None], tp_q[:, None, None]
    sl_t3, sl_q3 = sl_t[None, :, None], sl_q[None, :, None]
    dl3 = dl[None, None, :]
    tp_in = tp_t3 < dl3
    sl_in = sl_t3 < dl3
    tp_first = tp_in & (~sl_in | (tp_q3 < sl_q3))
    sl_first = sl_in & ~tp_first
    reason = np.full(shape, R_TIMEOUT, np.int8)
    reason[np.broadcast_to(sl_first, shape)] = R_SL
    reason[np.broadcast_to(sl_first & is_liq[None, :, None], shape)] = R_LIQ
    reason[np.broadcast_to(tp_first, shape)] = R_TP
    exit_t = np.broadcast_to(dl3, shape).copy()
    exit_t = np.where(sl_first, sl_t3, exit_t)
    exit_t = np.where(tp_first, tp_t3, exit_t)

    # ---- giá thoát
    to_px = p.samp[np.minimum(hk, len(p.samp) - 1)] if p.samp is not None and len(p.samp) else np.full(H, np.nan)
    to_px = to_px * (1 - s * st.exit_slip_pct / 100.0)
    if st.tp_order in ("limit", "trigger"):
        tp_px = lt
    else:
        tp_px = tp_tick * (1 - s * st.exit_slip_pct / 100.0)
    sl_px = sl_tick * (1 - s * st.sl_slip_pct / 100.0)
    exit_px = np.broadcast_to(to_px[None, None, :], shape).copy()
    exit_px = np.where(sl_first, sl_px[None, :, None], exit_px)
    if liq_lvl is not None:
        exit_px = np.where(sl_first & is_liq[None, :, None], liq_lvl, exit_px)
    exit_px = np.where(tp_first, tp_px[:, None, None], exit_px)

    # chưa đủ dữ liệu tới hạn giữ lệnh (vd lệnh rất gần hiện tại) -> OPEN
    timeout = reason == R_TIMEOUT
    open_ = timeout & ((dl3 > p.data_end) | ~np.isfinite(exit_px))
    reason[open_] = R_OPEN

    # ---- PnL
    gross = s * qty * (exit_px - e)
    fee_rate = np.where(reason == R_TP, st.maker_pct if st.tp_order == "limit" else st.taker_pct, st.taker_pct)
    fee_rate = np.where(reason == R_LIQ, 0.0, fee_rate)
    entry_fee = notional * st.taker_pct / 100.0
    exit_fee = qty * exit_px * fee_rate / 100.0
    gross = np.where(reason == R_LIQ, -margin, gross)
    fund = np.zeros(shape)
    if st.funding and p.fund_t is not None and len(p.fund_t):
        k = np.minimum((p.fund_t - p.entry_t) // MIN_MS, len(p.samp) - 1)
        fpx = np.where(np.isfinite(p.samp[k]), p.samp[k], e)
        cum = np.r_[0.0, np.cumsum(-s * qty * fpx * p.fund_rate)]
        fund = cum[np.searchsorted(p.fund_t, exit_t, "right")]
    fee = entry_fee + exit_fee
    pnl = gross - fee + fund
    pnl = np.where(reason == R_OPEN, np.nan, pnl)
    return TradeSim(reason, exit_t, exit_px, pnl, fee, fund)


# ============================================================================ chế độ vị thế
def take_mask(entry_t: np.ndarray, coin_idx: np.ndarray, exit_t: np.ndarray, valid: np.ndarray,
              mode: str, max_per_coin: int, max_total: int, cooldown_ms: int) -> np.ndarray:
    """entry_t (N,) đã sắp xếp; exit_t (N,C); valid (N,C) -> taken (N,C).
    Dùng 'khe' vị thế: mỗi coin K khe (single: K=1), toàn tài khoản G khe; lệnh vào được khi có khe trống."""
    N, C = exit_t.shape
    K = 1 if mode == "single" else (max_per_coin or 0)
    G = max_total or 0
    if K == 0 and G == 0:
        return valid.copy()
    taken = np.zeros((N, C), bool)
    ar = np.arange(C)
    ncoin = int(coin_idx.max()) + 1 if N else 0
    slots = np.full((ncoin, K, C), -INF, np.int64) if K else None
    gslots = np.full((G, C), -INF, np.int64) if G else None
    for i in range(N):
        v = valid[i]
        t = entry_t[i]
        ok = v.copy()
        if K:
            sl = slots[coin_idx[i]]
            j = np.argmin(sl, axis=0)
            ok &= sl[j, ar] <= t
        if G:
            gj = np.argmin(gslots, axis=0)
            ok &= gslots[gj, ar] <= t
        if not ok.any():
            continue
        taken[i] = ok
        x = exit_t[i]
        if K:
            sl[j[ok], ar[ok]] = x[ok] + cooldown_ms
        if G:
            gslots[gj[ok], ar[ok]] = x[ok]
    return taken


# ============================================================================ thống kê
def _max_dd(pnl_sorted: np.ndarray) -> float:
    if not len(pnl_sorted):
        return 0.0
    eq = np.r_[0.0, np.cumsum(pnl_sorted)]
    return float(np.max(np.maximum.accumulate(eq) - eq))


def stats_block(pnl: np.ndarray, reason: np.ndarray, exit_t: np.ndarray, entry_t: np.ndarray,
                taken: np.ndarray, margin: np.ndarray) -> dict:
    """Thống kê cho 1 cấu hình (vector 1 chiều)."""
    m = taken & (reason != R_OPEN) & (reason != R_NA)
    p = pnl[m]
    n = int(m.sum())
    wins = p[p > 0]
    loss = p[p <= 0]
    order = np.argsort(exit_t[m], kind="stable")
    dd = _max_dd(p[order])
    net = float(p.sum()) if n else 0.0
    gp, gl = float(wins.sum()), float(-loss.sum())
    rc = np.bincount(reason[m], minlength=6)
    hold = (exit_t[m] - entry_t[m]) / 60000.0
    return {
        "n": n,
        "skipped": int((~taken & (reason != R_NA)).sum()),
        "open": int((taken & (reason == R_OPEN)).sum()),
        "n_tp": int(rc[R_TP]), "n_sl": int(rc[R_SL]), "n_liq": int(rc[R_LIQ]), "n_timeout": int(rc[R_TIMEOUT]),
        "win_rate": float(len(wins) / n * 100) if n else 0.0,
        "net": net,
        "avg": net / n if n else 0.0,
        "pf": gp / gl if gl > 0 else (math.inf if gp > 0 else 0.0),
        "max_dd": dd,
        "ret_dd": net / dd if dd > 0 else (math.inf if net > 0 else 0.0),
        "avg_roe": float(np.mean(p / margin[m] * 100)) if n else 0.0,
        "avg_hold": float(np.mean(hold)) if n else 0.0,
        "worst": float(p.min()) if n else 0.0,
    }


def stability_block(pnl: np.ndarray, exit_t: np.ndarray, day: np.ndarray, coin: np.ndarray,
                    coins: list[str]) -> dict:
    """Độ ổn định của 1 cấu hình, trên các lệnh đã đóng (vector 1 chiều, cùng độ dài).
    r2       : R² của đường vốn (lãi cộng dồn theo thời điểm thoát) so với đường thẳng, mang dấu của độ dốc
               (1 = lãi tăng đều; gần 0 = giật cục / đi ngang; âm = đi xuống).
    day_pos  : % ngày (theo ngày thoát lệnh, chỉ tính ngày có lệnh đóng) có lãi.
    top3_day : % lãi ròng đến từ 3 ngày tốt nhất (chỉ khi lãi ròng > 0; > 100% = các ngày khác lỗ).
    net_xcoin: lãi ròng khi bỏ coin lãi nhiều nhất (best_coin, best_coin_pnl)."""
    out = {"r2": None, "day_pos": None, "n_days": 0, "top3_day": None, "net_xcoin": None, "best_coin": None,
           "best_coin_pnl": None}
    n = len(pnl)
    if not n:
        return out
    net = float(pnl.sum())
    o = np.argsort(exit_t, kind="stable")
    if n >= 3:
        x = (exit_t[o] - exit_t[o][0]) / 3_600_000.0
        y = np.cumsum(pnl[o])
        vx, vy = x.var(), y.var()
        if vx > 0 and vy > 0:
            r = float(np.cov(x, y, bias=True)[0, 1] / math.sqrt(vx * vy))
            out["r2"] = math.copysign(r * r, r)
    du, di = np.unique(day, return_inverse=True)
    dsum = np.bincount(di, weights=pnl, minlength=len(du))
    out["n_days"] = int(len(du))
    out["day_pos"] = float((dsum > 0).mean() * 100)
    if net > 0:
        out["top3_day"] = float(np.sort(dsum)[::-1][:3].clip(min=0).sum() / net * 100)
    cu, ci = np.unique(coin, return_inverse=True)
    csum = np.bincount(ci, weights=pnl, minlength=len(cu))
    b = int(np.argmax(csum))
    out["best_coin"], out["best_coin_pnl"] = coins[int(cu[b])], float(csum[b])
    out["net_xcoin"] = net - float(csum[b])
    return out


def actual_stats(df: pd.DataFrame) -> dict:
    d = df[df.pnl.notna()]
    p = d.pnl.to_numpy()
    n = len(p)
    order = np.argsort(d.end_ms.fillna(d.start_ms).to_numpy(), kind="stable")
    dd = _max_dd(p[order])
    gp, gl = p[p > 0].sum(), -p[p <= 0].sum()
    rc = d.reason.value_counts()
    return {"n": n, "n_tp": int(rc.get("TP", 0)), "n_sl": int(rc.get("SL", 0)), "n_liq": int(rc.get("LIQ", 0)),
            "n_timeout": int(rc.get("TIMEOUT", 0)), "n_manual": int(rc.get("MANUAL", 0)),
            "win_rate": float((p > 0).mean() * 100) if n else 0.0, "net": float(p.sum()),
            "avg": float(p.mean()) if n else 0.0, "pf": float(gp / gl) if gl > 0 else math.inf,
            "max_dd": dd, "ret_dd": float(p.sum() / dd) if dd > 0 else math.inf,
            "avg_hold": float(d.hold_min.mean()) if n else 0.0, "worst": float(p.min()) if n else 0.0}


# ============================================================================ toàn bộ lưới
class Replay:
    def __init__(self, trades: pd.DataFrame, paths: list[PricePath], st: Settings):
        self.st = st
        keep = np.array([not (st.skip_1m and p.res in ("1m",)) for p in paths], bool)
        self.trades = trades[keep].reset_index(drop=True)
        self.paths = [p for p, k in zip(paths, keep) if k]
        self.tps = np.array(st.tps, float)
        self.sls = np.array(st.sls, float)
        self.holds = np.array(st.holds, np.int64)
        self.window_min = min(p.window_ms for p in self.paths) // MIN_MS if self.paths else 0
        self.combos = [(a, b, c) for a in st.tps for b in st.sls for c in st.holds]
        N, C = len(self.paths), len(self.combos)
        self.entry_t = np.array([p.entry_t for p in self.paths], np.int64)
        self.entry_px = np.array([p.entry_px for p in self.paths], float)
        if st.sizing == "actual":
            notional = self.trades.notional.fillna(st.notional).to_numpy(float)
            margin = self.trades.margin.fillna(st.notional / st.leverage).to_numpy(float)
        else:
            notional = np.full(N, st.notional)
            margin = np.full(N, st.notional / st.leverage)
        self.notional, self.margin = notional, margin
        self.reason = np.empty((N, C), np.int8)
        self.exit_t = np.empty((N, C), np.int64)
        self.pnl = np.empty((N, C), np.float64)
        for i, p in enumerate(self.paths):
            r = simulate_trade(p, notional[i], margin[i], st, self.tps, self.sls, self.holds, self.window_min)
            self.reason[i] = r.reason.reshape(-1)
            self.exit_t[i] = r.exit_t.reshape(-1)
            self.pnl[i] = r.pnl.reshape(-1)
        # sắp theo thời điểm vào lệnh thật (tới ms)
        order = np.argsort(self.entry_t, kind="stable")
        self._apply_order(order)
        codes, uniq = pd.factorize(self.trades.coin)
        self.coin_idx = codes.astype(np.int64)
        self.coins = [str(c) for c in uniq]
        valid = self.reason != R_NA
        self.taken = {}
        for mode in st.modes:
            self.taken[mode] = take_mask(self.entry_t, self.coin_idx, np.where(self.reason == R_OPEN, INF, self.exit_t),
                                         valid, mode, st.max_per_coin, st.max_total, int(st.cooldown_min * MIN_MS))

    def entry_day(self, tz: str) -> np.ndarray:
        """Ngày vào lệnh (theo tz) dạng 'YYYY-MM-DD' cho từng lệnh."""
        return pd.to_datetime(self.entry_t, unit="ms", utc=True).tz_convert(tz).strftime("%Y-%m-%d").to_numpy()

    def subset(self, sel: np.ndarray) -> "Replay":
        """Bản thu gọn chỉ gồm các lệnh sel (vd lọc theo ngày): như thể bot chỉ chạy trong khoảng đó –
        tính lại lệnh nào được vào (chế độ 1 lệnh/coin, giới hạn vị thế). Không mô phỏng lại đường giá."""
        idx = np.flatnonzero(sel)
        sub = object.__new__(Replay)
        sub.__dict__.update(self.__dict__)
        sub.trades = self.trades.iloc[idx].reset_index(drop=True)
        sub.paths = [self.paths[i] for i in idx]
        for k in ("entry_t", "entry_px", "notional", "margin", "reason", "exit_t", "pnl", "coin_idx"):
            setattr(sub, k, getattr(self, k)[idx])
        valid = sub.reason != R_NA
        sub.taken = {mode: take_mask(sub.entry_t, sub.coin_idx, np.where(sub.reason == R_OPEN, INF, sub.exit_t), valid,
                                     mode, self.st.max_per_coin, self.st.max_total, int(self.st.cooldown_min * MIN_MS))
                     for mode in self.st.modes}
        return sub

    def _apply_order(self, order):
        self.trades = self.trades.iloc[order].reset_index(drop=True)
        self.paths = [self.paths[i] for i in order]
        for k in ("entry_t", "entry_px", "notional", "margin", "reason", "exit_t", "pnl"):
            setattr(self, k, getattr(self, k)[order])

    def combo_index(self, tp: float, sl: float, hold: int) -> int | None:
        for i, c in enumerate(self.combos):
            if abs(c[0] - tp) < 1e-9 and abs(c[1] - sl) < 1e-9 and c[2] == hold:
                return i
        return None

    def entry_hour(self, tz: str) -> np.ndarray:
        return pd.to_datetime(self.entry_t, unit="ms", utc=True).tz_convert(tz).hour.to_numpy()

    def top_hours(self, mode: str, tz: str, k: int = 8) -> tuple[np.ndarray, np.ndarray]:
        """Với mỗi cấu hình: chọn k giờ vào lệnh có lãi/lỗ (mô phỏng, mọi giờ) cao nhất, rồi chạy lại như bot chỉ vào
        lệnh trong k giờ đó (tính lại lệnh nào được vào). Trả (giờ đã chọn (C,k), taken (N,C))."""
        N, C = self.reason.shape
        hour = self.entry_hour(tz)
        done = self.taken[mode] & (self.reason != R_OPEN) & (self.reason != R_NA)
        onehot = (hour[None, :] == np.arange(24)[:, None]).astype(float)            # (24, N)
        by_hour = onehot @ np.where(done, self.pnl, 0.0)                              # (24, C)
        # cao → thấp; hoà thì giờ nhỏ trước (sắp ổn định theo -lãi)
        top = np.argsort(-by_hour, axis=0, kind="stable")[:k].T                      # (C, k)
        sel = np.zeros((24, C), bool)
        sel[top.T, np.arange(C)[None, :]] = True
        valid = (self.reason != R_NA) & sel[hour]                                     # (N, C)
        tk = take_mask(self.entry_t, self.coin_idx, np.where(self.reason == R_OPEN, INF, self.exit_t), valid, mode,
                       self.st.max_per_coin, self.st.max_total, int(self.st.cooldown_min * MIN_MS))
        return np.sort(top, axis=1), tk

    def exit_day(self, tz: str) -> np.ndarray:
        """Ngày (theo tz) của thời điểm thoát, (N, C) – lệnh chưa đóng / không có dữ liệu lấy ngày vào lệnh."""
        done = (self.reason != R_OPEN) & (self.reason != R_NA)
        t = np.where(done, self.exit_t, self.entry_t[:, None]).reshape(-1)
        loc = pd.DatetimeIndex(pd.to_datetime(t, unit="ms", utc=True)).tz_convert(tz).tz_localize(None)
        return loc.values.astype("datetime64[D]").astype(np.int64).reshape(self.exit_t.shape)

    def _stab(self, ci: int, tk: np.ndarray, day: np.ndarray) -> dict:
        m = tk & (self.reason[:, ci] != R_OPEN) & (self.reason[:, ci] != R_NA)
        return stability_block(self.pnl[m, ci], self.exit_t[m, ci], day[m, ci], self.coin_idx[m], self.coins)

    def neighbors(self, ci: int) -> list[int]:
        """Các ô kề trên heatmap TP × SL (cùng thời gian giữ): TP ±1 nấc, SL ±1 nấc theo thứ tự lưới."""
        tp, sl, h = self.combos[ci]
        i, j = self.st.tps.index(tp), self.st.sls.index(sl)
        out = []
        for a in (i - 1, i, i + 1):
            for b in (j - 1, j, j + 1):
                if (a, b) != (i, j) and 0 <= a < len(self.st.tps) and 0 <= b < len(self.st.sls):
                    k = self.combo_index(self.st.tps[a], self.st.sls[b], h)
                    if k is not None:
                        out.append(k)
        return out

    def table(self, tz: str = "Asia/Ho_Chi_Minh", top_k: int = 8) -> list[dict]:
        rows = []
        day = self.exit_day(tz) if len(self.paths) else None
        nbs = [self.neighbors(ci) for ci in range(len(self.combos))]
        for mode in self.st.modes:
            tk = self.taken[mode]
            hours, tk8 = self.top_hours(mode, tz, top_k) if len(self.paths) else (None, None)
            block = []
            for ci, (tp, sl, h) in enumerate(self.combos):
                s = stats_block(self.pnl[:, ci], self.reason[:, ci], self.exit_t[:, ci], self.entry_t, tk[:, ci],
                                self.margin)
                s.update({"mode": mode, "ci": ci, "tp": tp, "sl": sl, "hold": h})
                if day is not None:
                    s.update(self._stab(ci, tk[:, ci], day))
                if hours is not None:
                    s8 = stats_block(self.pnl[:, ci], self.reason[:, ci], self.exit_t[:, ci], self.entry_t,
                                     tk8[:, ci], self.margin)
                    s.update({"h8_hours": [int(x) for x in hours[ci]], "h8_n": s8["n"], "h8_net": s8["net"],
                              "h8_dd": s8["max_dd"], "h8_ret": s8["ret_dd"], "h8_pf": s8["pf"]})
                    s.update({"h8_" + k: v for k, v in self._stab(ci, tk8[:, ci], day).items()})
                block.append(s)
            # ổn định theo tham số: lãi ròng trung bình / thấp nhất của các ô kề trên heatmap (cùng chế độ)
            for ci, s in enumerate(block):
                nb = [block[k] for k in nbs[ci]]
                s["nb_cnt"] = len(nb)
                for pre in ("", "h8_"):
                    v = [x[pre + "net"] for x in nb if x.get(pre + "net") is not None]
                    s[pre + "nb_net"] = float(np.mean(v)) if v else None
                    s[pre + "nb_min"] = float(np.min(v)) if v else None
            rows += block
        return rows

    # ---------------------------------------------------------------- chi tiết 1 cấu hình
    def detail(self, mode: str, ci: int, tz: str, hours: list[int] | None = None) -> dict:
        """Chi tiết 1 cấu hình. hours: chỉ vào lệnh khi giờ vào (theo tz) nằm trong danh sách – mô phỏng như bot
        chỉ chạy các khung giờ đó (tính lại lệnh nào được vào, vì lệnh ngoài giờ không còn chiếm chỗ)."""
        from .bt import fmt_ms
        tp, sl, h = self.combos[ci]
        R, X, P = self.reason[:, ci], self.exit_t[:, ci], self.pnl[:, ci]
        hour = self.entry_hour(tz)
        hsel = sorted({int(x) for x in hours if 0 <= int(x) < 24}) if hours is not None else list(range(24))
        hmask = np.isin(hour, hsel)
        if hmask.all():
            tk = self.taken[mode][:, ci].copy()
        else:
            idx = np.flatnonzero(hmask)
            tk = np.zeros(len(R), bool)
            if len(idx):
                sub = take_mask(self.entry_t[idx], self.coin_idx[idx], np.where(R[idx] == R_OPEN, INF, X[idx])[:, None],
                                (R[idx] != R_NA)[:, None], mode, self.st.max_per_coin, self.st.max_total,
                                int(self.st.cooldown_min * MIN_MS))
                tk[idx] = sub[:, 0]
        rows = []
        one = Settings(**{**asdict(self.st), "tps": [tp], "sls": [sl], "holds": [h]})
        for i, p in enumerate(self.paths):
            if not hmask[i]:
                continue
            r = simulate_trade(p, self.notional[i], self.margin[i], one, np.array([tp]), np.array([sl]),
                               np.array([h]), self.window_min)
            a = self.trades.iloc[i]
            rc = int(r.reason.flat[0])
            rows.append({
                "id": int(a.no) if pd.notna(a.no) else i + 1,
                "coin": a.coin,
                "side": "LONG" if a.side == 1 else "SHORT",
                "hour": int(hour[i]),
                "entry_ms": int(p.entry_t),
                "entry_t": fmt_ms(p.entry_t, tz, True),
                "entry_px": p.entry_px,
                "entry_src": p.entry_src,
                "res": p.res,
                "taken": bool(tk[i]),
                "reason": REASONS[rc],
                "exit_t": fmt_ms(int(r.exit_t.flat[0]), tz, True) if rc not in (R_NA,) else "",
                "exit_px": float(r.exit_px.flat[0]) if np.isfinite(r.exit_px.flat[0]) else None,
                "hold_min": round((int(r.exit_t.flat[0]) - p.entry_t) / 60000.0, 2) if rc != R_NA else None,
                "pnl": float(r.pnl.flat[0]) if np.isfinite(r.pnl.flat[0]) else None,
                "fee": float(r.fee.flat[0]),
                "funding": float(r.funding.flat[0]),
                "a_reason": a.reason,
                "a_exit_t": fmt_ms(int(a.end_ms), tz) if pd.notna(a.end_ms) else "",
                "a_exit_px": float(a.exit_csv) if pd.notna(a.exit_csv) else None,
                "a_hold_min": round(float(a.hold_min), 2) if pd.notna(a.hold_min) else None,
                "a_pnl": float(a.pnl) if pd.notna(a.pnl) else None,
                "note": p.note,
            })
        # đường vốn & theo ngày (thực tế cũng chỉ lấy lệnh vào trong các giờ đã chọn để so cùng tập)
        m = tk & (R != R_OPEN) & (R != R_NA)
        et, pn, rs = X[m], P[m], R[m]
        o = np.argsort(et, kind="stable")
        eq = [[int(t), float(v)] for t, v in zip(et[o], np.cumsum(pn[o]))]
        am = hmask & self.trades.pnl.notna().to_numpy()
        a = self.trades[am]
        ah = hour[am]
        ae = a.end_ms.fillna(a.start_ms).to_numpy()
        ao = np.argsort(ae, kind="stable")
        aeq = [[int(t), float(v)] for t, v in zip(ae[ao], np.cumsum(a.pnl.to_numpy()[ao]))]
        day = pd.Series(pn, index=pd.to_datetime(et, unit="ms", utc=True).tz_convert(tz).strftime("%Y-%m-%d"))
        aday = pd.Series(a.pnl.to_numpy(), index=pd.to_datetime(ae, unit="ms", utc=True).tz_convert(tz)
                         .strftime("%Y-%m-%d"))
        days = sorted(set(day.index) | set(aday.index))
        dsum, adsum = day.groupby(level=0).sum(), aday.groupby(level=0).sum()
        by_day = [{"day": d, "sim": float(dsum.get(d, 0.0)), "actual": float(adsum.get(d, 0.0))} for d in days]

        # theo giờ VÀO lệnh & theo coin: mô phỏng (lệnh được vào) và thực tế, tách TP / SL / hết giờ
        def agg(key_s, key_a):
            s = pd.DataFrame({"k": key_s, "pnl": pn, "win": pn > 0, "tp": rs == R_TP,
                              "sl": (rs == R_SL) | (rs == R_LIQ), "to": rs == R_TIMEOUT}).groupby("k")
            ar = a.reason.to_numpy()
            ap = a.pnl.to_numpy()
            g = pd.DataFrame({"k": key_a, "pnl": ap, "win": ap > 0, "tp": ar == "TP", "sl": np.isin(ar, ("SL", "LIQ")),
                              "to": ar == "TIMEOUT"}).groupby("k")
            S = pd.DataFrame({"n": s.pnl.size(), "sim": s.pnl.sum(), "win": s.win.sum(), "n_tp": s.tp.sum(),
                              "n_sl": s.sl.sum(), "n_timeout": s.to.sum()})
            A = pd.DataFrame({"a_n": g.pnl.size(), "actual": g.pnl.sum(), "a_win": g.win.sum(), "a_tp": g.tp.sum(),
                              "a_sl": g.sl.sum(), "a_timeout": g.to.sum()})
            return S.join(A, how="outer").fillna(0)

        H = agg(hour[m], ah).reindex(range(24), fill_value=0)
        ints = ["n", "win", "n_tp", "n_sl", "n_timeout", "a_n", "a_win", "a_tp", "a_sl", "a_timeout"]
        conv = lambda r: {k: (int(v) if k in ints else float(v)) for k, v in r.items()}  # noqa: E731
        by_hour = [{"hour": int(k), **conv(r)} for k, r in H.iterrows()]
        C = agg(self.trades.coin.to_numpy()[m], a.coin.to_numpy()).sort_values("sim")
        by_coin = [{"coin": k, **conv(r), "pnl": float(r["sim"])} for k, r in C.iterrows() if r["n"] or r["a_n"]]
        return {"combo": {"mode": mode, "tp": tp, "sl": sl, "hold": h}, "hours": hsel,
                "stats": stats_block(P[hmask], R[hmask], X[hmask], self.entry_t[hmask], tk[hmask], self.margin[hmask]),
                "actual_stats": actual_stats(a),
                "trades": rows, "equity": eq, "actual_equity": aeq, "by_day": by_day, "by_hour": by_hour,
                "by_coin": by_coin}


# ============================================================================ hiệu chỉnh trượt giá
def calibrate(trades: pd.DataFrame, paths: list[PricePath], st: Settings, tp: float, sl: float, hold: int,
              window_min: int, min_n: int = 5, clamp: float = 3.0) -> dict:
    """Ước lượng trượt giá thật khi cắt lỗ (SL) và khi đóng hết giờ, từ các lệnh thật mà mô phỏng ra cùng lý do thoát.

    Với mỗi lệnh: so lãi/lỗ GỘP (trước phí & funding) mô phỏng không trượt giá với thật, quy ra % giá trị lúc thoát:
        trượt % = (gộp mô phỏng − gộp thật) / (khối lượng × giá thoát / giá vào) × 100
    > 0: thực tế khớp tệ hơn giá tick kích hoạt; < 0: tốt hơn (vd sàn kích hoạt SL theo mark price, sớm hơn).
    Lấy trung vị (bền với vài lệnh bot ghi sai)."""
    base = Settings(**{**asdict(st), "sl_slip_pct": 0.0, "exit_slip_pct": 0.0})
    vals: dict[str, list[float]] = {"SL": [], "TIMEOUT": []}
    for i, p in enumerate(paths):
        a = trades.iloc[i]
        if a.reason not in vals or not p.ok or pd.isna(a.pnl):
            continue
        if st.sizing == "fixed":
            notional, margin = st.notional, st.notional / st.leverage
        else:
            notional = a.notional if pd.notna(a.notional) and a.notional > 0 else st.notional
            margin = a.margin if pd.notna(a.margin) and a.margin > 0 else notional / st.leverage
        r = simulate_trade(p, notional, margin, base, np.array([tp]), np.array([sl]), np.array([hold]), window_min)
        if REASONS[int(r.reason.flat[0])] != a.reason:
            continue
        sx = float(r.exit_px.flat[0])
        if not np.isfinite(sx) or sx <= 0:
            continue
        sim_gross = float(r.pnl.flat[0] + r.fee.flat[0] - r.funding.flat[0])
        act_gross = float(a.pnl - (a.fee if pd.notna(a.fee) else 0.0) - (a.funding if pd.notna(a.funding) else 0.0))
        exit_value = notional * sx / p.entry_px
        vals[a.reason].append((sim_gross - act_gross) / exit_value * 100.0)

    def est(v):
        if len(v) < min_n:
            return 0.0, len(v)
        return float(np.clip(np.median(v), -clamp, clamp)), len(v)

    sl_slip, n_sl = est(vals["SL"])
    exit_slip, n_to = est(vals["TIMEOUT"])
    return {"sl_slip": round(sl_slip, 3), "n_sl": n_sl, "exit_slip": round(exit_slip, 3), "n_timeout": n_to,
            "min_n": min_n}


# ============================================================================ kiểm định: chạy lại đúng cấu hình thật
def validate(trades: pd.DataFrame, paths: list[PricePath], st: Settings, tp: float, sl: float, hold: int,
             window_min: int, tz: str = "Asia/Ho_Chi_Minh") -> dict:
    """Chạy lại từng lệnh thật với đúng cấu hình bot đã dùng, so với kết quả thật (lý do thoát, thời điểm, giá, PnL).
    Mỗi lệnh được xét độc lập (không áp dụng chế độ vị thế) để so 1-1."""
    from .bt import fmt_ms
    rows = []
    for i, p in enumerate(paths):
        a = trades.iloc[i]
        if a.reason not in ("TP", "SL", "TIMEOUT") or not p.ok:
            continue
        if st.sizing == "fixed":
            notional, margin = st.notional, st.notional / st.leverage
        else:
            notional = a.notional if pd.notna(a.notional) else st.notional
            margin = a.margin if pd.notna(a.margin) else notional / st.leverage
        r = simulate_trade(p, notional, margin, st, np.array([tp]), np.array([sl]), np.array([hold]), window_min)
        rc = int(r.reason.flat[0])
        if rc in (R_OPEN, R_NA):
            continue
        et = int(r.exit_t.flat[0])
        rows.append({"no": int(a.no) if pd.notna(a.no) else i + 1, "coin": a.coin,
                     "entry_t": fmt_ms(p.entry_t, tz, True), "a": a.reason, "s": REASONS[rc],
                     "delay": (int(a.end_ms) - et) / 1000.0 if pd.notna(a.end_ms) else np.nan,
                     "sp": float(r.pnl.flat[0]), "ap": float(a.pnl), "res": p.res, "src": p.entry_src,
                     "sx": float(r.exit_px.flat[0]), "ax": float(a.exit_csv) if pd.notna(a.exit_csv) else np.nan,
                     "s_exit_t": fmt_ms(et, tz, True), "a_exit_t": fmt_ms(int(a.end_ms), tz) if pd.notna(a.end_ms) else ""})
    if not rows:
        return {"n": 0}
    d = pd.DataFrame(rows)
    same = d.a == d.s
    dl = d.delay[same]
    xdiff = (d.sx / d.ax - 1).abs()[same] * 100
    ct = pd.crosstab(d.a, d.s)
    conf = {a: {s: int(ct.loc[a, s]) for s in ct.columns if ct.loc[a, s]} for a in ct.index}
    # ---- tách chênh lệch PnL (mô phỏng − thật) theo nhóm lệnh
    tp_ok = same & (d.a == "TP")
    med_tp = float(d.ap[tp_ok].median()) if tp_ok.any() else 0.0
    anom = tp_ok & (d.ap < 0.25 * med_tp) if med_tp > 0 else tp_ok & False
    groups = [
        ("TP", "Chốt TP – khớp lý do", tp_ok & ~anom,
         "Logic thoát lệnh: giá TP đặt đúng % từ giá vào. Chênh nhỏ = khác phí / khối lượng."),
        ("SL", "Cắt lỗ SL – khớp lý do", same & (d.a == "SL"),
         "Lệnh SL là market: thực tế khớp lệch so với tick kích hoạt (trượt giá) – xem hiệu chỉnh trượt giá."),
        ("TIMEOUT", "Đóng hết giờ – khớp lý do", same & (d.a == "TIMEOUT"),
         "Đóng market khi hết giờ: trượt giá + thời điểm bot đóng thật."),
        ("ANOM", "Bot ghi tp_hit nhưng PnL ≈ 0 / âm", anom,
         "Lỗi ghi nhận phía bot (PnL < 25% lãi TP thường gặp, nhiều lệnh phí = 0) – không phải sai số mô phỏng."),
        ("DIFF", "Lý do thoát khác thực tế", ~same,
         "Thường là giá đã chạm TP nhưng bot không khớp, hoặc giá vào thật không biết chính xác (OKX)."),
    ]
    bridge = []
    for key, label, m, note in groups:
        if m.any():
            bridge.append({"key": key, "label": label, "n": int(m.sum()), "sim": float(d.sp[m].sum()),
                           "actual": float(d.ap[m].sum()), "gap": float((d.sp - d.ap)[m].sum()),
                           "per_trade": float((d.sp - d.ap)[m].mean()), "note": note})
    mism = d[~same].copy()
    mism["sp"] = mism.sp.round(4)
    return {
        "tp": tp, "sl": sl, "hold": hold,
        "n": int(len(d)),
        "reason_match": float(same.mean() * 100),
        # thời điểm bot ghi nhận đóng lệnh trễ hơn tick chạm mức bao lâu (bot hỏi trạng thái lệnh định kỳ)
        "delay_median": float(dl.median()) if len(dl) else 0.0,
        "exit_within_10s": float((dl.abs() <= 10).mean() * 100) if len(dl) else 0.0,
        "exit_within_60s": float((dl.abs() <= 60).mean() * 100) if len(dl) else 0.0,
        "exit_px_diff_median": float(xdiff.median()) if len(xdiff) else 0.0,
        "exit_px_within_01": float((xdiff <= 0.1).mean() * 100) if len(xdiff) else 0.0,
        "pnl_sim": float(d.sp.sum()),
        "pnl_actual": float(d.ap.sum()),
        "pnl_by_reason": {k: {"sim": float(g.sp.sum()), "actual": float(g.ap.sum()), "n": int(len(g))}
                          for k, g in d.groupby("a")},
        "tick_pct": float((d.res == "tick").mean() * 100),
        "entry_csv_pct": float((d.src == "csv").mean() * 100),
        "confusion": conf,
        "bridge": bridge,
        "gross_tp_actual": float(d.ap[d.a == "TP"].sum()),
        "loss_actual": float(d.ap[d.a != "TP"].sum()),
        "mismatches": mism[["no", "coin", "entry_t", "a", "s", "a_exit_t", "s_exit_t", "ax", "sx", "ap", "sp"]]
        .head(200).to_dict("records"),
    }
