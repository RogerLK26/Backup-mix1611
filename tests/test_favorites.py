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
