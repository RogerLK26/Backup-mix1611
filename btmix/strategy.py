"""Backtest chiến lược thật (engine của `backtester`) nhưng trả kết quả theo đúng khuôn của chế độ replay:
bảng xếp hạng toàn bộ cấu hình, heatmap TP × SL, chi tiết từng cấu hình tính ngay khi bấm.

Cách làm: dùng lại nguyên engine nhanh `backtester.engine.fast` – với mỗi tín hiệu, thời điểm & giá chạm từng mức
TP/SL và giá đóng tại từng mốc hết giờ chỉ tính MỘT lần; mỗi cấu hình chỉ chạy lại sổ lệnh (giới hạn rủi ro, số dư,
thời gian nghỉ). Nhờ vậy:

  * chạy lưới hàng nghìn cấu hình như replay, nhưng điểm vào lệnh đến từ tín hiệu thật trên dữ liệu sàn;
  * giữ `FastEvaluator` trong RAM sau khi chạy xong -> bấm vào một dòng bất kỳ của bảng xếp hạng là có ngay chi tiết
    từng lệnh (không cần chạy lại cả lượt như bản cũ chỉ có top N);
  * lọc theo giờ vào lệnh trong trang chi tiết = chạy lại sổ lệnh với khung giờ đó (đúng như bot chỉ chạy giờ đó),
    không phải lọc bớt dòng.
"""
from __future__ import annotations

import dataclasses
import itertools
import re
import math
import random
import time
from collections import Counter

import numpy as np
import pandas as pd

# _Ctx / _get là phần dùng chung với `backtester.optimize` (cùng cách gom nhóm tham số & bộ nhớ đệm tín hiệu).
from backtester.optimize import (DURATION_KEYS, FAST_KEYS, LABELS, OBJECTIVES, Grid, _Ctx, _get, _num,
                                 fmt_value, parse_values, score)
from backtester.engine.fast import quick_stats
from backtester.progress import PROGRESS
from backtester.runner import prepare
from backtester.utils import MIN_MS, log

from replay.engine import stability_block

# lý do bỏ tín hiệu không phụ thuộc cấu hình (lọc coin / thiếu dữ liệu) – không tính vào cột "Bỏ" của bảng xếp hạng
STATIC_REJECTS = ("market_cap", "max_leverage", "no_data", "no_pullback", "outside_period")
REJECT_LABELS = {
    # lọc trước khi vào sổ lệnh (không phụ thuộc cấu hình trong lưới)
    "market_cap": "Vốn hoá ngoài khoảng cho phép",
    "max_leverage": "Đòn bẩy tối đa của sàn thấp hơn mức yêu cầu",
    "no_data": "Không có dữ liệu giá tại thời điểm vào lệnh",
    "no_pullback": "Giá không hồi đủ trong thời gian theo dõi",
    "outside_period": "Ngoài giai đoạn backtest",
    # giới hạn rủi ro / lịch chạy – phụ thuộc cấu hình, đếm trên mọi cấu hình trong lưới
    "session": "Ngoài khung giờ đã đặt",
    "weekday": "Ngoài ngày trong tuần đã chọn",
    "max_runtime": "Quá thời gian chạy tối đa mỗi ngày",
    "daily_max_trades": "Đã đủ số lệnh trong ngày",
    "daily_max_tp": "Đã đủ số TP trong ngày",
    "daily_max_sl": "Đã đủ số SL trong ngày",
    "daily_loss_limit": "Chạm giới hạn lỗ trong ngày",
    "coin_cooldown": "Coin đang nghỉ sau lệnh trước",
    "coin_max_tp": "Coin đã đủ TP trong ngày",
    "coin_max_sl": "Coin đã đủ SL trong ngày",
    "coin_position_open": "Coin đang có lệnh mở (đủ số lệnh/coin)",
    "max_open_positions": "Đã đủ số lệnh mở đồng thời",
    "insufficient_balance": "Không đủ số dư để vào lệnh",
    "global_sl_cooldown": "Cả chiến lược đang nghỉ sau SL",
    "consecutive_sl_pause": "Đang nghỉ sau nhiều SL liên tiếp",
}
TOP_HOURS_K = 8

