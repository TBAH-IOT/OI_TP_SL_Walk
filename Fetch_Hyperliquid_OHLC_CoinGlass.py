from __future__ import annotations

import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests


BASE_URL = "https://open-api-v4.coinglass.com/api/futures/price/history"
BAR = pd.Timedelta(minutes=5)
MAX_ROWS = 1000
DEFAULT_SYMBOLS = (
    "BTC", "ETH", "SOL", "ZEC", "BNB", "XRP", "DOGE", "UNI", "NEAR", "SUI",
    "ENA", "ADA", "AVAX", "LINK", "TRX", "APT", "LTC", "BCH", "DOT",
    "XLM", "ICP", "FIL", "ETC", "AAVE", "ARB", "OP", "INJ", "TIA", "ATOM",
)


def parse_utc(value: str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def get_key() -> str:
    key = os.getenv("COINGLASS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Thiếu biến môi trường COINGLASS_API_KEY")
    return key


def parse_symbols(value: str) -> list[str]:
    symbols = [item.strip().upper() for item in value.split(",") if item.strip()]
    if not symbols:
        raise argparse.ArgumentTypeError("--symbols không được rỗng")
    if len(set(symbols)) != len(symbols):
        raise argparse.ArgumentTypeError("--symbols có coin trùng nhau")
    return symbols


def fetch_symbol(
    key: str,
    symbol: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    output_dir: Path,
    pause: float,
) -> Path:
    rows: list[dict] = []
    cursor = start
    session = requests.Session()
    chunk = BAR * MAX_ROWS
    request_no = 0
    while cursor < end:
        # end_time is exclusive.  For a final partial chunk we must pass the
        # requested range end itself, otherwise the last 5-minute bar is lost.
        chunk_end = min(cursor + chunk - BAR, end)
        params = {
            "exchange": "Hyperliquid",
            "symbol": f"{symbol}-USD",
            "interval": "5m",
            "limit": MAX_ROWS,
            "start_time": int(cursor.timestamp() * 1000),
            "end_time": int(chunk_end.timestamp() * 1000),
        }
        last_error = None
        for retry in range(6):
            try:
                response = session.get(
                    BASE_URL,
                    headers={"accept": "application/json", "CG-API-KEY": key},
                    params=params,
                    timeout=60,
                )
                response.raise_for_status()
                payload = response.json()
                if str(payload.get("code")) != "0":
                    raise RuntimeError(payload.get("msg", payload))
                batch = payload.get("data") or []
                if not batch:
                    raise RuntimeError(
                        f"CoinGlass trả về 0 nến cho {symbol} tại {cursor.isoformat()}"
                    )
                rows.extend(batch)
                break
            except Exception as exc:
                last_error = exc
                if retry == 5:
                    raise RuntimeError(
                        f"Không tải được OHLC Hyperliquid {symbol}: {last_error}"
                    ) from exc
                time.sleep(min(60.0, 2.0 * (retry + 1)))
        request_no += 1
        print(
            f"OHLC {symbol} [{request_no}] "
            f"{cursor.isoformat()} -> {chunk_end.isoformat()} | +{len(batch):,}",
            flush=True,
        )
        # CoinGlass treats end_time as an exclusive boundary.  Moving to
        # chunk_end therefore continues exactly at the next 5-minute bar;
        # adding BAR here would silently skip one bar per request chunk.
        cursor = chunk_end
        if pause:
            time.sleep(pause)

    frame = pd.DataFrame(rows)
    if frame.empty:
        raise RuntimeError(f"OHLC rỗng: {symbol}")
    frame["bucket_5m"] = pd.to_datetime(frame["time"], unit="ms", utc=True)
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = (
        frame[["bucket_5m", "open", "high", "low", "close"]]
        .dropna()
        .sort_values("bucket_5m")
        .drop_duplicates("bucket_5m")
        .reset_index(drop=True)
    )
    expected = pd.date_range(start, end - BAR, freq=BAR)
    actual = pd.DatetimeIndex(frame["bucket_5m"])
    missing = expected.difference(actual)
    outside = frame[(frame["bucket_5m"] < start) | (frame["bucket_5m"] >= end)]
    if len(missing) or not outside.empty:
        raise RuntimeError(
            f"OHLC {symbol} không liên tục: thiếu {len(missing):,} nến "
            f"trong {start.isoformat()} -> {end.isoformat()}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{symbol}_ohlc_5m.csv"
    temporary = path.with_suffix(".tmp.csv")
    frame.to_csv(temporary, index=False)
    temporary.replace(path)
    print(f"OHLC {symbol} hoàn tất: {len(frame):,} nến -> {path}", flush=True)
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-04-30T22:00:00Z")
    parser.add_argument("--end", default="2026-10-05T11:50:00Z")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "data" / "hl_ohlc_5m",
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--pause", type=float, default=0.25)
    parser.add_argument(
        "--symbols",
        type=parse_symbols,
        default=list(DEFAULT_SYMBOLS),
        help="Danh sách coin, phân tách bằng dấu phẩy; mặc định là 30 coin",
    )
    args = parser.parse_args()
    start = parse_utc(args.start)
    end = parse_utc(args.end)
    if end <= start:
        raise ValueError("--end phải sau --start")
    key = get_key()
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        jobs = {
            executor.submit(
                fetch_symbol,
                key,
                symbol,
                start,
                end,
                args.output_dir,
                args.pause,
            ): symbol
            for symbol in args.symbols
        }
        for future in as_completed(jobs):
            future.result()
    print(f"Đã tạo OHLC Hyperliquid cho {len(args.symbols)} coin: {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
