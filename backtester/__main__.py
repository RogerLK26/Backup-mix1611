"""CLI:
    python -m backtester run configs/pump_short.yaml
    python -m backtester run configs/pump_short.yaml --exchange binance,bybit,okx
    python -m backtester run configs/pump_short.yaml --start 2026-09-01 --end 2026-09-08 --out results/test
    python -m backtester symbols bybit
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from .config import load_raw
from .data import EXCHANGES, get_exchange
from .runner import run
from .utils import data_path, setup_logging


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="backtester", description="Backtest chiến lược biến động ngắn hạn trên nhiều sàn")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="Chạy backtest theo file cấu hình")
    r.add_argument("config")
    r.add_argument("--exchange", help=f"Ghi đè sàn, nhiều sàn cách nhau dấu phẩy ({','.join(EXCHANGES)}, hoặc 'all')")
    r.add_argument("--start")
    r.add_argument("--end")
    r.add_argument("--resolution", choices=["auto", "tick", "1m"])
    r.add_argument("--out", help="Thư mục kết quả (mặc định results/<tên>_<thời gian>)")
    r.add_argument("-v", "--verbose", action="store_true")

    w = sub.add_parser("web", help="Mở giao diện web (cấu hình, chạy, xem kết quả)")
    w.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    w.add_argument("--lan", action="store_true", help="Cho phép thiết bị khác cùng mạng (điện thoại) truy cập")
    w.add_argument("--host", help="Địa chỉ lắng nghe (mặc định 127.0.0.1; tự là 0.0.0.0 khi có biến PORT như trên Railway)")
    w.add_argument("--no-browser", action="store_true", help="Không tự mở trình duyệt")

    c = sub.add_parser("cache", help="Xem dung lượng dữ liệu đã tải / xoá bớt")
    c.add_argument("--delete", action="store_true", help="Xoá các file khớp điều kiện (mặc định chỉ xem trước)")
    c.add_argument("--exchange", help="Sàn, cách nhau dấu phẩy (vd okx,bybit)")
    c.add_argument("--kind", help="klines,ticks,daily,funding,meta,marketcap")
    c.add_argument("--symbols", help="Coin, cách nhau dấu phẩy (vd PEPE,WIFUSDT)")
    c.add_argument("--from", dest="date_from", help="Từ ngày YYYY-MM-DD (chỉ nến 1m & tick)")
    c.add_argument("--to", dest="date_to", help="Đến ngày YYYY-MM-DD (tính cả ngày này)")
    c.add_argument("--yes", action="store_true", help="Không hỏi xác nhận")

    s = sub.add_parser("symbols", help="Liệt kê các cặp USDT perpetual của sàn")
    s.add_argument("exchange")

    a = ap.parse_args(argv)
    setup_logging(getattr(a, "verbose", False))

    if a.cmd == "web":
        from btmix.server import serve   # giao diện web của bản gộp (btmix/)
        cloud = "PORT" in os.environ
        host = a.host or ("0.0.0.0" if (a.lan or cloud) else "127.0.0.1")
        serve(host, a.port, not (a.no_browser or cloud))
        return 0

    if a.cmd == "cache":
        from .data import cache_admin as ca
        split = lambda v: [x.strip() for x in v.split(",") if x.strip()] if v else None  # noqa: E731
        filt = dict(exchanges=split(a.exchange), kinds=split(a.kind), symbols=split(a.symbols),
                    date_from=a.date_from, date_to=a.date_to)
        if not a.delete and not any(filt.values()):
            s = ca.summary("cache")
            print(f"Thư mục: {s['path']}")
            print(f"Tổng: {ca.human(s['size'])} ({s['files']:,} file) · ổ đĩa còn trống: {ca.human(s['disk_free'])}")
            for e in s["exchanges"]:
                print()
                print(f"  {e['exchange']:10s} {ca.human(e['size']):>10s}")
                for k in e["kinds"]:
                    rng = f"  {k['first']} → {k['last']}" if k["first"] else ""
                    print(f"     - {k['label']:22s} {ca.human(k['size']):>10s}  {k['files']:>6,} file  {k['symbols']:>4} coin{rng}")
            return 0
        prev = ca.delete("cache", dry_run=True, **filt)
        print(f"Khớp {prev['files']:,} file, {ca.human(prev['size'])}")
        if not a.delete or not prev["files"]:
            print("(Chỉ xem trước. Thêm --delete để xoá.)" if prev["files"] else "")
            return 0
        if not a.yes and input("Xoá? (y/N) ").strip().lower() != "y":
            return 0
        r = ca.delete("cache", dry_run=False, **filt)
        print(f"Đã xoá {r['files']:,} file, giải phóng {ca.human(r['size'])}")
        return 0

    if a.cmd == "symbols":
        ex = get_exchange(a.exchange)
        lst = ex.list_symbols()
        print(f"{len(lst)} cặp:")
        print(" ".join(sorted(x.symbol for x in lst)))
        return 0

    raw = load_raw(a.config)
    if a.start:
        raw["start"] = a.start
    if a.end:
        raw["end"] = a.end
    if a.resolution:
        raw.setdefault("data", {})["resolution"] = a.resolution
    exs = raw.get("exchange", "binance")
    if a.exchange:
        exs = a.exchange
    exchanges = list(EXCHANGES) if str(exs).lower() == "all" else (
        [x.strip().lower() for x in exs] if isinstance(exs, list) else [x.strip().lower() for x in str(exs).split(",")])
    name = str(raw.get("name", "strategy")).replace(" ", "_")
    out = Path(a.out) if a.out else data_path("results") / f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run(raw, exchanges, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
