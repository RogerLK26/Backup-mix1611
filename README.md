# Backtest Mix – backtest chiến lược **và** replay lệnh thật trong một tool

Bản gộp của hai tool:

| Lấy từ | Phần được giữ |
|---|---|
| [Backtest-Strategy](https://github.com/RogerLK26/Backtest-Strategy) | Toàn bộ engine backtest thật: tải dữ liệu 6 sàn, quét tín hiệu, khớp lệnh tới từng tick, quản lý rủi ro, quét hàng nghìn chiến lược (`backtester/`), form cấu hình đầy đủ, tiến trình "đang chạy" + hàng chờ |
| [Backtest-actual-0611](https://github.com/RogerLK26/Backtest-actual-0611) | Giao diện gọn 1 trang: bảng xếp hạng (lọc kiểu Excel, yêu thích theo nhóm, chỉ số ổn định, "8 giờ tốt nhất"), heatmap TP × SL, chi tiết từng cấu hình tính ngay khi bấm, lịch sử chạy – và nguyên chế độ replay lệnh thật (`replay/`) |

Kết quả: **chạy backtest thật như tool 1, nhưng xem kết quả theo cách của tool 2** – và vẫn replay được lệnh thật từ file CSV
của bot. Hai chế độ dùng chung bảng xếp hạng, heatmap, trang chi tiết, lịch sử, nhóm yêu thích, bộ lọc đã lưu và **chung một
cache dữ liệu sàn** (tải tick một lần, cả hai chế độ dùng được).

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m btmix web          # http://127.0.0.1:8000
```

Trên Windows có thể bấm thẳng `run_web.bat`.

## Hai chế độ

Nút chuyển ở góc trên bên phải.

### 1. Backtest chiến lược (dữ liệu sàn)

"Coin biến động x% trong nến 1m/3m/5m/… → vào lệnh Long/Short" trên **Binance, Bybit, OKX, Bitget, Gate, MEXC**, mô phỏng
khớp lệnh tới **từng giao dịch (tick, mili-giây)** nên TP/SL xảy ra dưới 1 phút vẫn chính xác.

Form cấu hình đủ như bản gốc, chia theo mục:

1. **Chiến lược, sàn & giai đoạn** – tên, nhiều sàn cùng lúc, từ/đến, múi giờ, vốn ban đầu, độ phân giải (tick / 1m), cách
   mô phỏng khi chỉ có nến 1m.
2. **Lọc coin** – chỉ chạy / loại trừ coin, volume 24h (rolling, đúng tới phút có tín hiệu), vốn hoá (CoinGecko), đòn bẩy tối
   đa sàn cho phép.
3. **Tín hiệu** – nhiều rule (HOẶC): khung nến, biến động ≥ (và ≤), chiều, kích hoạt chạm/đóng nến, vào lệnh thuận/ngược,
   **chờ giá hồi** (hồi x% từ đỉnh trong y phút).
4. **Vào lệnh & vị thế** – độ trễ bot (ms), trượt giá, ký quỹ (USDT hoặc % số dư), đòn bẩy.
5. **Thoát lệnh & phí** – TP/SL theo % giá hoặc % ROE, lệnh TP limit/market, cách khớp limit, trượt giá SL / đóng hết giờ,
   thanh lý isolated, phí maker/taker, funding thật.
6. **Giới hạn rủi ro** – lệnh mở đồng thời, lệnh/coin, max lệnh–TP–SL mỗi ngày (và mỗi coin mỗi ngày), nghỉ coin sau TP/SL,
   nghỉ cả chiến lược sau SL / sau N SL liên tiếp, giới hạn lỗ ngày, thời gian chạy tối đa, giờ bắt đầu ngày.
7. **Lịch chạy** – khung giờ vào lệnh (hỗ trợ qua đêm `22:00-02:00`), ngày trong tuần.
8. **Lưới cấu hình & xếp hạng** – TP / SL / thời gian giữ lệnh dạng danh sách `1, 1.5, 2` hoặc khoảng `0.5-3:0.25`; quét thêm
   tham số khác (đòn bẩy, biến động tín hiệu, giới hạn…); tiêu chí xếp hạng, bỏ qua cấu hình ít lệnh / sụt vốn quá mức,
   kiểm tra ngoài mẫu, số cấu hình tối đa.

Cấu hình lưu thành file YAML trong `configs/` (Lưu / Lưu thành mới / Xoá / Xuất – Nhập YAML), bản nháp tự lưu trên trình duyệt.

**Chạy nhiều sàn một lượt**: mỗi sàn là một "chế độ" trong bảng xếp hạng, so sánh cạnh nhau như chế độ 1 lệnh/coin vs nhiều
lệnh/coin của replay.

### 2. Replay lệnh thật (file CSV của bot)

Giữ nguyên bản gốc: lấy **chính các lệnh bot đã vào thật** (coin, chiều, đúng mili-giây, đúng giá) rồi hỏi *"cùng điểm vào đó,
nếu TP 2% / SL 5% / giữ 1h thì sao?"*, kèm phần **kiểm định** (so lý do thoát, thời điểm, giá, PnL với kết quả thật) và **tự
hiệu chỉnh trượt giá** từ lệnh thật. Đặt file CSV vào `Actual Performance/` hoặc bấm *Tải file…*.

## Kết quả (dùng chung cho cả 2 chế độ)

| Tab | Nội dung |
|---|---|
| **Tổng quan** | Cấu hình tốt nhất mỗi sàn / mỗi chế độ (replay: thêm cột *Thực tế* và khung *Kiểm định*), và vì sao tín hiệu bị bỏ (theo từng giới hạn) |
| **Bảng xếp hạng** | Toàn bộ cấu hình đã chạy: sắp xếp, lọc theo cột kiểu Excel, lưu bộ lọc đã đặt tên, ♥ nhóm yêu thích, chỉ số **ổn định** (R² đường vốn, % ngày lãi, net khi bỏ coin lãi nhất, net TB các ô lân cận), nhóm cột **8 giờ tốt nhất**, tải CSV |
| **Heatmap TP × SL** | Mỗi ô là một cấu hình; chọn chỉ số (net, net/DD, win rate, PF, ổn định, 8h tốt nhất…); bấm ô để mở chi tiết |
| **Chi tiết cấu hình** | Bấm **dòng bất kỳ** là có ngay: KPI, đường vốn, lãi/lỗ theo ngày, theo giờ vào lệnh, theo coin, bảng từng lệnh (tới mili-giây), tải CSV |

**Lọc theo giờ vào lệnh** trong trang chi tiết: với chế độ backtest, tool **chạy lại sổ lệnh** với đúng khung giờ đó (như bot chỉ
chạy các giờ này) chứ không chỉ lọc bớt dòng – nên số lệnh có thể tăng khi các lệnh ngoài giờ không còn chiếm chỗ.

### Lịch sử

Mỗi lượt chạy xong lưu vào `results/history/<thời điểm>/` (trên Railway: trong Volume): tóm tắt, bảng xếp hạng đầy đủ, cấu hình
đã nhập, bản sao cấu hình YAML (backtest) hoặc bản sao file lệnh thật (replay). Trong khung *Lịch sử chạy*: **Xem** lại kết quả,
**Dùng lại** cấu hình (điền lại lên form), 🗑 xoá. Xem chi tiết từng lệnh của lượt cũ sau khi máy chủ khởi động lại thì tool dựng
lại từ bản sao + cache (replay: vài giây; backtest: quét lại tín hiệu, dữ liệu đã có cache).

### Hàng chờ

Bấm Chạy khi đang có lượt chạy → lượt mới vào **hàng chờ** (đổi thứ tự / bỏ được, còn nguyên sau khi máy chủ khởi động lại).

## Chạy bằng dòng lệnh

```bash
python -m btmix bt configs/pump_short.yaml --exchange binance,bybit
python -m btmix replay "Actual Performance/Data Bybit.csv" --tp 0.5-3:0.25 --sl 3,5,10 --hold 30,60,120 --mode single,multi
python -m btmix prepare "Actual Performance/Data OKX.csv"      # chỉ tải trước tick
python -m btmix cache                                          # xem / xoá dữ liệu đã tải
```

Vẫn dùng được CLI gốc của engine: `python -m backtester run configs/pump_short.yaml` (xuất `report.html`, `trades.csv`,
`summary.json`… như bản cũ).

## Deploy lên Railway

Có sẵn `requirements.txt`, `railway.json`, `Procfile`, `.python-version`: Railway cài thư viện rồi chạy
`python -m btmix web --no-browser`, nghe ở `0.0.0.0:$PORT`, health check `/api/health`.

| Biến môi trường | Bắt buộc | Ý nghĩa |
|---|---|---|
| `BACKTEST_PASSWORD` | **Có** | Mật khẩu đăng nhập (tên đăng nhập mặc định `admin`, đổi bằng `BACKTEST_USER`) |
| `DATA_DIR` | Không | Thư mục dữ liệu (cache, kết quả, cấu hình, file lệnh thật). Không đặt thì tự dùng Volume đang gắn (`RAILWAY_VOLUME_MOUNT_PATH`) |
| `TICK_RAM_LIMIT` | Không | Số tick giữ trong RAM (mặc định 30 triệu ≈ 500 MB); gói ít RAM đặt `5000000` |
| `BARS_RAM_LIMIT` | Không | Số coin-ngày nến 1m giữ trong RAM (mặc định 12000); gói ít RAM đặt `4000` |

- **Gắn Volume** (vd `/data`): ổ đĩa Railway bị xoá mỗi lần deploy – không có Volume thì phải tải lại toàn bộ dữ liệu.
- **Region Singapore hoặc châu Âu**: Binance/Bybit chặn IP Mỹ.

## Cấu trúc

```
backtester/   engine backtest (giữ nguyên từ Backtest-Strategy): data/ (6 sàn + cache), engine/ (tín hiệu, khớp lệnh,
              rủi ro, engine nhanh), config.py, optimize.py, stats.py, report
replay/       engine replay lệnh thật (giữ nguyên từ Backtest-actual-0611): actual.py, paths.py, engine.py, runner.py;
              bt.py nay trỏ thẳng sang backtester/ (không cần vendor, dùng chung cache)
btmix/        phần gộp mới:
   strategy.py  chạy lưới backtest bằng engine nhanh rồi dọn kết quả về đúng khuôn bảng xếp hạng / chi tiết của replay
   jobs.py      hàng chờ, tiến trình, nhật ký, huỷ, lưu lịch sử – chung cho 2 chế độ
   history.py   lịch sử chạy      server.py  web API      static/  giao diện (1 trang)
configs/      cấu hình chiến lược mẫu (YAML)
tests/        unit test: engine đầy đủ, engine nhanh, replay, và lớp gộp (test_btrun.py)
tools/        verify.py – kiểm định độ chính xác bằng cách duyệt từng tick
```

Chạy test: `.venv/Scripts/python -m pytest -q` (không cần mạng, dữ liệu giả lập).

## Độ chính xác

Phần mô phỏng **không bị sửa đổi** khi gộp – vẫn là engine đã kiểm định của hai repo gốc:

- Backtest: `tools/verify.py` duyệt **từng tick** cho mọi lệnh và so với kết quả engine (100% khớp trên Binance, Bybit, OKX,
  Bitget, Gate trong dữ liệu thật tháng 10/2026); engine nhanh cho ra **đúng từng lệnh** như engine đầy đủ (`tests/test_fast.py`),
  và lớp gộp cho ra đúng kết quả của engine nhanh (`tests/test_btrun.py`).
- Replay: so với duyệt brute-force từng tick (`tests/test_replay_engine.py`); các con số kiểm định với lệnh thật (Bybit 682 lệnh,
  OKX 6.028 lệnh) xem phần *Kiểm định* trên giao diện.

Khác biệt duy nhất về số liệu so với bản gốc: cột **8 giờ tốt nhất** ở chế độ backtest chỉ *lọc* các lệnh vào trong 8 giờ đó
(không chạy lại sổ lệnh) để bảng xếp hạng không phải chạy gấp đôi; khi bấm vào ô để mở **chi tiết**, sổ lệnh được chạy lại đúng
với khung giờ đó.
