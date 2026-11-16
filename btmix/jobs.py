"""Điều phối các lượt chạy: hàng chờ, tiến trình, nhật ký, huỷ, lưu lịch sử – chung cho cả 2 chế độ.

* **Backtest chiến lược** (`BtJob`): quét tín hiệu trên dữ liệu sàn rồi chạy lưới TP × SL × giữ lệnh (+ tham số khác).
* **Replay lệnh thật** (`ReplayJob`): lấy đúng điểm vào lệnh của bot từ file CSV rồi chạy lưới đó.

Hai chế độ trả về cùng một khuôn kết quả (`rows` + `summary`) nên bảng xếp hạng, heatmap, chi tiết, lịch sử dùng chung.
Lượt chạy xong vẫn được giữ trong RAM (tối đa 3 lượt) để bấm vào dòng nào cũng có chi tiết ngay; hết RAM / khởi động
lại thì dựng lại từ lịch sử.
"""
from __future__ import annotations

import collections
import json
import logging
import threading
import time
import traceback
from pathlib import Path

from backtester.config import build_config
from backtester.optimize import Grid
from backtester.progress import PROGRESS, Cancelled
from backtester.utils import data_path, log, mem_usage, release_memory

from replay.bt import RESULTS_DIR
from replay.runner import Run as ReplayRun

from . import history
from .strategy import BtRun

JOBS_DIR = data_path("results") / "jobs"
MAX_LIVE_RUNS = 3


def resolve_actual(name) -> Path:
    """Tên file lệnh thật (tương đối trong thư mục 'Actual Performance') hoặc đường dẫn đầy đủ -> đường dẫn."""
    from replay.bt import DATA_DIR as ACTUAL_DIR
    if not name:
        raise ValueError("Chưa chọn file lệnh thật")
    p = Path(name)
    if p.is_file():
        return p
    q = ACTUAL_DIR / str(name).replace("\\", "/")
    if q.is_file():
        return q
    raise ValueError(f"Không thấy file {name}")


def _clean(o):
    """NaN / inf -> None để JSON hợp lệ."""
    import math
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


# ============================================================================ hai loại lượt chạy
class BtJob:
    """Backtest chiến lược trên 1 hoặc nhiều sàn (mỗi sàn là một 'chế độ' trong bảng xếp hạng)."""

    kind = "bt"

    def __init__(self, request: dict, progress=None):
        self.request = request
        self.raw = dict(request.get("config") or {})
        exs = request.get("exchanges") or self.raw.get("exchange") or ["binance"]
        if isinstance(exs, str):
            exs = [x.strip() for x in exs.split(",") if x.strip()]
        self.exchanges = [str(x).lower() for x in exs]
        self.title = str(self.raw.get("name") or "Chiến lược")
        self.progress = progress or (lambda *a: None)
        self.runs: dict[str, BtRun] = {}
        self.rows: list[dict] = []
        self.summary: dict = {}
        self.validation = None
        self.validate()

    def validate(self):
        """Lỗi cấu hình phải báo ngay lúc bấm Chạy, không đợi tới lúc chạy xong."""
        if not self.exchanges:
            raise ValueError("Chưa chọn sàn")
        g = Grid(self.raw)
        if not g.keys:
            raise ValueError("Nhập ít nhất một lưới (TP / SL / thời gian giữ lệnh) ở mục 8")
        base = {k: v for k, v in self.raw.items() if k not in ("sweep", "optimize")}
        # TP/SL/giữ lệnh có thể chỉ nằm trong lưới -> điền giá trị đầu tiên để kiểm tra cấu hình (như lúc chạy)
        from backtester.config import set_path
        for k, vals in zip(g.keys, g.values):
            if k in ("exit.take_profit", "exit.stop_loss", "exit.max_hold"):
                v = next((x for x in vals if x is not None), None)
                if v is not None:
                    base = set_path(base, k, v)
        build_config(dict(base, exchange=self.exchanges[0]))
        self.grid_info = {"keys": g.keys, "combos": g.total, "mode": g.mode,
                          "evaluate": min(g.total, g.max_combos)}

    def execute(self) -> dict:
        t0 = time.time()
        rows = []
        for ex in self.exchanges:
            PROGRESS.set_exchange(ex)
            log.info("=" * 60)
            log.info("SÀN %s", ex.upper())
            r = BtRun(self.raw, ex, progress=self.progress)
            r.execute()
            self.runs[ex] = r
            rows += r.rows
            # giữ phần tính trước của tối đa 2 sàn gần nhất (RAM); sàn cũ hơn sẽ dựng lại khi xem chi tiết
            for old in list(self.runs)[:-2]:
                self.runs[old].release()
        self.rows = rows
        first = self.runs[self.exchanges[0]]
        self.summary = {
            "kind": "bt",
            "title": self.title,
            "name": self.title,
            "exchanges": self.exchanges,
            "modes": self.exchanges,
            "tz": first.tz,
            "start": self.raw.get("start"), "end": self.raw.get("end"),
            "unit": first.summary.get("unit"),
            "keys": first.summary.get("keys"), "labels": first.summary.get("labels"),
            "objective": first.summary.get("objective"), "objective_label": first.summary.get("objective_label"),
            "grid_total": first.summary.get("grid_total"), "grid_mode": first.summary.get("grid_mode"),
            "oos_pct": first.summary.get("oos_pct") or 0,
            "initial_balance": first.summary.get("initial_balance"),
            "n_combos": len(rows),
            "per_exchange": {ex: {k: r.summary.get(k) for k in
                                  ("n_signals", "n_symbols", "n_combos", "elapsed_s", "rejects")}
                             for ex, r in self.runs.items()},
            "elapsed_s": round(time.time() - t0, 1),
        }
        return self.summary

    def detail(self, mode: str, ci: int, tz: str | None = None, hours=None) -> dict:
        r = self.runs.get(mode)
        if r is None:
            raise KeyError(mode)
        if r.ctx is None:          # đã giải phóng RAM -> tính lại phần tính trước của sàn đó
            r.execute()
        return r.detail(ci, tz, hours)

    # ---- lịch sử
    def hist_meta(self) -> dict:
        return history.meta_for("bt", self.title, self.rows, self.summary, {
            "exchanges": self.exchanges,
            "start": self.raw.get("start"), "end": self.raw.get("end"),
            "n_signals": sum((v.get("n_signals") or 0) for v in self.summary["per_exchange"].values()),
            "grid": self.request.get("grid") or self.raw.get("sweep"),
            "objective": self.summary.get("objective_label"),
        })

    def hist_args(self) -> dict:
        return {"kind": "bt", "config": self.raw, "exchanges": self.exchanges}

    def hist_extra(self) -> dict:
        return {"config": self.raw}


