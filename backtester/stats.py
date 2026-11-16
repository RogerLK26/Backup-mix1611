"""Tính chỉ số hiệu quả chiến lược từ danh sách lệnh."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .utils import fmt_ms, local_day_key


def summarize(res) -> dict:
    cfg = res.cfg
    t = res.trades
    s: dict = {
        "strategy": cfg.name,
        "exchange": cfg.exchange,
        "period": f"{fmt_ms(cfg.start_ms, cfg.timezone)} → {fmt_ms(cfg.end_ms, cfg.timezone)} ({cfg.timezone})",
        "symbols_in_universe": len(res.universe.symbols),
        "signals_found": res.n_candidates,
        "trades": int(len(t)),
        "initial_balance": cfg.initial_balance,
    }
    if len(t) == 0:
        s.update({"final_balance": cfg.initial_balance, "net_pnl": 0.0, "return_pct": 0.0})
        s["rejected"] = dict(res.rejected.most_common())
        return s
    net = t["net_pnl"]
    wins, losses = net[net > 0], net[net <= 0]
    eq = np.array([cfg.initial_balance] + list(t.sort_values("exit_t")["balance_after"]))
    peak = np.maximum.accumulate(eq)
    dd = peak - eq
    k = int(np.argmax(dd))
    final = float(eq[-1])
    days = max(1e-9, (cfg.end_ms - cfg.start_ms) / 86_400_000)
    s.update({
        "final_balance": final,
        "net_pnl": float(net.sum()),
        "return_pct": (final / cfg.initial_balance - 1) * 100,
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "win_rate_pct": len(wins) / len(t) * 100,
        "tp": int((t.reason == "TP").sum()),
        "sl": int((t.reason == "SL").sum()),
        "liquidations": int((t.reason == "LIQ").sum()),
        "timeouts": int((t.reason == "TIMEOUT").sum()),
        "gross_pnl": float(t.gross_pnl.sum()),
        "fees": float((t.entry_fee + t.exit_fee).sum()),
        "funding": float(t.funding.sum()),
        "profit_factor": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else math.inf,
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "expectancy_per_trade": float(net.mean()),
        "avg_roe_pct": float(t.roe_pct.mean()),
        "best_trade": float(net.max()),
        "worst_trade": float(net.min()),
        "max_drawdown": float(dd[k]),
        "max_drawdown_pct": float(dd[k] / peak[k] * 100) if peak[k] > 0 else 0.0,
        "avg_hold_s": float(t.hold_s.mean()),
        "median_hold_s": float(t.hold_s.median()),
        "trades_per_day": len(t) / days,
        "max_consecutive_losses": _max_streak(net.values <= 0),
        "resolution_tick_pct": float((t.resolution == "tick").mean() * 100),
        "ambiguous_trades": int(t.ambiguous.sum()),
        **_hour_stats(t, cfg.timezone),
    })
    s["rejected"] = dict(res.rejected.most_common())
    return s


def _hour_stats(t: pd.DataFrame, tz: str) -> dict:
    from .engine.fast import hour_stats
    hours = pd.to_datetime(t.entry_t, unit="ms", utc=True).dt.tz_convert(tz).dt.hour
    return hour_stats(hours.tolist(), t.net_pnl.tolist())


def _max_streak(mask) -> int:
    best = cur = 0
    for m in mask:
        cur = cur + 1 if m else 0
        best = max(best, cur)
    return best


def _counts(t: pd.DataFrame, key) -> dict:
    """Các cột đếm theo nhóm, tính vector hoá (nhanh hơn groupby.apply hàng chục lần)."""
    return {
        "tp": (t.reason == "TP").groupby(key).sum(),
        "sl": t.reason.isin(["SL", "LIQ"]).groupby(key).sum(),
        "win_rate_pct": (t.net_pnl > 0).groupby(key).mean() * 100,
    }


def by_symbol(t: pd.DataFrame) -> pd.DataFrame:
    if len(t) == 0:
        return pd.DataFrame()
    g, c = t.groupby("symbol"), _counts(t, t.symbol)
    df = pd.DataFrame({
        "trades": g.size(), "tp": c["tp"], "sl": c["sl"], "win_rate_pct": c["win_rate_pct"],
        "net_pnl": g.net_pnl.sum(), "avg_hold_s": g.hold_s.mean(),
    })
    return df.sort_values("net_pnl", ascending=False)


def by_day(t: pd.DataFrame, tz: str, reset_hour: int = 0) -> pd.DataFrame:
    if len(t) == 0:
        return pd.DataFrame()
    # giống local_day_key: giờ địa phương lùi reset_hour giờ rồi lấy ngày
    day = (pd.to_datetime(t.exit_t, unit="ms", utc=True).dt.tz_convert(tz)
           - pd.Timedelta(hours=reset_hour)).dt.strftime("%Y-%m-%d")
    day.name = "day"
    g, c = t.groupby(day), _counts(t, day)
    return pd.DataFrame({"trades": g.size(), "tp": c["tp"], "sl": c["sl"], "net_pnl": g.net_pnl.sum()})


def by_hour(t: pd.DataFrame, tz: str) -> pd.DataFrame:
    if len(t) == 0:
        return pd.DataFrame()
    hour = pd.to_datetime(t.entry_t, unit="ms", utc=True).dt.tz_convert(tz).dt.hour
    hour.name = "hour"
    g, c = t.groupby(hour), _counts(t, hour)
    return pd.DataFrame({
        "trades": g.size(), "win_rate_pct": c["win_rate_pct"], "net_pnl": g.net_pnl.sum(),
    }).reindex(range(24), fill_value=0)


LABELS_VI = {
    "outside_period": "ngoài thời gian backtest",
    "max_runtime": "quá thời gian chạy tối đa",
    "weekday": "ngày không giao dịch",
    "session": "ngoài khung giờ",
    "global_sl_cooldown": "nghỉ sau SL (cả chiến lược)",
    "consecutive_sl_pause": "nghỉ sau chuỗi SL liên tiếp",
    "daily_max_trades": "đủ số lệnh/ngày",
    "daily_max_tp": "đủ TP/ngày",
    "daily_max_sl": "đủ SL/ngày",
    "daily_loss_limit": "chạm giới hạn lỗ/ngày",
    "coin_cooldown": "coin đang nghỉ (sau TP/SL)",
    "coin_max_tp": "coin đủ TP/ngày",
    "coin_max_sl": "coin đủ SL/ngày",
    "coin_position_open": "coin đang có lệnh mở",
    "max_open_positions": "đủ số lệnh mở đồng thời",
    "insufficient_balance": "không đủ số dư",
    "market_cap": "không đạt market cap",
    "max_leverage": "sàn không cho đòn bẩy đủ cao",
    "no_data": "thiếu dữ liệu",
    "no_pullback": "giá không hồi đủ trong thời gian theo dõi",
}
