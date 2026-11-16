"""Engine nhanh (engine/fast.py) phải cho đúng các lệnh như engine đầy đủ với mọi tổ hợp TP / SL / giữ lệnh.

Chạy: python -m pytest -q tests/test_fast.py
"""
from __future__ import annotations

import itertools
import math

import numpy as np
import pytest

from backtester.engine.backtest import Backtester, Universe, scan_candidates
from backtester.engine.fast import ExitAxis, FastEvaluator, fast_result, quick_stats
from backtester.optimize import Grid, parse_values
from backtester.stats import summarize
from backtester.utils import DAY_MS, MIN_MS, parse_duration

from test_engine import D0, FakeStore, cfg_of

SYMS = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]


def _walk(seed: int) -> list[tuple[int, float]]:
    """Giá ngẫu nhiên có nhiều cú pump/dump để sinh tín hiệu, có cả những phút không có giao dịch."""
    rng = np.random.default_rng(seed)
    t, px, out = D0 - 2 * 3600_000, 100.0, []
    end = D0 + 2 * DAY_MS + 3 * 3600_000
    shock = 0.0
    while t < end:
        t += int(rng.integers(1500, 6000))
        if rng.random() < 0.0009:                       # bắt đầu cú biến động mạnh
            shock = rng.choice([0.07, -0.06, 0.09])
        step = shock * 0.12 if shock else 0.0
        shock = shock * 0.88 if abs(shock) > 0.004 else 0.0
        px *= math.exp(step + rng.normal(0, 0.0016))
        if rng.random() < 0.002:                        # khoảng trống không có tick (sàn vắng)
            t += int(rng.integers(60_000, 240_000))
        out.append((t, round(px, 6)))
    return out


@pytest.fixture(scope="module")
def data():
    return {s: _walk(i + 7) for i, s in enumerate(SYMS)}


def _universe():
    return Universe(SYMS, {s: {D0, D0 + DAY_MS} for s in SYMS})


AXIS_TP = [0.5, 1.5, 3, None]
AXIS_SL = [1, 4, None]
AXIS_HOLD = ["3m", "30m", None]

CASES = {
    "tick_limit": dict(),
    "tick_market_through_latency": {"exit.tp_order": "market", "exit.tp_fill": "through", "entry.latency_ms": 400,
                                    "entry.slippage_pct": 0.1, "exit.sl_slippage_pct": 0.1, "exit.exit_slippage_pct": 0.05},
    "tick_limits": {"limits.max_open_positions": 2, "limits.cooldown_after_sl_coin": "30m", "limits.max_sl_per_day": 4,
                    "limits.cooldown_after_tp_coin": "5m", "limits.max_consecutive_sl": 2,
                    "limits.pause_after_consecutive_sl": "1h", "position.margin_mode": "percent", "position.margin": 5},
    "tick_roe_lev": {"exit.unit": "roe", "position.leverage": 20},
    "tick_close_3m_both": {"signals": [{"timeframe": "3m", "change_pct": 3, "direction": "both", "trigger": "close",
                                        "side": "follow"}], "schedule.sessions": ["01:00-11:00", "14:00-23:00"]},
    "tick_max_leverage": {"universe.min_max_leverage": 10, "__lev": True},
    "tick_pullback": {"signals": [{"timeframe": "1m", "change_pct": 3, "direction": "both", "trigger": "touch",
                                   "side": "reverse", "pullback_pct": 1.5, "pullback_window": "10m"}],
                      "limits.max_open_positions": 2, "limits.cooldown_after_sl_coin": "20m"},
    "m1_pullback_close": {"__ticks": False, "signals": [{"timeframe": "3m", "change_pct": 3, "direction": "up",
                          "trigger": "close", "side": "reverse", "pullback_pct": 1, "pullback_window": "15m"}]},
    "m1_conservative": {"__ticks": False},
    "m1_ohlc_market": {"__ticks": False, "data.intrabar_fallback": "ohlc", "exit.tp_order": "market"},
    "m1_optimistic_close": {"__ticks": False, "data.intrabar_fallback": "optimistic",
                            "signals": [{"timeframe": "5m", "change_pct": 3, "direction": "up", "trigger": "close",
                                         "side": "reverse"}]},
}


def _cfg(over: dict, tp=None, sl=None, hold=None):
    o = {k: v for k, v in over.items() if not k.startswith("__")}
    return cfg_of(**{**o, "exit.take_profit": tp, "exit.stop_loss": sl, "exit.max_hold": hold})


