"""Pull and validate a 5-minute Hyperliquid OI cache for multiple coins via GFI.

The program is standalone: it does not import any other local Python file.
It performs four jobs:

1. Requests each symbol separately from the GFI API in small paginated chunks.
2. Saves one CSV per coin plus one combined 5m cache.
3. Audits coverage, duplicate timestamps, off-grid timestamps, missing 5m bars,
   missing OI/price values and the latest available timestamp.
4. Runs a clearly defined OI q01, 60-minute baseline on the resulting cache.

Default backtest convention:
  - OI q01 = the bottom 1% of 5m OI changes after a 7-day rolling lookback;
  - q01 signals are traded LONG by default; use --direction short to reverse;
  - horizon is fixed at 60 minutes (12 x 5m bars);
  - at most one trade per coin is open at a time; different coins may overlap;
  - fee is the complete round-trip fee, charged once per completed trade;
  - position size is fixed at 10% of the initial account, so overlapping trades
    cannot silently compound the account or create an unclear sizing rule.

The API key is read from NI_API_KEY or GFI_NI_API_KEY. If neither is set,
the program asks for it without displaying the characters. The key is never
written to disk.

Example:
    python D:\\Live_Trade\\Run_GFI_OI_10Coins_5m.py

To inspect an existing cache without calling the API:
    python D:\\Live_Trade\\Run_GFI_OI_10Coins_5m.py --reuse-cache
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import getpass
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd


API_URL = "https://sh-news-api-dev.gfigroup.io/api/hyperliquid/asset-ctxs/series"
DEFAULT_SYMBOLS = (
    "BTC",
    "ETH",
    "SOL",
    "ZEC",
    "BNB",
    "XRP",
    "DOGE",
    "UNI",
    "NEAR",
    "SUI",
    "ENA",
    "ADA",
    "AVAX",
    "LINK",
    "TON",
    "TRX",
    "APT",
    "LTC",
    "BCH",
    "DOT",
    "XLM",
    "ICP",
    "FIL",
    "ETC",
    "AAVE",
    "ARB",
    "OP",
    "INJ",
    "TIA",
    "ATOM",
)

BAR = pd.Timedelta(minutes=5)
BAR_SECONDS = 5 * 60
# The GFI endpoint currently caps a response at 1,000 rows.  A 3d10h window
# contains 984 five-minute bars, leaving a safety margin and preventing silent
# truncation.  Do not increase this unless the endpoint's page limit changes.
MAX_QUERY_RANGE = timedelta(days=3, hours=10)
LOOKBACK_BARS = 7 * 288
MIN_PERIODS = LOOKBACK_BARS // 4
HORIZON_BARS = 12
Q01 = 0.01


def parse_time(value: str | datetime | pd.Timestamp) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def format_time(value: datetime | pd.Timestamp) -> str:
    dt = parse_time(value)
    return dt.isoformat().replace("+00:00", "Z")


def floor_5m(value: datetime) -> datetime:
    value = value.astimezone(timezone.utc).replace(second=0, microsecond=0)
    minute = value.minute - (value.minute % 5)
    return value.replace(minute=minute)


def read_api_key() -> str:
    value = os.getenv("NI_API_KEY") or os.getenv("GFI_NI_API_KEY")
    if value and value.strip():
        return value.strip()

    value = getpass.getpass(
        "Dán API key GFI vào đây (ký tự sẽ không hiển thị): "
    ).strip()
    if not value:
        raise RuntimeError("API key đang trống")
    return value


def request_series(
    key: str,
    symbol: str,
    start: datetime,
    end: datetime,
    retries: int = 8,
) -> list[dict[str, Any]]:
    query = urlencode(
        {
            "symbol": symbol,
            "start": format_time(start),
            "end": format_time(end),
        }
    )
    request = Request(
        f"{API_URL}?{query}",
        headers={
            "X-Api-Key": key,
            "Accept": "application/json",
            "User-Agent": "GFI-Hyperliquid-OI-5m-Cache/1.0",
        },
        method="GET",
    )

    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
            rows = payload.get("rows") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise RuntimeError(
                    f"API không trả trường rows cho {symbol}: {payload!r}"
                )
            return [row for row in rows if isinstance(row, dict)]
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt >= retries:
                raise RuntimeError(
                    f"API HTTP {exc.code} cho {symbol}: {detail}"
                ) from exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                wait = float(retry_after) if retry_after else 0.0
            except ValueError:
                wait = 0.0
            if wait <= 0:
                wait = (5.0, 15.0, 30.0, 60.0, 90.0, 120.0, 180.0, 180.0)[
                    min(attempt, 7)
                ]
            print(
                f"  HTTP {exc.code}; đợi {wait:.1f}s rồi thử lại "
                f"({attempt + 1}/{retries})...",
                flush=True,
            )
            time.sleep(wait)
        except (URLError, TimeoutError) as exc:
            if attempt >= retries:
                reason = getattr(exc, "reason", str(exc))
                raise RuntimeError(
                    f"Không kết nối được API cho {symbol}: {reason}"
                ) from exc
            wait = min(60.0, 5.0 * (attempt + 1))
            print(
                f"  Lỗi mạng; đợi {wait:.1f}s rồi thử lại "
                f"({attempt + 1}/{retries})...",
                flush=True,
            )
            time.sleep(wait)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"API trả JSON không hợp lệ cho {symbol}"
            ) from exc

    raise RuntimeError(f"Không lấy được dữ liệu cho {symbol}")


def normalize_rows(
    rows: list[dict[str, Any]],
    symbol: str,
    requested_start: datetime,
    requested_end: datetime,
) -> tuple[pd.DataFrame, dict[str, int]]:
    valid_rows: list[dict[str, Any]] = []
    invalid_bucket = 0

    for row in rows:
        stamp = row.get("bucket_5m")
        if not stamp:
            invalid_bucket += 1
            continue
        try:
            parsed = pd.to_datetime(stamp, utc=True, errors="raise")
        except (TypeError, ValueError, OverflowError):
            invalid_bucket += 1
            continue
        clean = dict(row)
        clean["bucket_5m"] = parsed
        clean["symbol"] = symbol
        valid_rows.append(clean)

    if not valid_rows:
        return pd.DataFrame(columns=["bucket_5m", "symbol"]), {
            "api_rows_raw": len(rows),
            "api_rows_with_valid_timestamp": 0,
            "invalid_timestamp_rows": invalid_bucket,
            "duplicate_timestamp_rows": 0,
        }

    df = pd.DataFrame(valid_rows)
    df = df[
        (df["bucket_5m"] >= pd.Timestamp(requested_start))
        & (df["bucket_5m"] <= pd.Timestamp(requested_end))
    ].copy()
    duplicate_rows = int(df.duplicated("bucket_5m").sum())
    df = (
        df.sort_values("bucket_5m")
        .drop_duplicates("bucket_5m", keep="last")
        .reset_index(drop=True)
    )

    for field in (
        "open_interest",
        "oi_usd",
        "funding",
        "mark_close",
        "oracle_close",
        "mid_close",
        "premium_close",
        "day_ntl_vlm_close",
    ):
        if field in df.columns:
            df[field] = pd.to_numeric(df[field], errors="coerce")

    return df, {
        "api_rows_raw": len(rows),
        "api_rows_with_valid_timestamp": len(valid_rows),
        "invalid_timestamp_rows": invalid_bucket,
        "duplicate_timestamp_rows": duplicate_rows,
    }


def fetch_symbol(
    key: str,
    symbol: str,
    requested_start: datetime,
    requested_end: datetime,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Fetch the whole requested window without skipping empty chunks."""
    all_rows: list[dict[str, Any]] = []
    cursor = requested_start
    chunks = 0

    while cursor <= requested_end:
        # Keep each response below the API's current 1,000-row cap.
        query_end = min(
            requested_end,
            cursor + MAX_QUERY_RANGE - timedelta(minutes=5),
        )
        chunks += 1
        batch = request_series(key, symbol, cursor, query_end)
        all_rows.extend(batch)
        print(
            f"  {symbol} | request {chunks:02d} | "
            f"{format_time(cursor)} -> {format_time(query_end)} | "
            f"API rows: {len(batch):,}",
            flush=True,
        )
        if not batch and all_rows:
            print(
                f"  {symbol} | API không còn dữ liệu sau "
                f"{format_time(cursor)}; dừng phân trang.",
                flush=True,
            )
            break
        cursor = query_end + timedelta(minutes=5)
        time.sleep(0.50)

    df, stats = normalize_rows(
        all_rows, symbol, requested_start, requested_end
    )
    stats["api_chunks"] = chunks
    return df, stats


