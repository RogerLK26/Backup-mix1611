"""Điều phối 1 lượt chạy: đọc file lệnh thật -> trích đường giá -> kiểm định -> chạy lưới cấu hình."""
from __future__ import annotations

import dataclasses
import json
import math
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .actual import detect_exchange, guess_config, load_actual, summarize_actual
from .bt import MIN_MS, RESULTS_DIR, log
from .engine import Replay, Settings, actual_stats, calibrate, validate
from .paths import PathBuilder

DEFAULT_WINDOW_MIN = 240


def parse_list(v, cast=float) -> list:
    """'0.5, 1, 1.5' | '0.5-3:0.5' (từ-đến:bước) | [..] -> list. Thời gian: '30m', '2h', '90' (phút)."""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        items = []
        for x in v:
            items += parse_list(x, cast) if isinstance(x, str) else [cast(x)]
        return items
    out = []
    for part in str(v).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^([\d.]+[mh]?)\s*-\s*([\d.]+[mh]?)\s*:\s*([\d.]+[mh]?)$", part)
        if m:
            a, b, s = (_num(x, cast) for x in m.groups())
            if s <= 0:
                raise ValueError(f"Bước phải > 0: {part}")
            n = int(math.floor((b - a) / s + 1e-9)) + 1
            if n > 500:
                raise ValueError(f"Khoảng quá nhiều giá trị ({n}): {part}")
            out += [cast(round(a + i * s, 6)) for i in range(n)]
        else:
            out.append(_num(part, cast))
    return out


def _num(x: str, cast):
    x = x.strip().lower()
    if x.endswith("h"):
        return cast(float(x[:-1]) * 60)
    if x.endswith("m"):
        return cast(float(x[:-1]))
    return cast(float(x))


def guess_actual(df: pd.DataFrame) -> dict:
    """Cấu hình thật đoán từ file (chỉ dùng được khi cột Entry là giá khớp thật). Không chắc thì mặc định 1% / 10% / 120m."""
    g = guess_config(df)
    mv = ((df.exit_csv / df.entry_csv - 1) * 100 * df.side)[df.reason == "TP"]
    reliable = len(mv) >= 5 and 0 < g.get("tp", 0) and float(mv.quantile(0.75) - mv.quantile(0.25)) < 0.1
    tp = g["tp"] if reliable else 1.0
    sl = g.get("sl_from_name") if reliable and g.get("sl_from_name") else (g.get("sl") if reliable else 10.0)
    if not reliable:
        m = re.search(r"1\s*:\s*(\d+(?:\.\d+)?)", str(df.strategy.iloc[0])) if len(df) else None
        sl = float(m.group(1)) * tp if m else 10.0
    out = {"tp": float(tp), "sl": float(round(sl, 2)), "hold": int(g.get("hold_min") or 120), "reliable": bool(reliable)}
    # khối lượng & loại phí TP thật: từ các lệnh TP (lãi gộp = khối lượng × TP%)
    t = df[(df.reason == "TP") & df.pnl.notna() & df.fee.notna()]
    if len(t) >= 5:
        gross = t.pnl - t.fee - t.funding.fillna(0)
        notional = float((gross / (tp / 100)).median())
        if 1 < notional < 1e7:
            out["notional"] = round(notional, 2)
            fee_pct = float((-t.fee / notional * 100).median())
            out["fee_pct"] = round(fee_pct, 4)
            # Bybit: taker 0.055 + maker 0.02 ≈ 0.075% ; OKX: taker 0.05 × 2 ≈ 0.1%
            out["tp_order"] = "trigger" if fee_pct > 0.088 else "limit"
        # cột ký quỹ × đòn bẩy của file có đúng khối lượng thật không? (Bybit: đúng; OKX: không)
        nt = t.notional
        ok = nt.notna() & (nt > 0)
        if ok.sum() >= 5:
            ratio = (gross[ok] / (tp / 100)) / nt[ok]
            out["sizing"] = "actual" if float((ratio - 1).abs().median()) < 0.03 else "fixed"
    return out


