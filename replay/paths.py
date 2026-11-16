"""Trích "đường giá" thật sau thời điểm vào lệnh của từng lệnh thật, từ tick (từng giao dịch, tới mili-giây).

Ý tưởng để chạy hàng nghìn cấu hình TP/SL/giữ lệnh cực nhanh mà vẫn chính xác từng tick:
  * Lệnh chạm mức giá L lần đầu tiên ở tick nào? -> đó luôn là tick lập ĐỈNH MỚI (nếu L > entry) hoặc ĐÁY MỚI
    (nếu L < entry) kể từ lúc vào lệnh. Vì vậy chỉ cần lưu dãy tick lập đỉnh mới / đáy mới (thường vài trăm tick)
    thay vì hàng triệu tick, và lần chạm đầu tiên của BẤT KỲ mức TP/SL nào tìm được bằng tìm kiếm nhị phân.
  * Đóng lệnh khi hết giờ: lưu giá tick đầu tiên tại/sau mỗi mốc phút kể từ lúc vào lệnh.
  * Funding: lưu các mốc funding (thời điểm, tỷ lệ) trong cửa sổ.

Điểm vào lệnh (chế độ auto), theo thứ tự ưu tiên:
  1. File có cột Entry là giá khớp thật (kiểm tra mẫu: >= 60% lệnh có tick đúng giá đó quanh giây vào lệnh – Bybit)
     -> dùng giá đó, neo vào đúng tick có giá đó (tới mili-giây).
  2. Lệnh thật chốt TP: giá vào = giá thoát / (1 ∓ TP%) (TP là lệnh limit đặt đúng TP% từ giá khớp) và có tick đúng giá
     đó trong ±2 giây -> neo vào tick đó. (OKX: cột Entry là giá mở nến tín hiệu, không phải giá khớp.)
  3. Còn lại: tick đầu tiên tại/sau giữa giây vào lệnh (giây + 500ms) – sai số trung vị ~0.03% so với giá khớp thật.

Không có tick (vd dữ liệu hôm nay sàn chưa công bố) -> dùng nến 1m theo kịch bản BẤT LỢI (giá đi ngược trước, rồi mới
đi thuận trong cùng phút), đánh dấu resolution = "1m".
"""
from __future__ import annotations

import pickle
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .bt import DAY_MS, MIN_MS, PATH_CACHE, PROGRESS, Cancelled, log, now_ms, store_for

LOOKAHEAD_MS = 10 * MIN_MS      # lấy thêm tick sau hết giờ để có giá đóng lệnh thị trường
ANCHOR_BEFORE_MS = 1_000
ANCHOR_AFTER_MS = 2_000
ENTRY_TOL = 0.0005              # giá Entry/giá suy từ TP lệch tick thật > 0.05% -> coi là không khớp
TRUST_RATIO = 0.6               # >= 60% lệnh mẫu có cột Entry khớp tick -> cột Entry là giá khớp thật
SL_TOL = 0.003                  # giá suy từ SL (lệnh market, có trượt giá) lệch tick tối đa 0.3%
MID_SECOND_MS = 500


@dataclass
class PricePath:
    key: str
    symbol: str
    side: int
    entry_t: int
    entry_px: float
    entry_src: str              # csv | tp (suy từ giá TP) | sl (suy từ giá SL) | tick | 1m ; "*" = đã chỉnh cho khớp KQ thật
    res: str                    # tick | mixed | 1m | none
    window_ms: int
    data_end: int               # có dữ liệu giá tới thời điểm này (ms)
    # dãy tick lập đỉnh mới / đáy mới: thời điểm (ms kể từ entry_t, int32), giá, thứ tự trong chuỗi (int32)
    hi_t: np.ndarray = field(repr=False, default=None)
    hi_px: np.ndarray = field(repr=False, default=None)
    hi_q: np.ndarray = field(repr=False, default=None)     # thứ tự trong chuỗi giá (phân xử 2 mức cùng ms)
    lo_t: np.ndarray = field(repr=False, default=None)
    lo_px: np.ndarray = field(repr=False, default=None)
    lo_q: np.ndarray = field(repr=False, default=None)
    samp: np.ndarray = field(repr=False, default=None)     # samp[k] = giá tại/sau entry_t + k phút
    fund_t: np.ndarray = field(repr=False, default=None)
    fund_rate: np.ndarray = field(repr=False, default=None)
    n_ticks: int = 0
    note: str = ""
    made_at: int = 0

    @property
    def ok(self) -> bool:
        return self.res != "none"


