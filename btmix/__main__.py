"""CLI của bản gộp.

    python -m btmix web                                   # giao diện web (cả 2 chế độ): http://127.0.0.1:8000
    python -m btmix web --lan                             # cho điện thoại cùng Wi-Fi
    python -m btmix bt configs/pump_short.yaml --exchange binance,bybit
    python -m btmix replay "Actual Performance/Data Bybit.csv" --tp 0.5-3:0.25 --sl 3,5,10 --hold 30,60,120
    python -m btmix prepare "Actual Performance/Data OKX.csv"   # tải trước tick cho file lệnh thật
    python -m btmix cache                                 # xem / xoá dữ liệu đã tải
"""
from __future__ import annotations

import argparse
import os
import sys

from backtester.config import load_raw
from backtester.utils import setup_logging


def _utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def main(argv=None):
    _utf8()
    ap = argparse.ArgumentParser(prog="btmix", description="Backtest chiến lược + replay lệnh thật (bản gộp)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("web", help="Giao diện web (cấu hình, chạy, bảng xếp hạng, lịch sử)")
    w.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    w.add_argument("--host")
    w.add_argument("--lan", action="store_true", help="Cho thiết bị khác cùng mạng truy cập")
    w.add_argument("--no-browser", action="store_true")

    b = sub.add_parser("bt", help="Backtest chiến lược theo file cấu hình YAML")
    b.add_argument("config")
    b.add_argument("--exchange", help="Ghi đè sàn, cách nhau dấu phẩy (hoặc 'all')")
    b.add_argument("--start")
    b.add_argument("--end")
    b.add_argument("--top", type=int, default=20)
    b.add_argument("-v", "--verbose", action="store_true")

    for name, helptxt in (("replay", "Replay lệnh thật trong file CSV"),
                          ("prepare", "Chỉ tải trước tick & trích đường giá cho file lệnh thật")):
        r = sub.add_parser(name, help=helptxt)
        r.add_argument("file")
        r.add_argument("--exchange")
        r.add_argument("--from", dest="date_from")
        r.add_argument("--to", dest="date_to")
        r.add_argument("--coins", default="")
        r.add_argument("--tp", default="1")
        r.add_argument("--sl", default="10")
        r.add_argument("--hold", default="120")
        r.add_argument("--mode", default="single")
        r.add_argument("--entry", default="auto", choices=["auto", "csv", "tick"])
        r.add_argument("--notional", type=float)
        r.add_argument("--leverage", type=float, default=20)
        r.add_argument("--reverse", action="store_true")
        r.add_argument("--no-auto-slip", action="store_true")
        r.add_argument("--top", type=int, default=20)
        r.add_argument("-v", "--verbose", action="store_true")

    c = sub.add_parser("cache", help="Xem dung lượng dữ liệu đã tải / xoá bớt")
    c.add_argument("--delete", action="store_true")
    c.add_argument("--exchange")
    c.add_argument("--kind")
    c.add_argument("--symbols")
    c.add_argument("--from", dest="date_from")
    c.add_argument("--to", dest="date_to")
    c.add_argument("--yes", action="store_true")

    a = ap.parse_args(argv)
    setup_logging(getattr(a, "verbose", False))

    if a.cmd == "web":
        from .server import serve
        cloud = "PORT" in os.environ
        host = a.host or ("0.0.0.0" if (a.lan or cloud) else "127.0.0.1")
        serve(host, a.port, not (a.no_browser or cloud))
        return 0

    if a.cmd == "cache":
        from backtester.__main__ import main as bt_main
        args = ["cache"] + (["--delete"] if a.delete else []) + (["--yes"] if a.yes else [])
        for k, v in (("--exchange", a.exchange), ("--kind", a.kind), ("--symbols", a.symbols),
                     ("--from", a.date_from), ("--to", a.date_to)):
            if v:
                args += [k, v]
        return bt_main(args)

    if a.cmd == "bt":
        return _run_bt(a)
    return _run_replay(a)


def _run_bt(a) -> int:
    from .jobs import BtJob
    raw = load_raw(a.config)
    if a.start:
        raw["start"] = a.start
    if a.end:
        raw["end"] = a.end
    from backtester.data import EXCHANGES
    exs = a.exchange or raw.get("exchange", "binance")
    exchanges = list(EXCHANGES) if str(exs).lower() == "all" else (
        [x.strip().lower() for x in exs] if isinstance(exs, list)
        else [x.strip().lower() for x in str(exs).split(",")])

    def prog(stage, done, total):
        if total and (done % 200 == 0 or done == total):
            print(f"  {stage}: {done}/{total}", flush=True)

    job = BtJob({"config": raw, "exchanges": exchanges}, progress=prog)
    s = job.execute()
    print(f"\n{s['title']} · {', '.join(exchanges)} · {s['n_combos']} cấu hình ({s['elapsed_s']}s)")
    _print_rows(job.rows, a.top)
    return 0


def _run_replay(a) -> int:
    from .jobs import ReplayJob
    from replay.actual import detect_exchange
    from replay.bt import DATA_DIR as ACTUAL_DIR  # noqa: F401  (để thư mục được tạo khi cần)
    fees = {"bybit": (0.02, 0.055), "okx": (0.02, 0.05), "binance": (0.02, 0.05)}
    ex = (a.exchange or detect_exchange(a.file) or "").lower()
    mk, tk = fees.get(ex, (0.02, 0.05))
    grid = {"tp": a.tp, "sl": a.sl, "hold": a.hold} if a.cmd == "replay" else {"tp": "1", "sl": "10", "hold": "120"}
    req = {"kind": "replay", "file": a.file, "exchange": a.exchange, "grid": grid,
           "date_from": a.date_from, "date_to": a.date_to, "coins": a.coins, "entry_mode": a.entry,
           "settings": {"modes": a.mode.split(","), "leverage": a.leverage, "maker_pct": mk, "taker_pct": tk,
                        "auto_slip": not a.no_auto_slip, "direction": "reverse" if a.reverse else "same",
                        **({"notional": a.notional} if a.notional else {})}}

    def prog(stage, done, total):
        if total and (done % 200 == 0 or done == total):
            print(f"  {stage}: {done}/{total}", flush=True)

    job = ReplayJob(req, progress=prog)
    s = job.execute()
    v = job.validation
    print(f"\n{s['file']} ({s['exchange']}): {len(job.run.trades)} lệnh thật, đường giá: {s['resolution']}, "
          f"giá vào: {s['entry_src']}  ({s['elapsed_s']}s)")
    if v.get("n"):
        print(f"Kiểm định: lý do thoát khớp {v['reason_match']:.1f}% · thời điểm lệch ≤10s {v['exit_within_10s']:.1f}%"
              f" · PnL mô phỏng {v['pnl_sim']:.2f} vs thật {v['pnl_actual']:.2f}")
    if a.cmd == "prepare":
        return 0
    _print_rows(job.rows, a.top)
    return 0


def _print_rows(rows: list[dict], top: int):
    best = sorted([r for r in rows if r["n"]], key=lambda r: -(r["net"] or -1e18))[:top]
    print(f"\nTop {len(best)} theo lãi ròng:")
    print(f"{'chế độ':10s} {'TP%':>6s} {'SL%':>6s} {'giữ':>6s} {'lệnh':>6s} {'bỏ':>5s} {'TP':>5s} {'SL':>5s} "
          f"{'TO':>5s} {'win%':>6s} {'net':>10s} {'maxDD':>9s} {'net/DD':>7s} {'PF':>6s}")
    for r in best:
        print(f"{str(r['mode'])[:10]:10s} {_n(r['tp']):>6} {_n(r['sl']):>6} {_n(r['hold']):>6} {r['n']:6d} "
              f"{r['skipped']:5d} {r['n_tp']:5d} {r['n_sl']:5d} {r['n_timeout']:5d} {r['win_rate']:6.1f} "
              f"{r['net']:10.2f} {r['max_dd']:9.2f} {_f(r['ret_dd']):>7} {_f(r['pf']):>6}")


def _n(v):
    return "–" if v is None else (f"{v:g}")


def _f(v):
    return "–" if v is None else f"{v:.2f}"


if __name__ == "__main__":
    sys.exit(main())
