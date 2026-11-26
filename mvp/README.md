# Bàn giao lõi OI q01 chạy live

Mã này là **lõi chiến thuật áp dụng cho bất kỳ coin nào** có OI USD và giá nến 5 phút đáng tin cậy. Danh sách coin là cấu hình `MVP_SYMBOLS`; không có danh sách cố định trong công thức. Adapter `live_feed.py` minh họa nguồn GFI OI và Hyperliquid OHLC, có thể thay bằng feed của sản phẩm.

## Quy tắc cố định

1. Chỉ dùng nến 5 phút đã đóng. Tính `oi_usd.pct_change()` và phân vị thấp 1% trên tối đa 2.016 thay đổi gần nhất (7 ngày), cần ít nhất 504 thay đổi. Phân vị **bao gồm thay đổi hiện tại** như `check_oi_ls_V2.py`.
2. Phát **LONG** đúng một lần khi OI vừa đi vào trạng thái `change <= q01` (`onset`). OI thiếu, không hợp lệ hoặc đứt chuỗi 5 phút sẽ ngắt lịch sử liên tục; cần đủ 504 thay đổi mới trước khi phát tiếp. Không dùng L/S, NETPOS, TP, SL, ATR hay DCA.
3. Tín hiệu ở nến `t`: giá vào là **open nến `t+1`**. Giá ra là **close nến `t+12`**; thời điểm khớp ra `t+65 phút`, đúng **60 phút sau thời điểm vào**. Chỉ chốt nếu có đủ 12 nến giá liên tục. Phí round trip giả định 0,1% trong thống kê net; API cũng trả tỷ lệ tăng giá gross.
4. Một coin chỉ có một vị thế mô phỏng đang mở. Hệ thống phát tín hiệu, **không đặt lệnh thật**.

Các hàm thuần, không phụ thuộc coin hoặc API, ở `strategy.py`: `oi_drop_onset`, `schedule`, `gross_return`.

## Hợp đồng dữ liệu cho dev

Giữ một worker duy nhất cho SQLite tham chiếu. Mỗi feed coin có hai dòng dữ liệu theo cùng `symbol` và mốc UTC đầu nến:

- OI USD 5 phút (`oi_usd`) từ GFI hoặc nguồn tương đương. Nếu chỉ có OI theo số lượng hợp đồng, adapter phải quy đổi và kiểm chứng đơn vị trước khi gửi; không âm thầm thay thước đo tín hiệu.
- OHLC 5 phút của thị trường sẽ dùng để thực thi. Adapter mẫu lấy từ Hyperliquid. Dữ liệu giá luôn được gửi kể cả khi OI thiếu, để vị thế đang mở có thể chốt sau 60 phút.

`POST /v1/bar-close` với Bearer `MVP_INGEST_TOKEN` khi nến **đã đóng**:

```json
{"symbol":"BTC","ts":"2026-11-26T09:00:00Z","oi_usd":2200000000,"mark_close":60500,"open":60490,"high":60520,"low":60480,"close":60510,"historical":false}
```

Nếu OI/mark thiếu, gửi `null` cho các trường đó và vẫn gửi OHLC. `historical:true` dùng khi nạp hoặc bù dữ liệu quá cũ: lưu nến, cập nhật lệnh đang mở, **không phát chuông mới**. Khi nến kế tiếp bắt đầu, gửi `POST /v1/bar-open` gồm `symbol`, `ts`, `open` để cố định giá vào. Nếu lỡ sự kiện mở, API lấy open của nến đó khi nến đóng. Có thể gửi `POST /v1/price-tick` với giá và thời gian hiện tại để vẽ đường giá theo thời gian thực.

API đọc:

| Endpoint | Nội dung |
| --- | --- |
| `GET /v1/notifications` | Tối đa 5 tín hiệu gần nhất cho chuông, trạng thái **Đang mở / Đã kết thúc**, giá vào, giá hiện tại, đường giá thật và đường dự báo trung bình. |
| `GET /v1/notifications/{id}` | Chi tiết tín hiệu và thống kê backtest mới tính từ ledger lệnh đã đóng trong 365 ngày. |
| `GET /v1/performance?symbol=BTC` | Tổng lệnh, thắng, winrate net và gross, ROI/PNL, Max DD, Sharpe, đường vốn. |
| `GET /health` | Mốc nến giá và OI mới nhất mỗi coin để giám sát feed. |

Thông báo cũ không còn nằm trong 5 mục chuông; ledger lệnh đã đóng được giữ riêng để thống kê cập nhật sau **mỗi lệnh mới kết thúc**. Dự báo là trung bình của tối đa 500 đường giá tương đối 60 phút **đã hoàn thành trước tín hiệu** cùng coin; `forecast_n` là cỡ mẫu. Khi chưa có mẫu hợp lệ, forecast là `null`, không vẽ dự báo giả. `seed_history.py` chỉ nhập lịch sử giao dịch và đường giá đã được kiểm chứng.

## Chạy tham chiếu

Tại thư mục gốc repo:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r mvp/requirements.txt
$env:MVP_SYMBOLS = "BTC,SOL,ZEC,BNB,XRP,DOGE,UNI,NEAR,SUI,ENA"
$env:MVP_INGEST_TOKEN = "<secret>"
$env:GFI_API_KEY = "<secret>"
$env:MVP_DB_PATH = "mvp_data/live.sqlite3"
uvicorn mvp.live_mvp:app --host 127.0.0.1 --port 8000
```

Trong terminal khác, đặt cùng biến môi trường rồi chạy `python -m mvp.live_feed --once` để kiểm tra một chu kỳ, sau đó `python -m mvp.live_feed` để poll. Đổi `MVP_SYMBOLS` sang coin bất kỳ mà cả feed OI và feed giá hỗ trợ. Trường hợp dùng feed sản phẩm thay GFI/Hyperliquid, chỉ cần thực hiện hợp đồng ba endpoint ghi; `strategy.py` không đổi.

Thử lõi và API: `pip install -r mvp/requirements-dev.txt` rồi `python -m pytest -q mvp/test_*.py`.

## Giới hạn cần xử lý khi tích hợp

- Endpoint GFI đang dùng: `https://sh-news-api.gfigroup.io/api/hyperliquid/asset-ctxs/series` (không có `-dev`). Key chỉ ở biến môi trường/secret manager. GFI có các đoạn `oi_usd` và `mark_close` rỗng; adapter không tạo tín hiệu trên đoạn này. `/health` hiển thị độ trễ OI. Tỷ lệ thắng mark-at-trigger 58,31% từ nghiên cứu 01/08–26/11/2026 là **event study trước phí**, không phải winrate của lệnh vào next-open và không được điền sẵn vào UI.
- OHLC công khai Hyperliquid đủ cho bootstrap gần đây nhưng không thay thế kho lịch sử dài hạn. Không hiển thị số liệu backtest 1 năm nếu chưa nhập đủ lịch sử nến giá và lệnh theo đúng hợp đồng. Màn hình bắt đầu trống và tăng thống kê từ lệnh live hoặc seed đã xác minh.
- SQLite và worker mẫu dành cho **một tiến trình**. Khi đưa vào hệ thống nhiều replica, chuyển bars/signals/trades sang database dùng chung và khóa xử lý theo `(symbol,ts)`. Đặt các endpoint đọc sau auth/gateway của sản phẩm; API demo hiện chỉ bảo vệ endpoint ghi. Giám sát độ trễ, HTTP 429, OI thiếu, nến giá thiếu và trạng thái `data_gap`.
- File `data/` ở repo gốc là snapshot nghiên cứu TP/SL, **không phải** dữ liệu seed của live baseline. Không đưa API key, database vận hành hoặc thông tin người dùng lên GitHub.
