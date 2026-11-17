"""Giao diện web gộp: backtest chiến lược (dữ liệu sàn) + replay lệnh thật, chung một bảng xếp hạng / lịch sử.

    python -m btmix web              # http://127.0.0.1:8000
    python -m btmix web --lan        # cho điện thoại cùng Wi-Fi
"""
from __future__ import annotations

import base64
import csv
import hmac
import io
import json
import os
import re
import shutil
import socket
import threading
import time
import webbrowser
from pathlib import Path

import yaml
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from backtester.config import TIMEFRAMES, WEEKDAYS, build_config, load_raw
from backtester.data import EXCHANGES, cache_admin, get_exchange
from backtester.optimize import LABELS, OBJECTIVES, Grid, describe as describe_sweep
from backtester.utils import data_path, log, mem_usage, release_memory

from replay.actual import detect_exchange, load_actual, summarize_actual
from replay.bt import DATA_DIR as ACTUAL_DIR
from replay.runner import guess_actual

from . import history
from .jobs import RUNNER, _clean, payload, resolve_actual
from .strategy import PARAM_ENUMS, SIGNAL_ENUMS, _base_with_axis, prepare_raw

STATIC = Path(__file__).parent / "static"
REPO_CONFIGS = Path(__file__).resolve().parents[1] / "configs"
CONFIGS = data_path("configs")
PREFS_FILE = data_path("ui_prefs.json")
FAVORITES_FILE = data_path("favorites.json")
FILTERS_FILE = data_path("grid_filters.json")
_FILES_LOCK = threading.Lock()

# Đăng nhập (đặt khi deploy lên Internet): BACKTEST_PASSWORD (+ BACKTEST_USER, mặc định admin)
AUTH_USER = os.environ.get("BACKTEST_USER", "admin")
AUTH_PASS = os.environ.get("BACKTEST_PASSWORD", "")

EXCHANGE_INFO = {
    "binance": {"name": "Binance", "klines": "Toàn bộ lịch sử", "ticks": "Toàn bộ lịch sử"},
    "bybit": {"name": "Bybit", "klines": "Toàn bộ lịch sử", "ticks": "Toàn bộ lịch sử"},
    "okx": {"name": "OKX", "klines": "Toàn bộ lịch sử", "ticks": "Toàn bộ lịch sử"},
    "bitget": {"name": "Bitget", "klines": "≥ 1 năm", "ticks": "90 ngày gần nhất"},
    "gate": {"name": "Gate", "klines": "Theo tháng + ~7 ngày gần nhất", "ticks": "180 ngày gần nhất"},
    "mexc": {"name": "MEXC", "klines": "~30 ngày gần nhất", "ticks": "Không có (mô phỏng nến 1m)"},
}
FEES = {k: cls.default_fees for k, cls in EXCHANGES.items()}

app = FastAPI(title="Backtest Mix")


