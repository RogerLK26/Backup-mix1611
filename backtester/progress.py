"""Tiến trình & huỷ cho lượt chạy hiện tại (dùng bởi giao diện web; chạy CLI thì không làm gì)."""
from __future__ import annotations

import threading
import time


class Cancelled(Exception):
    pass


class Progress:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.exchange = ""
            self.stage = ""
            self.done = 0
            self.total = 0
            self.updated = time.time()
            self.cancel = threading.Event()

    def set_exchange(self, name: str):
        with self._lock:
            self.exchange = name
            self.stage, self.done, self.total = "", 0, 0

    def set_stage(self, stage: str, total: int = 0):
        with self._lock:
            self.stage, self.done, self.total = stage, 0, total
            self.updated = time.time()
        self.check()

    def step(self, done: int, total: int | None = None):
        with self._lock:
            self.done = done
            if total is not None:
                self.total = total
            self.updated = time.time()
        self.check()

    def check(self):
        if self.cancel.is_set():
            raise Cancelled("Đã huỷ")

    def snapshot(self) -> dict:
        with self._lock:
            return {"exchange": self.exchange, "stage": self.stage, "done": self.done, "total": self.total}


PROGRESS = Progress()
