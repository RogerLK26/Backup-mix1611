"""Mọi giá trị giao diện chào ra phải được engine chấp nhận – và ngược lại.

Bug từng gặp: ô Khung nến của form liệt kê "1D" trong khi engine chỉ nhận "1d", nên lượt chạy quét xong vài chục
nghìn tổ hợp rồi mới dừng giữa chừng. Test này chốt lại: danh sách giá trị chỉ khai báo MỘT chỗ (btmix/strategy.py),
server trả cho giao diện qua /api/meta, và từng giá trị đều dựng được Config thật.

Chạy: python -m pytest -q tests/test_form_values.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from backtester.config import TIMEFRAMES, WEEKDAYS, build_config, set_path

from btmix.strategy import PARAM_ENUMS, SIGNAL_ENUMS, prepare_raw

STATIC = Path(__file__).resolve().parents[1] / "btmix" / "static"

BASE = {
    "name": "t", "exchange": "binance", "start": "2026-01-05 00:00", "end": "2026-01-07 00:00",
    "timezone": "Asia/Ho_Chi_Minh", "initial_balance": 1000,
    "signals": [{"timeframe": "5m", "change_pct": 3, "direction": "up", "trigger": "touch", "side": "reverse"}],
    "position": {"margin": 10, "leverage": 10},
    "exit": {"take_profit": 1, "stop_loss": 3, "max_hold": "30m"},
    "universe": {"market_cap_source": "none"},
}


@pytest.mark.parametrize("key,values", sorted(PARAM_ENUMS.items()))
def test_param_values_build(key, values):
    assert values, key
    for v in values:
        build_config(set_path(BASE, key, v))          # không được ném lỗi


@pytest.mark.parametrize("field,values", sorted(SIGNAL_ENUMS.items()))
def test_signal_values_build(field, values):
    for v in values:
        build_config(set_path(BASE, f"signals.0.{field}", v))


def test_timeframes_match_engine():
    assert SIGNAL_ENUMS["timeframe"] == list(TIMEFRAMES)


def test_sweep_rejects_unknown_value_early():
    """Giá trị sai phải bị chặn lúc kiểm tra cấu hình, không phải giữa lượt chạy."""
    for sweep, bad in (({"signals.0.timeframe": "1m, 1D"}, "1D"),
                       ({"signals.0.side": "reverse, nguoc"}, "nguoc"),
                       ({"limits.timeout_counts_as": "pnl, tp"}, "tp")):
        with pytest.raises(ValueError) as e:
            prepare_raw(dict(BASE, sweep=sweep))
        assert bad in str(e.value)


def test_sweep_rejects_missing_signal():
    with pytest.raises(ValueError) as e:
        prepare_raw(dict(BASE, sweep={"signals.1.change_pct": "2, 3"}))
    assert "tín hiệu" in str(e.value)


# ---------------------------------------------------------------- giao diện không được tự chế danh sách
def test_form_does_not_hardcode_enum_lists():
    """config.js chỉ giữ NHÃN tiếng Việt; danh sách giá trị phải lấy từ /api/meta (META.enums)."""
    js = (STATIC / "config.js").read_text(encoding="utf-8")
    assert "META.enums" in js
    for key in PARAM_ENUMS:
        m = re.search(r'\{ k: "' + re.escape(key) + r'", label: "[^"]*", type: "select"[^}]*\}', js)
        assert m, f"không thấy ô {key} trong form"
        assert "opts: []" in m.group(0), f"ô {key} đang tự khai danh sách giá trị trong config.js"


def test_form_labels_cover_every_value():
    """Mỗi giá trị engine nhận đều có nhãn tiếng Việt (nếu thiếu thì hiện giá trị thô – chấp nhận được, nhưng xấu)."""
    js = (STATIC / "config.js").read_text(encoding="utf-8")
    block = js[js.index("const VI = {"):js.index("const viOpts")]
    missing = []
    for key, values in list(PARAM_ENUMS.items()) + [(k, v) for k, v in SIGNAL_ENUMS.items() if k != "timeframe"]:
        if f'"{key}"' not in block and f"{key}:" not in block:
            missing.append(key)
            continue
        for v in values:
            if f"{v}:" not in block and f'"{v}"' not in block:
                missing.append(f"{key}.{v}")
    assert not missing, f"thiếu nhãn tiếng Việt: {missing}"


def test_weekdays_match_engine():
    js = (STATIC / "config.js").read_text(encoding="utf-8")
    for d in WEEKDAYS:
        assert f'["{d}"' in js, d


# ---------------------------------------------------------------- danh sách "quét thêm tham số"
def _js_list(name: str) -> list[tuple[str, str, str]]:
    """Đọc bảng [khoá, nhãn, giá trị mẫu] khai trong config.js."""
    js = (STATIC / "config.js").read_text(encoding="utf-8")
    block = js[js.index(f"const {name} = ["):]
    block = block[:block.index("\n];")]
    return re.findall(r'\["([^"]+)",\s*"([^"]*)",\s*"([^"]*)"\]', block)


def test_sweepable_keys_supported():
    """Mọi tham số trong danh sách "+ Quét thêm tham số" phải quét được thật.

    Engine chuẩn bị danh sách coin & dữ liệu MỘT lần trước khi quét lưới, nên không nhận các khoá
    exchange / start / end / timezone / universe / data (xem Grid trong backtester/optimize.py)."""
    from backtester.optimize import Grid
    keys = [f"signals.0.{k}" for k, _, _ in _js_list("SIG_SWEEP")] + [k for k, _, _ in _js_list("OTHER_SWEEP")]
    assert len(keys) > 20
    for key in keys:
        field = key.split(".")[-1]
        sample = SIGNAL_ENUMS.get(field) or PARAM_ENUMS.get(key) or ["1", "2"]
        raw = prepare_raw(dict(BASE, sweep={key: ", ".join(str(x) for x in sample[:2])}))
        g = Grid(raw)                                  # ném lỗi nếu engine không quét được tham số này
        assert g.total >= 2, key


def test_sweepable_samples_valid():
    """Giá trị mẫu gợi ý trong ô nhập phải chạy được (chính là thứ người dùng hay bấm rồi chạy luôn)."""
    for prefix, name in (("signals.0.", "SIG_SWEEP"), ("", "OTHER_SWEEP")):
        for k, label, sample in _js_list(name):
            key = prefix + k
            raw = prepare_raw(dict(BASE, sweep={key: sample}))
            build_config({kk: vv for kk, vv in raw.items() if kk != "sweep"})
