"""Quét / tối ưu hàng trăm – hàng nghìn chiến lược cùng lúc.

Cấu hình (YAML):

    sweep:                                   # tham số cần thử: danh sách hoặc khoảng {from, to, step}
      exit.take_profit: {from: 0.5, to: 5, step: 0.5}
      exit.stop_loss: [2, 3, 5, 10]
      exit.max_hold: ["30m", "1h", "2h"]     # khoảng dùng phút: {from: 15, to: 240, step: 15}
      limits.max_open_positions: [3, 5]      # tham số khác cũng được (chạy lại tín hiệu cho mỗi giá trị)
    optimize:
      objective: net_pnl        # net_pnl | return_dd | profit_factor | win_rate_pct | expectancy
      min_trades: 10            # bỏ qua chiến lược có quá ít lệnh khi xếp hạng
      max_drawdown_pct: null    # bỏ qua chiến lược sụt vốn quá mức này
      top_k: 5                  # số chiến lược tốt nhất được chạy lại bằng engine đầy đủ (báo cáo chi tiết)
      oos_pct: 0                # >0: xếp hạng trên phần đầu, kiểm tra trên X% cuối giai đoạn (chống quá khớp)
      max_combos: 20000         # lưới lớn hơn -> tìm kiếm thông minh (lấy mẫu + tinh chỉnh quanh top)

TP / SL / thời gian giữ lệnh được đánh giá bằng engine nhanh (`engine/fast.py`): tín hiệu và điểm vào
chỉ tính một lần cho mọi tổ hợp. Các tham số khác làm tín hiệu/lệnh thay đổi nên được tính lại theo từng giá trị.
"""
from __future__ import annotations

import copy
import heapq
import itertools
import json
import math
import random
import re
import time
from collections import OrderedDict
from pathlib import Path

import pandas as pd

from .config import build_config, set_path
from .engine.backtest import Backtester, scan_candidates
from .engine.fast import ExitAxis, FastEvaluator, fast_result, hour_vectors, quick_stats
from .report import write_report
from .stats import summarize
from .progress import PROGRESS
from .utils import DAY_MS, HOUR_MS, MIN_MS, SEC_MS, log, parse_duration

FAST_KEYS = ("exit.take_profit", "exit.stop_loss", "exit.max_hold")
DATA_KEYS = ("exchange", "start", "end", "timezone", "universe", "data")
DURATION_KEYS = {"exit.max_hold", "signals.0.pullback_window", "signals.1.pullback_window", "limits.cooldown_after_tp_coin", "limits.cooldown_after_sl_coin",
                 "limits.cooldown_after_trade_coin", "limits.cooldown_after_sl_global",
                 "limits.pause_after_consecutive_sl", "limits.max_runtime"}
# tham số không ảnh hưởng tới điểm thoát lệnh (chỉ ảnh hưởng sổ lệnh) -> dùng chung phần tính trước
BOOK_ONLY = ("limits", "schedule", "initial_balance", "name")
BOOK_ONLY_PATHS = ("position.margin", "position.margin_mode")

LABELS = {
    "exit.take_profit": "TP", "exit.stop_loss": "SL", "exit.max_hold": "Giữ tối đa",
    "signals.0.change_pct": "Tín hiệu 1 (%)", "signals.1.change_pct": "Tín hiệu 2 (%)",
    "signals.0.pullback_pct": "Hồi từ đỉnh 1 (%)", "signals.1.pullback_pct": "Hồi từ đỉnh 2 (%)",
    "signals.0.pullback_window": "Theo dõi hồi 1", "signals.1.pullback_window": "Theo dõi hồi 2",
    "position.leverage": "Đòn bẩy", "position.margin": "Ký quỹ", "entry.latency_ms": "Độ trễ (ms)",
    "entry.slippage_pct": "Trượt giá vào (%)", "limits.max_open_positions": "Lệnh mở đồng thời",
    "limits.max_sl_per_day": "Max SL/ngày", "limits.max_sl_per_coin_per_day": "Max SL/coin/ngày",
    "limits.cooldown_after_sl_coin": "Nghỉ coin sau SL", "limits.cooldown_after_sl_global": "Nghỉ sau SL",
}
OBJECTIVES = {
    "net_pnl": "Lãi/lỗ ròng",
    "return_dd": "Lãi / Max drawdown",
    "profit_factor": "Profit factor",
    "win_rate_pct": "Win rate",
    "expectancy": "Lãi kỳ vọng / lệnh",
    "top5h": "Tổng lãi 5 giờ tốt nhất",
    "hours_pos": "Số giờ có lãi",
}
METRICS = ["trades", "win_rate_pct", "net_pnl", "return_pct", "profit_factor", "max_drawdown_pct", "max_drawdown",
           "hours_pos", "top5h_pnl",
           "tp", "sl", "timeouts", "liquidations", "fees", "expectancy_per_trade", "avg_hold_s"]
