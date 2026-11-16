"""Kiểm định độ chính xác (độc lập với engine):

1) Nến 1m của sàn có khớp với nến dựng lại từ tick không?
2) Với mỗi lệnh độ phân giải 'tick' trong trades.csv: duyệt TỪNG tick từ lúc khớp vào lệnh
   (brute-force, không dùng tối ưu quét nến) để tìm lần chạm TP/SL đầu tiên -> so với kết quả engine.

Cách dùng:
    python tools/verify.py results/smoke/binance --exchange binance
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtester.data import get_exchange  # noqa: E402
from backtester.data.store import DataStore  # noqa: E402
from backtester.utils import MIN_MS, parse_time  # noqa: E402


def check_klines(store: DataStore, symbol: str, day: int) -> dict:
    bars = store.day_bars(symbol, day)
    v = store.ticks(symbol, day, day + 86_400_000)
    if v is None or len(v[0]) == 0 or len(bars) == 0:
        return {}
    ts, px = v
    m = ts - ts % MIN_MS
    df = pd.DataFrame({"m": m, "px": px})
    g = df.groupby("m")["px"].agg(["first", "max", "min", "last"])
    k = pd.DataFrame({"o": bars.o, "h": bars.h, "l": bars.l, "c": bars.c}, index=bars.ts)
    j = k.join(g, how="inner")
    if len(j) == 0:
        return {}
    tol = 1e-9
    return {
        "minutes": len(j),
        "open_match": float((abs(j.o - j["first"]) <= tol * j.o).mean() * 100),
        "high_match": float((abs(j.h - j["max"]) <= tol * j.h).mean() * 100),
        "low_match": float((abs(j.l - j["min"]) <= tol * j.l).mean() * 100),
        "close_match": float((abs(j.c - j["last"]) <= tol * j.c).mean() * 100),
    }


def brute_exit(store, symbol, side, entry_ms, tp, sl, liq, deadline):
    t = entry_ms + 1
    up = [x for x in ([sl, liq] if side == -1 else [tp]) if x == x and x is not None]
    dn = [x for x in ([tp] if side == -1 else [sl, liq]) if x == x and x is not None]
    up_lv = min(up) if up else np.inf
    dn_lv = max(dn) if dn else -np.inf
    step = 3_600_000
    while t < deadline:
        b = min(deadline, t + step)
        v = store.ticks(symbol, t, b)
        if v is None:
            return None
        ts, px = v
        for i in range(len(px)):  # cố ý duyệt từng tick
            if px[i] >= up_lv or px[i] <= dn_lv:
                return int(ts[i]), float(px[i])
        t = b
    return "TIMEOUT", None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("result_dir")
    ap.add_argument("--exchange", required=True)
    ap.add_argument("--tz", default="Asia/Ho_Chi_Minh")
    ap.add_argument("--max-hold", type=float, default=None, help="phút (để kiểm tra lệnh TIMEOUT)")
    ap.add_argument("--cache", default="cache")
    a = ap.parse_args()

    store = DataStore(get_exchange(a.exchange), a.cache)
    t = pd.read_csv(Path(a.result_dir) / "trades.csv")

    print("== 1) Nến 1m của sàn so với nến dựng từ tick")
    seen = set()
    for _, r in t.iterrows():
        day = parse_time(r.entry_t[:10], a.tz)
        day = day - day % 86_400_000
        if (r.symbol, day) in seen:
            continue
        seen.add((r.symbol, day))
        res = check_klines(store, r.symbol, day)
        if res:
            print(f"  {r.symbol} {pd.to_datetime(day, unit='ms').date()}: {res}")

    print("== 2) Kiểm tra từng lệnh bằng duyệt tick brute-force")
    ok = bad = skip = 0
    for _, r in t.iterrows():
        if r.resolution != "tick":
            skip += 1
            continue
        side = 1 if r.side == "LONG" else -1
        entry_ms = parse_time(r.entry_t[:19], a.tz) + int(r.entry_t[20:23])
        exit_ms = parse_time(r.exit_t[:19], a.tz) + int(r.exit_t[20:23])
        deadline = exit_ms + 1 if r.reason == "TIMEOUT" else exit_ms + 86_400_000
        if r.reason == "TIMEOUT" and a.max_hold:
            deadline = entry_ms + int(a.max_hold * MIN_MS)
        tp = r.tp_px if pd.notna(r.tp_px) else None
        sl = r.sl_px if pd.notna(r.sl_px) else None
        liq = r.liq_px if pd.notna(r.liq_px) else None
        got = brute_exit(store, r.symbol, side, entry_ms, tp, sl, liq, deadline)
        if got is None:
            skip += 1
            continue
        if r.reason == "TIMEOUT":
            good = got[0] == "TIMEOUT" or got[0] >= exit_ms - 1
        else:
            good = got[0] == exit_ms
        if good:
            ok += 1
        else:
            bad += 1
            print(f"  LỆCH: {r.symbol} {r.side} vào {r.entry_t} engine={r.reason}@{r.exit_t} brute={got}")
    print(f"  Khớp: {ok}  Lệch: {bad}  Bỏ qua (không có tick): {skip}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
