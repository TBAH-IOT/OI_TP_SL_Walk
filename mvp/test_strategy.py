from datetime import datetime, timezone

import numpy as np
import pandas as pd

from mvp.strategy import oi_drop_onset, schedule


def test_q01_onset_matches_rolling_reference_and_works_for_any_coin():
    returns = np.array([0.001, 0.002, 0.003, 0.004, 0.005] * 101 + [0.01, -0.10])
    values = np.r_[1000.0, 1000.0 * np.cumprod(1 + returns)]
    changes = pd.Series(values).pct_change(fill_method=None)
    cutoff = changes.rolling(2016, min_periods=504).quantile(0.01)
    states = (changes <= cutoff).fillna(False)
    expected = bool(states.iloc[-1] and not states.iloc[-2])
    assert expected
    assert oi_drop_onset(values) == expected
    assert not oi_drop_onset(values[:-1])


def test_entry_next_open_and_exact_60_minute_exit():
    signal = datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc)
    entry, exit_candle, exit_instant = schedule(signal)
    assert entry.isoformat() == "2026-11-26T09:05:00+00:00"
    assert exit_candle.isoformat() == "2026-11-26T10:00:00+00:00"
    assert exit_instant.isoformat() == "2026-11-26T10:05:00+00:00"
    assert (exit_instant - entry).total_seconds() == 3600
