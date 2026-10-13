
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests


PACKAGE_ROOT = Path(__file__).resolve().parent
CACHE_ROOT = PACKAGE_ROOT / "data"


BAR = pd.Timedelta(minutes=5)
HORIZON_BARS = 12
POSITION_PCT = 0.1
LOOKBACK_BARS = 7 * 288
MIN_PERIODS = LOOKBACK_BARS // 4
ATR_PERIOD = 14
FEE = 0.001
INITIAL_CAPITAL = 10_000.0
TRAIN_START = pd.Timestamp("2026-05-01", tz="UTC")
DEFAULT_TEST_START = "2026-06-01T00:00:00Z"
TOP_N_COINS = 20
# TON is present in the GFI OI universe but has no Hyperliquid instrument
# (and therefore no valid Hyperliquid OHLC source to pair with its OI).
HYPERLIQUID_PRICE_UNAVAILABLE = {"TON"}


TP_GRID = (0.001, 0.002, 0.003, 0.005, 0.0075, 0.010)
SL_GRID = TP_GRID
BOOST_TIERS = ((5, 2.0), (3, 1.6), (2, 1.3))
HYPERLIQUID_URL = "https://api.hyperliquid.xyz/info"


def parse_utc(value: str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def contiguous_segments(df: pd.DataFrame) -> list[pd.DataFrame]:
    df = (
        df.sort_values("bucket_5m")
        .dropna(subset=["bucket_5m", "oi_usd", "mark_close"])
        .loc[lambda x: (x["oi_usd"] > 0) & (x["mark_close"] > 0)]
        .reset_index(drop=True)
    )
    if df.empty:
        return []
    breaks = df["bucket_5m"].diff().ne(BAR)
    return [
        part.reset_index(drop=True)
        for _, part in df.groupby(breaks.cumsum())
        if len(part) >= MIN_PERIODS + HORIZON_BARS + 1
    ]


def wilder_atr(ohlc: pd.DataFrame) -> pd.Series:
    high = ohlc["high"].to_numpy(float)
    low = ohlc["low"].to_numpy(float)
    close = ohlc["close"].to_numpy(float)
    prev = np.roll(close, 1)
    prev[0] = np.nan
    true_range = np.nanmax(
        np.vstack([high - low, np.abs(high - prev), np.abs(low - prev)]), axis=0
    )
    atr = np.full(len(true_range), np.nan, dtype=float)
    if len(atr) < ATR_PERIOD or not np.isfinite(true_range[:ATR_PERIOD]).all():
        return pd.Series(atr, index=ohlc.index)
    atr[ATR_PERIOD - 1] = true_range[:ATR_PERIOD].mean()
    for i in range(ATR_PERIOD, len(atr)):
        atr[i] = ((ATR_PERIOD - 1) * atr[i - 1] + true_range[i]) / ATR_PERIOD
    return pd.Series(atr, index=ohlc.index)


def hl_cache_path(cache_dir: Path, symbol: str) -> Path:
    return cache_dir / f"{symbol}_ohlc_5m.csv"


def read_ohlc_cache(cache_path: Path) -> pd.DataFrame:
    cached = pd.read_csv(cache_path)
    required = {"bucket_5m", "open", "high", "low", "close"}
    missing = required.difference(cached.columns)
    if missing:
        raise RuntimeError(
            f"Cache OHLC {cache_path} thiếu cột: {', '.join(sorted(missing))}"
        )
    cached["bucket_5m"] = pd.to_datetime(cached["bucket_5m"], utc=True)
    for column in ("open", "high", "low", "close"):
        cached[column] = pd.to_numeric(cached[column], errors="coerce")
    cached = (
        cached.dropna(subset=list(required))
        .sort_values("bucket_5m")
        .drop_duplicates("bucket_5m")
        .reset_index(drop=True)
    )
    if cached.empty:
        raise RuntimeError(f"Cache OHLC rỗng hoặc không hợp lệ: {cache_path}")
    return cached


def fetch_hyperliquid_ohlc(
    symbol: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    cache_path: Path,
    session: requests.Session,
) -> pd.DataFrame:
    cached = None
    if cache_path.exists():
        cached = read_ohlc_cache(cache_path)
        if (
            cached["bucket_5m"].min() <= start
            and cached["bucket_5m"].max() >= end - BAR
        ):
            return cached

    rows: list[dict[str, Any]] = []
    chunk = pd.Timedelta(minutes=5 * 1000)
    cursor = start
    attempts = 0
    while cursor < end:
        chunk_end = min(cursor + chunk, end)
        last_error = None
        for retry in range(5):
            try:
                response = session.post(
                    HYPERLIQUID_URL,
                    json={
                        "type": "candleSnapshot",
                        "req": {
                            "coin": symbol,
                            "interval": "5m",
                            "startTime": int(cursor.timestamp() * 1000),
                            "endTime": int(chunk_end.timestamp() * 1000),
                        },
                    },
                    timeout=30,
                )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, list):
                    raise RuntimeError(
                        f"Unexpected Hyperliquid response for {symbol}: {data}"
                    )
                if not data:
                    raise RuntimeError(
                        f"Hyperliquid trả về 0 nến cho {symbol} trong "
                        f"{cursor.isoformat()} -> {chunk_end.isoformat()}"
                    )
                chunk_rows = []
                for row in data:
                    if isinstance(row, dict):
                        timestamp = row["t"]
                        open_price = row["o"]
                        high_price = row["h"]
                        low_price = row["l"]
                        close_price = row["c"]
                    else:
                        timestamp, open_price, high_price, low_price, close_price = row[:5]
                    chunk_rows.append(
                        {
                            "bucket_5m": pd.to_datetime(int(timestamp), unit="ms", utc=True),
                            "open": float(open_price),
                            "high": float(high_price),
                            "low": float(low_price),
                            "close": float(close_price),
                        }
                    )
                rows.extend(chunk_rows)
                break
            except Exception as exc:
                last_error = exc
                time.sleep(1.5 * (retry + 1))
        else:
            raise RuntimeError(f"Không kéo được OHLC Hyperliquid {symbol}: {last_error}")
        attempts += 1
        cursor = chunk_end
        time.sleep(0.05)

    result = (
        pd.DataFrame(rows)
        .sort_values("bucket_5m")
        .drop_duplicates("bucket_5m")
        .reset_index(drop=True)
    )
    if result.empty:
        raise RuntimeError(f"OHLC rỗng: {symbol}")
    if cached is not None:
        result = (
            pd.concat([cached, result], ignore_index=True)
            .sort_values("bucket_5m")
            .drop_duplicates("bucket_5m")
            .reset_index(drop=True)
        )
    if result["bucket_5m"].min() > start or result["bucket_5m"].max() < end - BAR:
        cache_range = (
            f"{cached['bucket_5m'].min().isoformat()} -> "
            f"{cached['bucket_5m'].max().isoformat()}"
            if cached is not None
            else "không có"
        )
        raise RuntimeError(
            f"OHLC Hyperliquid {symbol} chưa phủ đủ khoảng "
            f"{start.isoformat()} -> {end.isoformat()}. "
            f"Cache hiện tại: {cache_range}. Không ghi đè cache thiếu."
        )
    result.to_csv(cache_path, index=False)
    print(f"OHLC Hyperliquid {symbol}: {len(result):,} nến, {attempts} requests")
    return result