@app.middleware("http")
async def guard(request: Request, call_next):
    if AUTH_PASS and request.url.path != "/api/health":
        ok = False
        h = request.headers.get("authorization", "")
        if h.lower().startswith("basic "):
            try:
                u, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
                ok = hmac.compare_digest(u, AUTH_USER) and hmac.compare_digest(pw, AUTH_PASS)
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return Response("Cần đăng nhập", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="Backtest Mix"'})
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for f in ("app.css", "app.js", "charts.js"):
        p = STATIC / f
        if p.exists():
            html = html.replace(f"/static/{f}", f"/static/{f}?v={int(p.stat().st_mtime)}")
    return HTMLResponse(html)


# ============================================================================ thông tin chung
@app.get("/api/meta")
def meta():
    exs = []
    for k, cls in EXCHANGES.items():
        exs.append({"id": k, **EXCHANGE_INFO.get(k, {"name": k}), "maker": cls.default_fees[0],
                    "taker": cls.default_fees[1], "tick_days": cls.tick_history_days})
    default = {}
    p = CONFIGS / "pump_short.yaml"
    if p.exists():
        try:
            default = load_raw(p) or {}
        except Exception:  # noqa: BLE001
            default = {}
    # các danh sách giá trị hợp lệ lấy thẳng từ engine -> giao diện không bao giờ chào giá trị engine không nhận
    return _clean({"exchanges": exs, "timeframes": list(TIMEFRAMES), "default": default, "fees": FEES,
                   "objectives": OBJECTIVES, "labels": LABELS, "actual_dir": str(ACTUAL_DIR),
                   "enums": {"signal": SIGNAL_ENUMS, "params": PARAM_ENUMS, "weekdays": WEEKDAYS},
                   "data_dir": str(data_path(".").resolve())})


# ============================================================================ cấu hình chiến lược (preset YAML)
def _safe_name(name: str) -> str:
    s = re.sub(r"[^\w\-. ]", "", str(name or ""), flags=re.UNICODE).strip().replace(" ", "_")
    if not s:
        raise HTTPException(400, "Tên cấu hình không hợp lệ")
    return s[:80]


def _seed_configs():
    """Dữ liệu nằm ở volume riêng (DATA_DIR) thì chép các cấu hình mẫu trong repo sang nếu chưa có."""
    try:
        if CONFIGS.resolve() == REPO_CONFIGS.resolve() or not REPO_CONFIGS.exists():
            return
    except OSError:
        return
    CONFIGS.mkdir(parents=True, exist_ok=True)
    for p in REPO_CONFIGS.glob("*.yaml"):
        if not (CONFIGS / p.name).exists():
            shutil.copy2(p, CONFIGS / p.name)


@app.get("/api/presets")
def presets():
    out = []
    for p in sorted(CONFIGS.glob("*.yaml")):
        try:
            raw = load_raw(p) or {}
        except Exception:  # noqa: BLE001
            continue
        ex = raw.get("exchange", "binance")
        sigs = [f"{s.get('timeframe', '1m')} {s.get('change_pct', '?')}%"
                for s in (raw.get("signals") or []) if isinstance(s, dict)]
        out.append({"file": p.stem, "name": raw.get("name", p.stem), "updated": p.stat().st_mtime,
                    "exchanges": ex if isinstance(ex, list) else [x.strip() for x in str(ex).split(",")],
                    "signals": sigs, "start": str(raw.get("start", ""))[:10], "end": str(raw.get("end", ""))[:10],
                    "sweep": bool(raw.get("sweep"))})
    return out


def _unique_file(base: str) -> str:
    base = _safe_name(base)
    if not (CONFIGS / f"{base}.yaml").exists():
        return base
    stem = re.sub(r"_(copy|ban_sao)\d*$", "", base)
    i = 1
    while (CONFIGS / f"{stem}_ban_sao{'' if i == 1 else i}.yaml").exists():
        i += 1
    return f"{stem}_ban_sao{'' if i == 1 else i}"


def _write_preset(file: str, raw: dict) -> str:
    CONFIGS.mkdir(parents=True, exist_ok=True)
    (CONFIGS / f"{file}.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return file


@app.post("/api/presets")
def create_preset(body: dict = Body(...)):
    raw = body.get("config") or {}
    return {"ok": True, "file": _write_preset(_unique_file(body.get("file") or raw.get("name") or "chien_luoc"), raw)}


@app.post("/api/presets/{file}/duplicate")
def duplicate_preset(file: str):
    src = CONFIGS / f"{_safe_name(file)}.yaml"
    if not src.exists():
        raise HTTPException(404, "Không tìm thấy cấu hình")
    raw = load_raw(src) or {}
    new = _unique_file(file)
    n = re.search(r"_ban_sao(\d*)$", new)
    base = re.sub(r" \(bản sao( \d+)?\)$", "", str(raw.get("name", file)))
    raw["name"] = f"{base} (bản sao{' ' + n.group(1) if n and n.group(1) else ''})"
    return {"ok": True, "file": _write_preset(new, raw)}


@app.get("/api/presets/{file}")
def preset(file: str):
    p = CONFIGS / f"{_safe_name(file)}.yaml"
    if not p.exists():
        raise HTTPException(404, "Không tìm thấy cấu hình")
    return _clean(load_raw(p))


@app.put("/api/presets/{file}")
def save_preset(file: str, raw: dict = Body(...)):
    return {"ok": True, "file": _write_preset(_safe_name(file), raw)}


@app.delete("/api/presets/{file}")
def delete_preset(file: str):
    p = CONFIGS / f"{_safe_name(file)}.yaml"
    if p.exists():
        p.unlink()
    return {"ok": True}


@app.post("/api/yaml/dump")
def yaml_dump(raw: dict = Body(...)):
    return {"yaml": yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)}


@app.post("/api/yaml/parse")
def yaml_parse(body: dict = Body(...)):
    try:
        raw = yaml.safe_load(body.get("text") or "")
    except yaml.YAMLError as e:
        raise HTTPException(400, f"File YAML không hợp lệ: {e}") from e
    if not isinstance(raw, dict):
        raise HTTPException(400, "File YAML không đúng định dạng cấu hình")
    return {"config": raw}


@app.post("/api/validate")
def validate(body: dict = Body(...)):
    raw, exchanges = body.get("config") or {}, body.get("exchanges") or []
    try:
        raw = prepare_raw(raw)
        base = raw
        if raw.get("sweep"):
            # TP/SL/giữ lệnh có thể chỉ nằm trong lưới -> điền giá trị đầu tiên như lúc chạy thật
            base = _base_with_axis({k: v for k, v in raw.items() if k not in ("sweep", "optimize")}, Grid(raw))
        for e in exchanges or [str(raw.get("exchange", "binance"))]:
            if isinstance(e, (list, tuple)):
                e = e[0]
            build_config(dict(base, exchange=e), get_exchange(e).default_fees)
        sweep = describe_sweep(raw) if raw.get("sweep") else None
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": True, "sweep": sweep}


# ============================================================================ file lệnh thật (chế độ replay)
_summ_cache: dict[tuple, dict] = {}


def _actual_files() -> list[Path]:
    ACTUAL_DIR.mkdir(parents=True, exist_ok=True)
    return sorted([p for p in ACTUAL_DIR.rglob("*.csv")], key=lambda p: p.stat().st_mtime, reverse=True)


@app.get("/api/files")
def files():
    out = []
    for p in _actual_files():
        key = (str(p), p.stat().st_mtime)
        if key not in _summ_cache:
            try:
                df = load_actual(p)
                s = summarize_actual(df)
                s["guess"] = guess_actual(df)
                s["coins_list"] = sorted(df.coin.unique().tolist())
            except Exception as e:  # noqa: BLE001
                s = {"error": str(e)}
            _summ_cache[key] = s
        ex = detect_exchange(p.name)
        out.append({"name": str(p.relative_to(ACTUAL_DIR)).replace("\\", "/"), "exchange": ex,
                    "fees": FEES.get(ex or "", (0.02, 0.05)), "size": p.stat().st_size, **_summ_cache[key]})
    return _clean({"files": out, "exchanges": list(EXCHANGES), "fees": FEES, "dir": str(ACTUAL_DIR)})


@app.post("/api/upload")
async def upload(request: Request):
    from urllib.parse import unquote
    name = unquote(request.headers.get("x-filename", "upload.csv"))
    name = re.sub(r"[^\w.\- ]", "_", Path(name).name)
    if not name.lower().endswith(".csv"):
        raise HTTPException(400, "Chỉ nhận file .csv")
    body = await request.body()
    try:
        load_actual(body)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"File không đọc được: {e}") from e
    ACTUAL_DIR.mkdir(parents=True, exist_ok=True)
    (ACTUAL_DIR / name).write_bytes(body)
    return {"name": name}