# nhãn tiếng Việt cho các tham số quét thêm (bản gốc chỉ đặt tên sẵn cho một số khoá)
SIG_LABELS = {"timeframe": "Khung nến", "change_pct": "Biến động", "max_change_pct": "Biến động tối đa",
              "direction": "Chiều biến động", "side": "Vào lệnh", "trigger": "Kích hoạt",
              "pullback_pct": "Hồi từ đỉnh", "pullback_window": "Theo dõi hồi"}
OTHER_LABELS = {"position.leverage": "Đòn bẩy", "position.margin": "Ký quỹ", "entry.latency_ms": "Độ trễ (ms)",
                "entry.slippage_pct": "Trượt giá vào (%)", "exit.tp_order": "Lệnh chốt lời",
                "exit.sl_slippage_pct": "Trượt giá SL (%)", "limits.max_open_positions": "Lệnh mở đồng thời",
                "limits.max_open_per_coin": "Lệnh mở / coin", "limits.max_trades_per_day": "Lệnh / ngày",
                "limits.max_sl_per_day": "Max SL/ngày", "limits.max_tp_per_day": "Max TP/ngày",
                "limits.cooldown_after_sl_coin": "Nghỉ coin sau SL", "limits.cooldown_after_tp_coin": "Nghỉ coin sau TP",
                "limits.cooldown_after_trade_coin": "Nghỉ coin sau lệnh", "limits.cooldown_after_sl_global": "Nghỉ sau SL",
                "limits.daily_loss_limit": "Giới hạn lỗ/ngày", "limits.day_reset_hour": "Giờ bắt đầu ngày",
                "universe.min_volume_24h": "Volume 24h ≥", "universe.min_max_leverage": "Đòn bẩy sàn ≥"}


def param_label(key: str) -> str:
    m = re.match(r"^signals\.(\d+)\.(\w+)$", key)
    if m:
        name = SIG_LABELS.get(m.group(2), m.group(2))
        return f"{name} (tín hiệu {int(m.group(1)) + 1})"
    return LABELS.get(key) or OTHER_LABELS.get(key) or key


def combo_label(keys, combo, unit: str) -> str:
    return " · ".join(f"{param_label(k)} {fmt_value(k, v, unit)}" for k, v in zip(keys, combo))


# "từ-đến:bước" viết thẳng trong ô nhập (cú pháp của chế độ replay) – engine gốc chỉ hiểu danh sách hoặc {from,to,step}
_RANGE = re.compile(r"^([\d.]+\s*[a-zA-Z]*)\s*-\s*([\d.]+\s*[a-zA-Z]*)\s*:\s*([\d.]+\s*[a-zA-Z]*)$")
_ZERO_IS_NONE = ("exit.take_profit", "exit.stop_loss")


def normalize_sweep(sweep: dict | None) -> dict:
    """Mở rộng lưới về danh sách giá trị, chấp nhận mọi cách viết:

        "1, 1.5, 2"            danh sách
        "0.5-3:0.25"           khoảng từ-đến:bước (gõ thẳng, như chế độ replay)
        "30m, 1h-4h:1h"        trộn cả hai, có cả thời gian
        {from: 0.5, to: 3, step: 0.25}   dạng YAML của bản gốc
        0 ở TP/SL/giữ lệnh     = không đặt (none)
    """
    out = {}
    for k, spec in (sweep or {}).items():
        if isinstance(spec, dict) or spec is None:
            out[k] = spec
            continue
        parts = re.split(r"[,;]", spec) if isinstance(spec, str) else list(spec)
        vals = []
        for p in parts:
            p = str(p).strip() if not isinstance(p, (int, float)) else p
            if p == "" or p is None:
                continue
            m = _RANGE.match(p) if isinstance(p, str) else None
            if m:
                vals += parse_values(k, {"from": m.group(1).strip(), "to": m.group(2).strip(),
                                         "step": m.group(3).strip()})
            else:
                vals.append(_num(p))          # "3" -> 3 để không trùng với 3.0 của khoảng
        # 0 = "không đặt": TP/SL bỏ hẳn mức đó, thời gian giữ lệnh = không giới hạn
        if k in _ZERO_IS_NONE or k in DURATION_KEYS:
            vals = [None if (str(v).strip() in ("0", "0.0") or v == 0) else v for v in vals]
        seen = []
        for v in vals:
            if v not in seen:
                seen.append(v)
        out[k] = seen or spec
    return out


