import io
import os
from contextlib import redirect_stdout
from math import ceil

import numpy as np
import pandas as pd

import tim_to_hop_down as base


UP_COMBOS = [
    ("OI:L+NETPOS:L", [("oi_chg_pct", "L"), ("net_position_norm", "L")]),
]

DOWN_COMBOS = [
    (
        "NETPOS:H+FUND:L+CALLPUSH:L",
        [
            ("net_position_norm", "H"),
            ("funding_rate", "L"),
            ("call_push_ratio", "L"),
        ],
    ),
]

HORIZON_LABEL = None

BACKTEST_COIN_CANDIDATES = [
    "BTC", "ETH", "SOL", "ZEC", "BNB", "XRP",
    "HYPE", "SKHYNIX", "DOGE", "UNI", "NEAR", "SKHY",
    "BZ", "SUI", "ENA", "BCH", "LINK", "SOXL", "BTW", "WLD",
]
EXCLUDED_COINS = {"HYPE", "SKHYNIX"}
BACKTEST_TARGET_COINS = 10

INITIAL_EQUITY = 10_000.0
ALLOC_PCT_PER_TRADE = 0.10

# Tổng chi phí của cả mô hình cho mỗi vị thế: 0.10% round-trip.
# Không nhân thêm theo số lần khớp DCA.
TOTAL_COST_PCT_ROUNDTRIP = 0.001
RISK_FREE_RATE_PER_TRADE = 0.0

# ATR Wilder chuẩn.
ATR_PERIOD = 14

# Mỗi mốc gồm (số ATR bất lợi từ giá tín hiệu, tỉ trọng vốn).
# Mốc 0 ATR là phần vào lệnh ban đầu; các mốc còn lại là lệnh DCA.
DCA_ATR_LEVELS = [
    (0.0, 1 / 3),
    (1.0, 1 / 3),
    (2.0, 1 / 3),
]

ATR_CACHE_PREFIX = "atr_ohlc_"


def _read_valid_cached_dataset(coin):
    path = base.dataset_cache_path(coin)
    if not os.path.exists(path):
        return None

    try:
        df = pd.read_csv(path, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True)
    except Exception:
        return None

    required = ["price_close"] + list(base.FACTOR_COLS)
    if df.empty or any(column not in df.columns for column in required):
        return None
    return df.sort_index()


def _atr_cache_path(coin):
    return os.path.join(base.OUTPUT_DIR, f"{ATR_CACHE_PREFIX}{coin}.csv")


def _normalise_ohlc(df):
    if df is None or df.empty:
        return None

    source_df = df.copy()
    if not isinstance(source_df.index, pd.DatetimeIndex):
        if "dt" not in source_df.columns:
            return None
        source_df.index = pd.to_datetime(source_df["dt"], utc=True, errors="coerce")

    column_map = {}
    for target, candidates in {
        "high": ("high", "high_price"),
        "low": ("low", "low_price"),
        "close": ("close", "close_price"),
    }.items():
        source = next((c for c in candidates if c in source_df.columns), None)
        if source is None:
            return None
        column_map[target] = source

    result = source_df[[column_map["high"], column_map["low"], column_map["close"]]].copy()
    result.columns = ["high", "low", "close"]
    for column in result.columns:
        result[column] = pd.to_numeric(result[column], errors="coerce")

    result = result[~result.index.isna()]
    result = result.replace([np.inf, -np.inf], np.nan).dropna()
    result = result[(result["high"] >= result["low"]) & (result["low"] > 0)]
    result = result.sort_index()
    return result[~result.index.duplicated(keep="last")]


def _load_or_fetch_ohlc(coin, days):
    path = _atr_cache_path(coin)

    if os.path.exists(path):
        try:
            cached = pd.read_csv(path, index_col=0)
            cached.index = pd.to_datetime(cached.index, utc=True)
            cached = _normalise_ohlc(cached)
            if cached is not None and not cached.empty:
                return cached
        except Exception:
            pass

    with redirect_stdout(io.StringIO()):
        raw = base.fetch_history(
            base.ENDPOINTS["price"],
            {
                "exchange": base.EXCHANGE,
                "symbol": f"{coin}{base.QUOTE}",
                "interval": base.INTERVAL,
            },
            days=days,
        )

    ohlc = _normalise_ohlc(raw)
    if ohlc is None or ohlc.empty:
        return None

    try:
        ohlc.to_csv(path)
    except OSError:
        pass
    return ohlc