def _empty() -> np.ndarray:
    return np.zeros(0)


def _records(ts: np.ndarray, px: np.ndarray, t0: int):
    """Các điểm lập đỉnh mới / đáy mới (tính từ điểm đầu). Thời điểm trả về là ms kể từ t0 (int32)."""
    if len(px) == 0:
        e = np.zeros(0, np.int32)
        return e, _empty(), e, e, _empty(), e
    q = np.arange(len(px), dtype=np.int32)
    rmax = np.maximum.accumulate(px)
    rmin = np.minimum.accumulate(px)
    nh = np.r_[True, rmax[1:] > rmax[:-1]]
    nl = np.r_[True, rmin[1:] < rmin[:-1]]
    off = (ts - t0).astype(np.int32)
    return off[nh], px[nh], q[nh], off[nl], px[nl], q[nl]


class PathDB:
    """Cache đường giá: SQLite, mỗi lệnh 1 dòng (pickle nén zlib) – ghi dần, chỉ đọc lệnh cần dùng."""

    def __init__(self, path):
        import sqlite3
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path), timeout=60, check_same_thread=False)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("CREATE TABLE IF NOT EXISTS paths (key TEXT PRIMARY KEY, data BLOB)")
        self.lock = threading.Lock()

    def get_many(self, keys: list[str]) -> dict[str, "PricePath"]:
        import zlib
        out = {}
        with self.lock:
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                q = f"SELECT key, data FROM paths WHERE key IN ({','.join('?' * len(chunk))})"
                for k, blob in self.con.execute(q, chunk):
                    try:
                        out[k] = pickle.loads(zlib.decompress(blob))
                    except Exception:  # noqa: BLE001 - bản cache cũ/hỏng: trích lại
                        pass
        return out

    def put_many(self, items: list["PricePath"]):
        import zlib
        if not items:
            return
        rows = [(p.key, zlib.compress(pickle.dumps(p, protocol=pickle.HIGHEST_PROTOCOL), 6)) for p in items]
        with self.lock:
            self.con.executemany("INSERT OR REPLACE INTO paths (key, data) VALUES (?, ?)", rows)
            self.con.commit()


def _bar_events(bars: pd.DataFrame, side: int, after_t: int, entry_minute_partial: bool):
    """Chuỗi giá giả lập từ nến 1m theo kịch bản bất lợi: mỗi phút O -> cực trị bất lợi -> cực trị thuận lợi -> C.
    Phút chứa thời điểm vào lệnh: chỉ biết chắc phần bất lợi (có thể đã xảy ra trước khi vào) -> giữ cực trị bất lợi."""
    ts, px = [], []
    for bt, o, h, l, c in zip(bars.ts.to_numpy(), bars.o.to_numpy(), bars.h.to_numpy(), bars.l.to_numpy(),
                              bars.c.to_numpy()):
        adv, fav = (h, l) if side == -1 else (l, h)
        if bt <= after_t < bt + MIN_MS:
            if entry_minute_partial:
                ts += [after_t + 1, bt + MIN_MS - 1]
                px += [adv, c]
            continue
        if bt < after_t:
            continue
        ts += [bt, bt + 15_000, bt + 30_000, bt + 45_000]
        px += [o, adv, fav, c]
    return np.asarray(ts, np.int64), np.asarray(px, np.float64)