@pytest.mark.parametrize("case", list(CASES))
def test_fast_matches_full_engine(data, case):
    over = CASES[case]
    store = FakeStore(data, with_ticks=over.get("__ticks", True))
    if over.get("__lev"):   # AAA bị sàn giới hạn x5 -> bị loại; CCC không có dữ liệu đòn bẩy -> vẫn giao dịch
        store.max_leverage = lambda sym, t: ({"AAAUSDT": 5.0, "BBBUSDT": 20.0}.get(sym), False)
    uni = _universe()
    base = _cfg(over, 1.5, 4, "30m")
    cands = scan_candidates(base, store, uni)
    assert len(cands) > 20, "dữ liệu giả lập phải sinh đủ tín hiệu"
    axis = ExitAxis(AXIS_TP, AXIS_SL, [parse_duration(h) for h in AXIS_HOLD])
    ev = FastEvaluator(Backtester(base, store, None, uni), cands, axis)
    checked = 0
    for (i, tp), (j, sl), (k, hold) in itertools.product(enumerate(AXIS_TP), enumerate(AXIS_SL), enumerate(AXIS_HOLD)):
        if tp is None and sl is None and hold is None:
            continue
        cfg = _cfg(over, tp, sl, hold)
        exact = Backtester(cfg, store, None, uni).run(scan_candidates(cfg, store, uni))
        book, _ = ev.run(cfg, i, j, k)
        got = sorted(book.closed, key=lambda r: (r["entry_t"], r["symbol"]))
        want = exact.trades.to_dict("records") if len(exact.trades) else []
        label = f"{case}: tp={tp} sl={sl} hold={hold}"
        assert len(got) == len(want), label
        if over.get("__lev"):
            assert want and all(w["symbol"] != "AAAUSDT" for w in want), label
        for g, w in zip(got, want):
            assert (g["symbol"], g["entry_t"], g["exit_t"], g["reason"]) == \
                   (w["symbol"], w["entry_t"], w["exit_t"], w["reason"]), label
            assert g["net_pnl"] == pytest.approx(w["net_pnl"], rel=1e-9, abs=1e-9), label
            assert g["resolution"] == w["resolution"], label
        # chỉ số tổng hợp trùng với stats.summarize
        qs, ss = quick_stats(book.closed, cfg.initial_balance), summarize(exact)
        for key in ("trades", "net_pnl", "win_rate_pct", "max_drawdown", "max_drawdown_pct", "tp", "sl", "timeouts",
                    "hours_pos", "top5h_pnl"):
            assert qs[key] == pytest.approx(ss.get(key, 0), rel=1e-9, abs=1e-9), f"{label} {key}"
        # báo cáo chi tiết dựng từ engine nhanh: bảng lệnh trùng engine đầy đủ ở mọi cột (trừ cờ "mơ hồ")
        fr = fast_result(ev, cfg, i, j, k, uni)
        if len(want):
            cols = [c for c in exact.trades.columns if c != "ambiguous"]
            ft, et = fr.trades[cols].reset_index(drop=True), exact.trades[cols].reset_index(drop=True)
            for c in cols:
                if ft[c].dtype.kind in "fc" or et[c].dtype.kind in "fc":
                    a, b = ft[c].astype(float).to_numpy(), et[c].astype(float).to_numpy()
                    assert np.allclose(a, b, rtol=1e-9, atol=1e-9, equal_nan=True), f"{label} cột {c}"
                else:
                    assert ft[c].tolist() == et[c].tolist(), f"{label} cột {c}"
            fs, es = summarize(fr), summarize(exact)
            for key in ("trades", "net_pnl", "max_drawdown", "win_rate_pct", "fees", "funding", "hours_pos", "top5h_pnl"):
                assert fs[key] == pytest.approx(es[key], rel=1e-9, abs=1e-9), f"{label} summary {key}"
        else:
            assert len(fr.trades) == 0
        checked += 1
    assert checked == len(AXIS_TP) * len(AXIS_SL) * len(AXIS_HOLD) - 1


class _Ex:
    default_fees = (0.02, 0.05)
    tick_history_days = None


