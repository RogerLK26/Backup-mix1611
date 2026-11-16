"""So engine (tìm kiếm nhị phân trên đỉnh/đáy mới) với duyệt brute-force từng tick, và kiểm tra chế độ vị thế.

    "../1. Back test Strategy/.venv/Scripts/python.exe" -m pytest -q tests
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from replay.engine import (R_OPEN, R_SL, R_TIMEOUT, R_TP, REASONS, Settings, simulate_trade,  # noqa: E402
                           take_mask)
from replay.paths import PricePath, _records  # noqa: E402

MIN = 60_000


def make_path(ts, px, entry_t, entry_px, side, window_min):
    hi_t, hi_px, hi_q, lo_t, lo_px, lo_q = _records(ts, px, entry_t)
    grid = entry_t + np.arange(window_min + 1) * MIN
    idx = np.searchsorted(ts, grid, "left")
    samp = np.full(window_min + 1, np.nan)
    ok = idx < len(ts)
    samp[ok] = px[idx[ok]]
    samp[0] = entry_px
    return PricePath("k", "X", side, entry_t, entry_px, "tick", "tick", window_min * MIN, int(ts[-1]) + 1,
                     hi_t, hi_px, hi_q, lo_t, lo_px, lo_q, samp, np.zeros(0, np.int64), np.zeros(0), len(ts))


def brute(ts, px, e, side, tp, sl, hold_min, entry_t, strict):
    lt = e * (1 + tp / 100) if side == 1 else e * (1 - tp / 100)
    ls = (e * (1 - sl / 100) if side == 1 else e * (1 + sl / 100)) if sl > 0 else None
    dl = entry_t + hold_min * MIN
    for t, p in zip(ts, px):
        if t >= dl:
            break
        hit_tp = (p > lt if strict else p >= lt) if side == 1 else (p < lt if strict else p <= lt)
        hit_sl = ls is not None and ((p <= ls) if side == 1 else (p >= ls))
        if hit_tp:
            return "TP", t, lt
        if hit_sl:
            return "SL", t, p
    k = np.searchsorted(ts, dl, "left")
    if k >= len(ts):
        return "OPEN", dl, np.nan
    return "TIMEOUT", dl, px[k]


@pytest.mark.parametrize("seed", range(25))
@pytest.mark.parametrize("side", [1, -1])
@pytest.mark.parametrize("strict", [False, True])
def test_matches_bruteforce(seed, side, strict):
    rng = np.random.default_rng(seed)
    n = 4000
    ts = np.cumsum(rng.integers(1, 4000, n)).astype(np.int64) + 1_000_000
    px = 100 * np.exp(np.cumsum(rng.normal(0, 0.0015, n)))
    px = np.round(px, 2)            # nhiều tick cùng giá -> kiểm tra phân xử bằng nhau
    entry_t, e = int(ts[0]) - 5, 100.0
    window = 120
    path = make_path(ts, px, entry_t, e, side, window)
    tps = np.array([0.3, 0.5, 1.0, 2.0, 5.0])
    sls = np.array([0.0, 0.5, 1.0, 3.0, 10.0])
    holds = np.array([5, 30, 60, 120])
    st = Settings(tp_fill="through" if strict else "touch", funding=False)
    r = simulate_trade(path, 40.0, 2.0, st, tps, sls, holds, window)
    for a, tp in enumerate(tps):
        for b, sl in enumerate(sls):
            for c, h in enumerate(holds):
                reason, t, x = brute(ts, px, e, side, tp, sl, h, entry_t, strict)
                got = REASONS[r.reason[a, b, c]]
                assert got == reason, (tp, sl, h, got, reason)
                if reason != "OPEN":
                    assert r.exit_t[a, b, c] == t
                    assert np.isclose(r.exit_px[a, b, c], x)


def test_pnl_short_tp_limit():
    ts = np.array([10, 20, 30], np.int64) + 1_000
    px = np.array([100.0, 99.5, 98.9])
    p = make_path(ts, px, 1_000, 100.0, -1, 10)
    st = Settings(maker_pct=0.02, taker_pct=0.05, funding=False)
    r = simulate_trade(p, 40.0, 2.0, st, np.array([1.0]), np.array([10.0]), np.array([10]), 10)
    assert r.reason.flat[0] == R_TP
    # 40 USDT notional, lãi 1% = 0.4, phí 40*0.05% + 39.6*0.02%
    assert np.isclose(r.pnl.flat[0], 0.4 - 0.02 - 0.00792)


def test_take_mask_single_vs_multi():
    # 3 lệnh cùng coin: vào 0, 10, 20; cấu hình A đóng sau 5, cấu hình B đóng sau 15
    entry = np.array([0, 10, 20], np.int64)
    coin = np.array([0, 0, 0])
    exit_t = np.array([[5, 15], [15, 25], [25, 35]], np.int64)
    valid = np.ones((3, 2), bool)
    single = take_mask(entry, coin, exit_t, valid, "single", 0, 0, 0)
    assert single[:, 0].tolist() == [True, True, True]
    assert single[:, 1].tolist() == [True, False, True]     # lệnh 2 bị bỏ vì lệnh 1 (B) chưa đóng
    multi = take_mask(entry, coin, exit_t, valid, "multi", 0, 0, 0)
    assert multi.all()
    multi2 = take_mask(entry, coin, exit_t, valid, "multi", 2, 0, 0)
    assert multi2.all()
    cap_total = take_mask(entry, np.array([0, 1, 2]), exit_t, valid, "multi", 0, 1, 0)
    assert cap_total[:, 1].tolist() == [True, False, True]


def test_open_when_data_ends():
    ts = np.array([10, 20], np.int64) + 1_000
    px = np.array([100.0, 100.1])
    p = make_path(ts, px, 1_000, 100.0, -1, 10)
    p.data_end = 1_000 + 2 * MIN
    r = simulate_trade(p, 40.0, 2.0, Settings(funding=False), np.array([1.0]), np.array([10.0]), np.array([5]), 10)
    assert r.reason.flat[0] == R_OPEN
    assert R_SL != R_TIMEOUT


def test_calibrate_recovers_known_sl_slippage():
    import pandas as pd
    from replay.engine import calibrate
    paths, rows = [], []
    for k in range(8):
        # short vào 100, giá lên 111 -> SL 10% kích hoạt ở tick 111
        ts = np.array([10, 20, 30], np.int64) + 1_000 + k
        px = np.array([105.0, 111.0, 112.0])
        p = make_path(ts, px, 1_000 + k, 100.0, -1, 10)
        paths.append(p)
        exit_px = 111.0 * 1.004                     # thực tế khớp tệ hơn 0.4%
        gross = -1 * (40.0 / 100.0) * (exit_px - 100.0)
        rows.append({"reason": "SL", "pnl": gross - 0.05, "fee": -0.05, "funding": 0.0, "notional": 40.0, "margin": 2.0})
    trades = pd.DataFrame(rows)
    c = calibrate(trades, paths, Settings(funding=False, notional=40.0), 1.0, 10.0, 10, 10)
    assert c["n_sl"] == 8
    assert abs(c["sl_slip"] - 0.4) < 1e-3


def test_stability_block():
    from replay.engine import stability_block
    H = 3_600_000
    # lãi đều mỗi giờ -> R² = 1; 2 ngày đều lãi; bỏ coin B (lãi nhiều nhất) còn phần của A
    pnl = np.array([1.0, 1.0, 1.0, 1.0, 1.0, 1.0])
    t = np.arange(6) * H
    day = np.array([0, 0, 0, 1, 1, 1])
    coin = np.array([0, 1, 1, 0, 1, 1])
    s = stability_block(pnl, t, day, coin, ["A", "B"])
    assert s["r2"] == pytest.approx(1.0)
    assert s["day_pos"] == 100 and s["n_days"] == 2
    assert s["top3_day"] == pytest.approx(100.0)
    assert s["best_coin"] == "B" and s["best_coin_pnl"] == 4 and s["net_xcoin"] == 2
    # đường vốn đi xuống -> R² mang dấu âm; lỗ ròng -> không có % lãi từ 3 ngày tốt nhất
    s = stability_block(-pnl, t, day, coin, ["A", "B"])
    assert s["r2"] == pytest.approx(-1.0) and s["top3_day"] is None and s["day_pos"] == 0
    # 1 ngày bùng lên: lãi dồn vào ngày đó
    s = stability_block(np.array([0.1, 0.1, 5.0, -0.1]), np.arange(4) * H, np.array([0, 1, 2, 3]), np.zeros(4, int), ["A"])
    assert s["top3_day"] > 100 and s["day_pos"] == 75 and s["net_xcoin"] == pytest.approx(0)
    assert stability_block(np.array([]), np.array([]), np.array([]), np.array([], int), [])["r2"] is None


@pytest.mark.parametrize("seed", range(5))
def test_reverse_direction_equals_path_built_for_other_side(seed):
    """Ngược hướng = đảo side của đường giá đã trích (dữ liệu đỉnh / đáy không phụ thuộc hướng)."""
    import dataclasses
    rng = np.random.default_rng(seed)
    ts = np.cumsum(rng.integers(1, 4000, 3000)).astype(np.int64) + 1_000_000
    px = np.round(100 * np.exp(np.cumsum(rng.normal(0, 0.0015, 3000))), 2)
    entry_t = int(ts[0]) - 5
    tps, sls, holds = np.array([0.5, 1.0, 2.0]), np.array([0.0, 1.0, 5.0]), np.array([30, 120])
    st = Settings(funding=False)
    for side in (1, -1):
        rev = dataclasses.replace(make_path(ts, px, entry_t, 100.0, -side, 120), side=side)
        ref = make_path(ts, px, entry_t, 100.0, side, 120)
        a = simulate_trade(rev, 40.0, 2.0, st, tps, sls, holds, 120)
        b = simulate_trade(ref, 40.0, 2.0, st, tps, sls, holds, 120)
        assert (a.reason == b.reason).all() and (a.exit_t == b.exit_t).all()
        assert np.allclose(np.nan_to_num(a.pnl), np.nan_to_num(b.pnl))
