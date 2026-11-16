"""Đọc & kiểm tra file cấu hình chiến lược (YAML)."""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .utils import MIN_MS, parse_duration, parse_time

# mọi khung đều chia hết 24h -> nến khung căn theo mốc 00:00 UTC như sàn (nến 1d mở lúc 07:00 giờ VN)
TIMEFRAMES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240,
              "6h": 360, "8h": 480, "12h": 720, "1d": 1440}
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass
class SignalRule:
    timeframe: str = "1m"
    change_pct: float = 5.0          # biến động tối thiểu (%) so với giá mở nến
    max_change_pct: float | None = None  # (trigger=close) bỏ qua nếu biến động vượt mức này
    direction: str = "up"            # up | down | both
    trigger: str = "touch"           # touch (chạm ngưỡng) | close (nến đóng)
    side: str = "reverse"            # long | short | follow (cùng chiều) | reverse (ngược chiều)
    # Chờ giá hồi: sau tín hiệu, theo dõi trong pullback_window; vào lệnh khi giá hồi pullback_pct % từ đỉnh
    # (tín hiệu tăng) hoặc bật pullback_pct % từ đáy (tín hiệu giảm). Hết thời gian mà chưa hồi -> bỏ tín hiệu.
    pullback_pct: float | None = None
    pullback_window_ms: int | None = None
    tf_minutes: int = 1

    def side_for(self, dir_sign: int) -> int:
        if self.side == "long":
            return 1
        if self.side == "short":
            return -1
        if self.side == "follow":
            return dir_sign
        return -dir_sign


@dataclass
class Config:
    name: str
    exchange: str
    start_ms: int
    end_ms: int
    timezone: str
    initial_balance: float
    # data
    resolution: str
    intrabar: str
    cache_dir: str
    workers: int
    # universe
    symbols: list[str]
    exclude: list[str]
    min_volume_24h: float | None
    max_volume_24h: float | None
    min_max_leverage: float | None
    min_market_cap: float | None
    max_market_cap: float | None
    market_cap_source: str
    unknown_market_cap: str
    coingecko_api_key: str
    coingecko_pro: bool
    coingecko_ids: dict
    # signals
    signals: list[SignalRule]
    # entry
    latency_ms: int
    entry_slippage_pct: float
    # position
    margin: float
    margin_mode: str
    leverage: float
    # exit
    take_profit: float | None
    stop_loss: float | None
    tp_sl_unit: str
    tp_order: str
    tp_fill: str
    sl_slippage_pct: float
    exit_slippage_pct: float
    max_hold_ms: int | None
    liquidation: bool
    maintenance_margin_pct: float
    # fees
    maker_pct: float
    taker_pct: float
    funding: bool
    # limits
    max_open_positions: int | None
    max_open_per_coin: int
    max_trades_per_day: int | None
    max_tp_per_day: int | None
    max_sl_per_day: int | None
    max_tp_per_coin_per_day: int | None
    max_sl_per_coin_per_day: int | None
    cooldown_after_tp_coin_ms: int | None
    cooldown_after_sl_coin_ms: int | None
    cooldown_after_trade_coin_ms: int | None
    cooldown_after_sl_global_ms: int | None
    max_consecutive_sl: int | None
    pause_after_consecutive_sl_ms: int | None
    daily_loss_limit: float | None
    max_runtime_ms: int | None
    day_reset_hour: int
    timeout_counts_as: str
    # schedule
    sessions: list[tuple[int, int]]
    weekdays: set[int]
    sweep: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @property
    def max_tf_minutes(self) -> int:
        return max(r.tf_minutes for r in self.signals)


def _opt_num(v):
    if v is None or v == "" or v is False:
        return None
    return float(v)


def _opt_int(v):
    if v is None or v == "" or v is False:
        return None
    return int(v)


def _parse_sessions(lst) -> list[tuple[int, int]]:
    out = []
    for s in lst or ["00:00-24:00"]:
        m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$", str(s))
        if not m:
            raise ValueError(f"Khung giờ không hợp lệ: {s!r} (dạng 08:00-11:30)")
        a = int(m.group(1)) * 60 + int(m.group(2))
        b = int(m.group(3)) * 60 + int(m.group(4))
        if not (0 <= a <= 1440 and 0 <= b <= 1440):
            raise ValueError(f"Khung giờ không hợp lệ: {s!r}")
        out.append((a, b))
    return out