def _wilder_atr(high, low, close, period):
    high = pd.Series(high, dtype="float64").to_numpy()
    low = pd.Series(low, dtype="float64").to_numpy()
    close = pd.Series(close, dtype="float64").to_numpy()

    previous_close = np.roll(close, 1)
    previous_close[0] = np.nan
    true_range = np.nanmax(
        np.vstack(
            [
                high - low,
                np.abs(high - previous_close),
                np.abs(low - previous_close),
            ]
        ),
        axis=0,
    )

    atr = np.full(len(true_range), np.nan, dtype="float64")
    if len(true_range) < period:
        return atr

    first_window = true_range[:period]
    if not np.isfinite(first_window).all():
        return atr

    atr[period - 1] = first_window.mean()
    for i in range(period, len(true_range)):
        if not np.isfinite(true_range[i]):
            atr[i] = atr[i - 1]
        else:
            atr[i] = ((period - 1) * atr[i - 1] + true_range[i]) / period
    return atr


def load_all_coin_data():
    coin_data = {}
    min_needed = base.BARS_PER_DAY * (base.LOOKBACK_DAYS + 3)

    for coin in BACKTEST_COIN_CANDIDATES:
        if coin in EXCLUDED_COINS:
            continue

        factor_df = _read_valid_cached_dataset(coin)
        if factor_df is None or len(factor_df) < min_needed:
            continue

        span_days = max(
            1,
            ceil((factor_df.index.max() - factor_df.index.min()).total_seconds() / 86400.0) + 2,
        )
        ohlc = _load_or_fetch_ohlc(coin, span_days)
        if ohlc is None:
            continue

        df = factor_df.join(ohlc[["high", "low", "close"]], how="inner")
        if len(df) < min_needed:
            continue

        try:
            masks = base.compute_masks(df)
        except Exception:
            continue

        high = df["high"].to_numpy(dtype="float64")
        low = df["low"].to_numpy(dtype="float64")
        close = df["price_close"].to_numpy(dtype="float64")
        atr = _wilder_atr(high, low, close, ATR_PERIOD)

        coin_data[coin] = {
            "masks": masks,
            "price": close,
            "high": high,
            "low": low,
            "atr": atr,
            "dt_index": df.index,
        }

        if len(coin_data) >= BACKTEST_TARGET_COINS:
            break

    if not coin_data:
        raise RuntimeError("Không có coin nào có đủ cache factor và dữ liệu OHLC để tính ATR.")

    return coin_data


def validate_combo(combo, coin_data, label):
    any_coin = next(iter(coin_data.values()))
    available = any_coin["masks"]
    problems = []
    for factor, direction in combo:
        if factor not in available:
            problems.append(f"[{label}] factor={factor}")
        elif direction not in available[factor]:
            problems.append(f"[{label}] direction={factor}:{direction}")
    if problems:
        raise ValueError("Invalid combo: " + ", ".join(problems))


def _combo_combined_mask(combo, masks):
    combined = None
    for factor, direction in combo:
        current = masks[factor][direction]
        combined = current.copy() if combined is None else (combined & current)
    return combined


def filter_no_reentry_while_open(idx_positions, horizon_bars):
    if len(idx_positions) == 0:
        return idx_positions

    kept = [idx_positions[0]]
    last_kept = idx_positions[0]
    for idx in idx_positions[1:]:
        if idx >= last_kept + horizon_bars:
            kept.append(idx)
            last_kept = idx
    return np.asarray(kept, dtype=idx_positions.dtype)


def _signal_indices(combos, masks, n, horizon_bars):
    union_mask = None
    for _, combo in combos:
        current = _combo_combined_mask(combo, masks)
        union_mask = current.copy() if union_mask is None else (union_mask | current)

    shifted = np.roll(union_mask, 1)
    shifted[0] = False
    onset_mask = union_mask & (~shifted)
    idx_positions = np.where(onset_mask)[0]
    idx_positions = filter_no_reentry_while_open(idx_positions, horizon_bars)
    return idx_positions[idx_positions + horizon_bars < n]


def extract_trades(combos, coin_data, side, horizon_bars, source_label="strategy"):
    all_trades = []

    for coin, data in coin_data.items():
        price = data["price"]
        dt_index = data["dt_index"]
        valid_idx = _signal_indices(combos, data["masks"], len(price), horizon_bars)

        for i0 in valid_idx:
            exit_idx = i0 + horizon_bars
            entry_price = price[i0]
            exit_price = price[exit_idx]
            price_return = exit_price / entry_price - 1.0
            trade_return = price_return if side == "long" else -price_return
            all_trades.append(
                {
                    "coin": coin,
                    "source": source_label,
                    "entry_time": dt_index[i0],
                    "exit_time": dt_index[exit_idx],
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "side": side,
                    "price_return": price_return,
                    "trade_return": trade_return,
                    "n_dca_fills": 1,
                }
            )

    return pd.DataFrame(all_trades)


