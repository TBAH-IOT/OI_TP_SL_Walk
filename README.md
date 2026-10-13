# OI_WALK

Package tối giản gồm một file code `OI_TP_SL_WALK.py` và snapshot data cần thiết để chạy.

Logic:

- Tín hiệu OI tại nến `t`.
- Vào lệnh tại `open` nến `t+1`.
- TP `1%`, SL `0.1%`, phí round-trip `0.1%`.
- Train từ `2026-05-01`, test từ `2026-06-01`.
- Mặc định chọn top 20 coin.
- Kết quả chỉ gồm TP/SL và Sharpe theo lợi nhuận ngày.

## Chạy

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python .\OI_TP_SL_WALK.py
```

Có thể thay đổi số coin hoặc khoảng thời gian:

```powershell
python .\OI_TP_SL_WALK.py --top-n 20 --train-start 2026-05-01 --start 2026-06-01
```

## Dữ liệu đi kèm

Repository có sẵn snapshot OI và OHLC Hyperliquid cần thiết để chạy ngay. Các file lớn được lưu bằng Git LFS. Không cần API key để chạy snapshot này.