# ============================================================================ chạy
@app.post("/api/run")
def start(body: dict = Body(...)):
    kind = body.get("kind") or "bt"
    try:
        if kind == "bt":
            raw = body.get("config") or {}
            title = str(raw.get("name") or "Chiến lược")
        else:
            title = str(body.get("file") or "")
            resolve_actual(title)
        info = RUNNER.submit(kind, body, title)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e
    return _clean({"ok": True, **info})


@app.post("/api/cancel")
def cancel(body: dict | None = Body(default=None)):
    return {"ok": RUNNER.cancel((body or {}).get("id"))}


@app.post("/api/queue/{jid}/move")
def queue_move(jid: str, body: dict = Body(...)):
    return {"ok": RUNNER.move(jid, int(body.get("delta", 0)))}


@app.get("/api/status")
def status():
    return _clean(RUNNER.status())


# ============================================================================ kết quả
def _job(run: str | None):
    try:
        return RUNNER.job_for(run)
    except KeyError as e:
        raise HTTPException(404, "Chưa có kết quả / lượt chạy không còn trong lịch sử") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e


@app.get("/api/result")
def result(run: str | None = None):
    rid = run or RUNNER.run_id
    if not rid:
        raise HTTPException(404, "Chưa có kết quả")
    try:
        return history.load_result(rid)
    except KeyError:
        if rid in RUNNER.runs:
            return {**payload(RUNNER.runs[rid]), "meta": {"id": rid}}
        raise HTTPException(404, "Lượt chạy không còn trong lịch sử") from None


