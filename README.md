# OI_Baseline

Logic:
- Tín hiệu OI tại nến `t`.
- Vào lệnh tại `open` nến `t+1`.
- Buy t+1 -- Sell t+12
- Kết quả gồm Net_profit; Max DD; WInrate; Calmar_Ratio; Sharpe_Ratio.

# OI_DCA_ATR

Logic:
- Tín hiệu OI tại nến `t`.
- Vào lệnh tại `open` nến `t+1`.
- DCA theo ATR -- Các mốc: 0xATR; 1xATR; 2xATR. 
- Kết quả gồm Net_profit; Max DD; WInrate; Calmar_Ratio; Sharpe_Ratio.

# OI_WALK

Logic:

- Tín hiệu OI tại nến `t`.
- Vào lệnh tại `open` nến `t+1`.
- TP `1%`, SL `0.1%`, phí round-trip `0.1%`.
- Train từ `2026-05-01`, test từ `2026-06-01`.
- Chọn top 30 coin.
- Kết quả gồm Net_profit; Max DD; WInrate; Calmar_Ratio; Sharpe_Ratio.

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
