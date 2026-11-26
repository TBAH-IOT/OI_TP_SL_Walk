"""Coin-independent core of the OI q01 LONG strategy.

Input is a contiguous sequence of *closed* 5-minute USD open-interest values,
oldest first. The caller must reset the sequence after missing or invalid bars.
No exchange orders, TP, SL, or price interpolation happen in this module.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Sequence

import numpy as np


BAR = timedelta(minutes=5)
LOOKBACK_BARS = 7 * 24 * 12
MIN_PERIODS = LOOKBACK_BARS // 4
LOW_QUANTILE = 0.01
HOLD_BARS = 12


def oi_drop_onset(
    oi_usd: Sequence[float],
    *,
    lookback_bars: int = LOOKBACK_BARS,
    min_periods: int = MIN_PERIODS,
    quantile: float = LOW_QUANTILE,
) -> bool:
    """Return whether the last closed bar newly enters the bottom OI q01 state.

    The rolling quantile includes the current 5-minute OI change, as in
    ``check_oi_ls_V2.py`` and the upstream baseline. The previous bar must
    have been outside the state. A history shorter than ``min_periods + 2``
    cannot establish onset.
    """
    if len(oi_usd) < min_periods + 2:
        return False
    values = np.asarray(oi_usd, dtype=float)
    if not np.isfinite(values).all() or np.any(values <= 0):
        return False
    changes = values[1:] / values[:-1] - 1.0
    current_window = changes[-lookback_bars:]
    previous_window = changes[:-1][-lookback_bars:]
    if len(current_window) < min_periods or len(previous_window) < min_periods:
        return False
    current_cutoff = float(np.quantile(current_window, quantile))
    previous_cutoff = float(np.quantile(previous_window, quantile))
    return bool(changes[-1] <= current_cutoff and changes[-2] > previous_cutoff)


def schedule(signal_bar_start: datetime) -> tuple[datetime, datetime, datetime]:
    """Return (entry candle start, exit candle start, scheduled exit instant).

    Enter at open of bar t+1; exit at close of bar t+12. The entry-to-exit
    interval is exactly 60 minutes.
    """
    if signal_bar_start.tzinfo is None:
        raise ValueError("signal_bar_start must include timezone")
    entry = signal_bar_start + BAR
    exit_candle = signal_bar_start + HOLD_BARS * BAR
    scheduled_exit = exit_candle + BAR
    return entry, exit_candle, scheduled_exit


def gross_return(entry_open: float, exit_close: float) -> float:
    """Long return before fees, e.g. 0.01 for +1%."""
    if not all(math.isfinite(v) and v > 0 for v in (entry_open, exit_close)):
        raise ValueError("entry and exit prices must be positive and finite")
    return exit_close / entry_open - 1.0