MAX_VALUES_PER_KEY = 2000
MAX_TOP_K = 200   # số chiến lược tối đa được chạy lại bằng engine đầy đủ cho mỗi tiêu chí
HARD_MAX_GRID = 10 ** 9
NONE_TOKENS = {"", "none", "null", "off", "khong", "không", "-", "∞"}


# ============================================================================ đọc lưới tham số
def fmt_duration(ms: int | None) -> str:
    if not ms:
        return "∞"
    for unit, size in (("d", DAY_MS), ("h", HOUR_MS), ("m", MIN_MS), ("s", SEC_MS)):
        if ms % size == 0:
            return f"{ms // size}{unit}"
    return f"{ms}ms"


def _num(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip()
    if s.lower() in NONE_TOKENS:
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d*\.?\d+(e-?\d+)?", s, re.I):
        return float(s)
    return s


def _decimals(*xs) -> int:
    d = 0
    for x in xs:
        s = f"{x:.10f}".rstrip("0")
        if "." in s:
            d = max(d, len(s.split(".")[1]))
    return min(d, 8)


def parse_values(key: str, spec) -> list:
    """Danh sách giá trị của một tham số từ list / chuỗi '1, 2, 3' / khoảng {from, to, step}."""
    is_dur = key in DURATION_KEYS
    if isinstance(spec, dict):
        a, b, st = spec.get("from"), spec.get("to"), spec.get("step")
        if a is None or b is None or st in (None, 0, ""):
            raise ValueError(f"sweep.{key}: khoảng cần đủ from, to, step")
        if is_dur:
            a, b, st = (parse_duration(x) or 0 for x in (a, b, st))
        else:
            a, b, st = float(a), float(b), float(st)
        if st <= 0 or b < a:
            raise ValueError(f"sweep.{key}: cần step > 0 và to ≥ from")
        n = int(math.floor((b - a) / st + 1e-9)) + 1
        if n > MAX_VALUES_PER_KEY:
            raise ValueError(f"sweep.{key}: {n} giá trị là quá nhiều (tối đa {MAX_VALUES_PER_KEY}) – tăng bước nhảy")
        if is_dur:
            vals = [fmt_duration(int(a + i * st)) for i in range(n)]
        else:
            d = _decimals(a, st)
            vals = [round(a + i * st, d) for i in range(n)]
            vals = [int(v) if float(v).is_integer() and d == 0 else v for v in vals]
    elif isinstance(spec, (list, tuple)):
        vals = [_num(v) for v in spec]
    elif isinstance(spec, str):
        vals = [_num(v) for v in re.split(r"[,;]", spec)]
    else:
        vals = [spec]
    out = []
    for v in vals:  # bỏ trùng, giữ thứ tự
        if v not in out:
            out.append(v)
    if not out:
        raise ValueError(f"sweep.{key}: chưa có giá trị nào")
    if is_dur:
        for v in out:
            parse_duration(v)  # báo lỗi định dạng sớm
    elif key in ("exit.take_profit", "exit.stop_loss"):
        for v in out:
            if v is not None and (not isinstance(v, (int, float)) or v <= 0):
                raise ValueError(f"sweep.{key}: giá trị {v!r} phải là số > 0 (hoặc 'none' = không đặt)")
    return out


class Grid:
    def __init__(self, raw: dict):
        sw = raw.get("sweep") or {}
        if not isinstance(sw, dict):
            raise ValueError("sweep phải là bảng {tham số: giá trị}")
        self.keys: list[str] = []
        self.values: list[list] = []
        for k, spec in sw.items():
            if k.split(".")[0] in DATA_KEYS:
                raise ValueError(f"sweep không hỗ trợ tham số dữ liệu '{k}' (chỉ signals/entry/position/exit/fees/limits/schedule)")
            self.keys.append(k)
            self.values.append(parse_values(k, spec))
        self.total = math.prod(len(v) for v in self.values) if self.keys else 0
        if self.total > HARD_MAX_GRID:
            raise ValueError(f"Lưới tham số quá lớn ({self.total:,} tổ hợp)")
        o = raw.get("optimize") or {}
        self.objective = str(o.get("objective") or "net_pnl")
        if self.objective not in OBJECTIVES:
            raise ValueError(f"optimize.objective: một trong {list(OBJECTIVES)}")
        self.min_trades = int(o.get("min_trades") or 0)
        self.max_dd = float(o["max_drawdown_pct"]) if o.get("max_drawdown_pct") not in (None, "") else None
        self.top_k = max(0, min(MAX_TOP_K, int(o.get("top_k", 5) if o.get("top_k") is not None else 5)))
        o2 = o.get("objective2") or None
        if o2 is not None and o2 not in OBJECTIVES:
            raise ValueError(f"optimize.objective2: một trong {list(OBJECTIVES)} hoặc để trống")
        self.objective2 = o2 if o2 != self.objective else None
        self.detail_engine = str(o.get("detail_engine") or "fast").lower()
        if self.detail_engine not in ("fast", "full"):
            raise ValueError("optimize.detail_engine: fast | full")
        self.oos_pct = float(o.get("oos_pct") or 0)
        if not 0 <= self.oos_pct <= 90:
            raise ValueError("optimize.oos_pct phải trong khoảng 0–90")
        self.max_combos = max(10, int(o.get("max_combos") or 20000))
        self.seed = int(o.get("seed") or 42)

    @property
    def mode(self) -> str:
        return "grid" if self.total <= self.max_combos else "smart"

    def label(self, combo: tuple, unit: str = "price") -> str:
        parts = []
        for k, v in zip(self.keys, combo):
            parts.append(f"{LABELS.get(k, k)} {fmt_value(k, v, unit)}")
        return " · ".join(parts)


def fmt_value(key: str, v, unit: str = "price") -> str:
    if v is None:
        return "không đặt" if key != "exit.max_hold" else "∞"
    if key in ("exit.take_profit", "exit.stop_loss"):
        return f"{v:g}%" + (" ROE" if unit == "roe" else "")
    if key in DURATION_KEYS:
        return fmt_duration(parse_duration(v))
    return f"{v:g}" if isinstance(v, float) else str(v)


def describe(raw: dict) -> dict:
    """Tóm tắt lưới cho giao diện (số tổ hợp, chế độ)."""
    g = Grid(raw)
    return {"keys": g.keys, "combos": g.total, "mode": g.mode,
            "evaluate": min(g.total, g.max_combos), "fast": all(k in FAST_KEYS for k in g.keys)}


# ============================================================================ điểm xếp hạng
def score(s: dict, objective: str) -> float:
    if not s["trades"]:
        return -math.inf
    if objective == "net_pnl":
        return s["net_pnl"]
    if objective == "return_dd":
        return s["net_pnl"] / max(s["max_drawdown"], 1e-9) if s["net_pnl"] > 0 else s["net_pnl"]
    if objective == "profit_factor":
        return min(s["profit_factor"], 1e6)
    if objective == "win_rate_pct":
        return s["win_rate_pct"]
    if objective == "top5h":
        return s["top5h_pnl"]
    if objective == "hours_pos":
        # số giờ có lãi; bằng nhau thì lãi/lỗ cao hơn đứng trước (phần lẻ nằm trong (-0.5, 0.5) nên không đổi thứ tự số giờ)
        return s["hours_pos"] + math.atan(s["net_pnl"]) / math.pi * 0.999
    return s["expectancy_per_trade"]


def _finite(v):
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None if math.isnan(v) else (1e9 if v > 0 else -1e9)
    return v


def _downsample(eq: list, n: int = 120) -> list:
    if len(eq) <= n:
        pts = eq
    else:
        step = (len(eq) - 1) / (n - 1)
        pts = [eq[round(i * step)] for i in range(n)]
    return [[int(t), round(float(b), 4)] for t, b in pts]


# ============================================================================ chạy
class _Ctx:
    """Bộ nhớ đệm tín hiệu & phần tính trước theo nhóm tham số."""

    def __init__(self, base: dict, prepared, grid: Grid):
        self.base, self.prepared, self.grid = base, prepared, grid
        self.cfg0, self.store, self.mcap, self.uni = prepared
        self.fast_idx = {k: grid.keys.index(k) for k in FAST_KEYS if k in grid.keys}
        self.outer_idx = [i for i, k in enumerate(grid.keys) if k not in FAST_KEYS]
        self.cands: dict[str, list] = {}
        self.evals: OrderedDict[str, FastEvaluator] = OrderedDict()
        self.cfgs: dict[tuple, object] = {}

    def axis_values(self, key: str) -> list:
        if key in self.fast_idx:
            return self.grid.values[self.fast_idx[key]]
        return [_get(self.base, key)]

    def raw_for(self, combo: tuple) -> dict:
        r = self.base
        for k, v in zip(self.grid.keys, combo):
            r = set_path(r, k, v)
        return r

    def outer(self, combo: tuple) -> tuple:
        return tuple(combo[i] for i in self.outer_idx)

    def cfg_for_outer(self, outer: tuple):
        if outer not in self.cfgs:
            r = self.base
            for i, v in zip(self.outer_idx, outer):
                r = set_path(r, self.grid.keys[i], v)
            # để build_config hợp lệ khi cấu hình gốc chưa có TP/SL/giữ lệnh
            for k in FAST_KEYS:
                if _get(r, k) in (None, "") and k in self.fast_idx:
                    vals = [v for v in self.axis_values(k) if v is not None]
                    if vals:
                        r = set_path(r, k, vals[0])
            self.cfgs[outer] = (r, build_config(dict(r, exchange=self.cfg0.exchange), self.store.ex.default_fees))
        return self.cfgs[outer]

    def evaluator(self, outer: tuple) -> FastEvaluator:
        r, cfg = self.cfg_for_outer(outer)
        ekey = _events_key(r)
        if ekey in self.evals:
            self.evals.move_to_end(ekey)
            return self.evals[ekey]
        skey = json.dumps(r.get("signals"), sort_keys=True, default=str)
        if skey not in self.cands:
            cands = scan_candidates(cfg, self.store, self.uni)
            log.info("Tín hiệu: %d (đã lọc volume 24h)", len(cands))
            if cfg.resolution != "1m" and cands and self.store.ex.tick_history_days != 0:
                self.store.prefetch_ticks([(c.symbol, c.t) for c in cands])
            self.cands[skey] = cands
        cands = [copy.copy(c) for c in self.cands[skey]]
        axis = ExitAxis(self.axis_values("exit.take_profit"), self.axis_values("exit.stop_loss"),
                        [parse_duration(v) for v in self.axis_values("exit.max_hold")])
        PROGRESS.set_stage("Tính trước điểm thoát lệnh")
        t0 = time.time()
        ev = FastEvaluator(Backtester(cfg, self.store, self.mcap, self.uni), cands, axis)
        log.info("Tính trước %d tín hiệu × (%d TP + %d SL + %d mốc giữ lệnh) trong %.1fs",
                 len(ev.items), len(axis.tps), len(axis.sls), len(axis.holds), time.time() - t0)
        self.evals[ekey] = ev
        while len(self.evals) > 6:
            self.evals.popitem(last=False)
        return ev

    def inner(self, combo: tuple) -> tuple[int, int, int]:
        def pos(k):
            return self.grid.values[self.fast_idx[k]].index(combo[self.fast_idx[k]]) if k in self.fast_idx else 0
        return pos("exit.take_profit"), pos("exit.stop_loss"), pos("exit.max_hold")


def _get(d: dict, path: str):
    cur = d
    for k in path.split("."):
        if isinstance(cur, list):
            k = int(k)
            if k >= len(cur):
                return None
            cur = cur[k]
        elif isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return None
        if cur is None:
            return None
    return cur


def _events_key(r: dict) -> str:
    x = {k: v for k, v in r.items() if k not in BOOK_ONLY and k not in ("sweep", "optimize")}
    x = copy.deepcopy(x)
    for p in BOOK_ONLY_PATHS + FAST_KEYS:
        a, b = p.split(".")
        if isinstance(x.get(a), dict):
            x[a].pop(b, None)
    return json.dumps(x, sort_keys=True, default=str)


def _valid_combo(ctx: _Ctx, combo: tuple) -> bool:
    vals = {k: combo[i] for i, k in enumerate(ctx.grid.keys)}
    get = lambda k: vals[k] if k in vals else _get(ctx.base, k)  # noqa: E731
    return any(get(k) not in (None, "") for k in FAST_KEYS)


def run_sweep(raw: dict, exchange: str, out_dir: Path, prepare_fn, run_one_fn) -> list[dict]:
    """Quét lưới / tối ưu trên một sàn. Trả về các dòng tóm tắt (top K chạy chi tiết) cho index.json."""
    t_start = time.time()
    grid = Grid(raw)
    base = {k: v for k, v in raw.items() if k not in ("sweep", "optimize")}
    prepared = prepare_fn(base, exchange)
    ctx = _Ctx(base, prepared, grid)
    t_eval = time.time()
    cfg0 = ctx.cfg0
    unit = cfg0.tp_sl_unit
    split_t = None
    if grid.oos_pct > 0:
        split_t = int(cfg0.end_ms - (cfg0.end_ms - cfg0.start_ms) * grid.oos_pct / 100.0)

    log.info("Lưới %d tham số, %s tổ hợp – chế độ %s", len(grid.keys), f"{grid.total:,}",
             "quét toàn bộ" if grid.mode == "grid" else f"tìm kiếm thông minh ({grid.max_combos:,} tổ hợp)")

    results: dict[tuple, dict] = {}
    heaps: list[list] = [[], []]  # heap (score, seq, combo, curve) theo từng tiêu chí – đường vốn của top 100
    seq = itertools.count()

    def evaluate(combo: tuple):
        if combo in results or not _valid_combo(ctx, combo):
            return
        outer = ctx.outer(combo)
        ev = ctx.evaluator(outer)
        _, cfg = ctx.cfg_for_outer(outer)
        i, j, k = ctx.inner(combo)
        book, _ = ev.run(cfg, i, j, k)
        closed = book.closed
        st = quick_stats(closed, cfg.initial_balance)
        row = {"combo": combo, **st}
        row["hv"] = hour_vectors(closed)   # theo giờ vào lệnh, cho bản đồ nhiệt lọc theo giờ
        row["sig"] = (st["trades"], round(st["net_pnl"], 6), round(st["fees"], 6), round(st["max_drawdown"], 6),
                      st["tp"], st["sl"], st["timeouts"], st["liquidations"], round(st["avg_hold_s"], 3))
        rank_src = st
        if split_t is not None:
            is_ = quick_stats(closed, cfg.initial_balance, None, split_t)
            oos = quick_stats(closed, cfg.initial_balance, split_t, None)
            row.update({"is_pnl": is_["net_pnl"], "is_trades": is_["trades"], "oos_pnl": oos["net_pnl"],
                        "oos_trades": oos["trades"], "oos_win_rate_pct": oos["win_rate_pct"]})
            rank_src = is_
        ok = rank_src["trades"] >= max(1, grid.min_trades) and \
            (grid.max_dd is None or rank_src["max_drawdown_pct"] <= grid.max_dd)
        row["score"] = score(rank_src, grid.objective)
        if grid.objective2:
            row["score2"] = score(rank_src, grid.objective2)
        row["ok"] = ok
        results[combo] = row
        if ok:
            pts = None
            for hp, key in zip(heaps, ("score", "score2")):
                if key not in row:
                    continue
                if len(hp) < 100 or row[key] > hp[0][0]:
                    pts = pts or _downsample(book.equity)
                    item = (row[key], next(seq), combo, pts)
                    if len(hp) < 100:
                        heapq.heappush(hp, item)
                    else:
                        heapq.heapreplace(hp, item)

    def progress_loop(combos, total_hint):
        PROGRESS.set_stage("Đánh giá chiến lược", total_hint)
        # nhóm theo tham số ngoài để dùng lại phần tính trước
        combos = sorted(combos, key=lambda c: json.dumps(ctx.outer(c), default=str))
        for n, combo in enumerate(combos, 1):
            if n == 1 or n % 25 == 0:
                PROGRESS.set_stage("Đánh giá chiến lược", total_hint)
                PROGRESS.step(len(results), total_hint)
            evaluate(combo)
        PROGRESS.step(len(results), total_hint)

    if grid.mode == "grid":
        progress_loop(list(itertools.product(*grid.values)), grid.total)
    else:
        rng = random.Random(grid.seed)
        budget = grid.max_combos
        dims = [len(v) for v in grid.values]
        first = set()
        while len(first) < int(budget * 0.6):
            first.add(tuple(rng.randrange(d) for d in dims))
        progress_loop([tuple(grid.values[d][i] for d, i in enumerate(ix)) for ix in first], budget)
        # tinh chỉnh quanh các chiến lược tốt nhất: thử các ô lân cận (±1, ±2 bước mỗi tham số)
        for _round in range(50):
            if len(results) >= budget:
                break
            top = sorted((r for r in results.values() if r["ok"]), key=lambda r: -r["score"])[:20]
            nxt = []
            for r in top:
                ix = [grid.values[d].index(v) for d, v in enumerate(r["combo"])]
                for d in range(len(dims)):
                    for delta in (-2, -1, 1, 2):
                        q = list(ix)
                        q[d] += delta
                        if 0 <= q[d] < dims[d]:
                            c = tuple(grid.values[e][i] for e, i in enumerate(q))
                            if c not in results:
                                nxt.append(c)
            nxt = list(dict.fromkeys(nxt))[: budget - len(results)]
            if not nxt:
                break
            progress_loop(nxt, budget)
        log.info("Tìm kiếm thông minh: đã thử %d / %d tổ hợp", len(results), grid.total)

    ranked = sorted(results.values(), key=lambda r: (not r["ok"], -r["score"]))
    n_ok = sum(1 for r in ranked if r["ok"])
    log.info("Đã đánh giá %d chiến lược (%d đạt điều kiện) trong %.1fs (tải dữ liệu %.1fs)",
             len(ranked), n_ok, time.time() - t_eval, t_eval - t_start)

    # ---- top K mỗi tiêu chí: chiến lược cho kết quả giống hệt nhau (vd SL quá xa, không bao giờ chạm) chỉ giữ một
    #      đại diện – đứng đầu theo tiêu chí 1 – để top K là K kết quả khác nhau và không tạo báo cáo trùng
    top_rows = []
    ok_rows = [r for r in ranked if r["ok"]]
    groups: dict = {}
    for r in ok_rows:                       # theo thứ tự xếp hạng tiêu chí 1 -> phần tử đầu là đại diện
        groups.setdefault(r["sig"], []).append(r)

    def pick(order, key):
        seen, n = set(), 0
        for r in order:
            if n >= grid.top_k:
                break
            if r["sig"] in seen:
                continue
            seen.add(r["sig"])
            n += 1
            groups[r["sig"]][0][key] = n

    pick(ok_rows, "rank")
    if grid.objective2:
        pick(sorted(ok_rows, key=lambda r: -r["score2"]), "rank2")
    todo = [r for r in ok_rows if r.get("rank")] + \
        sorted((r for r in ok_rows if r.get("rank2") and not r.get("rank")), key=lambda r: r["rank2"])
    n_dup = sum(len(g) - 1 for g in groups.values() if g[0] in todo)
    if todo:
        log.info("Tạo báo cáo chi tiết cho %d chiến lược top (%s)%s", len(todo),
                 "engine nhanh" if grid.detail_engine == "fast" else "engine đầy đủ",
                 f", gộp {n_dup} chiến lược cho kết quả giống hệt" if n_dup else "")
        PROGRESS.set_stage(f"Tạo báo cáo chi tiết {len(todo)} chiến lược top", len(todo))
    for i, r in enumerate(todo):
        PROGRESS.step(i)
        label = grid.label(r["combo"], unit)
        sub = f"top_{r['rank']:02d}" if r.get("rank") else f"top2_{r['rank2']:02d}"
        log.info("[%s] %s  (nhanh: %+.2f USDT)", sub, label, r["net_pnl"])
        raw_c = ctx.raw_for(r["combo"])
        if grid.detail_engine == "fast":
            t1 = time.time()
            outer = ctx.outer(r["combo"])
            ev = ctx.evaluator(outer)
            cfg_c = build_config(dict(raw_c, exchange=exchange), ctx.store.ex.default_fees)
            res = fast_result(ev, cfg_c, *ctx.inner(r["combo"]), ctx.uni)
            summ = summarize(res)
            summ["runtime_s"] = round(time.time() - t1, 1)
            summ["detail_engine"] = "fast"
            write_report(res, summ, out_dir / sub)
        else:
            _, summ = run_one_fn(raw_c, exchange, out_dir / sub, prepared=prepared)
        dup = [x for x in groups[r["sig"]] if x is not r]
        summ.update({"params": label, "dir": f"{exchange}/{sub}", "rank": r.get("rank"), "rank2": r.get("rank2"),
                     "fast_net_pnl": r["net_pnl"], "values": {k: v for k, v in zip(grid.keys, r["combo"])},
                     "detail_engine": grid.detail_engine, "dupes": len(dup),
                     "dupe_params": [grid.label(x["combo"], unit) for x in dup[:20]]})
        for x in dup:          # các chiến lược giống hệt dùng chung báo cáo của đại diện
            x["dir"] = f"{exchange}/{sub}"
        if split_t is not None:
            summ["oos_pnl"] = r["oos_pnl"]
        r["dir"] = summ["dir"]
        r["exact_net_pnl"] = summ.get("net_pnl")
        top_rows.append(summ)

    _write(out_dir, grid, ranked, heaps[0] + heaps[1], cfg0, exchange, split_t, time.time() - t_start, ctx, unit)
    with pd.option_context("display.width", 200, "display.max_columns", 20, "display.max_colwidth", 70):
        df = pd.DataFrame([{"params": grid.label(r["combo"], unit), **{m: r[m] for m in
                            ("trades", "win_rate_pct", "net_pnl", "profit_factor", "max_drawdown_pct")}}
                           for r in ranked[:15]])
        if len(df):
            print(df.round(2).to_string(index=False))
    return top_rows


def _write(out_dir: Path, grid: Grid, ranked: list, curves: list, cfg0, exchange: str, split_t, runtime: float,
           ctx: _Ctx, unit: str):
    out_dir.mkdir(parents=True, exist_ok=True)
    extra = ["is_pnl", "is_trades", "oos_pnl", "oos_trades", "oos_win_rate_pct"] if split_t is not None else []
    cols = METRICS + extra + ["score"] + (["score2"] if grid.objective2 else []) + ["ok"]
    idx_of = {r["combo"]: n for n, r in enumerate(ranked)}
    rows = []
    for r in ranked:
        rows.append(list(r["combo"]) + [_finite(r.get(c)) for c in cols])
    top = [{"rank": r.get("rank"), "rank2": r.get("rank2"), "row": n, "dir": r["dir"],
            "exact_net_pnl": _finite(r.get("exact_net_pnl"))} for n, r in enumerate(ranked) if r.get("dir")]
    data = {
        "exchange": exchange,
        "objective": grid.objective, "objective_label": OBJECTIVES[grid.objective],
        "objective2": grid.objective2, "objective2_label": OBJECTIVES.get(grid.objective2) if grid.objective2 else None,
        "keys": grid.keys, "labels": [LABELS.get(k, k) for k in grid.keys],
        "values": [[fmt_value(k, v, unit) for v in vals] for k, vals in zip(grid.keys, grid.values)],
        "raw_values": grid.values, "unit": unit,
        "total": grid.total, "evaluated": len(ranked), "ok": sum(1 for r in ranked if r["ok"]),
        "mode": grid.mode, "min_trades": grid.min_trades, "max_dd": grid.max_dd, "oos_pct": grid.oos_pct,
        "split_t": split_t, "start": cfg0.start_ms, "end": cfg0.end_ms, "tz": cfg0.timezone,
        "initial_balance": cfg0.initial_balance, "runtime_s": round(runtime, 1),
        "signals": sum(len(c) for c in ctx.cands.values()),
        "cols": cols, "rows": rows, "top": top,
        "curves": {str(idx_of[c]): pts for _, _, c, pts in curves if c in idx_of},
    }
    (out_dir / "sweep.json").write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    # theo giờ vào lệnh (0–23h), cùng thứ tự dòng với sweep.json: [lãi/lỗ x24, số lệnh x24, số lệnh lãi x24]
    hours = [[[round(float(v), 4) for v in r["hv"][0]], r["hv"][1].tolist(), r["hv"][2].tolist()] for r in ranked]
    (out_dir / "sweep_hours.json").write_text(json.dumps({"rows": hours}, separators=(",", ":")), encoding="utf-8")
    df = pd.DataFrame(rows, columns=[LABELS.get(k, k) for k in grid.keys] + cols)
    df.insert(0, "rank", range(1, len(df) + 1))
    df.to_csv(out_dir / "sweep.csv", index=False, encoding="utf-8-sig")
    log.info("Bảng xếp hạng: %s", (out_dir / "sweep.csv").resolve())
