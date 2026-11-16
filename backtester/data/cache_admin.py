"""Thống kê & xoá dữ liệu cache (nến 1m, tick, volume ngày, funding, market cap).

Cấu trúc cache:
    <cache>/<sàn>/klines/<COIN>/<YYYY-MM-DD>.parquet        nến 1m theo ngày UTC
    <cache>/<sàn>/ticks/<COIN>/<YYYY-MM-DD_HHMMSS>.parquet  tick theo khối (ngày hoặc giờ)
    <cache>/<sàn>/daily/<COIN>.parquet|json                  volume ngày (dùng lọc sơ bộ)
    <cache>/<sàn>/funding/<COIN>.parquet|json                lịch sử funding
    <cache>/<sàn>/symbols.json                               danh sách coin
    <cache>/coingecko/*                                      market cap
"""
from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path

from ..utils import data_path

KINDS = {
    "klines": "Nến 1m",
    "ticks": "Tick",
    "daily": "Volume ngày",
    "funding": "Funding",
    "meta": "Danh sách coin",
    "marketcap": "Market cap (CoinGecko)",
    "leverage": "Đòn bẩy tối đa (không tải lại được)",
}
DATED = {"klines", "ticks"}


def _walk(root: Path):
    """(sàn, loại, coin, ngày 'YYYY-MM-DD' | None, path, size) cho mọi file trong cache."""
    if not root.exists():
        return
    for exd in os.scandir(root):
        if not exd.is_dir():
            continue
        if exd.name == "coingecko":
            for f in os.scandir(exd.path):
                if f.is_file():
                    yield "coingecko", "marketcap", None, None, Path(f.path), f.stat().st_size
            continue
        for kd in os.scandir(exd.path):
            if kd.is_file():
                yield exd.name, "meta", None, None, Path(kd.path), kd.stat().st_size
                continue
            kind = kd.name
            if kind in DATED:
                for sd in os.scandir(kd.path):
                    if not sd.is_dir():
                        continue
                    for f in os.scandir(sd.path):
                        if f.is_file():
                            yield exd.name, kind, sd.name, f.name[:10], Path(f.path), f.stat().st_size
            elif kind == "leverage":
                for f in os.scandir(kd.path):
                    if f.is_file():
                        yield exd.name, kind, None, f.name[:10], Path(f.path), f.stat().st_size
            else:
                for f in os.scandir(kd.path):
                    if f.is_file():
                        yield exd.name, kind, Path(f.name).stem, None, Path(f.path), f.stat().st_size


def summary(cache_dir: str | Path = "cache") -> dict:
    root = data_path(cache_dir)
    total = files = 0
    ex = {}
    sym_size: dict[tuple[str, str], int] = defaultdict(int)
    for exn, kind, sym, day, _, size in _walk(root):
        total += size
        files += 1
        e = ex.setdefault(exn, {"exchange": exn, "size": 0, "files": 0, "kinds": {}})
        e["size"] += size
        e["files"] += 1
        k = e["kinds"].setdefault(kind, {"kind": kind, "label": KINDS.get(kind, kind), "size": 0, "files": 0,
                                         "symbols": set(), "first": None, "last": None})
        k["size"] += size
        k["files"] += 1
        if sym:
            k["symbols"].add(sym)
            sym_size[(exn, sym)] += size
        if day:
            k["first"] = day if k["first"] is None or day < k["first"] else k["first"]
            k["last"] = day if k["last"] is None or day > k["last"] else k["last"]
    out = []
    for e in sorted(ex.values(), key=lambda x: -x["size"]):
        kinds = []
        for k in sorted(e["kinds"].values(), key=lambda x: -x["size"]):
            k["symbols"] = len(k["symbols"])
            kinds.append(k)
        e["kinds"] = kinds
        e["top_symbols"] = [[s, v] for (x, s), v in sorted(sym_size.items(), key=lambda kv: -kv[1]) if x == e["exchange"]][:15]
        out.append(e)
    try:
        st = os.statvfs(root) if hasattr(os, "statvfs") else None
        free = st.f_bavail * st.f_frsize if st else None
    except OSError:
        free = None
    if free is None:
        import shutil
        try:
            free = shutil.disk_usage(root if root.exists() else root.parent).free
        except OSError:
            free = None
    return {"path": str(root.resolve()), "size": total, "files": files, "disk_free": free, "exchanges": out}


def delete(cache_dir: str | Path = "cache", exchanges: list[str] | None = None, kinds: list[str] | None = None,
           symbols: list[str] | None = None, date_from: str | None = None, date_to: str | None = None,
           dry_run: bool = True) -> dict:
    """Xoá file khớp mọi điều kiện đã cho (bỏ trống = không lọc theo điều kiện đó).

    date_from / date_to ('YYYY-MM-DD', tính cả 2 đầu) chỉ áp dụng cho nến 1m và tick; khi có lọc ngày,
    các loại không theo ngày (volume ngày, funding, ...) sẽ không bị xoá.
    """
    root = data_path(cache_dir)
    exs = {e.lower() for e in exchanges} if exchanges else None
    kds = set(kinds) if kinds else None
    syms = {s.upper().replace("-", "").replace("_", "") for s in symbols} if symbols else None
    syms = {s if s.endswith("USDT") else s + "USDT" for s in syms} if syms else None
    n = size = 0
    touched_dirs = set()
    for exn, kind, sym, day, path, sz in list(_walk(root)):
        if exs is not None and exn not in exs:
            continue
        if kds is not None and kind not in kds:
            continue
        if kds is None and kind == "leverage":
            continue  # bản ghi đòn bẩy theo ngày không tải lại được -> chỉ xoá khi chọn rõ loại này
        if syms is not None and (sym is None or sym.upper() not in syms):
            continue
        if date_from or date_to:
            if day is None:
                continue
            if date_from and day < date_from:
                continue
            if date_to and day > date_to:
                continue
        n += 1
        size += sz
        if not dry_run:
            try:
                path.unlink()
                touched_dirs.add(path.parent)
            except OSError:
                pass
    for d in sorted(touched_dirs, key=lambda p: -len(p.parts)):
        try:
            if d.exists() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    return {"files": n, "size": size, "dry_run": dry_run}


def human(n: float | None) -> str:
    if n is None:
        return "?"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or u == "TB":
            return f"{n:,.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024
    return str(n)
