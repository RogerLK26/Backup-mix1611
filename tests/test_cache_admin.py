"""Unit test thống kê / xoá cache (dùng thư mục tạm)."""
from backtester.data import cache_admin as ca


def _mk(p, size=10):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)


def _fixture(tmp_path):
    c = tmp_path / "cache"
    _mk(c / "okx/klines/PEPEUSDT/2026-10-01.parquet", 100)
    _mk(c / "okx/klines/PEPEUSDT/2026-10-09.parquet", 100)
    _mk(c / "okx/ticks/PEPEUSDT/2026-09-30_160000.parquet", 1000)
    _mk(c / "okx/ticks/WIFUSDT/2026-10-09_160000.parquet", 1000)
    _mk(c / "okx/daily/PEPEUSDT.parquet", 5)
    _mk(c / "okx/symbols.json", 7)
    _mk(c / "bybit/ticks/PEPEUSDT/2026-10-01_000000.parquet", 500)
    _mk(c / "coingecko/markets.json", 3)
    return c


def test_summary(tmp_path):
    c = _fixture(tmp_path)
    s = ca.summary(c)
    assert s["size"] == 100 + 100 + 1000 + 1000 + 5 + 7 + 500 + 3 and s["files"] == 8
    okx = next(e for e in s["exchanges"] if e["exchange"] == "okx")
    ticks = next(k for k in okx["kinds"] if k["kind"] == "ticks")
    assert ticks["size"] == 2000 and ticks["symbols"] == 2 and ticks["first"] == "2026-09-30" and ticks["last"] == "2026-10-09"


def test_delete_filters_and_dry_run(tmp_path):
    c = _fixture(tmp_path)
    # xem trước: tick OKX tới hết 2026-10-01
    r = ca.delete(c, exchanges=["okx"], kinds=["ticks"], date_to="2026-10-01")
    assert r == {"files": 1, "size": 1000, "dry_run": True}
    assert (c / "okx/ticks/PEPEUSDT/2026-09-30_160000.parquet").exists()
    # lọc theo coin (không cần hậu tố USDT), mọi sàn, mọi loại
    r = ca.delete(c, symbols=["pepe"], dry_run=False)
    assert r["files"] == 5
    assert not (c / "okx/ticks/PEPEUSDT").exists()          # thư mục rỗng được dọn
    assert (c / "okx/ticks/WIFUSDT/2026-10-09_160000.parquet").exists()
    assert (c / "okx/symbols.json").exists() and (c / "coingecko/markets.json").exists()
    # lọc ngày không đụng tới dữ liệu không theo ngày
    r = ca.delete(c, date_from="2000-01-01", dry_run=False)
    assert r["files"] == 1 and (c / "okx/symbols.json").exists()


def test_leverage_snapshots_protected(tmp_path):
    c = _fixture(tmp_path)
    _mk(c / "okx/leverage/2026-10-20.json", 50)
    assert ca.delete(c, dry_run=False)["files"] == 8                     # xoá "toàn bộ" vẫn giữ bản ghi đòn bẩy
    assert (c / "okx/leverage/2026-10-20.json").exists()
    assert ca.delete(c, kinds=["leverage"], dry_run=False)["files"] == 1