class Run:
    """1 lượt chạy (giữ kết quả trong RAM để xem chi tiết từng cấu hình)."""

    def __init__(self, file: str | Path, exchange: str | None, settings: dict, actual_cfg: dict | None = None,
                 tz: str = "Asia/Ho_Chi_Minh", date_from: str | None = None, date_to: str | None = None,
                 coins: list[str] | None = None, entry_mode: str = "auto", progress=None):
        self.file = Path(file)
        self.exchange = (exchange or detect_exchange(self.file.name) or "").lower()
        if not self.exchange:
            raise ValueError("Không nhận ra sàn từ tên file – hãy chọn sàn")
        self.tz = tz
        self.date_from, self.date_to = date_from, date_to
        self.coins = [c.strip().upper() for c in (coins or []) if c.strip()]
        self.progress = progress or (lambda *a: None)
        df = load_actual(self.file, tz)
        if date_from:
            df = df[df.start_ms >= pd.Timestamp(date_from, tz=tz).value // 10**6]
        if date_to:
            df = df[df.start_ms < (pd.Timestamp(date_to, tz=tz) + pd.Timedelta(days=1)).value // 10**6]
        if coins:
            cs = {c.strip().upper() for c in coins if c.strip()}
            df = df[df.coin.isin(cs)]
        self.trades = df.reset_index(drop=True)
        if not len(self.trades):
            raise ValueError("Không có lệnh nào trong khoảng đã chọn")
        self.actual_cfg = {**guess_actual(self.trades), **(actual_cfg or {})}
        self.st = Settings.from_dict(settings)
        self.entry_mode = entry_mode

    def execute(self) -> dict:
        t0 = time.time()
        st = self.st
        window_min = max(DEFAULT_WINDOW_MIN, st.window_min(DEFAULT_WINDOW_MIN), int(self.actual_cfg["hold"]))
        pb = PathBuilder(self.exchange, self.entry_mode, actual_tp=float(self.actual_cfg["tp"]),
                         actual_sl=float(self.actual_cfg["sl"]) or None)
        self.paths = pb.build(self.trades, window_min * MIN_MS, funding=True, progress=self.progress)
        self.progress("Kiểm định với kết quả thật", 0, 0)
        a_tp, a_sl, a_hold = float(self.actual_cfg["tp"]), float(self.actual_cfg["sl"]), int(self.actual_cfg["hold"])
        self.calibration = None
        raw_pnl = None
        if st.auto_slip:
            self.calibration = calibrate(self.trades, self.paths, st, a_tp, a_sl, a_hold, window_min)
            st.sl_slip_pct = self.calibration["sl_slip"]
            st.exit_slip_pct = self.calibration["exit_slip"]
            raw = Settings(**{**st.__dict__, "sl_slip_pct": 0.0, "exit_slip_pct": 0.0})
            raw_pnl = validate(self.trades, self.paths, raw, a_tp, a_sl, a_hold, window_min, self.tz).get("pnl_sim")
        self.validation = validate(self.trades, self.paths, st, float(self.actual_cfg["tp"]),
                                   float(self.actual_cfg["sl"]), int(self.actual_cfg["hold"]), window_min, self.tz)
        self.validation["pnl_sim_raw"] = raw_pnl
        self.validation["calibration"] = self.calibration
        self.validation["slip"] = {"sl": st.sl_slip_pct, "exit": st.exit_slip_pct}
        self.progress("Chạy lưới cấu hình", 0, 0)
        # Ngược hướng: cùng thời điểm & giá vào, đảo Long <-> Short. Kiểm định / hiệu chỉnh trượt giá ở trên vẫn dùng
        # hướng thật (để so với lệnh thật); lưới cấu hình dùng hướng đã đảo.
        if st.direction == "reverse":
            r_trades = self.trades.assign(side=-self.trades.side)
            r_paths = [dataclasses.replace(p, side=-p.side) for p in self.paths]
            self.replay = Replay(r_trades, r_paths, st)
        else:
            self.replay = Replay(self.trades, self.paths, st)
        self.rows = self.replay.table(self.tz)
        res = pd.Series([p.res for p in self.paths]).value_counts().to_dict()
        src = pd.Series([p.entry_src for p in self.paths if p.ok]).value_counts().to_dict()
        self.summary = {
            "exchange": self.exchange,
            "file": self.file.name,
            "actual": summarize_actual(self.trades),
            "actual_stats": actual_stats(self.trades),
            "actual_cfg": self.actual_cfg,
            "settings": st.__dict__,
            "n_combos": len(self.replay.combos),
            "resolution": {k: int(v) for k, v in res.items()},
            "entry_src": {k: int(v) for k, v in src.items()},
            "csv_trusted": pb.trust_csv,
            "missing": [{"coin": p.symbol, "t": int(p.entry_t), "note": p.note} for p in self.paths if not p.ok][:50],
            "elapsed_s": round(time.time() - t0, 1),
            "window_min": window_min,
            "direction": st.direction,
            # lệnh mô phỏng (một phần) bằng nến 1m: thứ tự trong nến giả định theo hướng bot thật -> với hướng ngược
            # là thứ tự THUẬN lợi (kết quả hơi lạc quan). Bật "Bỏ lệnh không có tick" để loại hẳn.
            "n_bar_paths": int(sum(1 for p in self.paths if p.ok and p.res != "tick")),
        }
        return self.summary

    def save_csv(self, out_dir: Path | None = None) -> Path:
        out_dir = out_dir or RESULTS_DIR / f"{self.exchange}_{time.strftime('%Y%m%d_%H%M%S')}"
        out_dir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(self.rows).to_csv(out_dir / "grid.csv", index=False, encoding="utf-8-sig")
        (out_dir / "summary.json").write_text(json.dumps({**self.summary, "validation": self.validation},
                                                         ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        return out_dir


def best_rows(rows: list[dict], key: str = "net", n: int = 20, min_trades: int = 0) -> list[dict]:
    r = [x for x in rows if x["n"] >= min_trades]
    return sorted(r, key=lambda x: (-(x[key] if np.isfinite(x[key]) else 1e18)))[:n]
