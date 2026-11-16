"""Mô phỏng khớp lệnh: thời điểm/giá vào lệnh và thoát lệnh (TP / SL / thanh lý / hết giờ).

Nguyên tắc:
  * Có tick (từng giao dịch khớp) -> mô phỏng chính xác tới mili-giây:
      - vào lệnh touch: tick đầu tiên chạm ngưỡng; khớp tại tick đầu tiên sau `latency_ms`.
      - TP limit khớp đúng giá TP; SL (stop-market) khớp tại giá tick kích hoạt (có trượt giá do gap)
        + trượt giá cấu hình.
  * Không có tick -> mô phỏng trong nến 1m theo `intrabar_fallback`:
      - conservative: luôn giả định kịch bản BẤT LỢI khi thứ tự giá trong nến không xác định.
      - ohlc: nến xanh O→L→H→C, nến đỏ O→H→L→C.
      - optimistic: giả định kịch bản có lợi (chỉ để so sánh).
    Lệnh nào phải dùng giả định này được đánh dấu `ambiguous`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..utils import DAY_MS, MIN_MS
from ..data.store import DataStore

INF = math.inf
LOOKAHEAD_MS = 10 * MIN_MS  # tìm tick khớp lệnh tối đa 10 phút sau thời điểm cần


@dataclass
class Levels:
    up: float
    up_reason: str
    down: float
    down_reason: str


@dataclass
class EntryFill:
    signal_t: int
    fill_t: int
    raw_px: float
    resolution: str     # tick | 1m


@dataclass
class ExitFill:
    t: int
    raw_px: float
    reason: str         # TP | SL | LIQ | TIMEOUT
    resolution: str
    ambiguous: bool


def make_levels(side: int, entry: float, tp_pct: float | None, sl_pct: float | None,
                liq_px: float | None) -> Levels:
    if side == 1:
        tp = entry * (1 + tp_pct / 100) if tp_pct else INF
        sl = entry * (1 - sl_pct / 100) if sl_pct else -INF
        if liq_px is not None and liq_px >= sl:
            return Levels(tp, "TP", liq_px, "LIQ")
        return Levels(tp, "TP", sl, "SL")
    tp = entry * (1 - tp_pct / 100) if tp_pct else -INF
    sl = entry * (1 + sl_pct / 100) if sl_pct else INF
    if liq_px is not None and liq_px <= sl:
        return Levels(liq_px, "LIQ", tp, "TP")
    return Levels(sl, "SL", tp, "TP")


class Simulator:
    def __init__(self, store: DataStore, use_ticks: bool, intrabar: str, latency_ms: int,
                 tp_through: bool, rules: list | None = None):
        self.store = store
        self.rules = rules or []
        self.use_ticks = use_ticks
        self.intrabar = intrabar
        self.latency = latency_ms
        self.tp_through = tp_through
        self.tick_missing = 0

    # ------------------------------------------------------------------ helpers
    def _ticks(self, symbol: str, a: int, b: int):
        if not self.use_ticks or b <= a:
            return None
        v = self.store.ticks(symbol, a, b)
        if v is None:
            self.tick_missing += 1
        return v

    def _bar(self, symbol: str, minute: int):
        bars = self.store.day_bars(symbol, minute - minute % DAY_MS)
        i = np.searchsorted(bars.ts, minute)
        if i < len(bars) and bars.ts[i] == minute:
            return bars.o[i], bars.h[i], bars.l[i], bars.c[i]
        return None

    def _last_close_before(self, symbol: str, t: int) -> float | None:
        """Giá đóng của nến 1m gần nhất kết thúc trước/tại t (lùi tối đa 2 ngày)."""
        day = t - t % DAY_MS
        for d in (day, day - DAY_MS, day - 2 * DAY_MS):
            bars = self.store.day_bars(symbol, d)
            i = np.searchsorted(bars.ts, t - MIN_MS, "right") - 1
            if i >= 0:
                return float(bars.c[i])
        return None

    def _first_tick_at(self, symbol: str, t: int):
        """(ts, px) của tick đầu tiên tại/sau t, hoặc None."""
        v = self._ticks(symbol, t, t + LOOKAHEAD_MS)
        if v is None or len(v[0]) == 0:
            return None
        return int(v[0][0]), float(v[1][0])

    # ------------------------------------------------------------------ entry
    def entry(self, cand) -> EntryFill | None:
        rule = self.rules[cand.rule_idx] if cand.rule_idx < len(self.rules) else None
        if rule is None or not rule.pullback_pct:
            return self._entry_signal(cand)
        key = (rule.pullback_pct, rule.pullback_window_ms, self.latency, self.use_ticks)
        cached = getattr(cand, "_pb", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        cand.no_pullback = False
        e = self._entry_signal(cand)
        if e is not None:
            cand.setup_t = e.signal_t
            e = self._pullback(cand, rule, e)
        cand._pb = (key, e)
        return e

    def _pullback(self, cand, rule, sig: EntryFill) -> EntryFill | None:
        """Sau tín hiệu, theo dõi trong pullback_window: vào lệnh khi giá hồi x% từ đỉnh (tín hiệu tăng)
        hoặc bật x% từ đáy (tín hiệu giảm). Đỉnh/đáy tính từ lúc mở nến tín hiệu. None nếu không hồi kịp."""
        sym, d, x = cand.symbol, cand.dir_sign, rule.pullback_pct / 100.0
        t0, t_end, c0 = sig.signal_t, sig.signal_t + rule.pullback_window_ms, cand.candle_ts
        v = self._ticks(sym, c0, t_end)
        if v is not None and len(v[0]):
            ts, px = v
            if d == 1:
                ext = np.maximum.accumulate(px)
                hit = (px <= ext * (1 - x)) & (ts >= t0)
            else:
                ext = np.minimum.accumulate(px)
                hit = (px >= ext * (1 + x)) & (ts >= t0)
            if hit.any():
                i = int(np.argmax(hit))
                trig = int(ts[i])
                if self.latency <= 0:
                    return EntryFill(trig, trig, float(px[i]), "tick")
                nxt = self._first_tick_at(sym, trig + self.latency)
                if nxt is not None:
                    return EntryFill(trig, nxt[0], nxt[1], "tick")
            cand.no_pullback = True
            return None
        # chỉ có nến 1m: đỉnh/đáy lấy theo high/low của nến; trong 1 nến giả định cực trị xảy ra trước nhịp hồi
        m0 = t0 - t0 % MIN_MS
        ext = None
        for day in range(c0 - c0 % DAY_MS, t_end, DAY_MS):
            bars = self.store.day_bars(sym, day)
            i0, i1 = np.searchsorted(bars.ts, max(c0, day), "left"), np.searchsorted(bars.ts, t_end, "left")
            for i in range(i0, i1):
                bt = int(bars.ts[i])
                o, h, l, c = bars.o[i], bars.h[i], bars.l[i], bars.c[i]
                if bt < m0:
                    ext = (h if ext is None else max(ext, h)) if d == 1 else (l if ext is None else min(ext, l))
                    continue
                if d == 1:
                    if ext is not None and bt > m0 and o <= ext * (1 - x):
                        return EntryFill(bt, bt, float(o), "1m")
                    top = h if ext is None else max(ext, h)
                    lvl = top * (1 - x)
                    if (c if bt == m0 else l) <= lvl:
                        t = bt + MIN_MS // 2
                        return EntryFill(t, t, float(max(lvl, l)), "1m")
                    ext = top
                else:
                    if ext is not None and bt > m0 and o >= ext * (1 + x):
                        return EntryFill(bt, bt, float(o), "1m")
                    bot = l if ext is None else min(ext, l)
                    lvl = bot * (1 + x)
                    if (c if bt == m0 else h) >= lvl:
                        t = bt + MIN_MS // 2
                        return EntryFill(t, t, float(min(lvl, h)), "1m")
                    ext = bot
        cand.no_pullback = True
        return None

    def _entry_signal(self, cand) -> EntryFill | None:
        sym = cand.symbol
        if cand.trigger == "touch":
            m0 = cand.bar_ts
            v = self._ticks(sym, m0, m0 + MIN_MS)
            if v is not None and len(v[0]):
                ts, px = v
                hit = px >= cand.threshold if cand.dir_sign == 1 else px <= cand.threshold
                if hit.any():
                    i = int(np.argmax(hit))
                    touch_t = int(ts[i])
                    if self.latency <= 0:
                        return EntryFill(touch_t, touch_t, float(px[i]), "tick")
                    nxt = self._first_tick_at(sym, touch_t + self.latency)
                    if nxt is not None:
                        return EntryFill(touch_t, nxt[0], nxt[1], "tick")
            # fallback nến 1m: khớp tại ngưỡng (hoặc giá mở nếu nhảy giá qua ngưỡng)
            bar = self._bar(sym, m0)
            if bar is None:
                return None
            o = bar[0]
            px = max(o, cand.threshold) if cand.dir_sign == 1 else min(o, cand.threshold)
            t = m0 + (0 if px == o else MIN_MS // 2)
            return EntryFill(t, t, float(px), "1m")
        # trigger = close
        t = cand.t
        if self.use_ticks:
            nxt = self._first_tick_at(sym, t + self.latency)
            if nxt is not None:
                return EntryFill(t, nxt[0], nxt[1], "tick")
        bar = self._bar(sym, t)
        if bar is not None:
            return EntryFill(t, t, float(bar[0]), "1m")
        px = self._last_close_before(sym, t)
        return None if px is None else EntryFill(t, t, px, "1m")

    # ------------------------------------------------------------------ exit
    def _hit_ticks(self, ts, px, lv: Levels, side: int):
        """Tick đầu tiên chạm mức trên/dưới -> (i, 'up'|'down') hoặc None."""
        up_strict = self.tp_through and lv.up_reason == "TP"
        dn_strict = self.tp_through and lv.down_reason == "TP"
        up = px > lv.up if up_strict else px >= lv.up
        dn = px < lv.down if dn_strict else px <= lv.down
        m = up | dn
        if not m.any():
            return None
        i = int(np.argmax(m))
        return i, ("up" if up[i] else "down")

    def _walk(self, start_px: float, pts: list[float], lv: Levels, gap_first: bool):
        """Đi theo đường giá tuyến tính qua các điểm. Trả về (k, 'up'|'down', raw_px) hoặc None."""
        for k, p in enumerate(pts):
            gap = gap_first and k == 0      # giá mở nến đã vượt mức -> khớp tại giá mở (gap)
            if p >= lv.up:
                return k, "up", (p if gap else lv.up)
            if p <= lv.down:
                return k, "down", (p if gap else lv.down)
        return None

    def _bar_path(self, o, h, l, c, side: int) -> list[float]:
        if self.intrabar == "ohlc":
            return [o, l, h, c] if c >= o else [o, h, l, c]
        adverse_low = side == 1
        if self.intrabar == "conservative":
            return [o, l, h, c] if adverse_low else [o, h, l, c]
        return [o, h, l, c] if adverse_low else [o, l, h, c]

    def _entry_bar_path(self, bar, side: int, dir_sign: int, thr: float) -> list[float]:
        o, h, l, c = bar
        g = h if dir_sign == 1 else l           # cực trị chắc chắn xảy ra SAU điểm chạm
        u = l if dir_sign == 1 else h           # cực trị có thể trước hoặc sau
        u_adverse = side == dir_sign
        if self.intrabar == "ohlc":
            pts = [o, l, h, c] if c >= o else [o, h, l, c]
            for k in range(len(pts) - 1):
                a, b = pts[k], pts[k + 1]
                if (dir_sign == 1 and a < thr <= b) or (dir_sign == -1 and a > thr >= b):
                    return pts[k + 1:]
            return [g, c]
        if self.intrabar == "conservative":
            return [u, g, c] if u_adverse else [g, c]
        return [g, c] if u_adverse else [u, g, c]

    def _exit_price(self, reason: str, level: float, tick_px: float, tp_limit: bool) -> float:
        if reason == "TP" and tp_limit:
            return level
        if reason == "LIQ":
            return level
        return tick_px

    def exit(self, symbol: str, side: int, entry: EntryFill, entry_px: float, lv: Levels,
             deadline: int, cand, tp_limit: bool) -> ExitFill:
        t0 = entry.fill_t
        m0 = t0 - t0 % MIN_MS
        m0_end = m0 + MIN_MS
        amb = False

        # ---- 1) phần còn lại của phút vào lệnh
        if entry.resolution == "tick":
            v = self._ticks(symbol, t0 + 1, min(m0_end, deadline))
            if v is not None:
                ts, px = v
                r = self._hit_ticks(ts, px, lv, side)
                if r is not None:
                    i, which = r
                    reason = lv.up_reason if which == "up" else lv.down_reason
                    level = lv.up if which == "up" else lv.down
                    return ExitFill(int(ts[i]), self._exit_price(reason, level, float(px[i]), tp_limit),
                                    reason, "tick", False)
        elif getattr(cand, "setup_t", None) is not None:
            pass  # vào lệnh khi giá hồi, chỉ có nến 1m: không biết đường giá phần còn lại của phút -> quét từ phút sau
        elif cand.trigger == "touch":
            bar = self._bar(symbol, m0)
            if bar is not None:
                pts = self._entry_bar_path(bar, side, cand.dir_sign, cand.threshold)
                amb = bool(bar[1] >= lv.up and bar[2] <= lv.down)
                w = self._walk(entry.raw_px, pts, lv, gap_first=False)
                if w is not None:
                    k, which, rpx = w
                    reason = lv.up_reason if which == "up" else lv.down_reason
                    level = lv.up if which == "up" else lv.down
                    t = t0 + (m0_end - t0) * (k + 1) // (len(pts) + 1)
                    return ExitFill(t, self._exit_price(reason, level, rpx, tp_limit), reason, "1m", amb)
        else:
            m0_end = m0  # trigger=close, nến 1m: vào lệnh tại giá mở nến m0 -> quét từ chính nến m0

        # ---- 2) quét các nến 1m tiếp theo để tìm nến chạm mức
        t = m0_end
        while t < deadline:
            day = t - t % DAY_MS
            bars = self.store.day_bars(symbol, day)
            i0 = np.searchsorted(bars.ts, t, "left")
            i1 = np.searchsorted(bars.ts, min(deadline, day + DAY_MS), "left")
            if i1 > i0:
                hh, ll = bars.h[i0:i1], bars.l[i0:i1]
                m = (hh >= lv.up) | (ll <= lv.down)
                if m.any():
                    i = i0 + int(np.argmax(m))
                    bt = int(bars.ts[i])
                    o, h, l, c = bars.o[i], bars.h[i], bars.l[i], bars.c[i]
                    b_end = min(bt + MIN_MS, deadline)
                    # có tick trong phút này thì chỉ dùng tick (kể cả khi không có giao dịch nào trước deadline);
                    # không dùng nến 1m vì high/low của nến có thể xảy ra sau deadline
                    v = self._ticks(symbol, bt, bt + MIN_MS)
                    if v is not None and len(v[0]):
                        ts, px = v
                        if b_end < bt + MIN_MS:
                            n = int(np.searchsorted(ts, b_end, "left"))
                            ts, px = ts[:n], px[:n]
                        r = self._hit_ticks(ts, px, lv, side) if len(ts) else None
                        if r is not None:
                            j, which = r
                            reason = lv.up_reason if which == "up" else lv.down_reason
                            level = lv.up if which == "up" else lv.down
                            return ExitFill(int(ts[j]), self._exit_price(reason, level, float(px[j]), tp_limit),
                                            reason, "tick", amb)
                        # tick không chạm (vd mức 'through' hoặc quá deadline) -> sang nến sau
                        t = bt + MIN_MS
                        continue
                    pts = self._bar_path(o, h, l, c, side)
                    both = h >= lv.up and l <= lv.down
                    w = self._walk(o, pts, lv, gap_first=True)
                    if w is not None:
                        k, which, rpx = w
                        reason = lv.up_reason if which == "up" else lv.down_reason
                        level = lv.up if which == "up" else lv.down
                        et = bt + (MIN_MS * k) // len(pts) if k else bt
                        return ExitFill(et, self._exit_price(reason, level, rpx, tp_limit), reason, "1m", amb or both)
                    t = bt + MIN_MS
                    continue
            t = min(deadline, day + DAY_MS)

        # ---- 3) hết thời gian giữ lệnh -> đóng lệnh thị trường
        nxt = self._first_tick_at(symbol, deadline) if self.use_ticks else None
        if nxt is not None:
            return ExitFill(nxt[0], nxt[1], "TIMEOUT", "tick", amb)
        bar = self._bar(symbol, deadline - deadline % MIN_MS)
        if bar is not None and deadline % MIN_MS == 0:
            return ExitFill(deadline, float(bar[0]), "TIMEOUT", "1m", amb)
        px = self._last_close_before(symbol, deadline - deadline % MIN_MS + MIN_MS)
        if px is None:
            px = entry_px
        return ExitFill(deadline, px, "TIMEOUT", "1m", amb)
