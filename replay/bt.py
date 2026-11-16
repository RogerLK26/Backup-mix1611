"""Cầu nối sang tầng dữ liệu sàn của `backtester` (adapter sàn, cache tick/nến 1m, funding).

Trong bản gộp này `backtester/` nằm ngay trong repo nên không cần vendor hay dò thư mục bên cạnh nữa:
replay và backtest dùng **chung một cache** (`<DATA_DIR>/cache/<sàn>/`), tải tick một lần là cả hai chế độ dùng được.

Thư mục dữ liệu (file lệnh thật, cache, kết quả): DATA_DIR, hoặc volume Railway (RAILWAY_VOLUME_MOUNT_PATH),
hoặc thư mục repo này.
"""
from __future__ import annotations

import os
from pathlib import Path

from backtester.data import EXCHANGES, get_exchange
from backtester.data.store import DataStore
from backtester.progress import PROGRESS, Cancelled
from backtester.utils import DAY_MS, HOUR_MS, MIN_MS, DATA_DIR as BASE, data_path, fmt_ms, log, now_ms, setup_logging

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = Path(os.environ.get("REPLAY_TICK_CACHE") or data_path("cache"))
PATH_CACHE = data_path("cache/paths")
DATA_DIR = data_path("Actual Performance")
RESULTS_DIR = data_path("results")

_stores: dict[str, DataStore] = {}


def store_for(exchange: str, workers: int = 8) -> DataStore:
    ex = exchange.lower()
    if ex not in _stores:
        _stores[ex] = DataStore(get_exchange(ex), cache_dir=CACHE_DIR, workers=workers)
    return _stores[ex]


__all__ = ["EXCHANGES", "DataStore", "PROGRESS", "Cancelled", "DAY_MS", "HOUR_MS", "MIN_MS", "fmt_ms", "log",
           "now_ms", "setup_logging", "store_for", "CACHE_DIR", "PATH_CACHE", "DATA_DIR", "RESULTS_DIR", "ROOT",
           "BASE"]
