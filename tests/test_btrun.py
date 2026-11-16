"""Bản gộp: lưới backtest (btmix/strategy.py) phải khớp với engine nhanh chạy trực tiếp.

Kiểm tra:
  1. mỗi dòng bảng xếp hạng = đúng kết quả FastEvaluator.run cho tổ hợp đó (số lệnh, lãi ròng, TP/SL/hết giờ);
  2. chi tiết 1 cấu hình = đúng các lệnh của tổ hợp đó;
  3. lọc theo giờ vào lệnh trong trang chi tiết = chạy lại sổ lệnh với khung giờ đó (không chỉ lọc bớt dòng).

Chạy: python -m pytest -q tests/test_btrun.py
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from backtester.config import build_config
from backtester.engine.backtest import Backtester, Universe, scan_candidates
from backtester.engine.fast import ExitAxis, FastEvaluator
from backtester.utils import DAY_MS, parse_duration

from btmix.strategy import BtRun, _sessions_for, normalize_sweep

from test_engine import D0, FakeStore
from test_fast import _walk

SYMS = ["AAAUSDT", "BBBUSDT"]


RAW = {
    "name": "mix", "exchange": "binance", "start": "2026-01-05 00:00", "end": "2026-01-07 00:00",
    "timezone": "UTC", "initial_balance": 1000,
    "data": {"resolution": "auto", "intrabar_fallback": "conservative"},
    "universe": {"market_cap_source": "none"},
    "signals": [{"timeframe": "1m", "change_pct": 4, "direction": "up", "trigger": "touch", "side": "reverse"}],
    "entry": {"latency_ms": 0, "slippage_pct": 0},
    "position": {"margin": 10, "leverage": 10},
    "exit": {"take_profit": 1, "stop_loss": 3, "max_hold": "30m", "liquidation": True},
    "fees": {"maker_pct": 0.02, "taker_pct": 0.05, "funding": False},
    "limits": {"max_open_positions": 3},
    "sweep": {"exit.take_profit": [0.5, 1, 2], "exit.stop_loss": [2, 5], "exit.max_hold": ["15m", "1h"]},
    "optimize": {"objective": "net_pnl", "min_trades": 1},
}


class _Ex:
    name = "binance"
    default_fees = (0.02, 0.05)
    tick_history_days = None


class Store(FakeStore):
    """FakeStore + vài thuộc tính mà lớp điều phối lưới cần (sàn, tải trước tick)."""
    ex = _Ex()

    def prefetch_ticks(self, items):
        return None


@pytest.fixture(scope="module")
def prepared():
    store = Store({s: _walk(i + 11) for i, s in enumerate(SYMS)})
    cfg = build_config(dict(RAW, sweep=None, optimize=None))
    uni = Universe(SYMS, {s: {D0, D0 + DAY_MS} for s in SYMS})
    return cfg, store, None, uni


@pytest.fixture(scope="module")
def run(prepared):
    r = BtRun(RAW, "binance", prepared=prepared)
    r.execute()
    return r


def test_rows_match_fast_engine(run, prepared):
    cfg, store, _, uni = prepared
    cands = scan_candidates(cfg, store, uni)
    axis = ExitAxis([0.5, 1, 2], [2, 5], [parse_duration(x) for x in ("15m", "1h")])
    ev = FastEvaluator(Backtester(cfg, store, None, uni), cands, axis)
    assert len(run.rows) == 3 * 2 * 2
    for row in run.rows:
        i = axis.tps.index(row["tp"])
        j = axis.sls.index(row["sl"])
        k = axis.holds.index(row["hold"] * 60_000)
        book, _ = ev.run(cfg, i, j, k)
        closed = book.closed
        assert row["n"] == len(closed)
        assert row["net"] == pytest.approx(sum(r["net_pnl"] for r in closed), abs=1e-9)
        assert row["n_tp"] == sum(1 for r in closed if r["reason"] == "TP")
        assert row["n_sl"] == sum(1 for r in closed if r["reason"] == "SL")
        assert row["n_timeout"] == sum(1 for r in closed if r["reason"] == "TIMEOUT")


def test_detail_matches_row(run):
    row = max(run.rows, key=lambda r: r["n"])
    d = run.detail(row["ci"])
    assert len(d["trades"]) == row["n"]
    assert sum(t["pnl"] for t in d["trades"]) == pytest.approx(row["net"], abs=1e-9)
    assert d["stats"]["n"] == row["n"]
    # tổng theo coin / theo giờ = tổng chung
    assert sum(c["pnl"] for c in d["by_coin"]) == pytest.approx(row["net"], abs=1e-9)
    assert sum(h["sim"] for h in d["by_hour"]) == pytest.approx(row["net"], abs=1e-9)
    assert len(d["by_hour"]) == 24


def test_detail_hours_reruns_book(run):
    row = max(run.rows, key=lambda r: r["n"])
    full = run.detail(row["ci"])
    hours = sorted({t["hour"] for t in full["trades"]})[:2]
    part = run.detail(row["ci"], hours=hours)
    assert part["hours"] == hours
    assert all(t["hour"] in hours for t in part["trades"])
    # chạy lại sổ lệnh: các lệnh ngoài giờ không còn chiếm chỗ -> có thể có thêm lệnh so với chỉ lọc bớt dòng
    kept = [t for t in full["trades"] if t["hour"] in hours]
    assert len(part["trades"]) >= len(kept)


def test_sessions_intersection():
    assert _sessions_for([1, 2, 5], []) == [(60, 180), (300, 360)]
    # giao với khung giờ đang đặt (kể cả khung qua đêm 22:00-02:00)
    assert _sessions_for([1, 23], [(1320, 120)]) == [(1380, 1440), (60, 120)]


def test_neighbours_and_stability(run):
    row = max(run.rows, key=lambda r: r["n"])
    assert row["nb_cnt"] > 0
    assert row["n_days"] >= 1
    assert row["day_pos"] is None or 0 <= row["day_pos"] <= 100
    if row["h8_hours"]:
        assert len(row["h8_hours"]) <= 8
        assert row["h8_n"] <= row["n"]


def test_normalize_sweep():
    """Ô nhập lưới chấp nhận cả danh sách lẫn khoảng "từ-đến:bước", và 0 = không đặt."""
    n = normalize_sweep({"exit.take_profit": "0.5-2:0.5"})["exit.take_profit"]
    assert n == [0.5, 1.0, 1.5, 2.0]
    assert normalize_sweep({"exit.stop_loss": "3, 5, 10"})["exit.stop_loss"] == [3, 5, 10]
    # trộn danh sách + khoảng, bỏ giá trị trùng
    assert normalize_sweep({"exit.take_profit": "1, 1-2:0.5"})["exit.take_profit"] == [1, 1.5, 2.0]
    # 0 = không đặt SL / không giới hạn thời gian giữ lệnh
    assert normalize_sweep({"exit.stop_loss": "3, 0"})["exit.stop_loss"] == [3, None]
    assert normalize_sweep({"exit.max_hold": "30m, 0"})["exit.max_hold"] == ["30m", None]
    # khoảng thời gian
    assert normalize_sweep({"exit.max_hold": "30m-2h:30m"})["exit.max_hold"] == ["30m", "1h", "90m", "2h"]
    # tham số khác giữ nguyên kiểu
    assert normalize_sweep({"position.leverage": [5, 10]})["position.leverage"] == [5, 10]


def test_grid_accepts_range_text(prepared):
    """Lưới gõ dạng khoảng chạy được thật (trước đây engine chỉ hiểu {from,to,step})."""
    raw = dict(RAW, sweep={"exit.take_profit": "0.5-1:0.5", "exit.stop_loss": "2, 5", "exit.max_hold": "15m, 1h"})
    r = BtRun(raw, "binance", prepared=prepared)
    r.execute()
    assert len(r.rows) == 2 * 2 * 2
    assert sorted({row["tp"] for row in r.rows}) == [0.5, 1.0]


def test_sweep_signal_params(prepared):
    """Lưới quét được cả tham số tín hiệu (khung nến, biến động %) chứ không chỉ TP/SL/giữ lệnh."""
    raw = dict(RAW, sweep={"exit.take_profit": "1", "exit.stop_loss": "3",
                           "signals.0.timeframe": "1m, 5m", "signals.0.change_pct": "3, 4"})
    r = BtRun(raw, "binance", prepared=prepared)
    r.execute()
    assert len(r.rows) == 4
    # mỗi tổ hợp tín hiệu cho tập lệnh khác nhau -> số lệnh không giống hệt nhau
    assert len({row["n"] for row in r.rows}) > 1
    # nhãn tiếng Việt, không phải khoá YAML
    assert "Khung nến (tín hiệu 1)" in r.rows[0]["params"]
    assert r.summary["labels"][-2:] == ["Khung nến (tín hiệu 1)", "Biến động (tín hiệu 1)"]


def test_rows_keep_grid_order(prepared):
    """Dòng giữ đúng thứ tự giá trị đã nhập trong lưới (không phải thứ tự chữ cái: '15m' < '1h' < '5m')."""
    raw = dict(RAW, sweep={"exit.take_profit": "2, 1", "exit.stop_loss": "3",
                           "signals.0.timeframe": "5m, 1m", "exit.max_hold": "1h"})
    r = BtRun(raw, "binance", prepared=prepared)
    r.execute()
    assert [row["tp"] for row in r.rows] == [2.0, 2.0, 1.0, 1.0]
    assert [row["values"]["signals.0.timeframe"] for row in r.rows] == ["5m", "1m", "5m", "1m"]