class PathBuilder:
    """Trích & cache đường giá cho một sàn."""

    def __init__(self, exchange: str, entry_mode: str = "auto", actual_tp: float | None = 1.0,
                 actual_sl: float | None = None, workers: int = 8):
        self.exchange = exchange.lower()
        self.entry_mode = entry_mode            # auto | csv | tick
        self.actual_tp = actual_tp              # TP% bot thật đã dùng (để suy giá vào của lệnh chốt TP)
        self.actual_sl = actual_sl              # SL% bot thật đã dùng (để suy giá vào của lệnh SL)
        self.trust_csv = False
        self.store = store_for(self.exchange, workers)
        self.db = PathDB(PATH_CACHE / f"{self.exchange}.sqlite")
        self._fund: dict[str, pd.DataFrame] = {}

    def key(self, row) -> str:
        e = row.entry_csv if self.entry_mode != "tick" and pd.notna(row.entry_csv) else ""
        x = f"{row.reason}|{row.exit_csv}|{row.end_ms}|{self.actual_tp}|{self.actual_sl}|v2" if self.entry_mode == "auto" else ""
        return f"{row.coin}|{int(row.start_ms)}|{int(row.side)}|{e}|{self.entry_mode}|{x}"

    def _fresh(self, p: PricePath | None, window_ms: int) -> bool:
        if p is None or p.window_ms < window_ms:
            return False
        if p.res == "tick":
            return True
        # thiếu tick (sàn chưa công bố dump) / không có dữ liệu: dùng lại trong 1 giờ, sau đó thử trích lại
        return now_ms() - p.made_at < 3_600_000

    # ------------------------------------------------------------------ main
    def build(self, trades: pd.DataFrame, window_ms: int, funding: bool = True, progress=None) -> list[PricePath]:
        """Đường giá cho từng dòng của `trades` (theo thứ tự dòng)."""
        keys = [self.key(r) for r in trades.itertuples()]
        if progress:
            progress("Đọc cache đường giá", 0, 0)
        cache = self.db.get_many(list(dict.fromkeys(keys)))
        todo = [i for i, k in enumerate(keys) if not self._fresh(cache.get(k), window_ms)]
        if todo:
            self._prefetch(trades.iloc[todo], window_ms, progress)
            self.trust_csv = self._csv_trust(trades.iloc[todo]) if self.entry_mode == "auto" else False
            if funding:
                if progress:
                    progress("Tải lịch sử funding", 0, 0)
                self._load_funding(trades.iloc[todo], window_ms)
            order = sorted(todo, key=lambda i: (trades.iloc[i].coin, trades.iloc[i].start_ms))
            pending: list[PricePath] = []
            t_save = time.time()
            try:
                for n, i in enumerate(order, 1):
                    if PROGRESS.cancel.is_set():
                        raise Cancelled("Đã huỷ")
                    row = trades.iloc[i]
                    try:
                        p = self._extract(row, keys[i], window_ms, funding)
                    except Cancelled:
                        raise
                    except Exception as e:  # noqa: BLE001
                        log.warning("Lỗi trích đường giá %s %s: %s", row.coin, row.start_ms, e)
                        p = PricePath(keys[i], row.coin, int(row.side), int(row.start_ms), float("nan"),
                                      "", "none", window_ms, 0, note=f"lỗi: {e}", made_at=now_ms())
                    cache[keys[i]] = p
                    pending.append(p)
                    if progress:
                        progress("Trích đường giá từ tick", n, len(order))
                    if time.time() - t_save > 20:
                        self.db.put_many(pending)
                        pending, t_save = [], time.time()
            finally:
                self.db.put_many(pending)
        return [cache[k] for k in keys]

    def _sym(self, coin: str):
        si = self.store.resolve(coin)
        return None if si is None else si.symbol

    def _prefetch(self, trades: pd.DataFrame, window_ms: int, progress):
        items = []
        ex = self.store.ex
        for r in trades.itertuples():
            s = self._sym(r.coin)
            if s is None:
                continue
            a, b = int(r.start_ms) - ANCHOR_BEFORE_MS, int(r.start_ms) + window_ms + LOOKAHEAD_MS
            c = ex.tick_chunk_start(a)
            while c < b:
                items.append((s, c))
                c += ex.tick_chunk_ms
        if progress:
            progress("Tải dữ liệu tick (lần đầu, có cache)", 0, 0)
        self.store.prefetch_ticks(items)

    def _csv_trust(self, trades: pd.DataFrame, n: int = 40) -> bool:
        """Cột Entry của file có phải giá khớp thật không? Kiểm tra mẫu n lệnh: có tick đúng giá đó quanh giây vào lệnh."""
        t = trades[trades.entry_csv.notna() & (trades.entry_csv > 0)]
        if not len(t):
            return False
        idx = np.unique(np.linspace(0, len(t) - 1, min(n, len(t))).astype(int))
        hit = tot = 0
        for r in t.iloc[idx].itertuples():
            s = self._sym(r.coin)
            if s is None:
                continue
            v = self.store.ticks(s, int(r.start_ms) - ANCHOR_BEFORE_MS, int(r.start_ms) + ANCHOR_AFTER_MS)
            if v is None or not len(v[0]):
                continue
            tot += 1
            hit += bool(np.min(np.abs(v[1] / r.entry_csv - 1)) <= ENTRY_TOL)
        ok = tot > 0 and hit / tot >= TRUST_RATIO
        log.info("Cột Entry khớp tick ở %d/%d lệnh mẫu -> %s", hit, tot,
                 "dùng giá Entry của file" if ok else "không phải giá khớp, suy giá vào từ TP / tick")
        return ok

    def _load_funding(self, trades: pd.DataFrame, window_ms: int):
        a = int(trades.start_ms.min()) - DAY_MS
        b = int(trades.start_ms.max()) + window_ms + DAY_MS
        for coin in trades.coin.unique():
            s = self._sym(coin)
            if s is None or s in self._fund:
                continue
            try:
                self._fund[s] = self.store.funding(s, a, b)
            except Exception as e:  # noqa: BLE001
                log.warning("Không tải được funding %s: %s", s, e)
                self._fund[s] = None

    # ------------------------------------------------------------------ ticks theo khối, dừng ở khối thiếu
    def _ticks_upto(self, sym: str, a: int, b: int):
        ex = self.store.ex
        c = ex.tick_chunk_start(a)
        parts_t, parts_p, end = [], [], a
        while c < b:
            lo, hi = max(a, c), min(b, c + ex.tick_chunk_ms)
            v = self.store.ticks(sym, lo, hi)
            if v is None:
                break
            parts_t.append(v[0])
            parts_p.append(v[1])
            end = hi
            c += ex.tick_chunk_ms
        if not parts_t:
            return None, a
        return (np.concatenate(parts_t), np.concatenate(parts_p)), end

    def _bars(self, sym: str, a: int, b: int) -> pd.DataFrame:
        b = min(b, now_ms() - now_ms() % MIN_MS)
        if b <= a:
            return pd.DataFrame({"ts": [], "o": [], "h": [], "l": [], "c": []})
        return self.store.bars_range(sym, a - a % MIN_MS, b)

    def _extract(self, row, key: str, window_ms: int, funding: bool) -> PricePath:
        side = int(row.side)
        st = int(row.start_ms)
        sym = self._sym(row.coin)
        if sym is None:
            return PricePath(key, row.coin, side, st, float("nan"), "", "none", window_ms, 0,
                             note=f"không tìm thấy symbol {row.coin} trên {self.exchange}", made_at=now_ms())
        csv_px = float(row.entry_csv) if pd.notna(row.entry_csv) and row.entry_csv > 0 else None
        end_need = st + window_ms + LOOKAHEAD_MS
        v, tick_end = self._ticks_upto(sym, st - ANCHOR_BEFORE_MS, end_need)

        entry_t, entry_px, src, res = st, None, "", "tick"
        ts = px = None
        note = ""
        if v is not None and len(v[0]):
            ts_all, px_all = v
            w0 = int(np.searchsorted(ts_all, st - ANCHOR_BEFORE_MS, "left"))
            w1 = int(np.searchsorted(ts_all, st + ANCHOR_AFTER_MS, "left"))
            anchor = None

            def nearest(price, tol):
                """Tick có giá gần `price` nhất quanh giây vào lệnh (nhiều tick cùng giá: lấy tick gần giữa giây)."""
                if w1 <= w0:
                    return None, None
                d = np.abs(px_all[w0:w1] / price - 1)
                best = float(d.min())
                if best > tol:
                    return None, best
                cand = np.flatnonzero(d <= best + 1e-12) + w0
                j = cand[np.argmin(np.abs(ts_all[cand] - (st + MID_SECOND_MS)))]
                return int(j), best

            if csv_px and (self.entry_mode == "csv" or (self.entry_mode == "auto" and self.trust_csv)):
                j, best = nearest(csv_px, ENTRY_TOL if self.entry_mode == "auto" else 1.0)
                if j is not None:
                    anchor, entry_px, src = j, csv_px, "csv"
                    if best > ENTRY_TOL:
                        note = f"giá Entry file lệch tick {best * 100:.2f}%"
            if anchor is None and self.entry_mode == "auto" and self.actual_tp and row.reason == "TP" \
                    and pd.notna(row.exit_csv) and row.exit_csv > 0:
                # short: thoát = vào × (1 − TP%)  ;  long: thoát = vào × (1 + TP%)
                implied = float(row.exit_csv) / (1 + side * self.actual_tp / 100.0)
                j, _ = nearest(implied, ENTRY_TOL)
                if j is not None:
                    anchor, entry_px, src = j, implied, "tp"
            if anchor is None and self.entry_mode == "auto" and not self.trust_csv and self.actual_sl \
                    and row.reason == "SL" and pd.notna(row.exit_csv) and row.exit_csv > 0:
                # SL kích hoạt ở vào × (1 ± SL%), khớp market (có thể trượt) -> neo vào tick gần giá suy ra nhất
                implied = float(row.exit_csv) / (1 - side * self.actual_sl / 100.0)
                j, _ = nearest(implied, SL_TOL)
                if j is not None:
                    anchor, entry_px, src = j, float(px_all[j]), "sl"
            if anchor is None:
                k = int(np.searchsorted(ts_all, st + MID_SECOND_MS, "left"))
                if k < len(ts_all) and ts_all[k] < st + LOOKAHEAD_MS:
                    anchor, entry_px, src = k, float(px_all[k]), "tick"
                    if csv_px and self.entry_mode == "csv":
                        entry_px, src = csv_px, "csv"
            if anchor is not None and src in ("tick", "sl") and self.entry_mode == "auto" and self.actual_tp \
                    and pd.notna(row.end_ms):
                # Giá vào ước lượng phải phù hợp kết quả thật: lệnh thật không chốt TP -> giá chưa chạm TP trước lúc
                # đóng; lệnh thật chốt TP -> giá đã chạm TP trước lúc đóng. Nếu không, dời giá vào về biên gần nhất.
                # (Không làm vậy thì sai số giá vào của lệnh SL/hết giờ biến thành "TP giả" – lệch có hệ thống.)
                tp = self.actual_tp / 100.0
                if row.reason == "TP":
                    b = int(np.searchsorted(ts_all, int(row.end_ms) + 1_000, "right"))
                else:
                    b = int(np.searchsorted(ts_all, int(row.end_ms) - 3_000, "left"))
                seg = px_all[anchor + 1:b]
                if len(seg):
                    if side == -1:
                        bound = seg.min() / (1 - tp)          # TP chạm khi giá <= vào × (1 − tp)
                        bad = entry_px >= bound if row.reason != "TP" else entry_px < bound
                    else:
                        bound = seg.max() / (1 + tp)
                        bad = entry_px <= bound if row.reason != "TP" else entry_px > bound
                    if bad:
                        adj = bound if row.reason == "TP" else bound * (1 + 1e-6 * side)
                        note = f"giá vào chỉnh {(adj / entry_px - 1) * 100:+.3f}% cho khớp kết quả thật"
                        entry_px = float(adj)
                        src = src + "*"
            if anchor is not None:
                entry_t = int(ts_all[anchor])
                cut = int(np.searchsorted(ts_all, entry_t + window_ms + LOOKAHEAD_MS, "left"))
                ts, px = ts_all[anchor + 1:cut], px_all[anchor + 1:cut]
        if ts is None:
            # không có tick quanh lúc vào lệnh -> nến 1m
            res = "1m"
            bars = self._bars(sym, st - MIN_MS, end_need)
            m0 = bars[(bars.ts <= st) & (bars.ts > st - MIN_MS)]
            if not len(m0):
                return PricePath(key, sym, side, st, float("nan"), "", "none", window_ms, 0,
                                 note="không có dữ liệu giá lúc vào lệnh", made_at=now_ms())
            b0 = m0.iloc[0]
            if csv_px and (self.entry_mode == "csv" or (self.entry_mode == "auto" and self.trust_csv
                                                        and b0.l * (1 - ENTRY_TOL) <= csv_px <= b0.h * (1 + ENTRY_TOL))):
                entry_px, src = csv_px, "csv"
            else:
                entry_px, src = float(b0.c), "1m"
                note = "không có tick: giá vào = giá đóng nến 1m"
            ts, px = _bar_events(bars, side, st, True)
            data_end = int(bars.ts.max()) + MIN_MS if len(bars) else st
        else:
            data_end = tick_end
            if tick_end < end_need:
                bars = self._bars(sym, tick_end, end_need)
                bt, bp = _bar_events(bars, side, tick_end - 1, False)
                if len(bt):
                    res = "mixed"
                    ts, px = np.concatenate([ts, bt]), np.concatenate([px, bp])
                    data_end = int(bars.ts.max()) + MIN_MS
        n = len(ts)
        hi_t, hi_px, hi_q, lo_t, lo_px, lo_q = _records(ts, px, entry_t)
        k = window_ms // MIN_MS
        grid = entry_t + np.arange(0, k + 1, dtype=np.int64) * MIN_MS
        idx = np.searchsorted(ts, grid, "left")
        samp = np.full(k + 1, np.nan)
        ok = idx < n
        samp[ok] = px[idx[ok]]
        samp[0] = entry_px
        if n:
            # sau tick cuối (không có giao dịch nào sau mốc trong 10 phút) -> giá giao dịch gần nhất
            last = np.searchsorted(ts, grid, "right") - 1
            fill = (~ok) & (last >= 0) & (grid <= data_end)
            samp[fill] = px[last[fill]]
        f_t = f_r = _empty()
        if funding:
            fdf = self._fund.get(sym)
            if fdf is not None and len(fdf):
                ft = fdf["ts"].to_numpy()
                ft = ft - ft % 1000
                m = (ft > entry_t) & (ft <= entry_t + window_ms)
                f_t, f_r = ft[m].astype(np.int64), fdf["rate"].to_numpy()[m].astype(np.float64)
        return PricePath(key, sym, side, entry_t, float(entry_px), src, res, window_ms, int(data_end),
                         hi_t, hi_px, hi_q, lo_t, lo_px, lo_q, samp.astype(np.float32), f_t, f_r,
                         n_ticks=n, note=note, made_at=now_ms())