def load_ohlc(
    coins: tuple[str, ...],
    start: pd.Timestamp,
    end: pd.Timestamp,
    cache_dir: Path,
) -> dict[str, pd.DataFrame]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    result = {}
    for i, symbol in enumerate(coins, 1):
        print(f"OHLC [{i}/{len(coins)}] {symbol}")
        result[symbol] = fetch_hyperliquid_ohlc(
            symbol,
            start,
            end,
            hl_cache_path(cache_dir, symbol),
            session,
        )
    return result


def select_top_coins(
    oi: pd.DataFrame,
    ranking_start: pd.Timestamp,
    ranking_end: pd.Timestamp,
    top_n: int,
    excluded_symbols: set[str] | None = None,
) -> tuple[str, ...]:
    ranking = oi[
        (oi["bucket_5m"] >= ranking_start)
        & (oi["bucket_5m"] < ranking_end)
        & (oi["oi_usd"] > 0)
    ]
    if excluded_symbols:
        ranking = ranking[~ranking["symbol"].isin(excluded_symbols)]
    scores = (
        ranking.groupby("symbol", as_index=False)["oi_usd"]
        .median()
        .sort_values("oi_usd", ascending=False)
    )
    coins = tuple(scores.head(top_n)["symbol"].tolist())
    if len(coins) < top_n:
        available = ", ".join(coins) if coins else "không có"
        raise RuntimeError(
            f"Không đủ dữ liệu cho top {top_n} coin. "
            f"Tập OI chỉ có {len(coins)} coin trong giai đoạn "
            f"{ranking_start.date()} -> {ranking_end.date()}: {available}. "
            f"Cần tạo/cung cấp file OI chứa ít nhất {top_n} coin."
        )
    print(
        f"Top {top_n} coin theo median OI tháng train "
        f"({ranking_start.date()} -> {ranking_end.date()}): {', '.join(coins)}"
    )
    return coins