class ReplayJob:
    """Replay các lệnh thật trong file CSV (nguyên engine của bản 'Backtest actual')."""

    kind = "replay"

    def __init__(self, request: dict, progress=None, file: Path | None = None):
        from replay.runner import parse_list
        self.request = request
        self.progress = progress or (lambda *a: None)
        self.file = resolve_actual(file or request.get("file"))
        g = request.get("grid", {})
        settings = dict(request.get("settings", {}))
        settings["tps"] = parse_list(g.get("tp", "1"))
        settings["sls"] = parse_list(g.get("sl", "10"))
        settings["holds"] = parse_list(g.get("hold", "120"), int)
        n = len(set(settings["tps"])) * len(set(settings["sls"])) * len(set(settings["holds"]))
        if n == 0:
            raise ValueError("Lưới trống")
        if n > 20000:
            raise ValueError(f"Lưới quá lớn ({n} cấu hình, tối đa 20.000)")
        act = {k: request.get("actual", {}).get(k) for k in ("tp", "sl", "hold")}
        act = {k: (int(v) if k == "hold" else float(v)) for k, v in act.items() if v not in (None, "")}
        import re as _re
        coins = [c for c in _re.split(r"[,\s]+", request.get("coins", "") or "") if c]
        self.run = ReplayRun(self.file, request.get("exchange") or None, settings, act,
                             date_from=request.get("date_from") or None, date_to=request.get("date_to") or None,
                             coins=coins, entry_mode=request.get("entry_mode", "auto"), progress=self.progress)
        self.title = self.file.name
        self.rows: list[dict] = []
        self.summary: dict = {}
        self.validation = None
        self.grid_info = {"combos": n, "mode": "grid", "evaluate": n, "keys": ["tp", "sl", "hold"]}

    def execute(self) -> dict:
        s = self.run.execute()
        self.rows = self.run.rows
        self.validation = self.run.validation
        self.summary = {**s, "kind": "replay", "title": self.file.name, "modes": self.run.st.modes,
                        "tz": self.run.tz, "keys": [], "labels": []}
        return self.summary

    def detail(self, mode: str, ci: int, tz: str | None = None, hours=None) -> dict:
        return self.run.replay.detail(mode, ci, tz or self.run.tz, hours)

    def hist_meta(self) -> dict:
        s = self.summary
        r = self.run
        return history.meta_for("replay", self.file.name, self.rows, s, {
            "file": self.file.name, "exchange": r.exchange, "n_trades": int(len(r.trades)),
            "start_ms": int(r.trades.start_ms.min()), "end_ms": int(r.trades.start_ms.max()),
            "date_from": r.date_from, "date_to": r.date_to, "coins": r.coins,
            "direction": r.st.direction, "grid": self.request.get("grid", {}),
            "actual_cfg": {k: r.actual_cfg.get(k) for k in ("tp", "sl", "hold")},
            "actual_net": s["actual_stats"]["net"], "reason_match": r.validation.get("reason_match"),
        })

    def hist_args(self) -> dict:
        r = self.run
        return {"kind": "replay", "file": self.file.name, "exchange": r.exchange, "settings": r.st.__dict__,
                "actual_cfg": r.actual_cfg, "tz": r.tz, "date_from": r.date_from, "date_to": r.date_to,
                "coins": r.coins, "entry_mode": r.entry_mode}

    def hist_extra(self) -> dict:
        return {"source": self.file}