def _hours(hours: str | None):
    if hours is None or hours == "":
        return None
    if hours == "none":
        return []
    try:
        return [int(x) for x in hours.split(",") if x.strip() != ""]
    except ValueError:
        raise HTTPException(400, "Bộ lọc giờ không hợp lệ") from None


@app.get("/api/detail")
def detail(mode: str, ci: int, run: str | None = None, hours: str | None = None, key: str | None = None):
    job = _job(run)
    try:
        return _clean(job.detail(mode, int(ci), None, _hours(hours), key or None))
    except KeyError:
        raise HTTPException(404, "Không tìm thấy cấu hình này trong lượt chạy") from None
    except IndexError:
        raise HTTPException(404, "Không tìm thấy cấu hình này trong lượt chạy") from None


@app.get("/api/rows")
def rows_dated(run: str | None = None, date_from: str | None = None, date_to: str | None = None):
    """Bảng xếp hạng tính lại chỉ trên các lệnh vào trong khoảng ngày (chỉ chế độ replay)."""
    job = _job(run)
    if job.kind != "replay":
        raise HTTPException(400, "Lọc theo ngày chỉ dùng cho chế độ replay")
    if not date_from and not date_to:
        return _clean({"rows": job.rows})
    import pandas as pd
    r = job.run.replay
    day = r.entry_day(job.run.tz)
    sel = (day >= date_from if date_from else True) & (day <= date_to if date_to else True)
    sel = pd.Series(sel).to_numpy() if not isinstance(sel, bool) else None
    if sel is None or not sel.any():
        raise HTTPException(400, "Không có lệnh nào trong khoảng ngày đã chọn")
    sub = r.subset(sel)
    return _clean({"rows": sub.table(job.run.tz), "n": int(sel.sum())})


# ============================================================================ xuất file
def _csv(rows: list[dict], name: str):
    if not rows:
        raise HTTPException(404, "Không có dữ liệu")
    cols = list(dict.fromkeys(k for r in rows for k in r))
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/export/grid.csv")
def export_grid(run: str | None = None):
    job = _job(run)
    keys = job.summary.get("keys") or []
    labels = job.summary.get("labels") or keys
    rows = []
    for r in job.rows:
        vals = r.get("values") or {}
        # các tham số quét thêm (khung nến, biến động %…) thành cột riêng, tên cột là nhãn tiếng Việt
        extra = {labels[i] if i < len(labels) else k: vals.get(k)
                 for i, k in enumerate(keys) if k in vals and k not in ("exit.take_profit", "exit.stop_loss",
                                                                       "exit.max_hold")}
        head = {k: r.get(k) for k in ("mode", "ci", "tp", "sl", "hold")}
        rest = {k: v for k, v in r.items()
                if k not in ("combo", "values", "labels", "h8_hours", *head)}
        rows.append({**head, **extra, **rest,
                     "h8_hours": " ".join(str(h) for h in (r.get("h8_hours") or []))})
    return _csv(rows, f"bang_xep_hang_{job.kind}.csv")


