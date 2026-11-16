"""Chạy backtest đầy đủ: 1 sàn, nhiều sàn, hoặc quét tham số (sweep)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

from .config import build_config
from .data import get_exchange
from .data.marketcap import MarketCap
from .data.store import DataStore
from .engine.backtest import Backtester, load_klines, prepare_universe, scan_candidates
from .report import write_report
from .stats import summarize
from .progress import PROGRESS, Cancelled
from .utils import log


def _mcap_for(cfg):
    if not (cfg.min_market_cap or cfg.max_market_cap) or cfg.market_cap_source == "none":
        return None
    return MarketCap(cfg.cache_dir, cfg.coingecko_api_key, cfg.coingecko_pro, cfg.coingecko_ids)


def prepare(raw: dict, exchange: str):
    raw = dict(raw, exchange=exchange)
    ex = get_exchange(exchange)
    cfg = build_config(raw, ex.default_fees)
    store = DataStore(ex, cfg.cache_dir, cfg.workers)
    mcap = _mcap_for(cfg)
    if cfg.resolution != "1m" and ex.tick_history_days == 0:
        log.warning("%s không có dữ liệu tick lịch sử -> mô phỏng trong nến 1m (%s)", exchange, cfg.intrabar)
    uni = prepare_universe(cfg, store, mcap)
    for n in uni.notes:
        log.warning(n)
    load_klines(cfg, store, uni)
    return cfg, store, mcap, uni


def run_one(raw: dict, exchange: str, out_dir: Path, prepared=None, write=True):
    t0 = time.time()
    cfg, store, mcap, uni = prepared or prepare(raw, exchange)
    if prepared is not None:
        cfg = build_config(dict(raw, exchange=exchange), store.ex.default_fees)
    cands = scan_candidates(cfg, store, uni)
    log.info("Tìm thấy %d tín hiệu thô (đã lọc volume 24h)", len(cands))
    if cfg.resolution != "1m" and cands and store.ex.tick_history_days != 0:
        store.prefetch_ticks([(c.symbol, c.t) for c in cands])
    bt = Backtester(cfg, store, mcap, uni)
    res = bt.run(cands)
    summ = summarize(res)
    summ["runtime_s"] = round(time.time() - t0, 1)
    if write:
        write_report(res, summ, out_dir)
    return res, summ


BATCH_DATA_KEYS = ("start", "end", "timezone", "universe", "data")


def run_batch(raw: dict, out_root: Path):
    """Chạy chi tiết nhiều cấu hình (vd nhóm yêu thích) trong một lượt để so sánh với nhau.
    raw["batch"] = [{"label", "config", "fav_id"?}]; cấu hình cùng sàn & cùng dữ liệu dùng chung phần đã tải."""
    out_root.mkdir(parents=True, exist_ok=True)
    items = raw["batch"]
    rows, cache = [], {}
    for i, it in enumerate(items, 1):
        cfg_raw = dict(it["config"])
        ex = cfg_raw.get("exchange", "binance")
        ex = (ex[0] if isinstance(ex, list) else str(ex).split(",")[0]).strip().lower()
        cfg_raw["exchange"] = ex
        label, sub = it.get("label") or f"Cấu hình {i}", f"fav_{i:02d}"
        log.info("=" * 70)
        log.info("[%d/%d] %s · %s", i, len(items), label, ex.upper())
        PROGRESS.set_exchange(f"{ex} · {i}/{len(items)}")
        try:
            key = json.dumps([ex] + [cfg_raw.get(k) for k in BATCH_DATA_KEYS], sort_keys=True, default=str)
            if key not in cache:
                cache.clear()   # giữ một bộ dữ liệu trong RAM
                cache[key] = prepare(cfg_raw, ex)
            _, summ = run_one(cfg_raw, ex, out_root / sub, prepared=cache[key])
            summ.update({"params": label, "dir": sub, "exchange": ex, "fav_id": it.get("fav_id")})
            rows.append(summ)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("Lỗi khi chạy %s: %s", label, e)
            rows.append({"exchange": ex, "params": label, "dir": sub, "fav_id": it.get("fav_id"), "error": str(e)})
        save_json(rows, out_root / "index.json")
    return rows


def run(raw: dict, exchanges: list[str], out_root: Path):
    if raw.get("batch"):
        return run_batch(raw, out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    rows = []
    for exn in exchanges:
        log.info("=" * 70)
        log.info("SÀN %s", exn.upper())
        PROGRESS.set_exchange(exn)
        try:
            if raw.get("sweep"):
                rows.extend(sweep(raw, exn, out_root / exn))
            else:
                _, summ = run_one(raw, exn, out_root / exn)
                summ["dir"] = exn
                rows.append(summ)
                print_summary(summ)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("Lỗi khi backtest %s: %s", exn, e)
            rows.append({"exchange": exn, "error": str(e)})
        save_json(rows, out_root / "index.json")
    if len(rows) > 1:
        cols = ["exchange", "params", "trades", "win_rate_pct", "net_pnl", "return_pct", "profit_factor",
                "max_drawdown_pct", "tp", "sl", "liquidations", "timeouts", "fees", "resolution_tick_pct", "error"]
        df = pd.DataFrame(rows)
        df = df[[c for c in cols if c in df.columns]]
        p = out_root / "comparison.csv"
        df.to_csv(p, index=False, encoding="utf-8-sig")
        log.info("Bảng so sánh: %s", p)
        with pd.option_context("display.width", 200, "display.max_columns", 20):
            print(df.round(2).to_string(index=False))
    return rows


def sweep(raw: dict, exchange: str, out_dir: Path):
    """Quét / tối ưu nhiều chiến lược (xem optimize.py). Trả về các dòng của top K đã chạy chi tiết."""
    from .optimize import run_sweep
    return run_sweep(raw, exchange, out_dir, prepare, run_one)


def print_summary(s: dict):
    def f(x, d=2):
        return f"{x:,.{d}f}" if isinstance(x, (int, float)) else str(x)

    lines = [
        f"Chiến lược     : {s['strategy']}  |  Sàn: {s['exchange']}",
        f"Thời gian      : {s['period']}",
        f"Coin hợp lệ    : {s['symbols_in_universe']}   Tín hiệu: {s['signals_found']}   Lệnh: {s['trades']}",
    ]
    if s["trades"]:
        lines += [
            f"Thắng/Thua     : {s['wins']}/{s['losses']}  (win rate {f(s['win_rate_pct'])}%)   "
            f"TP {s['tp']} | SL {s['sl']} | LIQ {s['liquidations']} | Hết giờ {s['timeouts']}",
            f"Lãi/Lỗ ròng    : {f(s['net_pnl'])} USDT  ({f(s['return_pct'])}%)   "
            f"Số dư: {f(s['initial_balance'])} → {f(s['final_balance'])}",
            f"Phí            : {f(s['fees'])}   Funding: {f(s['funding'], 4)}   PF: {f(s['profit_factor'])}",
            f"Max drawdown   : {f(s['max_drawdown'])} USDT ({f(s['max_drawdown_pct'])}%)   "
            f"Chuỗi thua dài nhất: {s['max_consecutive_losses']}",
            f"Giữ lệnh TB    : {f(s['avg_hold_s'], 1)}s (trung vị {f(s['median_hold_s'], 1)}s)   "
            f"Độ phân giải tick: {f(s['resolution_tick_pct'], 1)}%   Lệnh mơ hồ (1m): {s['ambiguous_trades']}",
        ]
    if s.get("rejected"):
        from .stats import LABELS_VI
        rj = ", ".join(f"{LABELS_VI.get(k, k)}: {v}" for k, v in s["rejected"].items())
        lines.append(f"Tín hiệu bị bỏ : {rj}")
    print("\n".join(lines))


def save_json(obj, p: Path):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