def payload(job) -> dict:
    """Kết quả gửi cho giao diện / lưu lịch sử – cùng khuôn cho cả 2 chế độ."""
    out = {"summary": job.summary, "rows": job.rows, "kind": job.kind, "modes": job.summary.get("modes", [])}
    if getattr(job, "validation", None) is not None:
        out["validation"] = job.validation
    return _clean(out)


def rebuild(run_id: str):
    """Dựng lại một lượt trong lịch sử để xem chi tiết từng lệnh (replay: vài giây nhờ cache đường giá;
    backtest: quét lại tín hiệu, cache dữ liệu sàn nên nhanh hơn lần đầu)."""
    a = history.load_args(run_id)
    req = history.load_request(run_id)
    if a.get("kind") == "bt":
        job = BtJob({"config": a["config"], "exchanges": a["exchanges"]})
    else:
        job = ReplayJob(req, file=history.source_file(run_id, a["file"]))
    job.execute()
    return job


# ============================================================================ hàng chờ
class LogBuf(logging.Handler):
    def __init__(self, maxlen: int = 400):
        super().__init__(logging.INFO)
        self.buf: collections.deque = collections.deque(maxlen=maxlen)
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.buf.append(self.format(record))
        except Exception:  # noqa: BLE001
            pass


class Runner:
    """Chạy lần lượt từng lượt; bấm Chạy khi đang bận thì lượt mới vào hàng chờ (còn nguyên sau khi khởi động lại)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.current: dict | None = None
        self.queue: list[dict] = []
        self.logs = LogBuf()
        self.stage = {"stage": "", "done": 0, "total": 0}
        self.run_id: str | None = None
        self.runs: collections.OrderedDict = collections.OrderedDict()   # id -> job (giữ trong RAM)
        self.rebuild_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        logging.getLogger("bt").addHandler(self.logs)
        logging.getLogger("bt").setLevel(logging.INFO)

    # ---- tiến trình
    def progress(self, stage, done, total):
        self.stage = {"stage": stage, "done": done, "total": total}

    def busy(self) -> bool:
        return self.current is not None and self.current["status"] == "running"

    # ---- hàng chờ (lưu trên đĩa)
    @staticmethod
    def _save(meta: dict):
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        (JOBS_DIR / f"{meta['id']}.json").write_text(json.dumps(meta, ensure_ascii=False, default=str),
                                                     encoding="utf-8")

    def submit(self, kind: str, request: dict, title: str) -> dict:
        """Kiểm tra cấu hình ngay (lỗi báo liền), rồi xếp vào hàng chờ."""
        jid = time.strftime("%Y%m%d-%H%M%S") + "-" + f"{int(time.time() * 1000) % 10000:04d}"
        job = make_job(kind, request)          # ném ValueError nếu cấu hình sai
        info = getattr(job, "grid_info", {})
        del job
        meta = {"id": jid, "kind": kind, "title": title, "request": request, "status": "queued",
                "created": time.time(), "started": None, "finished": None, "error": None, "run_id": None,
                "grid": info}
        with self.lock:
            self._save(meta)
            self.queue.append(meta)
            self._ensure_worker()
        return {"id": jid, "queued": len(self.queue) > 1 or self.busy(), **info}

    def restore(self):
        if not JOBS_DIR.exists():
            return
        waiting = []
        for p in sorted(JOBS_DIR.glob("*.json")):
            try:
                m = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if m.get("status") == "running":
                m.update(status="error", error="Máy chủ khởi động lại khi lượt này đang chạy – hãy chạy lại",
                         finished=time.time())
                self._save(m)
            elif m.get("status") == "queued":
                waiting.append(m)
        with self.lock:
            self.queue = sorted(waiting, key=lambda m: m.get("queue_pos", m["created"]))
            self._ensure_worker()

    def _ensure_worker(self):
        if self.queue and (self._worker is None or not self._worker.is_alive()):
            self._worker = threading.Thread(target=self._loop, daemon=True)
            self._worker.start()

    def _loop(self):
        while True:
            with self.lock:
                if not self.queue:
                    self._worker = None
                    return
                meta = self.queue.pop(0)
                meta.update(status="running", started=time.time())
                self.current = meta
                self.logs.buf.clear()
                self.stage = {"stage": "Chuẩn bị", "done": 0, "total": 0}
                PROGRESS.reset()
                self._save(meta)
                self._order()
            self._work(meta)
            before, after = release_memory()
            if before and after:
                log.info("Giải phóng RAM sau lượt chạy: %s MB -> %s MB", before // 2 ** 20, after // 2 ** 20)

    def _work(self, meta: dict):
        try:
            job = make_job(meta["kind"], meta["request"], progress=self.progress)
            job.execute()
            rid = history.new_id()
            try:
                extra = job.hist_extra()
                history.save(rid, job.hist_meta(), meta["request"], job.hist_args(), payload(job),
                             source=extra.get("source"), config=extra.get("config"))
            except Exception as e:  # noqa: BLE001
                log.error("Không lưu được lịch sử: %s", e)
            self.keep(rid, job)
            meta.update(status="done", run_id=rid)
            self.run_id = rid
        except Cancelled:
            meta["status"] = "cancelled"
            self.logs.buf.append("Đã huỷ lượt chạy.")
        except Exception as e:  # noqa: BLE001
            meta.update(status="error", error=str(e))
            log.error("LỖI: %s", e)
            self.logs.buf.append("LỖI: " + "".join(traceback.format_exception_only(type(e), e)).strip())
            log.debug(traceback.format_exc())
        finally:
            meta["finished"] = time.time()
            meta["log"] = list(self.logs.buf)[-200:]
            self._save(meta)

    def _order(self):
        for i, m in enumerate(self.queue):
            m["queue_pos"] = i
            self._save(m)

    def cancel(self, jid: str | None = None) -> bool:
        with self.lock:
            if self.current and (jid is None or self.current["id"] == jid) and self.busy():
                PROGRESS.cancel.set()
                return True
            for m in self.queue:
                if m["id"] == jid:
                    self.queue.remove(m)
                    m.update(status="cancelled", finished=time.time())
                    self._save(m)
                    self._order()
                    return True
        return False

    def move(self, jid: str, delta: int) -> bool:
        with self.lock:
            ids = [m["id"] for m in self.queue]
            if jid not in ids:
                return False
            i = ids.index(jid)
            j = max(0, min(len(self.queue) - 1, i + delta))
            self.queue.insert(j, self.queue.pop(i))
            self._order()
            return True

    def status(self) -> dict:
        m = self.current
        q = [{"id": x["id"], "kind": x["kind"], "title": x["title"], "created": x["created"]} for x in self.queue]
        out = {"queue": q, "run_id": self.run_id, "ram_mb": round(mem_usage() / 2 ** 20) if mem_usage() else None}
        if m is None:
            out["status"] = "idle"
            return out
        p = PROGRESS.snapshot()
        stage = dict(self.stage)
        if p["stage"] and (not stage["total"] or p["stage"] != stage["stage"]):
            stage = {"stage": p["stage"], "done": p["done"], "total": p["total"]}
        out.update({
            "status": m["status"], "id": m["id"], "kind": m["kind"], "title": m["title"], "error": m.get("error"),
            "exchange": p["exchange"], "started": m.get("started"), "finished": m.get("finished"),
            "elapsed": round(time.time() - (m.get("started") or time.time()), 1),
            "run_id": m.get("run_id") or self.run_id, "grid": m.get("grid"),
            **stage,
            "log": list(self.logs.buf)[-120:],
        })
        return out

    # ---- lượt chạy giữ trong RAM
    def keep(self, rid: str, job):
        self.runs[rid] = job
        self.runs.move_to_end(rid)
        while len(self.runs) > MAX_LIVE_RUNS:
            self.runs.popitem(last=False)

    def job_for(self, run_id: str | None):
        rid = run_id or self.run_id
        if not rid:
            raise KeyError("Chưa có kết quả")
        if rid in self.runs:
            self.runs.move_to_end(rid)
            return self.runs[rid]
        with self.rebuild_lock:
            if rid in self.runs:
                return self.runs[rid]
            job = rebuild(rid)
            self.keep(rid, job)
            return job


def make_job(kind: str, request: dict, progress=None):
    if kind == "bt":
        return BtJob(request, progress=progress)
    if kind == "replay":
        return ReplayJob(request, progress=progress)
    raise ValueError(f"Chế độ không hợp lệ: {kind}")


RUNNER = Runner()