def prepare_raw(raw: dict) -> dict:
    """Bản cấu hình đã chuẩn hoá lưới – dùng cho cả lúc kiểm tra cấu hình lẫn lúc chạy."""
    if not raw.get("sweep"):
        return raw
    return dict(raw, sweep=normalize_sweep(raw["sweep"]))



def _f(v):
    """Số hữu hạn cho JSON (inf -> None)."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        return v
    return v


def _sessions_for(hours: list[int], current: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Khung giờ (phút trong ngày) chỉ gồm các giờ đã chọn, giao với khung giờ đang đặt trong cấu hình."""
    want = []
    for h in sorted(set(hours)):
        if want and want[-1][1] == h * 60:
            want[-1] = (want[-1][0], (h + 1) * 60)
        else:
            want.append((h * 60, (h + 1) * 60))
    if not current:
        return [tuple(x) for x in want]
    out = []
    for a, b in current:
        rng = [(a, b)] if a < b else [(a, 1440), (0, b)]     # khung qua đêm
        for x, y in rng:
            for p, q in want:
                lo, hi = max(x, p), min(y, q)
                if lo < hi:
                    out.append((lo, hi))
    return out or [(0, 0)]


def _arrays(closed: list[dict]) -> dict:
    n = len(closed)
    return {
        "pnl": np.fromiter((r["net_pnl"] for r in closed), float, n),
        "exit_t": np.fromiter((r["exit_t"] for r in closed), np.int64, n),
        "entry_t": np.fromiter((r["entry_t"] for r in closed), np.int64, n),
        "margin": np.fromiter((r["margin"] for r in closed), float, n),
        "hour": np.fromiter((r["hour"] for r in closed), np.int64, n),
        "symbol": np.array([r["symbol"] for r in closed], object),
    }


def _stats(closed: list[dict], initial: float, subset: bool = False) -> dict:
    """Chỉ số của 1 cấu hình, đặt tên như bảng xếp hạng của replay."""
    st = quick_stats(closed, initial, 0 if subset else None, None)
    n = st["trades"]
    out = {
        "n": n, "n_tp": st["tp"], "n_sl": st["sl"], "n_liq": st["liquidations"], "n_timeout": st["timeouts"],
        "win_rate": st["win_rate_pct"], "net": st["net_pnl"], "avg": st["expectancy_per_trade"],
        "pf": _f(st["profit_factor"]), "max_dd": st["max_drawdown"], "max_dd_pct": st["max_drawdown_pct"],
        "ret_pct": st["return_pct"], "fees": st["fees"], "avg_hold": st["avg_hold_s"] / 60.0,
        "hours_pos": st["hours_pos"], "top5h": st["top5h_pnl"],
        # lãi / sụt vốn tối đa; không có sụt vốn -> không chia được (để trống)
        "ret_dd": (st["net_pnl"] / st["max_drawdown"]) if st["max_drawdown"] > 0 else None,
    }
    if n:
        a = _arrays(closed)
        out["avg_roe"] = float(np.mean(a["pnl"] / a["margin"] * 100.0))
        out["worst"] = float(a["pnl"].min())
    else:
        out.update({"avg_roe": 0.0, "worst": 0.0})
    return out


