"""Quét tín hiệu (vector hoá trên nến 1m) -> danh sách ứng viên vào lệnh.

Với nến khung lớn (3m, 5m, ...): nến được dựng từ nến 1m, căn theo mốc UTC như sàn.
  - trigger=touch : ứng viên ở phút 1m ĐẦU TIÊN trong nến khung mà giá chạm ngưỡng
                    open_khung * (1 ± x%). Thời điểm chính xác trong phút xác định bằng tick.
  - trigger=close : ứng viên tại thời điểm đóng nến khung nếu (close-open)/open >= x%.
Mỗi nến khung chỉ sinh tối đa 1 tín hiệu cho mỗi chiều của mỗi rule.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..config import SignalRule
from ..utils import DAY_MS, MIN_MS


@dataclass
class Candidate:
    t: int              # thời điểm sớm nhất (touch: đầu phút chạm; close: lúc nến đóng)
    symbol: str
    rule_idx: int
    dir_sign: int       # +1 tăng, -1 giảm
    side: int           # +1 long, -1 short
    trigger: str
    bar_ts: int         # phút 1m chứa điểm chạm (touch)
    candle_ts: int      # mở nến khung
    tf_open: float
    threshold: float    # giá ngưỡng (touch)
    change_pct: float   # biến động tại tín hiệu (%)
    vol24: float        # volume 24h (USDT) tính đến phút trước tín hiệu


def rolling_24h(ts: np.ndarray, qv: np.ndarray) -> np.ndarray:
    """vol24[i] = tổng qv của các nến 1m có ts trong [ts_i - 24h, ts_i) (các phút đã đóng)."""
    cs = np.concatenate([[0.0], np.cumsum(qv)])
    j = np.searchsorted(ts, ts - DAY_MS, "left")
    return cs[np.arange(len(ts))] - cs[j]


def scan_symbol(symbol: str, ts, o, h, l, c, qv, rules: list[SignalRule], t_min: int, t_max: int,
                allowed_days: set[int] | None = None) -> list[Candidate]:
    n = len(ts)
    if n == 0:
        return []
    vol24 = rolling_24h(ts, qv)
    out: list[Candidate] = []
    for ri, r in enumerate(rules):
        tfms = r.tf_minutes * MIN_MS
        bucket = ts - ts % tfms
        # chỉ số bắt đầu mỗi nhóm nến khung
        starts = np.flatnonzero(np.r_[True, bucket[1:] != bucket[:-1]])
        gid = np.cumsum(np.r_[True, bucket[1:] != bucket[:-1]]) - 1
        tf_open = o[starts][gid]
        dirs = [1, -1] if r.direction == "both" else ([1] if r.direction == "up" else [-1])
        x = r.change_pct / 100.0
        if r.trigger == "touch":
            for d in dirs:
                thr = tf_open * (1 + d * x)
                hit = h >= thr if d == 1 else l <= thr
                idx = np.flatnonzero(hit)
                if len(idx) == 0:
                    continue
                # phút chạm đầu tiên trong mỗi nến khung
                _, first = np.unique(gid[idx], return_index=True)
                for i in idx[first]:
                    t = int(ts[i])
                    if t < t_min - MIN_MS or t >= t_max:
                        continue
                    if allowed_days is not None and (t - t % DAY_MS) not in allowed_days:
                        continue
                    out.append(Candidate(t, symbol, ri, d, r.side_for(d), "touch", t, int(bucket[i]),
                                         float(tf_open[i]), float(thr[i]), d * r.change_pct, float(vol24[i])))
        else:
            ends = np.r_[starts[1:] - 1, n - 1]
            close = c[ends]
            op = o[starts]
            chg = (close - op) / op
            t_close = bucket[starts] + tfms
            # chỉ nhận nến khung đã đóng trong phạm vi dữ liệu
            last_ok = ts[ends] + MIN_MS >= t_close
            for d in dirs:
                m = (chg * d >= x) & last_ok
                if r.max_change_pct is not None:
                    m &= chg * d <= r.max_change_pct / 100.0
                for k in np.flatnonzero(m):
                    t = int(t_close[k])
                    if t < t_min or t >= t_max:
                        continue
                    if allowed_days is not None and ((t - 1) - (t - 1) % DAY_MS) not in allowed_days:
                        continue
                    i_last = ends[k]
                    # volume 24h tính đến hết nến khung
                    v24 = float(vol24[i_last] + qv[i_last])
                    out.append(Candidate(t, symbol, ri, d, r.side_for(d), "close", int(ts[i_last]),
                                         int(bucket[starts[k]]), float(op[k]), float("nan"),
                                         float(chg[k] * 100), v24))
    return out