def _get(d: dict, path: str, default=None):
    cur = d
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return default if cur is None and default is not None else cur


def build_config(raw: dict, exchange_defaults_fees: tuple[float, float] | None = None) -> Config:
    tz = raw.get("timezone", "Asia/Ho_Chi_Minh")
    g = lambda p, d=None: _get(raw, p, d)  # noqa: E731

    sigs = []
    for i, s in enumerate(g("signals", []) or []):
        r = SignalRule(
            timeframe=str(s.get("timeframe", "1m")),
            change_pct=float(s.get("change_pct", 5)),
            max_change_pct=_opt_num(s.get("max_change_pct")),
            direction=str(s.get("direction", "up")).lower(),
            trigger=str(s.get("trigger", "touch")).lower(),
            side=str(s.get("side", "reverse")).lower(),
            pullback_pct=_opt_num(s.get("pullback_pct")),
            pullback_window_ms=parse_duration(s.get("pullback_window")),
        )
        if r.pullback_pct is not None:
            if r.pullback_pct <= 0:
                raise ValueError(f"signals[{i}].pullback_pct phải > 0")
            r.pullback_window_ms = r.pullback_window_ms or 10 * MIN_MS
        if r.timeframe not in TIMEFRAMES:
            raise ValueError(f"signals[{i}].timeframe phải là một trong {list(TIMEFRAMES)}")
        r.tf_minutes = TIMEFRAMES[r.timeframe]
        if r.direction not in ("up", "down", "both"):
            raise ValueError(f"signals[{i}].direction: up | down | both")
        if r.trigger not in ("touch", "close"):
            raise ValueError(f"signals[{i}].trigger: touch | close")
        if r.side not in ("long", "short", "follow", "reverse"):
            raise ValueError(f"signals[{i}].side: long | short | follow | reverse")
        if r.change_pct <= 0:
            raise ValueError(f"signals[{i}].change_pct phải > 0")
        sigs.append(r)
    if not sigs:
        raise ValueError("Cần ít nhất 1 tín hiệu trong 'signals'")

    fees_default = exchange_defaults_fees or (0.02, 0.05)
    wd = g("schedule.weekdays") or WEEKDAYS
    weekdays = {WEEKDAYS.index(str(x).lower()[:3]) for x in wd}

    cfg = Config(
        name=str(raw.get("name", "strategy")),
        exchange=str(raw.get("exchange", "binance")).lower(),
        start_ms=parse_time(raw["start"], tz),
        end_ms=parse_time(raw["end"], tz),
        timezone=tz,
        initial_balance=float(raw.get("initial_balance", 1000)),
        resolution=str(g("data.resolution", "auto")).lower(),
        intrabar=str(g("data.intrabar_fallback", "conservative")).lower(),
        cache_dir=str(g("data.cache_dir", "cache")),
        workers=int(g("data.workers", 8)),
        symbols=[str(x) for x in (g("universe.symbols") or [])],
        exclude=[str(x) for x in (g("universe.exclude") or [])],
        min_volume_24h=_opt_num(g("universe.min_volume_24h")),
        max_volume_24h=_opt_num(g("universe.max_volume_24h")),
        min_max_leverage=_opt_num(g("universe.min_max_leverage")),
        min_market_cap=_opt_num(g("universe.min_market_cap")),
        max_market_cap=_opt_num(g("universe.max_market_cap")),
        market_cap_source=str(g("universe.market_cap_source", "supply")).lower(),
        unknown_market_cap=str(g("universe.unknown_market_cap", "exclude")).lower(),
        coingecko_api_key=str(g("universe.coingecko_api_key", "") or ""),
        coingecko_pro=bool(g("universe.coingecko_pro", False)),
        coingecko_ids=dict(g("universe.coingecko_ids") or {}),
        signals=sigs,
        latency_ms=int(g("entry.latency_ms", 0) or 0),
        entry_slippage_pct=float(g("entry.slippage_pct", 0) or 0),
        margin=float(g("position.margin", 10)),
        margin_mode=str(g("position.margin_mode", "fixed")).lower(),
        leverage=float(g("position.leverage", 10)),
        take_profit=_opt_num(g("exit.take_profit")),
        stop_loss=_opt_num(g("exit.stop_loss")),
        tp_sl_unit=str(g("exit.unit", "price")).lower(),
        tp_order=str(g("exit.tp_order", "limit")).lower(),
        tp_fill=str(g("exit.tp_fill", "touch")).lower(),
        sl_slippage_pct=float(g("exit.sl_slippage_pct", 0) or 0),
        exit_slippage_pct=float(g("exit.exit_slippage_pct", 0) or 0),
        max_hold_ms=parse_duration(g("exit.max_hold")),
        liquidation=bool(g("exit.liquidation", True)),
        maintenance_margin_pct=float(g("exit.maintenance_margin_pct", 0.5)),
        maker_pct=float(g("fees.maker_pct", fees_default[0])),
        taker_pct=float(g("fees.taker_pct", fees_default[1])),
        funding=bool(g("fees.funding", True)),
        max_open_positions=_opt_int(g("limits.max_open_positions")),
        max_open_per_coin=int(g("limits.max_open_per_coin", 1) or 1),
        max_trades_per_day=_opt_int(g("limits.max_trades_per_day")),
        max_tp_per_day=_opt_int(g("limits.max_tp_per_day")),
        max_sl_per_day=_opt_int(g("limits.max_sl_per_day")),
        max_tp_per_coin_per_day=_opt_int(g("limits.max_tp_per_coin_per_day")),
        max_sl_per_coin_per_day=_opt_int(g("limits.max_sl_per_coin_per_day")),
        cooldown_after_tp_coin_ms=parse_duration(g("limits.cooldown_after_tp_coin")),
        cooldown_after_sl_coin_ms=parse_duration(g("limits.cooldown_after_sl_coin")),
        cooldown_after_trade_coin_ms=parse_duration(g("limits.cooldown_after_trade_coin")),
        cooldown_after_sl_global_ms=parse_duration(g("limits.cooldown_after_sl_global")),
        max_consecutive_sl=_opt_int(g("limits.max_consecutive_sl")),
        pause_after_consecutive_sl_ms=parse_duration(g("limits.pause_after_consecutive_sl")),
        daily_loss_limit=_opt_num(g("limits.daily_loss_limit")),
        max_runtime_ms=parse_duration(g("limits.max_runtime")),
        day_reset_hour=int(g("limits.day_reset_hour", 0) or 0),
        timeout_counts_as=str(g("limits.timeout_counts_as", "pnl")).lower(),
        sessions=_parse_sessions(g("schedule.sessions")),
        weekdays=weekdays,
        sweep=dict(raw.get("sweep") or {}),
        raw=raw,
    )
    _validate(cfg)
    return cfg