def _stability(closed: list[dict], tz: str) -> dict:
    if not closed:
        return {"r2": None, "day_pos": None, "n_days": 0, "top3_day": None, "net_xcoin": None, "best_coin": None,
                "best_coin_pnl": None}
    a = _arrays(closed)
    day = pd.to_datetime(a["exit_t"], unit="ms", utc=True).tz_convert(tz).strftime("%Y-%m-%d").to_numpy()
    codes, uniq = pd.factorize(a["symbol"])
    return stability_block(a["pnl"], a["exit_t"], day, codes.astype(np.int64), [str(c) for c in uniq])


def _top_hours(closed: list[dict], k: int = TOP_HOURS_K) -> list[int]:
    """k giờ vào lệnh có tổng lãi cao nhất (giờ theo múi giờ của cấu hình)."""
    if not closed:
        return []
    a = _arrays(closed)
    by = np.bincount(a["hour"], weights=a["pnl"], minlength=24)
    return sorted(int(h) for h in np.argsort(-by, kind="stable")[:k])


class BtRun:
    """1 lượt backtest chiến lược trên 1 sàn (lưới cấu hình + chi tiết theo yêu cầu)."""

    def __init__(self, raw: dict, exchange: str, progress=None, h8: bool = True, prepared=None):
        self.raw = prepare_raw(raw)
        self._prepared = prepared          # (cfg, store, mcap, universe) có sẵn – dùng lại dữ liệu đã tải / test
        self.exchange = exchange.lower()
        self.tz = raw.get("timezone") or "Asia/Ho_Chi_Minh"
        self.grid = Grid(self.raw)
        if not self.grid.keys:
            raise ValueError("Cần ít nhất một tham số trong 'Nhiều chiến lược' (TP / SL / thời gian giữ lệnh)")
        if not any(k in FAST_KEYS for k in self.grid.keys):
            raise ValueError("Lưới phải có ít nhất một trong: chốt lời, cắt lỗ, thời gian giữ lệnh")
        self.progress = progress or (lambda *a: None)
        self.h8 = h8
        self.rows: list[dict] = []
        self.combos: list[tuple] = []
        self.ctx: _Ctx | None = None
        self.split_t: int | None = None
        self.summary: dict = {}
        self.rejected: Counter = Counter()

    # ------------------------------------------------------------------ chạy lưới
    def execute(self) -> dict:
        t0 = time.time()
        grid = self.grid
        base = _base_with_axis({k: v for k, v in self.raw.items() if k not in ("sweep", "optimize")}, grid)
        prepared = self._prepared or prepare(base, self.exchange)
        self.ctx = ctx = _Ctx(base, prepared, grid)
        cfg0 = ctx.cfg0
        self.unit = cfg0.tp_sl_unit
        # Kiểm tra ngoài mẫu: xếp hạng trên phần đầu giai đoạn, xem kết quả trên x% cuối (chống quá khớp)
        self.split_t = int(cfg0.end_ms - (cfg0.end_ms - cfg0.start_ms) * grid.oos_pct / 100.0) \
            if grid.oos_pct > 0 else None
        t_eval = time.time()
        log.info("Lưới %d tham số, %s tổ hợp – %s", len(grid.keys), f"{grid.total:,}",
                 "quét toàn bộ" if grid.mode == "grid" else f"tìm kiếm thông minh ({grid.max_combos:,} tổ hợp)")

        res: dict[tuple, dict] = {}

        def evaluate(combo: tuple):
            if combo in res or not self._valid(combo):
                return
            outer = ctx.outer(combo)
            ev = ctx.evaluator(outer)
            _, cfg = ctx.cfg_for_outer(outer)
            i, j, k = ctx.inner(combo)
            book, rejected = ev.run(cfg, i, j, k)
            res[combo] = self._row(combo, book.closed, rejected, cfg)

        def loop(combos, total_hint):
            PROGRESS.set_stage("Đánh giá chiến lược", total_hint)
            combos = sorted(combos, key=lambda c: str(ctx.outer(c)))
            for n, combo in enumerate(combos, 1):
                if n == 1 or n % 25 == 0:
                    PROGRESS.set_stage("Đánh giá chiến lược", total_hint)
                    PROGRESS.step(len(res), total_hint)
                    self.progress("Đánh giá chiến lược", len(res), total_hint)
                evaluate(combo)
            PROGRESS.step(len(res), total_hint)

        if grid.mode == "grid":
            loop(list(itertools.product(*grid.values)), grid.total)
        else:
            self._smart(loop, res)

        combos = sorted(res.keys(), key=lambda c: tuple(str(x) for x in c))
        self.combos = combos
        rows = []
        for ci, c in enumerate(combos):
            r = res[c]
            r["ci"] = ci
            r["mode"] = self.exchange
            rows.append(r)
        self._neighbors(rows)
        self.rows = rows
        self.rejected = Counter()
        for r in rows:
            self.rejected.update(r.pop("_rejects", {}))
        n_sig = len(ctx.evals[next(iter(ctx.evals))].items) if ctx.evals else 0
        self.summary = {
            "kind": "bt",
            "exchange": self.exchange,
            "name": self.raw.get("name") or "strategy",
            "tz": self.tz,
            "start": self.raw.get("start"), "end": self.raw.get("end"),
            "unit": self.unit,
            "keys": grid.keys,
            "labels": [param_label(k) for k in grid.keys],
            "objective": grid.objective, "objective_label": OBJECTIVES[grid.objective],
            "n_combos": len(rows), "grid_total": grid.total, "grid_mode": grid.mode,
            "n_signals": n_sig,
            "oos_pct": grid.oos_pct or 0,
            "n_symbols": len(getattr(ctx.uni, "symbols", []) or []),
            "initial_balance": cfg0.initial_balance,
            "resolution": {"tick": 0},
            "elapsed_s": round(time.time() - t0, 1),
            "eval_s": round(time.time() - t_eval, 1),
            "rejects": [{"reason": k, "label": REJECT_LABELS.get(k, k), "n": int(v)}
                        for k, v in self.rejected.most_common()],
        }
        log.info("Đã đánh giá %d chiến lược trong %.1fs (chuẩn bị dữ liệu %.1fs)",
                 len(rows), time.time() - t_eval, t_eval - t0)
        return self.summary

    def _valid(self, combo: tuple) -> bool:
        vals = {k: combo[i] for i, k in enumerate(self.grid.keys)}
        get = lambda k: vals[k] if k in vals else _get(self.ctx.base, k)  # noqa: E731
        return any(get(k) not in (None, "") for k in FAST_KEYS)

    def _smart(self, loop, res: dict):
        """Lưới lớn hơn giới hạn: lấy mẫu ngẫu nhiên rồi tinh chỉnh quanh các chiến lược tốt nhất (như repo gốc)."""
        grid = self.grid
        rng = random.Random(grid.seed)
        budget = grid.max_combos
        dims = [len(v) for v in grid.values]
        first = set()
        while len(first) < int(budget * 0.6):
            first.add(tuple(rng.randrange(d) for d in dims))
        loop([tuple(grid.values[d][i] for d, i in enumerate(ix)) for ix in first], budget)
        for _round in range(50):
            if len(res) >= budget:
                break
            top = sorted((r for r in res.values() if r["ok"]), key=lambda r: -r["score"])[:20]
            nxt = []
            for r in top:
                ix = [grid.values[d].index(v) for d, v in enumerate(r["combo"])]
                for d in range(len(dims)):
                    for delta in (-2, -1, 1, 2):
                        q = list(ix)
                        q[d] += delta
                        if 0 <= q[d] < dims[d]:
                            c = tuple(grid.values[e][i] for e, i in enumerate(q))
                            if c not in res:
                                nxt.append(c)
            nxt = list(dict.fromkeys(nxt))[: budget - len(res)]
            if not nxt:
                break
            loop(nxt, budget)
        log.info("Tìm kiếm thông minh: đã thử %d / %d tổ hợp", len(res), grid.total)

    # ------------------------------------------------------------------ 1 dòng bảng xếp hạng
    def _row(self, combo: tuple, closed: list[dict], rejected: Counter, cfg) -> dict:
        grid = self.grid
        vals = dict(zip(grid.keys, combo))
        st = _stats(closed, cfg.initial_balance)
        row = {"combo": combo, **st}
        row["tp"] = _axis_value(self, vals, "exit.take_profit", cfg.take_profit)
        row["sl"] = _axis_value(self, vals, "exit.stop_loss", cfg.stop_loss)
        hold = vals.get("exit.max_hold", cfg.max_hold_ms)
        row["hold"] = _hold_min(hold)
        row["params"] = combo_label(grid.keys, combo, self.unit)
        row["values"] = {k: (None if v is None else (v if isinstance(v, (int, float)) else str(v)))
                         for k, v in vals.items()}
        row["labels"] = {k: fmt_value(k, v, self.unit) for k, v in vals.items()}
        row["skipped"] = int(sum(v for k, v in rejected.items() if k not in STATIC_REJECTS))
        row["open"] = 0
        row["_rejects"] = dict(rejected)
        row.update(_stability(closed, self.tz))
        if self.h8 and closed:
            hrs = _top_hours(closed)
            sub = [r for r in closed if r["hour"] in set(hrs)]
            s8 = _stats(sub, cfg.initial_balance, subset=True)
            row["h8_hours"] = hrs
            row.update({"h8_n": s8["n"], "h8_net": s8["net"], "h8_dd": s8["max_dd"], "h8_ret": s8["ret_dd"],
                        "h8_pf": s8["pf"]})
            row.update({"h8_" + k: v for k, v in _stability(sub, self.tz).items()})
        rank = st
        if self.split_t is not None:
            is_ = quick_stats(closed, cfg.initial_balance, None, self.split_t)
            oos = quick_stats(closed, cfg.initial_balance, self.split_t, None)
            row.update({"is_n": is_["trades"], "is_net": is_["net_pnl"], "oos_n": oos["trades"],
                        "oos_net": oos["net_pnl"], "oos_win_rate": oos["win_rate_pct"],
                        "oos_dd": oos["max_drawdown"]})
            rank = {"n": is_["trades"], "net": is_["net_pnl"], "max_dd": is_["max_drawdown"],
                    "max_dd_pct": is_["max_drawdown_pct"], "pf": is_["profit_factor"],
                    "win_rate": is_["win_rate_pct"], "avg": is_["expectancy_per_trade"],
                    "top5h": is_["top5h_pnl"], "hours_pos": is_["hours_pos"]}
        # xếp hạng theo tiêu chí đã chọn (dùng cho tìm kiếm thông minh & cột điểm)
        qs = {"trades": rank["n"], "net_pnl": rank["net"], "max_drawdown": rank["max_dd"],
              "max_drawdown_pct": rank["max_dd_pct"],
              "profit_factor": rank["pf"] if rank["pf"] is not None else math.inf,
              "win_rate_pct": rank["win_rate"], "expectancy_per_trade": rank["avg"], "top5h_pnl": rank["top5h"],
              "hours_pos": rank["hours_pos"]}
        row["score"] = _f(score(qs, grid.objective))
        row["ok"] = bool(rank["n"] >= max(1, grid.min_trades) and
                         (grid.max_dd is None or rank["max_dd_pct"] <= grid.max_dd))
        return row

    @staticmethod
    def _neighbors(rows: list[dict]):
        """Độ ổn định theo tham số: lãi ròng trung bình / thấp nhất của các ô kề trên heatmap TP × SL."""
        tps = sorted({r["tp"] for r in rows if r["tp"] is not None})
        sls = sorted({r["sl"] for r in rows if r["sl"] is not None})
        by = {}
        for r in rows:
            by[(r["tp"], r["sl"], r["hold"])] = r
        for r in rows:
            if r["tp"] is None or r["sl"] is None:
                r.update({"nb_cnt": 0, "nb_net": None, "nb_min": None, "h8_nb_net": None, "h8_nb_min": None})
                continue
            i, j = tps.index(r["tp"]), sls.index(r["sl"])
            nb = []
            for a in (i - 1, i, i + 1):
                for b in (j - 1, j, j + 1):
                    if (a, b) != (i, j) and 0 <= a < len(tps) and 0 <= b < len(sls):
                        x = by.get((tps[a], sls[b], r["hold"]))
                        if x is not None:
                            nb.append(x)
            r["nb_cnt"] = len(nb)
            for pre in ("", "h8_"):
                v = [x[pre + "net"] for x in nb if x.get(pre + "net") is not None]
                r[pre + "nb_net"] = float(np.mean(v)) if v else None
                r[pre + "nb_min"] = float(np.min(v)) if v else None

    # ------------------------------------------------------------------ chi tiết 1 cấu hình
    def detail(self, ci: int, tz: str | None = None, hours: list[int] | None = None) -> dict:
        """Chi tiết 1 cấu hình: từng lệnh, đường vốn, theo ngày / giờ vào lệnh / coin.
        `hours` – chỉ cho vào lệnh trong các giờ đó: chạy lại sổ lệnh với khung giờ này (đúng như bot chỉ chạy giờ đó)."""
        if self.ctx is None:
            raise RuntimeError("Lượt chạy chưa sẵn sàng (máy chủ đã khởi động lại) – hãy chạy lại")
        tz = tz or self.tz
        combo = self.combos[ci]
        ctx = self.ctx
        outer = ctx.outer(combo)
        ev = ctx.evaluator(outer)
        _, cfg = ctx.cfg_for_outer(outer)
        hsel = sorted({int(h) for h in hours if 0 <= int(h) < 24}) if hours is not None else list(range(24))
        if len(hsel) < 24:
            cfg = dataclasses.replace(cfg, sessions=_sessions_for(hsel, cfg.sessions))
        book, rejected = ev.run(cfg, *ctx.inner(combo), full=True)
        closed = sorted(book.closed, key=lambda r: r["entry_t"])
        row = next((r for r in self.rows if r["ci"] == ci), None)
        trades = []
        for n, r in enumerate(closed, 1):
            trades.append({
                "id": n, "coin": r["symbol"], "side": r["side"], "hour": r["hour"],
                "entry_ms": int(r["entry_t"]), "entry_t": _fmt(r["entry_t"], tz), "entry_px": r["entry_px"],
                "exit_t": _fmt(r["exit_t"], tz), "exit_px": r["exit_px"], "reason": r["reason"],
                "hold_min": round(r["hold_s"] / 60.0, 2), "pnl": r["net_pnl"], "fee": r["entry_fee"] + r["exit_fee"],
                "funding": r["funding"], "roe": r["roe_pct"], "res": r["resolution"], "taken": True,
                "tp_px": r.get("tp_px"), "sl_px": r.get("sl_px"), "rule": r.get("rule"),
                "move_pct": r.get("move_pct"), "signal_t": _fmt(r.get("signal_t"), tz),
                "notional": r.get("notional"), "margin": r.get("margin"),
            })
        a = _arrays(closed)
        o = np.argsort(a["exit_t"], kind="stable") if len(closed) else np.array([], int)
        equity = [[int(t), float(v)] for t, v in zip(a["exit_t"][o], np.cumsum(a["pnl"][o]))] if len(closed) else []
        by_day, by_hour, by_coin = _groups(closed, a, o, tz)
        return {
            "combo": {"mode": self.exchange, "ci": ci, "tp": row and row["tp"], "sl": row and row["sl"],
                      "hold": row and row["hold"], "params": row and row["params"]},
            "hours": hsel,
            "stats": _stats(closed, cfg.initial_balance, subset=len(hsel) < 24),
            "trades": trades, "equity": equity, "by_day": by_day, "by_hour": by_hour, "by_coin": by_coin,
            "rejects": [{"reason": k, "label": REJECT_LABELS.get(k, k), "n": int(v)}
                        for k, v in Counter(rejected).most_common() if k not in STATIC_REJECTS],
        }

    def release(self):
        """Giải phóng phần tính trước (RAM) – bảng xếp hạng vẫn xem được, chi tiết sẽ phải chạy lại."""
        self.ctx = None