def load_cached_symbol(
    path: Path,
    symbol: str,
    requested_start: datetime,
    requested_end: datetime,
) -> tuple[pd.DataFrame, dict[str, int]]:
    df = pd.read_csv(path)
    if "bucket_5m" not in df.columns:
        raise RuntimeError(f"Cache {path} thiếu bucket_5m")
    df["bucket_5m"] = pd.to_datetime(df["bucket_5m"], utc=True, errors="coerce")
    df["symbol"] = symbol
    for field in (
        "open_interest",
        "oi_usd",
        "funding",
        "mark_close",
        "oracle_close",
        "mid_close",
        "premium_close",
        "day_ntl_vlm_close",
    ):
        if field in df.columns:
            df[field] = pd.to_numeric(df[field], errors="coerce")
    df = df.dropna(subset=["bucket_5m"])
    df = df[
        (df["bucket_5m"] >= pd.Timestamp(requested_start))
        & (df["bucket_5m"] <= pd.Timestamp(requested_end))
    ].sort_values("bucket_5m")
    duplicate_rows = int(df.duplicated("bucket_5m").sum())
    df = df.drop_duplicates("bucket_5m", keep="last").reset_index(drop=True)
    return df, {
        "api_rows_raw": len(df),
        "api_rows_with_valid_timestamp": len(df),
        "invalid_timestamp_rows": 0,
        "duplicate_timestamp_rows": duplicate_rows,
        "api_chunks": 0,
    }


