"""Xuất kết quả: trades.csv, summary.json, các bảng thống kê (report.html bổ sung sau)."""
from __future__ import annotations

import json
from pathlib import Path

from .stats import by_day, by_hour, by_symbol
from .utils import fmt_ms, log


def trades_for_export(res):
    t = res.trades.copy()
    tz = res.cfg.timezone
    for col in ("candle_t", "setup_t", "signal_t", "entry_t", "exit_t"):
        if col in t:
            t[col] = [fmt_ms(x, tz, with_ms=True) for x in t[col]]
    return t


def write_report(res, summ: dict, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    tz = res.cfg.timezone
    trades_for_export(res).to_csv(out_dir / "trades.csv", index=False, encoding="utf-8-sig")
    (out_dir / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    by_symbol(res.trades).to_csv(out_dir / "by_symbol.csv", encoding="utf-8-sig")
    by_day(res.trades, tz, res.cfg.day_reset_hour).to_csv(out_dir / "by_day.csv", encoding="utf-8-sig")
    by_hour(res.trades, tz).to_csv(out_dir / "by_hour.csv", encoding="utf-8-sig")
    from .html_report import render_html, report_data
    data = report_data(res, summ)
    (out_dir / "result.json").write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    (out_dir / "report.html").write_text(render_html(res, summ, data), encoding="utf-8")
    log.info("Đã ghi kết quả vào %s", out_dir.resolve())
