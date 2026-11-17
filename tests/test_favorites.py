"""Nhóm yêu thích: lưu / bỏ cả loạt cấu hình (nút "♡ Lưu tất cả…" của bảng xếp hạng).

Chạy: python -m pytest -q tests/test_favorites.py
"""
from __future__ import annotations

import json

import pytest

from btmix import server


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "FAVORITES_FILE", tmp_path / "favorites.json")


def rows(n: int, mode: str = "binance") -> list[dict]:
    return [{"mode": mode, "tp": 1 + i * 0.5, "sl": 3, "hold": 30, "params": f"cfg {i}"} for i in range(n)]


def test_set_many_adds_and_removes():
    d = server.favorites_set_many({"items": rows(5), "group": "Top", "on": True, "kind": "bt"})
    assert d["changed"] == 5
    assert [g["name"] for g in d["groups"]] == ["Top"]
    assert len(d["items"]) == 5

    # thêm lại đúng nhóm đó: không nhân đôi
    d = server.favorites_set_many({"items": rows(5), "group": "Top", "on": True})
    assert d["changed"] == 0 and len(d["items"]) == 5

    # cùng cấu hình, nhóm thứ hai
    d = server.favorites_set_many({"items": rows(2), "group": "Xem sau", "on": True})
    assert d["changed"] == 2
    assert sorted(x["groups"] for x in d["items"])[0] == ["Top"]

    # bỏ khỏi nhóm: cấu hình không còn nhóm nào thì rời danh sách yêu thích
    d = server.favorites_set_many({"items": rows(5), "group": "Top", "on": False})
    assert d["changed"] == 5
    assert len(d["items"]) == 2 and all(x["groups"] == ["Xem sau"] for x in d["items"])


def test_set_many_matches_single_set():
    """Lưu hàng loạt và lưu từng cái phải ra cùng một kết quả."""
    server.favorites_set_many({"items": rows(3), "group": "A", "on": True})
    bulk = json.loads((server.FAVORITES_FILE).read_text(encoding="utf-8"))
    (server.FAVORITES_FILE).unlink()
    for r in rows(3):
        server.favorites_set({**r, "group": "A", "on": True})
    one = json.loads((server.FAVORITES_FILE).read_text(encoding="utf-8"))
    assert [x["params"] for x in bulk["items"]] == [x["params"] for x in one["items"]]
    assert [x["groups"] for x in bulk["items"]] == [x["groups"] for x in one["items"]]


def test_set_many_limits():
    with pytest.raises(Exception) as e:
        server.favorites_set_many({"items": rows(2001), "group": "A", "on": True})
    assert "2.000" in str(e.value) or "2000" in str(e.value)
    with pytest.raises(Exception):
        server.favorites_set_many({"items": rows(2), "group": "   ", "on": True})


def test_key_is_independent_of_label():
    """Cấu hình yêu thích nhận dạng theo GIÁ TRỊ, không theo dòng mô tả.

    Dòng mô tả đổi theo phiên bản (vd "signals.0.timeframe 1m" -> "Khung nến (tín hiệu 1) 1m"); trước đây khoá
    gồm cả dòng này nên cấu hình đã lưu không còn khớp với bảng -> bấm tym xong vẫn thấy chưa lưu.
    """
    base = {"mode": "okx", "tp": 1, "sl": 3, "hold": 30,
            "key": "exit.take_profit=1|exit.stop_loss=3|exit.max_hold=30m|signals.0.timeframe=1m"}
    server.favorites_set({**base, "params": "TP 1% · SL 3% · signals.0.timeframe 1m", "group": "A", "on": True})
    # cùng cấu hình, nhãn mô tả đã đổi -> vẫn là một mục, không tạo thêm
    d = server.favorites_set({**base, "params": "TP 1% · SL 3% · Khung nến (tín hiệu 1) 1m",
                              "group": "B", "on": True})
    assert len(d["items"]) == 1
    assert d["items"][0]["groups"] == ["A", "B"]

    # khác khung nến = cấu hình khác, dù TP/SL/giữ giống nhau
    d = server.favorites_set({**base, "key": base["key"].replace("1m", "5m"), "params": "x", "group": "A", "on": True})
    assert len(d["items"]) == 2


def test_legacy_items_still_match():
    """Mục lưu từ bản cũ (chưa có khoá) vẫn tra được theo cách cũ."""
    server.favorites_set({"mode": "okx", "tp": 2, "sl": 5, "hold": 60, "params": "cfg", "group": "A", "on": True})
    d = server.favorites_set({"mode": "okx", "tp": 2, "sl": 5, "hold": 60, "params": "cfg", "group": "B", "on": True})
    assert len(d["items"]) == 1 and d["items"][0]["groups"] == ["A", "B"]