def audit_symbol(
    df: pd.DataFrame,
    symbol: str,
    requested_start: datetime,
    requested_end: datetime,
    fetch_stats: dict[str, int],
) -> dict[str, Any]:
    required = ["bucket_5m", "oi_usd", "mark_close"]
    missing_columns = [col for col in required if col not in df.columns]

    result: dict[str, Any] = {
        "symbol": symbol,
        "requested_start": format_time(requested_start),
        "requested_end": format_time(requested_end),
        "rows": int(len(df)),
        "api_chunks": int(fetch_stats.get("api_chunks", 0)),
        "api_rows_raw": int(fetch_stats.get("api_rows_raw", 0)),
        "invalid_timestamp_rows": int(
            fetch_stats.get("invalid_timestamp_rows", 0)
        ),
        "duplicate_timestamp_rows": int(
            fetch_stats.get("duplicate_timestamp_rows", 0)
        ),
        "missing_columns": ",".join(missing_columns),
        "actual_start": "",
        "actual_end": "",
        "expected_bars_requested": 0,
        "expected_bars_actual_span": 0,
        "missing_bars_in_actual_span": 0,
        "missing_oi_rows": 0,
        "missing_mark_rows": 0,
        "off_grid_rows": 0,
        "gap_events": 0,
        "max_gap_minutes": 0.0,
        "coverage_requested_pct": 0.0,
        "coverage_actual_span_pct": 0.0,
        "lag_from_requested_end_minutes": np.nan,
        "status": "NO_DATA",
    }

    requested_seconds = (requested_end - requested_start).total_seconds()
    result["expected_bars_requested"] = int(requested_seconds // BAR_SECONDS) + 1

    if df.empty or missing_columns:
        if missing_columns and not df.empty:
            result["status"] = "MISSING_COLUMNS"
        return result

    stamps = pd.to_datetime(df["bucket_5m"], utc=True).sort_values()
    actual_start = stamps.iloc[0]
    actual_end = stamps.iloc[-1]
    result["actual_start"] = format_time(actual_start)
    result["actual_end"] = format_time(actual_end)

    span_seconds = (actual_end - actual_start).total_seconds()
    expected_span = int(span_seconds // BAR_SECONDS) + 1
    result["expected_bars_actual_span"] = expected_span

    diffs = stamps.diff().dropna().dt.total_seconds() / BAR_SECONDS
    if not diffs.empty:
        gap_diffs = diffs[diffs > 1.0]
        result["gap_events"] = int(len(gap_diffs))
        result["missing_bars_in_actual_span"] = int(
            np.maximum(np.floor(gap_diffs).astype(int) - 1, 0).sum()
        )
        result["max_gap_minutes"] = float(diffs.max() * 5.0)

    off_grid = (
        (stamps.dt.minute % 5 != 0)
        | (stamps.dt.second != 0)
        | (stamps.dt.microsecond != 0)
    )
    result["off_grid_rows"] = int(off_grid.sum())
    result["missing_oi_rows"] = int(df["oi_usd"].isna().sum())
    result["missing_mark_rows"] = int(df["mark_close"].isna().sum())
    result["coverage_requested_pct"] = round(
        100.0 * len(df) / result["expected_bars_requested"], 3
    )
    result["coverage_actual_span_pct"] = round(
        100.0 * len(df) / expected_span if expected_span else 0.0,
        3,
    )
    result["lag_from_requested_end_minutes"] = round(
        (pd.Timestamp(requested_end) - actual_end).total_seconds() / 60.0,
        3,
    )

    if (
        result["invalid_timestamp_rows"]
        or result["duplicate_timestamp_rows"]
        or result["off_grid_rows"]
        or result["missing_oi_rows"]
        or result["missing_mark_rows"]
        or result["missing_bars_in_actual_span"]
    ):
        result["status"] = "HAS_GAPS_OR_INVALID_ROWS"
    elif actual_start > pd.Timestamp(requested_start) + BAR:
        result["status"] = "PARTIAL_START"
    elif actual_end < pd.Timestamp(requested_end) - pd.Timedelta(minutes=10):
        result["status"] = "PARTIAL_END"
    else:
        result["status"] = "OK"
    return result


def contiguous_segments(df: pd.DataFrame) -> list[pd.DataFrame]:
    if df.empty:
        return []
    ordered = (
        df.sort_values("bucket_5m")
        .dropna(subset=["bucket_5m", "oi_usd", "mark_close"])
        .loc[lambda x: (x["oi_usd"] > 0) & (x["mark_close"] > 0)]
        .reset_index(drop=True)
    )
    if ordered.empty:
        return []
    breaks = ordered["bucket_5m"].diff().ne(BAR)
    return [
        part.reset_index(drop=True)
        for _, part in ordered.groupby(breaks.cumsum())
        if len(part) >= MIN_PERIODS + HORIZON_BARS + 1
    ]


def build_q01_events(
    symbol: str,
    df: pd.DataFrame,
    evaluation_start: pd.Timestamp,
    evaluation_end: pd.Timestamp,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for segment_id, part in enumerate(contiguous_segments(df), start=1):
        factor = part["oi_usd"].pct_change()
        lower = factor.rolling(
            LOOKBACK_BARS, min_periods=MIN_PERIODS
        ).quantile(Q01)
        state = (factor <= lower).fillna(False)
        onset = state & ~state.shift(1, fill_value=False)

        for position in np.flatnonzero(onset.to_numpy()):
            timestamp = pd.Timestamp(part.loc[position, "bucket_5m"])
            end_position = position + HORIZON_BARS
            if timestamp < evaluation_start or timestamp > evaluation_end:
                continue
            if end_position >= len(part):
                continue
            entry = float(part.loc[position, "mark_close"])
            path = (
                part.loc[position + 1 : end_position, "mark_close"]
                .to_numpy(dtype=float)
                / entry
                - 1.0
            )
            if len(path) != HORIZON_BARS or not np.isfinite(path).all():
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "segment": segment_id,
                    "timestamp": timestamp,
                    "return_60m_long": float(path[-1]),
                    "path_long": path.tolist(),
                }
            )
    if not rows:
        return pd.DataFrame(
            columns=[
                "symbol",
                "segment",
                "timestamp",
                "return_60m_long",
                "path_long",
            ]
        )
    return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)