def _sweep_env(data, tmp_path):
    from backtester.stats import summarize as summ_fn
    store = FakeStore(data)
    store.ex = _Ex()
    store.prefetch_ticks = lambda items: None
    uni = _universe()

    def prepare_fn(raw, exchange):
        from backtester.config import build_config
        return build_config(dict(raw, exchange=exchange), _Ex.default_fees), store, None, uni

    def run_one_fn(raw, exchange, out_dir, prepared=None):
        from backtester.config import build_config
        cfg = build_config(dict(raw, exchange=exchange), _Ex.default_fees)
        res = Backtester(cfg, store, None, uni).run(scan_candidates(cfg, store, uni))
        return res, summ_fn(res)
    return prepare_fn, run_one_fn


def _raw(**extra):
    raw = {
        "name": "t", "exchange": "binance", "start": "2026-01-05 00:00", "end": "2026-01-07 00:00",
        "timezone": "UTC", "initial_balance": 1000, "data": {"resolution": "auto"},
        "universe": {"market_cap_source": "none"},
        "signals": [{"timeframe": "1m", "change_pct": 5, "direction": "up", "trigger": "touch", "side": "reverse"}],
        "position": {"margin": 10, "leverage": 10},
        "exit": {"take_profit": 2, "stop_loss": 2, "max_hold": "30m"},
        "fees": {"maker_pct": 0.02, "taker_pct": 0.05, "funding": False}, "limits": {},
    }
    raw.update(extra)
    return raw


def test_run_sweep_grid_top_k_matches_exact(data, tmp_path):
    import json
    from backtester.optimize import run_sweep
    prep, one = _sweep_env(data, tmp_path)
    raw = _raw(sweep={"exit.take_profit": {"from": 0.5, "to": 3, "step": 0.5}, "exit.stop_loss": [1, 3, None],
                      "exit.max_hold": ["5m", "1h"], "limits.max_open_positions": [1, 3]},
               optimize={"objective": "net_pnl", "min_trades": 3, "top_k": 3, "oos_pct": 25})
    rows = run_sweep(raw, "binance", tmp_path / "binance", prep, one)
    assert len(rows) == 3
    for r in rows:   # chạy lại bằng engine đầy đủ phải ra đúng số liệu của engine nhanh
        assert r["net_pnl"] == pytest.approx(r["fast_net_pnl"], abs=1e-9)
    assert rows[0]["fast_net_pnl"] >= rows[1]["fast_net_pnl"] >= rows[2]["fast_net_pnl"]
    S = json.loads((tmp_path / "binance" / "sweep.json").read_text(encoding="utf-8"))
    assert S["evaluated"] == S["total"] == 6 * 3 * 2 * 2 and S["mode"] == "grid"
    assert "oos_pnl" in S["cols"] and S["split_t"] and sum(1 for t in S["top"] if t["rank"]) == 3
    assert (tmp_path / "binance" / "sweep.csv").exists()


def test_run_sweep_two_objectives(data, tmp_path):
    import json
    from backtester.optimize import run_sweep
    prep, one = _sweep_env(data, tmp_path)
    raw = _raw(sweep={"exit.take_profit": {"from": 0.5, "to": 3, "step": 0.5}, "exit.stop_loss": [1, 3, None],
                      "exit.max_hold": ["5m", "1h"]},
               optimize={"objective": "net_pnl", "objective2": "top5h", "min_trades": 3, "top_k": 3})
    rows = run_sweep(raw, "binance", tmp_path / "binance", prep, one)
    r1 = sorted((r for r in rows if r.get("rank")), key=lambda r: r["rank"])
    r2 = sorted((r for r in rows if r.get("rank2")), key=lambda r: r["rank2"])
    assert [r["rank"] for r in r1] == [1, 2, 3] and [r["rank2"] for r in r2] == [1, 2, 3]
    assert len({r["dir"] for r in rows}) == len(rows) <= 6          # không chạy trùng
    for r in rows:
        assert r["net_pnl"] == pytest.approx(r["fast_net_pnl"], abs=1e-9)
    assert r2[0]["top5h_pnl"] >= r2[1]["top5h_pnl"] >= r2[2]["top5h_pnl"]
    S = json.loads((tmp_path / "binance" / "sweep.json").read_text(encoding="utf-8"))
    assert S["objective2"] == "top5h" and "score2" in S["cols"] and "top5h_pnl" in S["cols"]


