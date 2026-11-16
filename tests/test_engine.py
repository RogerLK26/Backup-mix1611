"""Unit test engine với dữ liệu giả lập (không cần mạng).  Chạy: python -m pytest -q"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from backtester.config import build_config
from backtester.data.base import SymbolInfo
from backtester.data.store import Bars
from backtester.engine.backtest import Backtester, Universe, scan_candidates
from backtester.engine.signals import rolling_24h
from backtester.utils import DAY_MS, MIN_MS, date_to_ms

D0 = date_to_ms("2026-01-05")  # thứ Hai
SYM = "AAAUSDT"


class FakeStore:
    """Giả lập DataStore: nến 1m dựng từ danh sách tick."""

    def __init__(self, ticks: dict[str, list[tuple[int, float]]], with_ticks=True, flat_px: dict | None = None):
        self.with_ticks = with_ticks
        self.t = {}
        self.bars = {}
        self.stats = {"tick_chunks_loaded": 0}
        for s, lst in ticks.items():
            lst = sorted(lst)
            ts = np.array([x[0] for x in lst], dtype=np.int64)
            px = np.array([x[1] for x in lst], dtype=np.float64)
            self.t[s] = (ts, px)
            df = pd.DataFrame({"m": ts - ts % MIN_MS, "px": px})
            g = df.groupby("m").px.agg(["first", "max", "min", "last"])
            # lấp các phút không có giao dịch bằng giá đóng trước đó (giống sàn)
            full = range(D0 - DAY_MS, D0 + 3 * DAY_MS, MIN_MS)
            last = (flat_px or {}).get(s, float(px[0]))
            rows = []
            for m in full:
                if m in g.index:
                    r = g.loc[m]
                    rows.append((m, r["first"], r["max"], r["min"], r["last"], 1e5))
                    last = r["last"]
                else:
                    rows.append((m, last, last, last, last, 1e5))
            self.bars[s] = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "qv"])

    def symbols(self):
        return {s: SymbolInfo(s, s, s[:-4]) for s in self.t}

    def day_bars(self, symbol, day):
        df = self.bars[symbol]
        return Bars(df[(df.ts >= day) & (df.ts < day + DAY_MS)].reset_index(drop=True))

    def loaded_days(self, symbol):
        return [D0 - DAY_MS, D0, D0 + DAY_MS]

    def ticks(self, symbol, a, b):
        if not self.with_ticks:
            return None
        ts, px = self.t[symbol]
        i, j = np.searchsorted(ts, a), np.searchsorted(ts, b)
        return ts[i:j], px[i:j]

    def funding(self, symbol, start, end):
        return pd.DataFrame({"ts": pd.Series(dtype="int64"), "rate": pd.Series(dtype="float64")})


def cfg_of(**over):
    raw = {
        "name": "t", "exchange": "binance", "start": "2026-01-05 00:00", "end": "2026-01-07 00:00",
        "timezone": "UTC", "initial_balance": 1000,
        "data": {"resolution": "auto", "intrabar_fallback": "conservative"},
        "universe": {"market_cap_source": "none"},
        "signals": [{"timeframe": "1m", "change_pct": 5, "direction": "up", "trigger": "touch", "side": "reverse"}],
        "entry": {"latency_ms": 0, "slippage_pct": 0},
        "position": {"margin": 10, "leverage": 10},
        "exit": {"take_profit": 2, "stop_loss": 2, "max_hold": "30m", "liquidation": True},
        "fees": {"maker_pct": 0.02, "taker_pct": 0.05, "funding": False},
        "limits": {},
    }
    for k, v in over.items():
        cur = raw
        ks = k.split(".")
        for kk in ks[:-1]:
            cur = cur.setdefault(kk, {})
        cur[ks[-1]] = v
    return build_config(raw)


def run(store, cfg, symbols=None):
    symbols = symbols or list(store.t)
    uni = Universe(symbols, {s: {D0, D0 + DAY_MS} for s in symbols})
    cands = scan_candidates(cfg, store, uni)
    return Backtester(cfg, store, None, uni).run(cands)


def base_ticks(start=D0 + 10 * MIN_MS, px=100.0):
    """Giá đứng 100 trong 10 phút để có nến nền."""
    return [(start - k * 10_000, px) for k in range(1, 60)]


# ---------------------------------------------------------------- tests
def test_rolling_24h():
    ts = np.arange(0, 3 * 1440) * MIN_MS
    qv = np.ones(len(ts))
    v = rolling_24h(ts, qv)
    assert v[0] == 0 and v[10] == 10 and v[1440] == 1440 and v[-1] == 1440


def test_short_touch_tp_exact_tick():
    m = D0 + 10 * MIN_MS
    ticks = base_ticks() + [(m + 1000, 100.0), (m + 2000, 103.0), (m + 3500, 105.2), (m + 4000, 104.0),
                            (m + 9000, 103.2), (m + 15000, 102.9)]
    st = FakeStore({SYM: ticks})
    res = run(st, cfg_of())
    t = res.trades
    assert len(t) == 1
    r = t.iloc[0]
    assert r.side == "SHORT" and r.signal_t == m + 3500
    assert r.entry_px == pytest.approx(105.2)
    assert r.reason == "TP" and r.exit_t == m + 15000      # 102.9 <= 105.2*0.98=103.096
    assert r.exit_px == pytest.approx(105.2 * 0.98)       # TP limit khớp đúng giá
    qty = 100 / 105.2
    gross = qty * (105.2 - 105.2 * 0.98)
    fees = 100 * 0.0005 + qty * 105.2 * 0.98 * 0.0002
    assert r.net_pnl == pytest.approx(gross - fees)
    assert r.resolution == "tick"


def test_latency_and_slippage_entry():
    m = D0 + 10 * MIN_MS
    ticks = base_ticks() + [(m + 1000, 100.0), (m + 2000, 105.0), (m + 2100, 105.5), (m + 2600, 106.0),
                            (m + 40000, 100.0)]
    st = FakeStore({SYM: ticks})
    res = run(st, cfg_of(**{"entry.latency_ms": 500, "entry.slippage_pct": 0.1}))
    r = res.trades.iloc[0]
    assert r.signal_t == m + 2000 and r.entry_t == m + 2600       # tick đầu tiên sau 500ms
    assert r.entry_px == pytest.approx(106.0 * (1 - 0.001))         # short: bán thấp hơn


def test_sl_gap_fill_at_tick_price_plus_slippage():
    m = D0 + 10 * MIN_MS
    ticks = base_ticks() + [(m + 1000, 100.0), (m + 2000, 105.0), (m + 70000, 106.0), (m + 80000, 109.0)]
    st = FakeStore({SYM: ticks})
    res = run(st, cfg_of(**{"exit.sl_slippage_pct": 0.1}))
    r = res.trades.iloc[0]
    assert r.reason == "SL" and r.exit_t == m + 80000
    assert r.exit_px == pytest.approx(109.0 * 1.001)       # gap qua SL 107.1 -> khớp 109 + trượt giá


def test_liquidation_before_sl():
    m = D0 + 10 * MIN_MS
    ticks = base_ticks() + [(m + 1000, 100.0), (m + 2000, 105.0), (m + 70000, 115.0)]
    st = FakeStore({SYM: ticks})
    res = run(st, cfg_of(**{"exit.stop_loss": 20, "position.leverage": 20}))
    r = res.trades.iloc[0]
    assert r.reason == "LIQ"
    assert r.gross_pnl == pytest.approx(-10)                # mất toàn bộ ký quỹ


def test_timeout():
    m = D0 + 10 * MIN_MS
    ticks = base_ticks() + [(m + 1000, 100.0), (m + 2000, 105.0), (m + 5 * MIN_MS, 104.5),
                            (m + 2000 + 30 * MIN_MS + 5, 104.0)]
    st = FakeStore({SYM: ticks})
    res = run(st, cfg_of())
    r = res.trades.iloc[0]
    assert r.reason == "TIMEOUT" and r.exit_t == m + 2000 + 30 * MIN_MS + 5 and r.exit_px == pytest.approx(104.0)


def test_1m_conservative_both_levels_same_bar_is_sl():
    m = D0 + 10 * MIN_MS
    # nến sau lệnh chạm cả TP và SL; thứ tự thật TP trước nhưng chỉ có nến 1m -> conservative = SL
    ticks = base_ticks() + [(m + 1000, 100.0), (m + 2000, 105.0), (m + 30000, 105.0),
                            (m + MIN_MS + 1000, 104.0), (m + MIN_MS + 2000, 102.0), (m + MIN_MS + 3000, 108.0),
                            (m + MIN_MS + 50000, 105.0)]
    st = FakeStore({SYM: ticks})
    r_tick = run(st, cfg_of()).trades.iloc[0]
    assert r_tick.reason == "TP"
    st_1m = FakeStore({SYM: ticks}, with_ticks=False)
    r_1m = run(st_1m, cfg_of(**{"data.resolution": "1m"})).trades.iloc[0]
    assert r_1m.reason == "SL" and bool(r_1m.ambiguous)
    r_opt = run(st_1m, cfg_of(**{"data.resolution": "1m", "data.intrabar_fallback": "optimistic"})).trades.iloc[0]
    assert r_opt.reason == "TP"


def test_1m_entry_bar_conservative():
    m = D0 + 10 * MIN_MS
    # nến vào lệnh: O=100, L=97 (trước điểm chạm), H=108 (sau điểm chạm -> SL 107.1 chắc chắn bị chạm)
    ticks = base_ticks() + [(m + 1000, 100.0), (m + 2000, 97.0), (m + 3000, 105.0), (m + 4000, 108.0), (m + 50000, 104.0)]
    st = FakeStore({SYM: ticks}, with_ticks=False)
    r = run(st, cfg_of(**{"data.resolution": "1m"})).trades.iloc[0]
    assert r.reason == "SL" and r.entry_px == pytest.approx(105.0)


def test_max_open_positions_and_exact_order_in_minute():
    m = D0 + 10 * MIN_MS
    a = base_ticks() + [(m + 1000, 100.0), (m + 40000, 105.0), (m + 5 * MIN_MS, 100.0)]
    b = base_ticks() + [(m + 1000, 100.0), (m + 10000, 105.0), (m + 5 * MIN_MS, 100.0)]
    st = FakeStore({"AAAUSDT": a, "BBBUSDT": b})
    res = run(st, cfg_of(**{"limits.max_open_positions": 1}))
    # BBB chạm sớm hơn trong cùng phút -> được vào, AAA bị chặn
    assert list(res.trades.symbol) == ["BBBUSDT"]
    assert res.rejected["max_open_positions"] == 1


def test_cooldowns_and_daily_limits():
    ticks = base_ticks()
    # 3 lần pump liên tiếp mỗi 10 phút, mỗi lần đều SL
    for k in range(3):
        m = D0 + (10 + 10 * k) * MIN_MS
        ticks += [(m + 1000, 100.0), (m + 2000, 105.0), (m + 3000, 108.0), (m + 4 * MIN_MS, 100.0)]
    st = FakeStore({SYM: ticks})
    res = run(st, cfg_of())
    assert (res.trades.reason == "SL").sum() == 3
    res = run(st, cfg_of(**{"limits.cooldown_after_sl_coin": "15m"}))
    assert len(res.trades) == 2 and res.rejected["coin_cooldown"] == 1
    res = run(st, cfg_of(**{"limits.max_sl_per_day": 1}))
    assert len(res.trades) == 1 and res.rejected["daily_max_sl"] == 2
    res = run(st, cfg_of(**{"limits.cooldown_after_sl_global": "30m"}))
    assert len(res.trades) == 1
    res = run(st, cfg_of(**{"limits.max_consecutive_sl": 2, "limits.pause_after_consecutive_sl": "1h"}))
    assert len(res.trades) == 2 and res.rejected["consecutive_sl_pause"] == 1


def test_sessions_overnight():
    ticks = base_ticks()
    m = D0 + 10 * MIN_MS  # 00:10 UTC
    ticks += [(m + 1000, 100.0), (m + 2000, 105.0), (m + 4 * MIN_MS, 100.0)]
    st = FakeStore({SYM: ticks})
    assert len(run(st, cfg_of(**{"schedule.sessions": ["22:00-01:00"]})).trades) == 1
    res = run(st, cfg_of(**{"schedule.sessions": ["01:00-22:00"]}))
    assert len(res.trades) == 0 and res.rejected["session"] == 1


def test_3m_touch_and_close_trigger():
    m = D0 + 12 * MIN_MS  # mở nến 3m
    ticks = base_ticks(px=100.0) + [(m + 1000, 100.0), (m + 30000, 102.0), (m + MIN_MS + 5000, 103.5),
                                     (m + 2 * MIN_MS + 1000, 106.5), (m + 2 * MIN_MS + 50000, 106.0),
                                     (m + 3 * MIN_MS + 500, 106.1), (m + 10 * MIN_MS, 100.0)]
    st = FakeStore({SYM: ticks})
    sig = [{"timeframe": "3m", "change_pct": 6, "direction": "up", "trigger": "touch", "side": "reverse"}]
    r = run(st, cfg_of(signals=sig)).trades.iloc[0]
    assert r.signal_t == m + 2 * MIN_MS + 1000 and r.entry_px == pytest.approx(106.5)
    sig = [{"timeframe": "3m", "change_pct": 5, "direction": "up", "trigger": "close", "side": "follow"}]
    r = run(st, cfg_of(signals=sig)).trades.iloc[0]
    assert r.side == "LONG" and r.signal_t == m + 3 * MIN_MS and r.entry_t == m + 3 * MIN_MS + 500


def test_roe_unit():
    m = D0 + 10 * MIN_MS
    ticks = base_ticks() + [(m + 1000, 100.0), (m + 2000, 105.0), (m + 50000, 100.0)]
    st = FakeStore({SYM: ticks})
    r = run(st, cfg_of(**{"exit.unit": "roe", "exit.take_profit": 20, "exit.stop_loss": 20})).trades.iloc[0]
    assert r.tp_px == pytest.approx(105 * 0.98) and r.sl_px == pytest.approx(105 * 1.02)
    assert not math.isnan(r.net_pnl)


def test_min_max_leverage_filter():
    m = D0 + 10 * MIN_MS
    t = base_ticks() + [(m + 1000, 100.0), (m + 2000, 105.0), (m + 50000, 100.0)]
    st = FakeStore({"AAAUSDT": t, "BBBUSDT": t})
    levs = {"AAAUSDT": 8.0, "BBBUSDT": 25.0}
    st.max_leverage = lambda sym, ts: (levs.get(sym), False)
    res = run(st, cfg_of(**{"universe.min_max_leverage": 10}))
    assert list(res.trades.symbol) == ["BBBUSDT"] and res.rejected["max_leverage"] == 1
    assert res.trades.iloc[0].max_lev == 25.0


PB_SIG = [{"timeframe": "1m", "change_pct": 5, "direction": "up", "trigger": "touch", "side": "reverse",
           "pullback_pct": 2, "pullback_window": "10m"}]


def _pb_ticks():
    m = D0 + 10 * MIN_MS
    # pump +5% (chạm 105 lúc m+2s), lên đỉnh 106, hồi 1,4% (chưa đủ), rồi hồi 2,08% từ đỉnh -> vào SHORT
    return m, base_ticks() + [(m + 1000, 100.0), (m + 2000, 105.0), (m + 30000, 106.0), (m + 90000, 104.5),
                              (m + 150000, 103.8), (m + 200000, 103.5), (m + 20 * MIN_MS, 101.0)]


def test_pullback_entry_after_drop_from_peak():
    m, ticks = _pb_ticks()
    st = FakeStore({SYM: ticks})
    r = run(st, cfg_of(signals=PB_SIG)).trades.iloc[0]
    assert r.side == "SHORT" and r.setup_t == m + 2000          # thời điểm tín hiệu pump
    assert r.signal_t == m + 150000 and r.entry_px == pytest.approx(103.8)   # 103.8 <= 106 * 0.98


def test_pullback_window_expires():
    m, ticks = _pb_ticks()
    st = FakeStore({SYM: ticks})
    sig = [dict(PB_SIG[0], pullback_window="2m")]               # hết 2 phút mà chưa hồi đủ -> bỏ
    res = run(st, cfg_of(signals=sig))
    assert len(res.trades) == 0 and res.rejected["no_pullback"] == 1


def test_pullback_with_1m_bars_only():
    m, ticks = _pb_ticks()
    st = FakeStore({SYM: ticks}, with_ticks=False)
    r = run(st, cfg_of(signals=PB_SIG, **{"data.resolution": "1m"})).trades.iloc[0]
    # nến m+2' mở cửa 103.8, đã thấp hơn đỉnh 106 * 0.98 = 103.88 -> vào lệnh ngay giá mở nến
    assert r.resolution == "1m" and r.entry_px == pytest.approx(103.8) and r.entry_t == m + 2 * MIN_MS


def test_chosen_coins_override_exclude():
    """Coin chỉ định ở 'Chỉ test các coin' vẫn được test dù nằm trong danh sách loại trừ (kèm ghi chú)."""
    from backtester.engine.backtest import prepare_universe

    class Store:
        def __init__(self, names):
            self._s = {n: SymbolInfo(n, n, n[:-4]) for n in names}

        def symbols(self):
            return self._s

        def resolve(self, name):
            n = name.upper()
            n = n if n.endswith("USDT") else n + "USDT"
            return self._s.get(n)

    st = Store(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    uni = prepare_universe(cfg_of(**{"universe.symbols": ["ETH"], "universe.exclude": ["BTCUSDT", "ETHUSDT"]}), st, None)
    assert uni.symbols == ["ETHUSDT"] and any("ETHUSDT" in n and "loại trừ" in n for n in uni.notes)
    # không chỉ định coin -> danh sách loại trừ vẫn áp dụng như cũ
    uni = prepare_universe(cfg_of(**{"universe.exclude": ["BTCUSDT", "ETHUSDT"]}), st, None)
    assert uni.symbols == ["SOLUSDT"] and not uni.notes


def test_daily_and_12h_timeframes():
    """Nến 1d / 12h dựng từ nến 1m, căn mốc 00:00 UTC; touch lấy phút chạm đầu tiên, close lấy lúc nến đóng."""
    from backtester.config import TIMEFRAMES, SignalRule
    from backtester.engine.signals import scan_symbol
    assert TIMEFRAMES["1d"] == 1440 and TIMEFRAMES["12h"] == 720
    ts = np.arange(D0, D0 + 2 * DAY_MS, MIN_MS)
    px = np.full(len(ts), 100.0)
    i10 = 600                                   # 10:00 UTC ngày đầu: giá lên 104 rồi giữ 103.5 tới hết ngày
    px[i10] = 104.0
    px[i10 + 1:1440] = 103.5
    px[1440:] = 103.5                           # ngày 2 đi ngang
    c = px.copy()
    o = px.copy()
    o[1:] = px[:-1]
    h, l, qv = np.maximum(o, c), np.minimum(o, c), np.ones(len(ts))
    mk = lambda tf, trig: SignalRule(timeframe=tf, change_pct=3, direction="up", trigger=trig, tf_minutes=TIMEFRAMES[tf])  # noqa: E731
    rules = [mk("1d", "touch"), mk("1d", "close"), mk("12h", "touch")]
    out = scan_symbol("X", ts, o, h, l, c, qv, rules, D0, D0 + 2 * DAY_MS)
    by = {r: [x for x in out if x.rule_idx == r] for r in range(3)}
    assert [(x.t, x.candle_ts) for x in by[0]] == [(D0 + i10 * MIN_MS, D0)]          # chạm lúc 10:00, nến ngày D0
    assert [(x.t, x.candle_ts) for x in by[1]] == [(D0 + DAY_MS, D0)]               # nến ngày đóng +3.5% lúc 00:00 hôm sau
    assert [x.candle_ts for x in by[2]] == [D0]                                      # nến 12h đầu (00:00–12:00 UTC)
