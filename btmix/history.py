"""Lịch sử các lượt chạy – chung cho cả 2 chế độ (backtest chiến lược & replay lệnh thật).

Mỗi lượt lưu trong `results/history/<id>/` (trên Railway: trong Volume):

    meta.json        tóm tắt để hiện trong khung Lịch sử
    request.json     đúng những gì đã nhập trên giao diện (nút "Dùng lại cấu hình")
    args.json        tham số dựng lại lượt chạy (xem chi tiết từng lệnh sau khi máy chủ khởi động lại)
    result.json.gz   kết quả: tóm tắt + bảng xếp hạng toàn bộ cấu hình (+ kiểm định, với chế độ replay)
    config.yaml      (backtest) bản sao cấu hình chiến lược
    source/<file>    (replay) bản sao file lệnh thật đã dùng
"""
from __future__ import annotations

import gzip
import json
import re
import shutil
import time
import uuid
from pathlib import Path

import yaml

from replay.bt import RESULTS_DIR, now_ms

HIST = RESULTS_DIR / "history"
_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")


def new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


def _dir(run_id: str) -> Path:
    if not _ID.match(run_id or ""):
        raise KeyError(run_id)
    d = HIST / run_id
    if not d.is_dir():
        raise KeyError(run_id)
    return d


def _best(rows: list[dict], mode: str) -> dict | None:
    r = [x for x in rows if x["mode"] == mode and x["n"] > 0]
    if not r:
        return None
    b = max(r, key=lambda x: (x["net"] if x["net"] is not None else -1e18))
    return {k: b.get(k) for k in ("tp", "sl", "hold", "params", "n", "net", "max_dd", "win_rate")}


def save(run_id: str, meta: dict, request: dict, args: dict, result: dict,
         source: Path | None = None, config: dict | None = None) -> dict:
    d = HIST / run_id
    d.mkdir(parents=True, exist_ok=True)
    if source is not None:
        (d / "source").mkdir(exist_ok=True)
        shutil.copy2(source, d / "source" / Path(source).name)
    if config is not None:
        (d / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (d / "args.json").write_text(json.dumps(args, ensure_ascii=False, default=str), encoding="utf-8")
    (d / "request.json").write_text(json.dumps(request, ensure_ascii=False, default=str), encoding="utf-8")
    with gzip.open(d / "result.json.gz", "wt", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, default=str)
    meta = {"id": run_id, "created": now_ms(), **meta}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, default=str), encoding="utf-8")
    return meta


def meta_for(kind: str, title: str, rows: list[dict], summary: dict, extra: dict | None = None) -> dict:
    modes = sorted({r["mode"] for r in rows})
    return {
        "kind": kind, "title": title, "modes": modes,
        "n_combos": len(rows), "elapsed_s": summary.get("elapsed_s"),
        "best": {m: _best(rows, m) for m in modes},
        **(extra or {}),
    }


def list_runs() -> list[dict]:
    out = []
    if HIST.exists():
        for d in HIST.iterdir():
            p = d / "meta.json"
            if d.is_dir() and p.exists():
                try:
                    m = json.loads(p.read_text(encoding="utf-8"))
                    m["size"] = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                    out.append(m)
                except Exception:  # noqa: BLE001
                    continue
    return sorted(out, key=lambda m: m.get("created", 0), reverse=True)


def load_result(run_id: str) -> dict:
    d = _dir(run_id)
    with gzip.open(d / "result.json.gz", "rt", encoding="utf-8") as f:
        res = json.load(f)
    res["meta"] = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    return res


def load_request(run_id: str) -> dict:
    return json.loads((_dir(run_id) / "request.json").read_text(encoding="utf-8"))


def load_args(run_id: str) -> dict:
    return json.loads((_dir(run_id) / "args.json").read_text(encoding="utf-8"))


def source_file(run_id: str, name: str) -> Path:
    return _dir(run_id) / "source" / Path(name).name


def delete(run_id: str) -> bool:
    try:
        d = _dir(run_id)
    except KeyError:
        return False
    shutil.rmtree(d, ignore_errors=True)
    return not d.exists()