def test_run_sweep_full_engine_detail_matches_fast(data, tmp_path):
    from backtester.optimize import run_sweep
    prep, one = _sweep_env(data, tmp_path)
    base = {"exit.take_profit": [0.5, 1.5, 3], "exit.stop_loss": [1, 4], "exit.max_hold": ["5m", "1h"]}
    rf = run_sweep(_raw(sweep=base, optimize={"top_k": 4, "min_trades": 3}), "binance", tmp_path / "f", prep, one)
    re_ = run_sweep(_raw(sweep=base, optimize={"top_k": 4, "min_trades": 3, "detail_engine": "full"}),
                    "binance", tmp_path / "e", prep, one)
    assert [r["params"] for r in rf] == [r["params"] for r in re_]
    for a, b in zip(rf, re_):
        assert a["detail_engine"] == "fast" and b["detail_engine"] == "full"
        for k in ("trades", "net_pnl", "max_drawdown", "fees", "hours_pos", "top5h_pnl"):
            assert a[k] == pytest.approx(b[k], rel=1e-9, abs=1e-9), k
    assert (tmp_path / "f" / "top_01" / "trades.csv").exists() and (tmp_path / "f" / "top_01" / "report.html").exists()


def test_run_sweep_merges_identical_results(data, tmp_path):
    import json
    from backtester.optimize import run_sweep
    prep, one = _sweep_env(data, tmp_path)
    # SL 40/50/60% không bao giờ chạm (thanh lý / hết giờ đến trước) -> các tổ hợp chỉ khác SL cho kết quả giống hệt
    raw = _raw(sweep={"exit.take_profit": [1, 2, 3], "exit.stop_loss": [40, 50, 60], "exit.max_hold": ["30m"]},
               position={"margin": 10, "leverage": 2}, optimize={"top_k": 3, "min_trades": 1})
    rows = run_sweep(raw, "binance", tmp_path / "d", prep, one)
    assert len(rows) == 3                                         # 3 kết quả khác nhau (theo TP), không phải 9
    assert len({r["net_pnl"] for r in rows}) == 3
    assert all(r["dupes"] == 2 and len(r["dupe_params"]) == 2 for r in rows)
    S = json.loads((tmp_path / "d" / "sweep.json").read_text(encoding="utf-8"))
    assert len(S["top"]) == 9 and sum(1 for t in S["top"] if t["rank"]) == 3   # bản trùng trỏ về báo cáo đại diện


@pytest.mark.parametrize("obj2", ["top5h", "hours_pos"])
def test_second_ranking_is_independent(data, tmp_path, obj2):
    """Top theo tiêu chí 2 lấy trên TOÀN BỘ chiến lược đạt điều kiện, không lọc từ top của tiêu chí 1."""
    import json
    from backtester.optimize import run_sweep, score
    prep, one = _sweep_env(data, tmp_path)
    raw = _raw(sweep={"exit.take_profit": {"from": 0.5, "to": 3, "step": 0.5}, "exit.stop_loss": [1, 2, 4, None],
                      "exit.max_hold": ["5m", "30m", "2h"]},
               optimize={"objective": "net_pnl", "objective2": obj2, "min_trades": 3, "top_k": 3})
    rows = run_sweep(raw, "binance", tmp_path / "o", prep, one)
    S = json.loads((tmp_path / "o" / "sweep.json").read_text(encoding="utf-8"))
    ci = {c: len(S["keys"]) + i for i, c in enumerate(S["cols"])}
    ok = [r for r in S["rows"] if r[ci["ok"]]]
    # top 3 kết quả khác nhau theo tiêu chí 2 tính độc lập từ toàn bộ bảng
    best2, seen = [], set()
    for r in sorted(ok, key=lambda r: -r[ci["score2"]]):
        sig = (r[ci["trades"]], round(r[ci["net_pnl"]], 6), round(r[ci["max_drawdown"]], 6))
        if sig not in seen:
            seen.add(sig)
            best2.append(r[ci["score2"]])
        if len(best2) == 3:
            break
    got2 = [score(r, obj2) for r in sorted((r for r in rows if r.get("rank2")), key=lambda r: r["rank2"])]
    assert got2 == pytest.approx(best2, abs=1e-9)
    # và top 3 của tiêu chí 1 cũng tính trên toàn bộ bảng
    got1 = [r["net_pnl"] for r in sorted((r for r in rows if r.get("rank")), key=lambda r: r["rank"])]
    assert got1[0] == pytest.approx(max(r[ci["net_pnl"]] for r in ok), abs=1e-9)