def _validate(c: Config):
    if c.end_ms <= c.start_ms:
        raise ValueError("end phải sau start")
    if c.resolution not in ("auto", "tick", "1m"):
        raise ValueError("data.resolution: auto | tick | 1m")
    if c.intrabar not in ("conservative", "ohlc", "optimistic"):
        raise ValueError("data.intrabar_fallback: conservative | ohlc | optimistic")
    if c.margin_mode not in ("fixed", "percent"):
        raise ValueError("position.margin_mode: fixed | percent")
    if c.tp_sl_unit not in ("price", "roe"):
        raise ValueError("exit.unit: price | roe")
    if c.tp_order not in ("limit", "market"):
        raise ValueError("exit.tp_order: limit | market")
    if c.tp_fill not in ("touch", "through"):
        raise ValueError("exit.tp_fill: touch | through")
    if c.market_cap_source not in ("supply", "coingecko", "current", "none"):
        raise ValueError("universe.market_cap_source: supply | coingecko | current | none")
    if c.unknown_market_cap not in ("exclude", "include"):
        raise ValueError("universe.unknown_market_cap: exclude | include")
    if c.timeout_counts_as not in ("pnl", "none"):
        raise ValueError("limits.timeout_counts_as: pnl | none")
    if c.leverage <= 0 or c.margin <= 0:
        raise ValueError("leverage và margin phải > 0")
    if c.take_profit is None and c.stop_loss is None and c.max_hold_ms is None:
        raise ValueError("Cần ít nhất một trong exit.take_profit / exit.stop_loss / exit.max_hold")


def load_raw(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def set_path(raw: dict, path: str, value) -> dict:
    """Gán giá trị theo đường dẫn 'a.b.0.c' (dùng cho sweep)."""
    out = copy.deepcopy(raw)
    cur = out
    keys = path.split(".")
    for k in keys[:-1]:
        cur = cur[int(k)] if isinstance(cur, list) else cur.setdefault(k, {})
    last = keys[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value
    return out


def tf_ms(rule: SignalRule) -> int:
    return rule.tf_minutes * MIN_MS
