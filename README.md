# OI_WALK — Hyperliquid OI q01 TP/SL walk-forward

Bộ mã này chạy backtest OI q01 trên dữ liệu Open Interest của GFI và OHLC 5m Hyperliquid.

Logic chính:

- Tín hiệu OI được xác định tại nến `t`.
- Vào lệnh tại `open` của nến `t+1`.
- TP mặc định `1%`, SL mặc định `0.1%`.
- Tổng phí round-trip mặc định `0.1%`.
- Walk-forward: train từ `2026-05-01`, test từ `2026-06-01`.
- Mặc định chọn top 20 coin theo median OI trong giai đoạn train.
- OHLC dùng nguồn Hyperliquid qua CoinGlass; TON bị loại vì không có instrument Hyperliquid phù hợp.
- Kết quả chỉ hiển thị strategy `TP_SL`, kèm Sharpe theo lợi nhuận thực nhận hằng ngày.

## Cài đặt

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 1. Tạo cache OI GFI

Không ghi API key vào code hoặc Git. Đặt key trong biến môi trường:

```powershell
$env:NI_API_KEY = "YOUR_GFI_API_KEY"
python .\Run_GFI_OI_30Coins_5m.py --start 2026-04-24T00:00:00Z --cache-dir .\data\gfi_oi_5m_30coins_cache
```

Script cũng chấp nhận biến `GFI_NI_API_KEY`. Có thể dùng `--reuse-cache` nếu cache đã tồn tại.

## 2. Tạo cache OHLC Hyperliquid

```powershell
$env:COINGLASS_API_KEY = "YOUR_COINGLASS_API_KEY"
python .\Fetch_Hyperliquid_OHLC_CoinGlass.py --start 2026-04-30T22:00:00Z --end 2026-10-05T11:50:00Z --output-dir .\data\hl_ohlc_5m
```

## 3. Chạy walk-forward

```powershell
python .\OI_TP_SL_WALK.py
```

Kết quả được ghi vào:

```text
data/oi_tp_sl_walk_hyperliquid_close_entry_open_t1_top20_train_may_test_june/
```

Muốn thay đổi số coin hoặc khoảng thời gian:

```powershell
python .\OI_TP_SL_WALK.py --top-n 20 --train-start 2026-05-01 --start 2026-06-01
```

## 4. Mở chart chi tiết

```powershell
python .\OI_TP_SL_WALK_Chart.py
```

Mở `http://127.0.0.1:8765`. Chart hỗ trợ chọn coin, khoảng thời gian, số dư ví ban đầu, xem marker trigger/entry/exit và số dư sau từng lệnh.

## Dữ liệu đi kèm

Repository có kèm snapshot OI và OHLC cần thiết để chạy ngay, được lưu bằng Git LFS vì dung lượng lớn. API key chỉ cần khi muốn cập nhật dữ liệu mới; tuyệt đối không ghi key vào code hoặc commit.