def test_sweep_hours_file_matches_trades(data, tmp_path):
    """sweep_hours.json: lãi/lỗ, số lệnh, lệnh lãi theo giờ vào lệnh – khớp tổng và khớp danh sách lệnh chi tiết."""
    import json
    import pandas as pd
    from backtester.optimize import run_sweep
    prep, one = _sweep_env(data, tmp_path)
    raw = _raw(sweep={"exit.take_profit": [0.5, 1.5, 3], "exit.stop_loss": [1, 4], "exit.max_hold": ["5m", "1h"]},
               timezone="Asia/Ho_Chi_Minh", optimize={"top_k": 2, "min_trades": 1})
    run_sweep(raw, "binance", tmp_path / "h", prep, one)
    S = json.loads((tmp_path / "h" / "sweep.json").read_text(encoding="utf-8"))
    H = json.loads((tmp_path / "h" / "sweep_hours.json").read_text(encoding="utf-8"))["rows"]
    assert len(H) == len(S["rows"])
    ci = {c: len(S["keys"]) + i for i, c in enumerate(S["cols"])}
    for r, (hp, ht, hw) in zip(S["rows"], H):
        assert len(hp) == len(ht) == len(hw) == 24
        assert sum(hp) == pytest.approx(r[ci["net_pnl"]], abs=1e-3) and sum(ht) == r[ci["trades"]]
        assert sum(hw) == round(r[ci["win_rate_pct"]] * r[ci["trades"]] / 100)
    t = next(x for x in S["top"] if x["rank"] == 1)
    tr = pd.read_csv(tmp_path / "h" / "top_01" / "trades.csv")
    hour = tr.entry_t.str.slice(11, 13).astype(int)          # giờ địa phương (Asia/Ho_Chi_Minh)
    hp, ht, hw = H[t["row"]]
    for hh in range(24):
        sel = tr[hour == hh]
        assert ht[hh] == len(sel) and hw[hh] == int((sel.net_pnl > 0).sum())
        assert hp[hh] == pytest.approx(sel.net_pnl.sum(), abs=1e-3)


def test_top_k_limit_200():
    assert Grid({"sweep": {"exit.take_profit": [1]}, "optimize": {"top_k": 500}}).top_k == 200
    assert Grid({"sweep": {"exit.take_profit": [1]}, "optimize": {"top_k": 150}}).top_k == 150


def test_run_sweep_smart_mode_respects_budget(data, tmp_path):
    import json
    from backtester.optimize import run_sweep
    prep, one = _sweep_env(data, tmp_path)
    raw = _raw(sweep={"exit.take_profit": {"from": 0.2, "to": 6, "step": 0.2}, "exit.stop_loss": {"from": 0.5, "to": 10, "step": 0.5},
                      "exit.max_hold": ["5m", "15m", "1h", "4h"]},
               optimize={"objective": "return_dd", "top_k": 1, "max_combos": 300})
    run_sweep(raw, "binance", tmp_path / "x", prep, one)
    S = json.loads((tmp_path / "x" / "sweep.json").read_text(encoding="utf-8"))
    assert S["mode"] == "smart" and S["total"] == 30 * 20 * 4 and 180 <= S["evaluated"] <= 300


def test_parse_values_ranges_and_lists():
    assert parse_values("exit.take_profit", {"from": 0.5, "to": 2, "step": 0.5}) == [0.5, 1.0, 1.5, 2.0]
    assert parse_values("exit.take_profit", {"from": 0.1, "to": 0.3, "step": 0.1}) == [0.1, 0.2, 0.3]
    assert parse_values("exit.stop_loss", "1, 2, none, 2") == [1, 2, None]
    assert parse_values("exit.max_hold", {"from": "15m", "to": "1h", "step": "15m"}) == ["15m", "30m", "45m", "1h"]
    assert parse_values("exit.max_hold", {"from": 30, "to": 90, "step": 30}) == ["30m", "1h", "90m"]
    assert parse_values("limits.max_open_positions", [3, 5]) == [3, 5]
    with pytest.raises(ValueError):
        parse_values("exit.take_profit", [0, 1])
    with pytest.raises(ValueError):
        parse_values("exit.take_profit", {"from": 1, "to": 5000, "step": 0.001})
    with pytest.raises(ValueError):
        Grid({"sweep": {"universe.min_volume_24h": [1, 2]}})
    g = Grid({"sweep": {"exit.take_profit": [1, 2, 3], "exit.stop_loss": {"from": 1, "to": 10, "step": 1}},
              "optimize": {"max_combos": 20}})
    assert g.total == 30 and g.mode == "smart"