def extract_trades_dca(combos, coin_data, side, horizon_bars,
                       dca_levels=None, source_label="strategy_DCA"):
    if dca_levels is None:
        dca_levels = DCA_ATR_LEVELS

    total_weight = sum(weight for _, weight in dca_levels)
    if abs(total_weight - 1.0) > 1e-9:
        raise ValueError(f"Tổng trọng số DCA_ATR_LEVELS phải bằng 1.0, hiện tại={total_weight}")

    all_trades = []

    for coin, data in coin_data.items():
        price = data["price"]
        high = data["high"]
        low = data["low"]
        atr = data["atr"]
        dt_index = data["dt_index"]
        valid_idx = _signal_indices(combos, data["masks"], len(price), horizon_bars)

        for i0 in valid_idx:
            atr_value = atr[i0]
            if not np.isfinite(atr_value) or atr_value <= 0:
                continue

            signal_price = price[i0]
            exit_idx = i0 + horizon_bars
            exit_price = price[exit_idx]

            fills = [(signal_price, dca_levels[0][1], 0.0)]
            for atr_multiple, weight in dca_levels[1:]:
                if side == "long":
                    trigger_price = signal_price - atr_multiple * atr_value
                    hit = np.where(low[i0 + 1:exit_idx + 1] <= trigger_price)[0]
                else:
                    trigger_price = signal_price + atr_multiple * atr_value
                    hit = np.where(high[i0 + 1:exit_idx + 1] >= trigger_price)[0]

                if len(hit) > 0:
                    fills.append((trigger_price, weight, atr_multiple))

            total_filled_weight = sum(weight for _, weight, _ in fills)
            average_entry = sum(price_value * weight for price_value, weight, _ in fills)
            average_entry /= total_filled_weight

            price_return = exit_price / average_entry - 1.0
            trade_return = price_return if side == "long" else -price_return
            atr_pct = atr_value / signal_price * 100.0

            all_trades.append(
                {
                    "coin": coin,
                    "source": source_label,
                    "entry_time": dt_index[i0],
                    "exit_time": dt_index[exit_idx],
                    "entry_price": average_entry,
                    "signal_price": signal_price,
                    "exit_price": exit_price,
                    "side": side,
                    "price_return": price_return,
                    "trade_return": trade_return,
                    "atr_value": atr_value,
                    "atr_pct": atr_pct,
                    "n_dca_fills": len(fills),
                    "total_filled_weight": total_filled_weight,
                }
            )

    return pd.DataFrame(all_trades)


def _max_concurrent_trades(df):
    events = []
    for _, row in df.iterrows():
        events.append((row["entry_time"], 1))
        events.append((row["exit_time"], -1))
    events.sort(key=lambda item: (item[0], item[1]))

    current = 0
    peak = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def simulate_equity(trades_df, initial_equity, alloc_pct, total_cost_pct):
    if trades_df.empty:
        return trades_df, {
            "n_trades": 0,
            "n_positions": 0,
            "total_dca_fills": 0,
            "net_profit_pct": np.nan,
            "profit_factor": np.nan,
            "winrate_pct": np.nan,
            "accuracy_direction_pct": np.nan,
            "max_drawdown_pct": np.nan,
            "avg_return_per_trade_pct": np.nan,
            "sharpe_ratio": np.nan,
            "final_equity": initial_equity,
            "max_concurrent_trades": 0,
        }

    df = trades_df.sort_values("exit_time").reset_index(drop=True)
    if "total_filled_weight" not in df.columns:
        df["total_filled_weight"] = 1.0

    df["total_cost_pct_applied"] = total_cost_pct
    df["net_trade_return"] = df["trade_return"] - total_cost_pct

    equity = initial_equity
    equity_curve = []
    for net_return, filled_weight in zip(
        df["net_trade_return"], df["total_filled_weight"]
    ):
        pnl = equity * alloc_pct * filled_weight * net_return
        equity += pnl
        equity_curve.append(equity)
    df["equity_after_trade"] = equity_curve

    running_max = df["equity_after_trade"].cummax()
    drawdown = (df["equity_after_trade"] - running_max) / running_max
    max_drawdown_pct = drawdown.min() * 100.0

    gross_profit = df.loc[df["net_trade_return"] > 0, "net_trade_return"].sum()
    gross_loss = -df.loc[df["net_trade_return"] < 0, "net_trade_return"].sum()
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.nan

    n_positions = len(df)
    winrate_pct = (df["net_trade_return"] > 0).mean() * 100.0
    accuracy_direction_pct = (df["trade_return"] > 0).mean() * 100.0
    final_equity = equity_curve[-1]
    net_profit_pct = (final_equity / initial_equity - 1.0) * 100.0

    avg_return = df["net_trade_return"].mean()
    std_return = df["net_trade_return"].std(ddof=1) if n_positions > 1 else np.nan
    sharpe_ratio = (
        (avg_return - RISK_FREE_RATE_PER_TRADE) / std_return
        if pd.notna(std_return) and std_return > 0
        else np.nan
    )

    total_dca_fills = int(df["n_dca_fills"].sum()) if "n_dca_fills" in df else n_positions
    metrics = {
        "n_trades": n_positions,
        "n_positions": n_positions,
        "total_dca_fills": total_dca_fills,
        "net_profit_pct": net_profit_pct,
        "profit_factor": profit_factor,
        "winrate_pct": winrate_pct,
        "accuracy_direction_pct": accuracy_direction_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "avg_return_per_trade_pct": avg_return * 100.0,
        "sharpe_ratio": sharpe_ratio,
        "final_equity": final_equity,
        "max_concurrent_trades": _max_concurrent_trades(df),
        "avg_dca_fills": df["n_dca_fills"].mean() if "n_dca_fills" in df else 1.0,
        "pct_trades_full_dca": (
            (df["n_dca_fills"] == len(DCA_ATR_LEVELS)).mean() * 100.0
            if "n_dca_fills" in df
            else 0.0
        ),
        "avg_atr_pct": df["atr_pct"].mean() if "atr_pct" in df else np.nan,
    }
    return df, metrics