def execute_baseline(
    events: pd.DataFrame,
    direction: str,
    fee: float,
    initial_capital: float,
    position_pct: float,
) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame()

    next_available: dict[str, pd.Timestamp] = {}
    rows: list[dict[str, Any]] = []
    notional = initial_capital * position_pct
    ordered = events.sort_values(["timestamp", "symbol"])

    for _, event in ordered.iterrows():
        symbol = str(event["symbol"])
        entry_time = pd.Timestamp(event["timestamp"])
        if symbol in next_available and entry_time <= next_available[symbol]:
            continue

        raw_long = float(event["return_60m_long"])
        raw_return = raw_long if direction == "LONG" else -raw_long
        net_return = raw_return - fee
        exit_time = entry_time + HORIZON_BARS * BAR
        rows.append(
            {
                "symbol": symbol,
                "entry_time": entry_time,
                "exit_time": exit_time,
                "direction": direction,
                "gross_return": raw_return,
                "fee": fee,
                "net_return": net_return,
                "pnl_usd": notional * net_return,
                "hold_5m": HORIZON_BARS,
                "exit_type": "TIME_60M",
            }
        )
        next_available[symbol] = exit_time

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(
        ["exit_time", "entry_time", "symbol"]
    ).reset_index(drop=True)


def portfolio_metrics(
    trades: pd.DataFrame,
    initial_capital: float,
) -> dict[str, Any]:
    if trades.empty:
        return {
            "trades": 0,
            "winrate_pct": np.nan,
            "roi_pct": np.nan,
            "max_dd_pct": np.nan,
            "calmar": np.nan,
            "profit_factor": np.nan,
            "max_concurrent": 0,
            "first_entry": "",
            "last_exit": "",
        }

    equity = initial_capital + trades["pnl_usd"].cumsum()
    peak = equity.cummax().clip(lower=initial_capital)
    drawdown = equity / peak - 1.0
    max_dd = float(-drawdown.min())
    final_equity = float(equity.iloc[-1])
    net = trades["net_return"].to_numpy(dtype=float)
    wins = net[net > 0].sum()
    losses = -net[net < 0].sum()
    pf = float(wins / losses) if losses > 0 else np.inf

    first_entry = pd.Timestamp(trades["entry_time"].min())
    last_exit = pd.Timestamp(trades["exit_time"].max())
    days = max((last_exit - first_entry).total_seconds() / 86400.0, 1.0)
    cagr = (final_equity / initial_capital) ** (365.0 / days) - 1.0
    calmar = cagr / max_dd if max_dd > 0 else np.nan

    timeline = pd.concat(
        [
            trades[["symbol", "entry_time"]]
            .rename(columns={"entry_time": "time"})
            .assign(delta=1),
            trades[["symbol", "exit_time"]]
            .rename(columns={"exit_time": "time"})
            .assign(delta=-1),
        ],
        ignore_index=True,
    ).sort_values(["time", "delta"])
    concurrent = timeline.groupby("time")["delta"].cumsum()
    max_concurrent = int(concurrent.max()) if not concurrent.empty else 0

    return {
        "trades": int(len(trades)),
        "winrate_pct": float((net > 0).mean() * 100.0),
        "roi_pct": float((final_equity / initial_capital - 1.0) * 100.0),
        "max_dd_pct": max_dd * 100.0,
        "calmar": float(calmar),
        "profit_factor": pf,
        "max_concurrent": max_concurrent,
        "first_entry": first_entry.isoformat(),
        "last_exit": last_exit.isoformat(),
        "gross_sum_pct": float(trades["gross_return"].sum() * 100.0),
        "fee_drag_pct": float(trades["fee"].sum() * 100.0),
        "final_equity": final_equity,
    }


