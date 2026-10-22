from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd


DEFAULT_DATA = (
    Path(r"C:\Users\thaon\Documents\Codex\2026-10-07")
    / "ibygv6p-2gkt6pyogum10rsnrxrkir6ojrg-32safw8-x20-x20"
    / "outputs"
    / "BTC_OI_5m_1y_2025-10-06_2026-10-07.csv"
)
DEFAULT_OUTPUT = Path(r"D:\Live_Trade\gfi_oi_tp_60m_results.csv")

BAR = timedelta(minutes=5)
LOOKBACK_BARS = 7 * 288
MIN_PERIODS = LOOKBACK_BARS // 4
HORIZON_BARS = 12
SIGNALS = ("oi_up_q99", "oi_down_q01")
TP_GRID = (0.001, 0.002, 0.003, 0.005, 0.0075, 0.010)


def load_events(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.read_csv(path)
    required = {"bucket_5m", "oi_usd", "mark_close"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"Thiếu cột bắt buộc: {sorted(missing)}")

    df["bucket_5m"] = pd.to_datetime(df["bucket_5m"], utc=True)
    for col in ["oi_usd", "mark_close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = (
        df.sort_values("bucket_5m")
        .drop_duplicates("bucket_5m")
        .dropna(subset=["bucket_5m", "oi_usd", "mark_close"])
        .reset_index(drop=True)
    )

    segments = []
    breaks = df["bucket_5m"].diff().ne(BAR)
    for _, part in df.groupby(breaks.cumsum()):
        part = part.reset_index(drop=True)
        if len(part) < MIN_PERIODS + HORIZON_BARS:
            continue
        segments.append(part)

    rows: list[dict[str, object]] = []
    for segment_id, part in enumerate(segments, start=1):
        factor = part["oi_usd"].pct_change()
        upper = factor.rolling(
            LOOKBACK_BARS, min_periods=MIN_PERIODS
        ).quantile(0.99)
        lower = factor.rolling(
            LOOKBACK_BARS, min_periods=MIN_PERIODS
        ).quantile(0.01)
        states = {
            "oi_up_q99": (factor >= upper).fillna(False),
            "oi_down_q01": (factor <= lower).fillna(False),
        }

        for signal, state in states.items():
            onset = state & ~state.shift(1, fill_value=False)
            positions = np.flatnonzero(onset.to_numpy())
            for position in positions:
                if position + HORIZON_BARS >= len(part):
                    continue
                entry = float(part.loc[position, "mark_close"])
                path = (
                    part.loc[position + 1 : position + HORIZON_BARS, "mark_close"]
                    .to_numpy(dtype=float)
                    / entry
                    - 1.0
                )
                rows.append(
                    {
                        "segment": segment_id,
                        "timestamp": part.loc[position, "bucket_5m"].isoformat(),
                        "signal": signal,
                        "oi_chg_pct": float(factor.iloc[position]),
                        "return_60m": float(path[HORIZON_BARS - 1]),
                        "path": path.tolist(),
                    }
                )

    events = pd.DataFrame(rows)
    if events.empty:
        raise RuntimeError("Không tạo được event OI")
    return df, events


def summarize_direction(values: np.ndarray) -> tuple[str, float]:
    mean_return = float(values.mean())
    return ("LONG", mean_return) if mean_return >= 0 else ("SHORT", -mean_return)


def evaluate_tp(events: pd.DataFrame, signal: str, direction: str, tp: float | None) -> dict[str, object]:
    part = events[events["signal"] == signal]
    pnl_rows = []
    hold_rows = []
    hit_rows = []

    for _, row in part.iterrows():
        path = np.asarray(row["path"], dtype=float)
        signed_path = path if direction == "LONG" else -path
        if tp is None:
            pnl = float(signed_path[HORIZON_BARS - 1])
            hold = HORIZON_BARS
            hit = False
        else:
            hit_positions = np.flatnonzero(signed_path >= tp)
            if len(hit_positions):
                hold = int(hit_positions[0] + 1)
                pnl = float(tp)
                hit = True
            else:
                hold = HORIZON_BARS
                pnl = float(signed_path[HORIZON_BARS - 1])
                hit = False
        pnl_rows.append(pnl)
        hold_rows.append(hold)
        hit_rows.append(hit)

    pnl = np.asarray(pnl_rows, dtype=float)
    n = len(pnl)
    mean = float(pnl.mean()) if n else np.nan
    std = float(pnl.std(ddof=1)) if n > 1 else np.nan
    se = std / np.sqrt(n) if n > 1 else np.nan
    return {
        "signal": signal,
        "direction": direction,
        "tp": "fixed_60m" if tp is None else tp,
        "n_event": n,
        "n_win": int((pnl > 0).sum()),
        "winrate": float((pnl > 0).mean()) if n else np.nan,
        "mean_pnl": mean,
        "median_pnl": float(np.median(pnl)) if n else np.nan,
        "t_stat": float(mean / se) if np.isfinite(se) and se > 0 else np.nan,
        "tp_hit_rate": float(np.mean(hit_rows)) if n and tp is not None else np.nan,
        "time_exit_rate": float(np.mean(np.asarray(hold_rows) == HORIZON_BARS)) if n else np.nan,
        "avg_hold_5m": float(np.mean(hold_rows)) if n else np.nan,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if not args.data.exists():
        raise FileNotFoundError(f"Không tìm thấy dữ liệu: {args.data}")

    _, events = load_events(args.data)
    result_rows: list[dict[str, object]] = []

    for signal in SIGNALS:
        part = events[events["signal"] == signal]
        values = part["return_60m"].to_numpy(dtype=float)
        direction, avg_expected_pnl = summarize_direction(values)

        baseline = evaluate_tp(events, signal, direction, None)
        baseline["tp_basis"] = "none"
        result_rows.append(baseline)

        mean_tp = max(avg_expected_pnl, 0.0001)
        mean_tp_row = evaluate_tp(events, signal, direction, mean_tp)
        mean_tp_row["tp_basis"] = "mean_60m_return"
        result_rows.append(mean_tp_row)

        for tp in TP_GRID:
            row = evaluate_tp(events, signal, direction, tp)
            row["tp_basis"] = "grid"
            result_rows.append(row)

    result = pd.DataFrame(result_rows)
    result.to_csv(args.output, index=False)

    print(f"Nguồn dữ liệu: {args.data}")
    print(
        f"Khoảng dữ liệu: {events['timestamp'].min()} -> {events['timestamp'].max()}"
    )
    print(f"Tổng event: {len(events)}")
    print("\nKẾT QUẢ TP SỚM | MAX HOLD = 60M | GROSS, CHƯA TRỪ PHÍ")
    display = result.copy()
    for col in ["tp", "winrate", "mean_pnl", "median_pnl", "tp_hit_rate", "time_exit_rate"]:
        if col == "tp":
            display[col] = display[col].map(
                lambda x: x if isinstance(x, str) else f"{x * 100:.3f}%"
            )
        else:
            display[col] = display[col].map(
                lambda x: f"{x * 100:.3f}%" if pd.notna(x) else "NA"
            )
    display["t_stat"] = display["t_stat"].map(
        lambda x: f"{x:.3f}" if pd.notna(x) else "NA"
    )
    display["avg_hold_5m"] = display["avg_hold_5m"].map(
        lambda x: f"{x:.2f}" if pd.notna(x) else "NA"
    )
    print(display.to_string(index=False))
    print(f"\nĐã lưu: {args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"LỖI: {exc}", file=sys.stderr)
        raise SystemExit(1)