def _base_with_axis(base: dict, grid: Grid) -> dict:
    """Cấu hình nền để chuẩn bị dữ liệu: TP/SL/giữ lệnh có thể chỉ nằm trong lưới, điền tạm giá trị đầu tiên
    (mọi tổ hợp đều chạy lại từ phần tính trước nên giá trị này không ảnh hưởng kết quả)."""
    from backtester.config import set_path
    for k, vals in zip(grid.keys, grid.values):
        if k in FAST_KEYS and _get(base, k) in (None, ""):
            v = next((x for x in vals if x is not None), None)
            if v is not None:
                base = set_path(base, k, v)
    return base


def _axis_value(run: "BtRun", vals: dict, key: str, cfg_val):
    if key in vals:
        v = vals[key]
        return None if v is None else float(v)
    return None if cfg_val is None else float(cfg_val)


def _hold_min(v) -> int:
    from backtester.utils import parse_duration
    if v is None or v == "":
        return 0
    ms = v if isinstance(v, (int, float)) and v > 10_000 else parse_duration(v)
    return int((ms or 0) // MIN_MS)


def _fmt(ms, tz: str) -> str:
    if ms is None or (isinstance(ms, float) and math.isnan(ms)):
        return ""
    from backtester.utils import fmt_ms
    return fmt_ms(int(ms), tz, True)


def _groups(closed: list[dict], a: dict, order, tz: str):
    """Lãi/lỗ theo ngày thoát lệnh, theo giờ vào lệnh (0–23) và theo coin."""
    if not closed:
        return [], [{"hour": h, "n": 0, "sim": 0.0, "win": 0, "n_tp": 0, "n_sl": 0, "n_timeout": 0} for h in range(24)], []
    reason = np.array([r["reason"] for r in closed], object)
    df = pd.DataFrame({
        "day": pd.to_datetime(a["exit_t"], unit="ms", utc=True).tz_convert(tz).strftime("%Y-%m-%d"),
        "hour": a["hour"], "coin": a["symbol"], "pnl": a["pnl"], "win": a["pnl"] > 0,
        "tp": reason == "TP", "sl": np.isin(reason, ("SL", "LIQ")), "to": reason == "TIMEOUT",
    })

    def agg(keys, name):
        g = df.groupby(keys)
        out = pd.DataFrame({"n": g.pnl.size(), "sim": g.pnl.sum(), "win": g.win.sum(), "n_tp": g.tp.sum(),
                            "n_sl": g.sl.sum(), "n_timeout": g.to.sum()})
        return out

    ints = ("n", "win", "n_tp", "n_sl", "n_timeout")
    conv = lambda r: {k: (int(v) if k in ints else float(v)) for k, v in r.items()}  # noqa: E731
    D = agg("day", "day")
    by_day = [{"day": k, "sim": float(r["sim"]), "actual": 0.0, **conv(r)} for k, r in D.iterrows()]
    H = agg("hour", "hour").reindex(range(24), fill_value=0)
    by_hour = [{"hour": int(k), **conv(r)} for k, r in H.iterrows()]
    C = agg("coin", "coin").sort_values("sim")
    by_coin = [{"coin": str(k), "pnl": float(r["sim"]), **conv(r)} for k, r in C.iterrows()]
    return by_day, by_hour, by_coin