def _number(value, digits=4):
    return "nan" if pd.isna(value) else f"{value:.{digits}f}"


def print_metrics(label, metrics):
    print(f"strategy: {label}")
    print(f"n_positions: {metrics['n_positions']}")
    print(f"total_dca_fills: {metrics['total_dca_fills']}")
    print(f"winrate_pct: {_number(metrics['winrate_pct'], 1)}%")
    print(f"accuracy_direction_pct: {_number(metrics['accuracy_direction_pct'], 1)}%")
    print(f"net_profit_pct: {_number(metrics['net_profit_pct'], 2)}%")
    print(f"final_equity: {_number(metrics['final_equity'], 2)}")
    print(f"profit_factor: {_number(metrics['profit_factor'], 2)}")
    print(f"max_drawdown_pct: {_number(metrics['max_drawdown_pct'], 2)}%")
    print(f"avg_return_per_trade_pct: {_number(metrics['avg_return_per_trade_pct'], 3)}%")
    print(f"sharpe_ratio: {_number(metrics['sharpe_ratio'], 4)}")
    print(f"max_concurrent_trades: {metrics['max_concurrent_trades']}")
    print(f"avg_dca_fills: {_number(metrics.get('avg_dca_fills', np.nan), 2)}")
    print(f"pct_trades_full_dca: {_number(metrics.get('pct_trades_full_dca', np.nan), 1)}%")
    print(f"avg_atr_pct: {_number(metrics.get('avg_atr_pct', np.nan), 3)}%")
    print()


def _simulate_pair(up_trades, down_trades):
    frames = [frame for frame in (up_trades, down_trades) if not frame.empty]
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return simulate_equity(
        combined,
        INITIAL_EQUITY,
        ALLOC_PCT_PER_TRADE,
        TOTAL_COST_PCT_ROUNDTRIP,
    )


def run():
    coin_data = load_all_coin_data()
    for label, combo in UP_COMBOS + DOWN_COMBOS:
        validate_combo(combo, coin_data, label)

    horizon_label = HORIZON_LABEL or base.HORIZON_LABELS[0]
    horizon_bars = base.HORIZONS_BARS[base.HORIZON_LABELS.index(horizon_label)]

    up_trades = extract_trades(UP_COMBOS, coin_data, "long", horizon_bars, "UP_OR")
    down_trades = extract_trades(DOWN_COMBOS, coin_data, "short", horizon_bars, "DOWN_OR")
    _, baseline_metrics = _simulate_pair(up_trades, down_trades)

    up_trades_dca = extract_trades_dca(
        UP_COMBOS, coin_data, "long", horizon_bars, DCA_ATR_LEVELS, "UP_OR_DCA_ATR"
    )
    down_trades_dca = extract_trades_dca(
        DOWN_COMBOS, coin_data, "short", horizon_bars, DCA_ATR_LEVELS, "DOWN_OR_DCA_ATR"
    )
    _, dca_metrics = _simulate_pair(up_trades_dca, down_trades_dca)

    print_metrics("baseline", baseline_metrics)
    print_metrics("DCA_ATR", dca_metrics)

    print(f"atr_period: {ATR_PERIOD}")
    print(
        "dca_atr_levels: "
        + ", ".join(f"{multiple:.1f}x/{weight:.2f}" for multiple, weight in DCA_ATR_LEVELS)
    )
    print(f"total_cost_pct_roundtrip: {TOTAL_COST_PCT_ROUNDTRIP * 100:.2f}%")


if __name__ == "__main__":
    run()
