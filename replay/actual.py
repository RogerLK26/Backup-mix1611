"""Đọc file lịch sử lệnh thật (xuất từ bot) về một bảng chuẩn.

Hỗ trợ cả 2 định dạng hiện có (Bybit: "Chiến lược / Phí / PNL / Lý do", OKX: "Cấu hình / Phí GD / Total PNL /
Lý do dừng"), dòng đầu "sep=," tuỳ chọn. Thời gian trong file là giờ địa phương (mặc định Asia/Ho_Chi_Minh, UTC+7 –
đã kiểm tra khớp tick thật ở cả Bybit và OKX).
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import numpy as np
import pandas as pd

TZ_DEFAULT = "Asia/Ho_Chi_Minh"

# tên cột chuẩn -> các tên có thể gặp trong file
COLS = {
    "no": ["#", "STT", "No"],
    "coin": ["Cặp", "Coin", "Symbol", "Pair"],
    "side": ["Hướng", "Side", "Direction"],
    "strategy": ["Chiến lược", "Cấu hình", "Strategy", "Config"],
    "account": ["Tài khoản", "Account"],
    "start": ["Bắt đầu", "Start", "Open time"],
    "end": ["Kết thúc", "End", "Close time"],
    "entry": ["Entry", "Giá vào"],
    "exit": ["Exit", "Giá ra"],
    "margin": ["Ký quỹ (USDT)", "Margin", "Ký quỹ"],
    "leverage": ["Đòn bẩy", "Leverage"],
    "fee": ["Phí (USDT)", "Phí GD (USDT)", "Fee"],
    "funding": ["Funding (USDT)", "Funding"],
    "pnl": ["PNL (USDT)", "Total PNL (USDT)", "PnL", "PNL"],
    "reason": ["Lý do", "Lý do dừng", "Reason"],
}

REASON_MAP = {"tp_hit": "TP", "sl_hit": "SL", "max_runtime_reached": "TIMEOUT", "manually_stopped": "MANUAL",
              "liquidated": "LIQ"}


def detect_exchange(name: str) -> str | None:
    n = name.lower()
    for ex in ("bybit", "okx", "binance", "bitget", "gate", "mexc"):
        if ex in n:
            return ex
    return None


def _read_raw(src) -> pd.DataFrame:
    if isinstance(src, (bytes, bytearray)):
        text = bytes(src).decode("utf-8-sig", errors="replace")
    else:
        text = Path(src).read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    if lines and lines[0].strip().lower().startswith("sep="):
        lines = lines[1:]
    return pd.read_csv(io.StringIO("\n".join(lines)))


def _pick(df: pd.DataFrame, key: str):
    for c in COLS[key]:
        if c in df.columns:
            return df[c]
    return None


def _num(s) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def load_actual(src, tz: str = TZ_DEFAULT) -> pd.DataFrame:
    """Trả về DataFrame chuẩn (mỗi dòng 1 lệnh thật), sắp xếp theo thời gian vào lệnh.

    Cột: id, coin, side(+1 long/-1 short), strategy, start_ms, end_ms (UTC ms), entry_csv, exit_csv, margin, leverage,
    notional, fee, funding, pnl, reason (TP/SL/TIMEOUT/MANUAL), hold_min.
    """
    raw = _read_raw(src)
    need = ["coin", "side", "start"]
    miss = [k for k in need if _pick(raw, k) is None]
    if miss:
        raise ValueError(f"File thiếu cột bắt buộc: {miss}. Các cột có: {list(raw.columns)}")
    out = pd.DataFrame()
    no = _pick(raw, "no")
    out["no"] = _num(no).astype("Int64") if no is not None else np.arange(1, len(raw) + 1)
    out["coin"] = _pick(raw, "coin").astype(str).str.strip().str.upper()
    side = _pick(raw, "side").astype(str).str.upper().str.strip()
    out["side"] = np.where(side.str.startswith("L") | side.str.startswith("B"), 1, -1)
    for k in ("strategy", "account"):
        s = _pick(raw, k)
        out[k] = s.astype(str) if s is not None else ""

    def to_ms(s):
        if s is None:
            return pd.Series([pd.NA] * len(raw), dtype="Int64")
        dt = pd.to_datetime(s, dayfirst=True, errors="coerce")
        dt = dt.dt.tz_localize(tz, ambiguous="NaT", nonexistent="NaT").dt.tz_convert("UTC")
        ms = (dt - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(milliseconds=1)
        return pd.Series(ms, dtype="Int64").where(dt.notna())

    out["start_ms"] = to_ms(_pick(raw, "start"))
    out["end_ms"] = to_ms(_pick(raw, "end"))
    for k, col in (("entry", "entry_csv"), ("exit", "exit_csv"), ("margin", "margin"), ("leverage", "leverage"),
                   ("fee", "fee"), ("funding", "funding"), ("pnl", "pnl")):
        s = _pick(raw, k)
        out[col] = _num(s) if s is not None else np.nan
    out["notional"] = out["margin"] * out["leverage"]
    r = _pick(raw, "reason")
    out["reason_raw"] = r.astype(str) if r is not None else ""
    out["reason"] = out["reason_raw"].map(lambda x: REASON_MAP.get(str(x).strip().lower(), str(x).upper()))
    out["hold_min"] = (out["end_ms"] - out["start_ms"]) / 60000.0
    out = out[out["start_ms"].notna()].copy()
    out["start_ms"] = out["start_ms"].astype("int64")
    out = out.sort_values(["start_ms", "coin"], kind="stable").reset_index(drop=True)
    out["id"] = np.arange(len(out))
    return out


def summarize_actual(df: pd.DataFrame) -> dict:
    """Tóm tắt nhanh để hiển thị trên giao diện."""
    if not len(df):
        return {"n": 0}
    rc = df["reason"].value_counts().to_dict()
    strat = df["strategy"].value_counts().index[0] if "strategy" in df else ""
    guess = guess_config(df)
    return {
        "n": int(len(df)),
        "coins": int(df["coin"].nunique()),
        "start_ms": int(df["start_ms"].min()),
        "end_ms": int(df["start_ms"].max()),
        "reasons": {k: int(v) for k, v in rc.items()},
        "pnl": float(df["pnl"].sum(skipna=True)),
        "strategy": strat,
        "sides": {("LONG" if k == 1 else "SHORT"): int(v) for k, v in df["side"].value_counts().items()},
        "guess": guess,
    }


def guess_config(df: pd.DataFrame, entry: pd.Series | None = None) -> dict:
    """Đoán cấu hình thật (TP%, SL%, thời gian giữ tối đa) từ dữ liệu, để tool chạy lại & so khớp.
    entry: giá vào thật (vd lấy từ tick) nếu cột Entry của file không đáng tin (OKX)."""
    g: dict = {}
    ent = df["entry_csv"] if entry is None else entry
    mv = (df["exit_csv"] / ent - 1) * 100 * df["side"]
    tp = mv[df["reason"] == "TP"]
    sl = mv[df["reason"] == "SL"]
    if len(tp) >= 3:
        g["tp"] = float(round(tp.median(), 2))
    if len(sl) >= 3:
        g["sl"] = float(round(-sl.median(), 1))
    to = df.loc[df["reason"] == "TIMEOUT", "hold_min"]
    if len(to):
        g["hold_min"] = int(round(to.median()))
    m = re.search(r"1\s*:\s*(\d+(?:\.\d+)?)", str(df["strategy"].iloc[0]))
    if m and "tp" in g:
        g["sl_from_name"] = float(m.group(1)) * g["tp"]
    return g