def print_coverage_table(coverage: pd.DataFrame) -> None:
    cols = [
        "symbol",
        "status",
        "rows",
        "actual_start",
        "actual_end",
        "coverage_requested_pct",
        "missing_bars_in_actual_span",
        "duplicate_timestamp_rows",
        "max_gap_minutes",
    ]
    display = coverage[cols].copy()
    for col in ("coverage_requested_pct", "max_gap_minutes"):
        display[col] = display[col].map(
            lambda value: f"{float(value):.3f}"
            if pd.notna(value)
            else "NA"
        )
    print("\nBẢNG KIỂM TRA CACHE 5M")
    print(display.to_string(index=False))


def print_results_table(summary: pd.DataFrame) -> None:
    if summary.empty:
        print("\nKhông tạo được event OI q01 nào.")
        return
    display = summary.copy()
    for col in ("winrate_pct", "roi_pct", "max_dd_pct", "gross_sum_pct", "fee_drag_pct"):
        if col in display:
            display[col] = display[col].map(
                lambda value: f"{float(value):.3f}%"
                if pd.notna(value)
                else "NA"
            )
    print("\nKẾT QUẢ OI q01 | 5M | HORIZON 60M | FIXED POSITION")
    print(display.to_string(index=False))


def parse_symbols(value: str) -> list[str]:
    symbols = [item.strip().upper() for item in value.split(",") if item.strip()]
    if not symbols:
        raise argparse.ArgumentTypeError("Danh sách symbol đang trống")
    if len(set(symbols)) != len(symbols):
        raise argparse.ArgumentTypeError("Danh sách symbol bị trùng")
    return symbols


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols",
        type=parse_symbols,
        default=list(DEFAULT_SYMBOLS),
        help="Danh sách symbol Hyperliquid, phân cách bằng dấu phẩy; mặc định 30 coin",
    )
    parser.add_argument(
        "--start",
        default="2026-04-24T00:00:00Z",
        help="Thời điểm bắt đầu; mặc định 2026-04-24 để đủ lookback train tháng 5",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="Thời điểm kết thúc; bỏ trống = nến 5m gần nhất hiện có",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "data" / "gfi_oi_5m_30coins_cache",
    )
    parser.add_argument(
        "--reuse-cache",
        action="store_true",
        help="Không gọi API; đọc các file SYMBOL_oi_5m.csv đã có",
    )
    parser.add_argument(
        "--direction",
        choices=("long", "short"),
        default="long",
        help="Hướng giao dịch q01; mặc định long",
    )
    parser.add_argument(
        "--fee-pct",
        type=float,
        default=0.10,
        help="Tổng phí vào+ra mỗi lệnh, %%: mặc định 0.10",
    )
    parser.add_argument(
        "--position-pct",
        type=float,
        default=10.0,
        help="Notional cố định mỗi lệnh theo %% vốn đầu kỳ: mặc định 10",
    )
    parser.add_argument(
        "--initial-capital",
        type=float,
        default=10000.0,
        help="Vốn giả định để quy đổi PnL; mặc định 10000",
    )
    parser.add_argument(
        "--no-backtest",
        action="store_true",
        help="Chỉ tải/kiểm tra cache, không chạy OI q01",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Số coin tải song song; mặc định 1 để tránh HTTP 429 của GFI",
    )
    args = parser.parse_args()

    if not 0 < args.fee_pct < 100:
        raise ValueError("--fee-pct phải nằm trong (0, 100)")
    if not 0 < args.position_pct <= 100:
        raise ValueError("--position-pct phải nằm trong (0, 100]")
    if args.initial_capital <= 0:
        raise ValueError("--initial-capital phải dương")
    if args.workers < 1:
        raise ValueError("--workers phải >= 1")

    requested_start = floor_5m(parse_time(args.start))
    requested_end = (
        floor_5m(parse_time(args.end))
        if args.end
        else floor_5m(datetime.now(timezone.utc))
    )
    if requested_end < requested_start:
        raise ValueError("--end phải sau --start")

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    key = None if args.reuse_cache else read_api_key()
    all_frames_by_symbol: dict[str, pd.DataFrame] = {}
    coverage_rows_by_symbol: dict[str, dict[str, Any]] = {}
    per_symbol_paths: dict[str, str] = {}

    print("=" * 100)
    print(f"GFI HYPERLIQUID OI CACHE 5M | {len(args.symbols)} COIN")
    print(f"Symbols: {', '.join(args.symbols)}")
    print(f"Requested: {format_time(requested_start)} -> {format_time(requested_end)}")
    print(f"Cache dir: {args.cache_dir}")
    print(f"Mode: {'REUSE CACHE' if args.reuse_cache else 'FETCH GFI API'}")
    print("=" * 100)

    def process_symbol(symbol: str) -> tuple[str, pd.DataFrame, dict[str, Any], Path]:
        cache_path = args.cache_dir / f"{symbol}_oi_5m.csv"
        if args.reuse_cache:
            if not cache_path.exists():
                raise FileNotFoundError(
                    f"Thiếu cache {cache_path}; bỏ --reuse-cache để tải API"
                )
            df, fetch_stats = load_cached_symbol(
                cache_path,
                symbol,
                requested_start,
                requested_end,
            )
        else:
            assert key is not None
            df, fetch_stats = fetch_symbol(
                key, symbol, requested_start, requested_end
            )
            df.to_csv(cache_path, index=False)

        audit = audit_symbol(
            df, symbol, requested_start, requested_end, fetch_stats
        )
        return symbol, df, audit, cache_path

    worker_count = min(args.workers, len(args.symbols))
    print(f"Workers: {worker_count}")
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(process_symbol, symbol): symbol
            for symbol in args.symbols
        }
        for index, future in enumerate(as_completed(futures), start=1):
            symbol, df, audit, cache_path = future.result()
            all_frames_by_symbol[symbol] = df
            coverage_rows_by_symbol[symbol] = audit
            per_symbol_paths[symbol] = str(cache_path)
            print(f"\n[{index}/{len(args.symbols)}] {symbol} hoàn tất")
            print(
                f"  Kết quả: {len(df):,} rows | "
                f"{audit['actual_start'] or 'NO_DATA'} -> "
                f"{audit['actual_end'] or 'NO_DATA'} | "
                f"status={audit['status']}",
                flush=True,
            )

    all_frames = [all_frames_by_symbol[symbol] for symbol in args.symbols]
    coverage_rows = [coverage_rows_by_symbol[symbol] for symbol in args.symbols]
    coverage = pd.DataFrame(coverage_rows)
    coverage_path = args.cache_dir / "coverage_report.csv"
    coverage.to_csv(coverage_path, index=False)
    print_coverage_table(coverage)

    nonempty_frames = [frame for frame in all_frames if not frame.empty]
    if nonempty_frames:
        combined = (
            pd.concat(nonempty_frames, ignore_index=True, sort=False)
            .sort_values(["bucket_5m", "symbol"])
            .reset_index(drop=True)
        )
    else:
        combined = pd.DataFrame()
    combined_path = args.cache_dir / f"oi_5m_{len(args.symbols)}coins_combined.csv"
    combined.to_csv(combined_path, index=False)

    manifest = {
        "api_url": API_URL,
        "symbols": args.symbols,
        "requested_start": format_time(requested_start),
        "requested_end": format_time(requested_end),
        "bar": "5m",
        "lookback_bars": LOOKBACK_BARS,
        "horizon_bars": HORIZON_BARS,
        "per_symbol_files": per_symbol_paths,
        "combined_file": str(combined_path),
        "coverage_file": str(coverage_path),
        "total_rows": int(len(combined)),
    }
    manifest_path = args.cache_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\nĐÃ LƯU CACHE")
    print(f"  Combined: {combined_path}")
    print(f"  Coverage: {coverage_path}")
    print(f"  Manifest: {manifest_path}")

    if args.no_backtest:
        return 0

    evaluation_start = pd.Timestamp(requested_start)
    evaluation_end = pd.Timestamp(requested_end)
    event_frames: list[pd.DataFrame] = []
    for symbol, frame in zip(args.symbols, all_frames):
        events = build_q01_events(
            symbol, frame, evaluation_start, evaluation_end
        )
        if not events.empty:
            event_frames.append(events)

    if event_frames:
        events = pd.concat(event_frames, ignore_index=True).sort_values(
            ["timestamp", "symbol"]
        )
    else:
        events = pd.DataFrame()

    events_path = args.cache_dir / "oi_q01_5m_60m_events.csv"
    if not events.empty:
        events.to_csv(events_path, index=False)
    else:
        events_path.write_text("", encoding="utf-8")

    direction = args.direction.upper()
    fee = args.fee_pct / 100.0
    position_pct = args.position_pct / 100.0
    trades = execute_baseline(
        events,
        direction,
        fee,
        args.initial_capital,
        position_pct,
    )
    trades_path = args.cache_dir / "oi_q01_5m_60m_baseline_trades.csv"
    if not trades.empty:
        trades.to_csv(trades_path, index=False)
    else:
        trades_path.write_text("", encoding="utf-8")

    if events.empty:
        print("\nKhông tạo được event OI q01; kiểm tra coverage và cột oi_usd/mark_close.")
        return 0

    if trades.empty:
        print("\nCó event nhưng không tạo được trade sau bộ lọc non-overlap theo coin.")
        return 0

    per_coin_rows: list[dict[str, Any]] = []
    for symbol, group in trades.groupby("symbol"):
        net = group["net_return"].to_numpy(dtype=float)
        per_coin_rows.append(
            {
                "symbol": symbol,
                "trades": len(group),
                "winrate_pct": float((net > 0).mean() * 100.0),
                "roi_pct_fixed_notional": float(
                    group["pnl_usd"].sum() / args.initial_capital * 100.0
                ),
                "mean_net_return_pct": float(net.mean() * 100.0),
            }
        )
    per_coin = pd.DataFrame(per_coin_rows).sort_values("symbol")
    per_coin_path = args.cache_dir / "oi_q01_5m_60m_per_coin.csv"
    per_coin.to_csv(per_coin_path, index=False)
    print("\nOI q01 EVENTS THEO COIN")
    event_counts = events.groupby("symbol").size().rename("events")
    print(
        per_coin.merge(event_counts, left_on="symbol", right_index=True, how="left")
        .sort_values("symbol")
        .to_string(index=False)
    )

    metrics = portfolio_metrics(trades, args.initial_capital)
    print("\nPORTFOLIO BASELINE")
    print(f"  Hướng q01              : {direction}")
    print(f"  Horizon                : 60 phút = {HORIZON_BARS} nến 5m")
    print(f"  Tổng phí round-trip    : {args.fee_pct:.3f}%/lệnh")
    print(f"  Notional cố định       : {args.position_pct:.3f}% vốn đầu kỳ/lệnh")
    print(f"  Số event q01           : {len(events):,}")
    print(f"  Số lệnh sau non-overlap : {metrics['trades']:,}")
    print(f"  Winrate net             : {metrics['winrate_pct']:.3f}%")
    print(f"  ROI net trên vốn        : {metrics['roi_pct']:.3f}%")
    print(f"  Max drawdown realized   : {metrics['max_dd_pct']:.3f}%")
    print(f"  Calmar                  : {metrics['calmar']:.4f}")
    print(f"  Profit factor           : {metrics['profit_factor']:.4f}")
    print(f"  Max concurrent          : {metrics['max_concurrent']}")
    print(f"  Trade window            : {metrics['first_entry']} -> {metrics['last_exit']}")
    print(f"  Events: {events_path}")
    print(f"  Trades: {trades_path}")
    print(f"  Per coin: {per_coin_path}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nĐã dừng theo yêu cầu.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