def make_events(
    oi: pd.DataFrame,
    ohlc: dict[str, pd.DataFrame],
    coins: tuple[str, ...],
    entry_delay_bars: int = 1,
    event_start: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if entry_delay_bars < 0 or entry_delay_bars >= HORIZON_BARS:
        raise ValueError("entry_delay_bars phải nằm trong khoảng 0 đến HORIZON_BARS - 1")
    events: list[dict[str, Any]] = []
    for symbol in coins:
        symbol_df = oi[oi["symbol"] == symbol].copy()
        if symbol_df.empty:
            continue
        bars = ohlc[symbol].set_index("bucket_5m").sort_index()
        atr = wilder_atr(bars)
        for segment_id, part in enumerate(contiguous_segments(symbol_df), start=1):
            factor = part["oi_usd"].pct_change()
            lower = factor.rolling(
                LOOKBACK_BARS, min_periods=MIN_PERIODS
            ).quantile(0.01)
            state = (factor <= lower).fillna(False)
            onset = state & ~state.shift(1, fill_value=False)
            for position in np.flatnonzero(onset.to_numpy()):
                end_position = position + HORIZON_BARS
                if end_position >= len(part):
                    continue
                ts = part.loc[position, "bucket_5m"]
                if event_start is not None and ts < event_start:
                    continue
                future_ts = part.loc[position + 1 : end_position, "bucket_5m"]
                atr_value = atr.get(ts, np.nan)
                lows = bars["low"].reindex(future_ts).to_numpy(float)
                if not np.isfinite(atr_value) or not np.isfinite(lows).all():
                    continue
                entry_position = position + entry_delay_bars
                entry_time = part.loc[entry_position, "bucket_5m"]
                if entry_delay_bars == 0:
                    entry = float(bars["close"].get(entry_time, np.nan))
                else:
                    entry = float(bars["open"].get(entry_time, np.nan))
                if not np.isfinite(entry):
                    continue
                path = (
                    bars["close"].reindex(future_ts).to_numpy(float) / entry - 1.0
                )
                if len(path) != HORIZON_BARS or not np.isfinite(path).all():
                    continue
                events.append(
                    {
                        "symbol": symbol,
                        "segment": segment_id,
                        "timestamp": ts,
                        "entry_time": entry_time,
                        "entry_price": entry,
                        "path_long": path.tolist(),
                        "path_times": future_ts.tolist(),
                        "low_path": lows.tolist(),
                        "atr": float(atr_value),
                    }
                )
    if not events:
        raise RuntimeError("Không tạo được event OI q01")
    return pd.DataFrame(events).sort_values(["timestamp", "symbol"]).reset_index(drop=True)


def boost_multiplier(n: int) -> float:
    for minimum, multiplier in BOOST_TIERS:
        if n >= minimum:
            return multiplier
    return 1.0


def simulate_exit(
    events: pd.DataFrame,
    tp: float | None,
    sl: float | None,
) -> pd.DataFrame:
    next_available: dict[str, pd.Timestamp] = {}
    rows = []
    for _, event in events.sort_values(["timestamp", "symbol"]).iterrows():
        symbol = str(event["symbol"])
        entry_time = pd.Timestamp(event["entry_time"])
        if symbol in next_available and entry_time <= next_available[symbol]:
            continue
        path = np.asarray(event["path_long"], dtype=float)
        tp_idx = (
            np.flatnonzero(path >= tp)
            if tp is not None
            else np.array([], dtype=int)
        )
        sl_idx = (
            np.flatnonzero(path <= -sl)
            if sl is not None
            else np.array([], dtype=int)
        )
        first_tp = int(tp_idx[0]) if len(tp_idx) else None
        first_sl = int(sl_idx[0]) if len(sl_idx) else None
        if first_tp is not None and (first_sl is None or first_tp < first_sl):
            gross, exit_type, exit_index = float(tp), "TP", first_tp
        elif first_sl is not None:
            gross, exit_type, exit_index = float(-sl), "SL", first_sl
        else:
            gross, exit_type, exit_index = float(path[-1]), "TIME_60M", len(path) - 1
        exit_time = pd.Timestamp(event["path_times"][exit_index])
        rows.append(
            {
                "symbol": symbol,
                "entry_time": entry_time,
                "exit_time": exit_time,
                "gross_return": gross,
                "net_return": gross - FEE,
                "pnl_usd": INITIAL_CAPITAL * POSITION_PCT * (gross - FEE),
                "exit_type": exit_type,
                "filled_weight": 1.0,
                "size_multiplier": 1.0,
            }
        )
        next_available[symbol] = exit_time
    return pd.DataFrame(rows)


def simulate_dca(events: pd.DataFrame, use_boost: bool) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame()
    counts = events.groupby("timestamp")["symbol"].nunique().to_dict()
    next_available: dict[str, pd.Timestamp] = {}
    rows = []
    for _, event in events.sort_values(["timestamp", "symbol"]).iterrows():
        symbol = str(event["symbol"])
        entry_time = pd.Timestamp(event["entry_time"])
        if symbol in next_available and entry_time <= next_available[symbol]:
            continue
        path = np.asarray(event["path_long"], dtype=float)
        signal_price = float(event["entry_price"])
        atr = float(event["atr"])
        lows = np.asarray(event["low_path"], dtype=float)
        fills = [(signal_price, 1 / 3)]
        for multiple in (1.0, 2.0):
            trigger = signal_price - multiple * atr
            if np.any(lows <= trigger):
                fills.append((trigger, 1 / 3))
        filled_weight = sum(weight for _, weight in fills)
        average_entry = sum(price * weight for price, weight in fills) / filled_weight
        exit_price = signal_price * (1.0 + float(path[-1]))
        gross = exit_price / average_entry - 1.0
        multiplier = boost_multiplier(
            int(counts.get(pd.Timestamp(event["timestamp"]), 1))
        ) if use_boost else 1.0
        net = gross - FEE
        exit_time = pd.Timestamp(event["path_times"][-1])
        rows.append(
            {
                "symbol": symbol,
                "entry_time": entry_time,
                "exit_time": exit_time,
                "gross_return": gross,
                "net_return": net,
                "pnl_usd": INITIAL_CAPITAL * POSITION_PCT * filled_weight * multiplier * net,
                "exit_type": "TIME_60M",
                "filled_weight": filled_weight,
                "size_multiplier": multiplier,
            }
        )
        next_available[symbol] = exit_time
    return pd.DataFrame(rows)


def select_parameters(train: pd.DataFrame) -> tuple[float, float, dict[str, float]]:
    tp_candidates = []
    for tp in TP_GRID:
        candidate = simulate_exit(train, tp, None)
        tp_candidates.append((float(candidate["net_return"].mean()), tp))
    selected_tp = max(tp_candidates, key=lambda item: (item[0], -item[1]))[1]

    pair_candidates = []
    for tp in TP_GRID:
        for sl in SL_GRID:
            candidate = simulate_exit(train, tp, sl)
            pair_candidates.append((float(candidate["net_return"].mean()), tp, sl))
    selected = max(
        pair_candidates,
        key=lambda item: (item[0], -abs(item[1] - item[2]), -item[1] - item[2]),
    )
    return selected_tp, selected[1], {
        "selected_sl": selected[2],
        "train_tp_only_mean": max(tp_candidates)[0],
        "train_tp_sl_mean": selected[0],
    }


def metrics(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0}

    trades = trades.sort_values(["exit_time", "entry_time", "symbol"]).reset_index(drop=True)
    exit_time = pd.to_datetime(trades["exit_time"], utc=True)
    equity = INITIAL_CAPITAL + trades["pnl_usd"].cumsum()
    peak = equity.cummax().clip(lower=INITIAL_CAPITAL)
    drawdown = equity / peak - 1.0
    wins = trades.loc[trades["pnl_usd"] > 0, "pnl_usd"].sum()
    losses = -trades.loc[trades["pnl_usd"] < 0, "pnl_usd"].sum()

    first = pd.Timestamp(trades["entry_time"].min())
    last = pd.Timestamp(trades["exit_time"].max())
    roi = float(equity.iloc[-1] / INITIAL_CAPITAL - 1.0)
    max_dd = float(-drawdown.min())
    timeline = pd.concat(
        [
            trades[["symbol", "entry_time"]]
            .rename(columns={"entry_time": "time"})
            .assign(delta=1),
            trades[["symbol", "exit_time"]]
            .rename(columns={"exit_time": "time"})
            .assign(delta=-1),
        ]
    ).sort_values(["time", "delta"])
    concurrent = timeline.groupby("time")["delta"].cumsum()
    counts = trades["exit_type"].value_counts()

    # Use a regular daily realized-PnL series, including zero-return days.
    # This avoids overstating Sharpe by treating overlapping trades as
    # independent observations. Crypto trades run 24/7, so annualize with
    # sqrt(365); risk-free rate is assumed to be zero.
    daily_pnl = trades.assign(_exit_day=exit_time.dt.floor("D")).groupby(
        "_exit_day"
    )["pnl_usd"].sum()
    daily_index = pd.date_range(
        daily_pnl.index.min(), daily_pnl.index.max(), freq="D", tz="UTC"
    )
    daily_pnl = daily_pnl.reindex(daily_index, fill_value=0.0)
    daily_equity_before = INITIAL_CAPITAL + daily_pnl.cumsum().shift(
        1, fill_value=0.0
    )
    daily_returns = daily_pnl / daily_equity_before
    daily_std = float(daily_returns.std(ddof=1))
    sharpe_ratio = (
        float(daily_returns.mean() / daily_std * np.sqrt(365.0))
        if np.isfinite(daily_std) and daily_std > 0
        else np.nan
    )

    return {
        "trades": int(len(trades)),
        "winrate_pct": float((trades["pnl_usd"] > 0).mean() * 100),
        "roi_pct": roi * 100,
        "max_dd_pct": max_dd * 100,
        # Requested definition: ROI / Max DD, without annualization.
        "calmar": roi / max_dd if max_dd else np.nan,
        "sharpe_ratio": sharpe_ratio,
        "profit_factor": float(wins / losses) if losses else np.inf,
        "max_concurrent": int(concurrent.max()),
        "tp_rate_pct": float(counts.get("TP", 0) / len(trades) * 100),
        "sl_rate_pct": float(counts.get("SL", 0) / len(trades) * 100),
        "time_rate_pct": float(counts.get("TIME_60M", 0) / len(trades) * 100),
        "first_entry": first.isoformat(),
        "last_exit": last.isoformat(),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data",
        type=Path,
        default=CACHE_ROOT / "gfi_oi_5m_30coins_cache" / "oi_5m_30coins_combined.csv",
    )
    parser.add_argument(
        "--start",
        default=DEFAULT_TEST_START,
        help="Thời điểm bắt đầu test OOS; mặc định 2026-06-01",
    )
    parser.add_argument(
        "--train-start",
        default=TRAIN_START.isoformat(),
        help="Thời điểm bắt đầu train; mặc định 2026-05-01",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=TOP_N_COINS,
        help="Số coin chọn theo median OI trong giai đoạn train; mặc định 20",
    )
    parser.add_argument(
        "--ohlc-cache",
        type=Path,
        default=CACHE_ROOT / "hl_ohlc_5m",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=CACHE_ROOT / "oi_tp_sl_walk_hyperliquid_close_entry_open_t1_top20_train_may_test_june",
    )
    parser.add_argument(
        "--entry-delay-bars",
        type=int,
        default=1,
        help="Số nến chờ sau tín hiệu OI trước khi vào lệnh; 1 = tín hiệu t, vào t+1",
    )
    args = parser.parse_args()
    eval_start = parse_utc(args.start)
    train_start = parse_utc(args.train_start)
    if args.top_n <= 0:
        raise ValueError("--top-n phải lớn hơn 0")
    if eval_start <= train_start:
        raise ValueError("--start phải sau --train-start để có dữ liệu train")
    if not args.data.exists():
        raise FileNotFoundError(
            f"Chưa có file OI đầu vào: {args.data}. "
            "Hãy chạy Run_GFI_OI_30Coins_5m.py để tạo cache OI trước, "
            "hoặc truyền đúng file bằng tham số --data."
        )

    oi = pd.read_csv(args.data)
    oi["bucket_5m"] = pd.to_datetime(oi["bucket_5m"], utc=True)
    oi["symbol"] = oi["symbol"].astype(str).str.upper().str.strip()
    oi["oi_usd"] = pd.to_numeric(oi["oi_usd"], errors="coerce")
    oi["mark_close"] = pd.to_numeric(oi["mark_close"], errors="coerce")
    model_oi_start = train_start - LOOKBACK_BARS * BAR
    oi = oi[oi["bucket_5m"] >= model_oi_start].copy()
    coins = select_top_coins(
        oi,
        train_start,
        eval_start,
        args.top_n,
        excluded_symbols=HYPERLIQUID_PRICE_UNAVAILABLE,
    )
    oi = oi[oi["symbol"].isin(coins)].copy()
    oi_min = train_start - pd.Timedelta(hours=2)
    oi_max = oi["bucket_5m"].max() + pd.Timedelta(hours=2)
    hl = load_ohlc(coins, oi_min, oi_max, args.ohlc_cache)

    events = make_events(
        oi,
        hl,
        coins,
        entry_delay_bars=args.entry_delay_bars,
        event_start=train_start,
    ).sort_values(["timestamp", "symbol"]).reset_index(drop=True)
    oos = events[events["timestamp"] >= eval_start].copy()
    if oos.empty:
        raise RuntimeError("Không có event OOS")

    fold_rows = []
    tpsl_tables = []
    month = eval_start.floor("D").replace(day=1)
    last_month = oos["timestamp"].max().floor("D").replace(day=1)
    while month <= last_month:
        fold_start = month
        fold_end = month + pd.offsets.MonthBegin(1)
        train_end = fold_start - HORIZON_BARS * BAR
        train = events[
            (events["timestamp"] >= train_start)
            & (events["timestamp"] < train_end)
        ]
        test = oos[
            (oos["timestamp"] >= fold_start)
            & (oos["timestamp"] < fold_end)
        ]
        if not test.empty and len(train) >= 30:
            _selected_tp, pair_tp, selection = select_parameters(train)
            trades = simulate_exit(test, pair_tp, selection["selected_sl"])
            trades["strategy"] = "TP_SL"
            trades["fold_start"] = fold_start
            trades["selected_tp"] = pair_tp
            trades["selected_sl"] = selection["selected_sl"]
            tpsl_tables.append(trades)
            fold_rows.append(
                {
                    "fold_start": fold_start,
                    "fold_end": fold_end,
                    "train_start": train_start,
                    "train_n": len(train),
                    "test_n": len(test),
                    "tp_sl_tp": pair_tp,
                    "tp_sl_sl": selection["selected_sl"],
                    "train_tp_sl_mean_pct": selection["train_tp_sl_mean"] * 100,
                    "parameter_source": "expanding_train_from_may",
                }
            )
        month = fold_end

    if not tpsl_tables:
        raise RuntimeError("Không có fold TP/SL đủ dữ liệu")

    tpsl_all = pd.concat(tpsl_tables, ignore_index=True)
    summary = pd.DataFrame(
        [{"strategy": "TP_SL", **metrics(tpsl_all)}]
    )
    folds = pd.DataFrame(fold_rows)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary.to_csv(args.output_dir / "summary.csv", index=False)
    folds.to_csv(args.output_dir / "fold_selection.csv", index=False)
    tpsl_all.to_csv(args.output_dir / "trades.csv", index=False)

    with (args.output_dir / "run_meta.json").open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "start": eval_start.isoformat(),
                "train_start": train_start.isoformat(),
                "raw_oi_start": oi["bucket_5m"].min().isoformat(),
                "raw_oi_end": oi["bucket_5m"].max().isoformat(),
                "event_start": events["timestamp"].min().isoformat(),
                "event_end": events["timestamp"].max().isoformat(),
                "oos_event_count": len(oos),
                "coins": list(coins),
                "top_n": args.top_n,
                "coin_ranking": "median oi_usd during initial train window",
                "horizon_minutes": 60,
                "entry_delay_bars": args.entry_delay_bars,
                "ohlc_source": "CoinGlass futures price/history exchange=Hyperliquid",
                "fee_pct_roundtrip": FEE * 100,
                "calmar_formula": "ROI / Max Drawdown",
                "sharpe_formula": (
                    "daily realized PnL returns, zero-return days included, "
                    "risk_free=0, annualized with sqrt(365)"
                ),
                "accounting": "fixed initial notional, 10% per position; DCA filled_weight and boost applied",
            },
            handle,
            indent=2,
        )

    print(f"Nguồn OI: {args.data}")
    print("Nguồn OHLC: CoinGlass futures price/history | exchange=Hyperliquid")
    print(f"Train: {train_start.isoformat()} -> {eval_start.isoformat()}")
    print(f"OOS: {eval_start.isoformat()} -> event cuối {events['timestamp'].max().isoformat()}")
    print(f"Events toàn bộ: {len(events):,} | OOS: {len(oos):,}")
    print("\nFOLD TP/SL - TRAIN MỞ RỘNG TỪ THÁNG 5/2026")
    print(folds.to_string(index=False))
    print("\nKẾT QUẢ DUY NHẤT: TP/SL")
    summary_display = summary[
        [
            "strategy",
            "trades",
            "winrate_pct",
            "roi_pct",
            "max_dd_pct",
            "calmar",
            "sharpe_ratio",
            "profit_factor",
            "max_concurrent",
            "tp_rate_pct",
            "sl_rate_pct",
            "time_rate_pct",
            "first_entry",
            "last_exit",
        ]
    ].rename(
        columns={
            "winrate_pct": "winrate %",
            "roi_pct": "ROI %",
            "max_dd_pct": "max DD %",
            "calmar": "Calmar",
            "sharpe_ratio": "Sharpe",
            "profit_factor": "PF",
            "max_concurrent": "max concurrent",
            "tp_rate_pct": "TP rate %",
            "sl_rate_pct": "SL rate %",
            "time_rate_pct": "time rate %",
            "first_entry": "first entry",
            "last_exit": "last exit",
        }
    )
    print(summary_display.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\nCông thức Calmar không annualize: ROI / Max DD")
    print("Sharpe: lợi nhuận thực nhận theo ngày, gồm cả ngày 0%, annualize sqrt(365), risk-free = 0")
    print(f"Saved: {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