@app.get("/api/export/trades.csv")
def export_trades(mode: str, ci: int, run: str | None = None, hours: str | None = None, key: str | None = None):
    job = _job(run)
    d = job.detail(mode, int(ci), None, _hours(hours), key or None)
    c = d["combo"]
    name = f"lenh_{mode}_tp{c.get('tp')}_sl{c.get('sl')}_giu{c.get('hold')}.csv".replace(" ", "")
    return _csv(d["trades"], name)


@app.get("/api/export/validation.csv")
def export_validation(run: str | None = None):
    job = _job(run)
    if job.kind != "replay" or not getattr(job, "validation", None):
        raise HTTPException(404, "Lượt chạy này không có phần kiểm định")
    return _csv(job.validation.get("rows") or [], "kiem_dinh.csv")


# ============================================================================ lịch sử
@app.get("/api/history")
def history_list():
    return {"runs": history.list_runs()}


@app.get("/api/history/{run_id}/request")
def history_request(run_id: str):
    try:
        return {"request": history.load_request(run_id), "meta": next(
            (m for m in history.list_runs() if m["id"] == run_id), {})}
    except (KeyError, OSError):
        raise HTTPException(404, "Không tìm thấy lượt chạy") from None


@app.delete("/api/history/{run_id}")
def history_delete(run_id: str):
    RUNNER.runs.pop(run_id, None)
    return {"ok": history.delete(run_id)}


@app.post("/api/history/delete")
def history_delete_many(body: dict = Body(...)):
    ids = body.get("ids") or []
    if body.get("all"):
        ids = [m["id"] for m in history.list_runs()]
    n = 0
    for i in ids:
        RUNNER.runs.pop(i, None)
        n += 1 if history.delete(i) else 0
    return {"ok": True, "deleted": n}


# ============================================================================ yêu thích & bộ lọc đã lưu
def _load_list(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


DEFAULT_GROUP = "Yêu thích"
GROUP_COLORS = ["#e5484d", "#f5a524", "#30a46c", "#3e8ef7", "#8e4ec6", "#d6409f", "#12a594", "#a18072",
                "#e54666", "#0090ff"]
_LOCK = threading.Lock()


def _fav_key(d: dict) -> tuple:
    """Cấu hình yêu thích = (chế độ/sàn, khoá theo giá trị tham số) – không gắn với một lượt chạy.

    Bản cũ dùng cả dòng mô tả `params`; dòng mô tả đổi theo phiên bản nên cấu hình đã lưu không còn khớp.
    Mục đã lưu từ bản cũ (chưa có `key`) vẫn tra được theo cách cũ."""
    try:
        key = str(d.get("key") or "")
        if key:
            return (str(d["mode"]), key)
        return (str(d["mode"]), round(float(d["tp"] or 0), 6), round(float(d["sl"] or 0), 6), int(d["hold"] or 0),
                str(d.get("params") or ""))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Cấu hình không hợp lệ") from None


def _load_favs() -> dict:
    d = _load_list(FAVORITES_FILE)
    if isinstance(d, list):
        d = {"groups": [{"name": DEFAULT_GROUP, "color": GROUP_COLORS[0]}] if d else [],
             "items": [{**x, "groups": [DEFAULT_GROUP]} for x in d if isinstance(x, dict)]}
    if not isinstance(d, dict):
        d = {}
    groups = [g for g in d.get("groups", []) if isinstance(g, dict) and g.get("name")]
    names = {g["name"] for g in groups}
    items = []
    for x in d.get("items", []):
        if isinstance(x, dict):
            gs = [g for g in x.get("groups", []) if g in names]
            if gs:
                items.append({**x, "groups": gs})
    return {"groups": groups, "items": items}


def _save_favs(d: dict) -> dict:
    d["items"].sort(key=lambda f: (str(f.get("mode")), f.get("tp") or 0, f.get("sl") or 0, f.get("hold") or 0))
    _save_json(FAVORITES_FILE, d)
    return d


def _group_name(v) -> str:
    name = re.sub(r"\s+", " ", str(v or "")).strip()[:40]
    if not name:
        raise HTTPException(400, "Thiếu tên nhóm")
    return name


def _find_group(d: dict, name: str):
    return next((g for g in d["groups"] if g["name"].casefold() == name.casefold()), None)


def _new_group(d: dict, name: str) -> dict:
    g = _find_group(d, name)
    if g:
        return g
    used = {x.get("color") for x in d["groups"]}
    color = next((c for c in GROUP_COLORS if c not in used), GROUP_COLORS[len(d["groups"]) % len(GROUP_COLORS)])
    g = {"name": name, "color": color}
    d["groups"].append(g)
    return g


@app.get("/api/favorites")
def favorites_list():
    return _load_favs()


@app.post("/api/favorites/set")
def favorites_set(body: dict = Body(...)):
    key = _fav_key(body)
    name = _group_name(body.get("group") or DEFAULT_GROUP)
    on = bool(body.get("on", True))
    with _LOCK:
        d = _load_favs()
        g = _new_group(d, name) if on else _find_group(d, name)
        item = next((x for x in d["items"] if _fav_key(x) == key), None)
        if on:
            if item is None:
                item = {"mode": str(body.get("mode")), "tp": body.get("tp"), "sl": body.get("sl"),
                        "hold": body.get("hold"), "params": str(body.get("params") or ""),
                        "key": str(body.get("key") or ""), "kind": body.get("kind"), "groups": [],
                        "saved": int(time.time() * 1000)}
                d["items"].append(item)
            if g["name"] not in item["groups"]:
                item["groups"].append(g["name"])
        elif item is not None and g is not None:
            item["groups"] = [x for x in item["groups"] if x != g["name"]]
            if not item["groups"]:
                d["items"].remove(item)
        return _save_favs(d)


@app.post("/api/favorites/set-many")
def favorites_set_many(body: dict = Body(...)):
    """{items: [{mode, tp, sl, hold, params}], group, on}: thêm / bỏ cả loạt cấu hình khỏi một nhóm."""
    items = body.get("items") or []
    if not isinstance(items, list):
        raise HTTPException(400, "Danh sách cấu hình không hợp lệ")
    if len(items) > 2000:
        raise HTTPException(400, f"Quá nhiều cấu hình ({len(items)}) – lọc bớt rồi lưu, tối đa 2.000")
    name = _group_name(body.get("group") or DEFAULT_GROUP)
    on = bool(body.get("on", True))
    with _LOCK:
        d = _load_favs()
        g = _new_group(d, name) if on else _find_group(d, name)
        if g is None:
            raise HTTPException(404, "Không tìm thấy nhóm")
        index = {_fav_key(x): x for x in d["items"]}
        n = 0
        for it in items:
            key = _fav_key(it)
            item = index.get(key)
            if on:
                if item is None:
                    item = {"mode": str(it.get("mode")), "tp": it.get("tp"), "sl": it.get("sl"),
                            "hold": it.get("hold"), "params": str(it.get("params") or ""),
                            "key": str(it.get("key") or ""), "kind": body.get("kind"), "groups": [],
                            "saved": int(time.time() * 1000)}
                    d["items"].append(item)
                    index[key] = item
                if g["name"] not in item["groups"]:
                    item["groups"].append(g["name"])
                    n += 1
            elif item is not None and g["name"] in item["groups"]:
                item["groups"] = [x for x in item["groups"] if x != g["name"]]
                n += 1
        if not on:
            d["items"] = [x for x in d["items"] if x["groups"]]
        return {**_save_favs(d), "changed": n}


@app.post("/api/favorites/groups")
def favorites_groups(body: dict = Body(...)):
    action = body.get("action")
    name = _group_name(body.get("name"))
    with _LOCK:
        d = _load_favs()
        g = _find_group(d, name)
        if action == "create":
            if g:
                raise HTTPException(400, f'Đã có nhóm "{g["name"]}"')
            _new_group(d, name)
        elif action == "rename":
            new = _group_name(body.get("new_name"))
            if not g:
                raise HTTPException(404, "Không tìm thấy nhóm")
            other = _find_group(d, new)
            if other and other is not g:
                raise HTTPException(400, f'Đã có nhóm "{other["name"]}"')
            old, g["name"] = g["name"], new
            for x in d["items"]:
                x["groups"] = [new if n == old else n for n in x["groups"]]
        elif action == "delete":
            if not g:
                raise HTTPException(404, "Không tìm thấy nhóm")
            d["groups"].remove(g)
            for x in d["items"]:
                x["groups"] = [n for n in x["groups"] if n != g["name"]]
            d["items"] = [x for x in d["items"] if x["groups"]]
        else:
            raise HTTPException(400, "Thao tác không hợp lệ")
        return _save_favs(d)


@app.get("/api/filters")
def filters_list():
    d = _load_list(FILTERS_FILE)
    return {"filters": d if isinstance(d, list) else []}


@app.post("/api/filters")
def filters_save(body: dict = Body(...)):
    name = str(body.get("name") or "").strip()[:80]
    state = body.get("state")
    if not name or not isinstance(state, dict):
        raise HTTPException(400, "Thiếu tên hoặc nội dung bộ lọc")
    if len(json.dumps(state)) > 20000:
        raise HTTPException(400, "Bộ lọc quá lớn")
    with _LOCK:
        items = [f for f in filters_list()["filters"] if f.get("name") != name]
        items.append({"name": name, "state": state, "saved": int(time.time() * 1000)})
        items.sort(key=lambda f: f["name"].lower())
        _save_json(FILTERS_FILE, items)
    return {"filters": items}


@app.post("/api/filters/delete")
def filters_delete(body: dict = Body(...)):
    name = str(body.get("name") or "")
    with _LOCK:
        items = [f for f in filters_list()["filters"] if f.get("name") != name]
        _save_json(FILTERS_FILE, items)
    return {"filters": items}


@app.get("/api/prefs")
def prefs_get():
    d = _load_list(PREFS_FILE)
    return d if isinstance(d, dict) else {}


@app.put("/api/prefs")
def prefs_put(body: dict = Body(...)):
    if not isinstance(body, dict) or len(json.dumps(body)) > 200_000:
        raise HTTPException(400, "Dữ liệu không hợp lệ")
    _save_json(PREFS_FILE, body)
    return {"ok": True}


# ============================================================================ dữ liệu đã tải (cache) & RAM
@app.get("/api/cache")
def cache_info():
    s = cache_admin.summary("cache")
    res = data_path("results")
    s["results_size"] = sum(f.stat().st_size for f in res.rglob("*") if f.is_file()) if res.exists() else 0
    s["kinds"] = cache_admin.KINDS
    return _clean(s)


@app.post("/api/cache/delete")
def cache_delete(body: dict = Body(...)):
    dry = bool(body.get("dry_run", True))
    if not dry and RUNNER.busy():
        raise HTTPException(409, "Đang có lượt chạy – hãy chờ xong rồi xoá dữ liệu.")
    return cache_admin.delete("cache", body.get("exchanges") or None, body.get("kinds") or None,
                              body.get("symbols") or None, body.get("from") or None, body.get("to") or None, dry)


@app.get("/api/memory")
def memory():
    return {"rss": mem_usage()}


@app.post("/api/memory/release")
def memory_release():
    if RUNNER.busy():
        raise HTTPException(409, "Đang có lượt chạy")
    RUNNER.runs.clear()
    before, after = release_memory()
    return {"before": before, "after": after}


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def serve(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = True):
    import uvicorn
    _seed_configs()
    CONFIGS.mkdir(parents=True, exist_ok=True)
    ACTUAL_DIR.mkdir(parents=True, exist_ok=True)
    RUNNER.restore()
    url = f"http://127.0.0.1:{port}"
    if host not in ("127.0.0.1", "localhost"):
        try:
            ip = socket.gethostbyname(socket.gethostname())
            print(f"  Máy khác cùng mạng: http://{ip}:{port}")
        except OSError:
            pass
    print(f"  Giao diện: {url}   (dữ liệu: {data_path('.').resolve()})")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
