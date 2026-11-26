"""Import verified upstream trades and optional completed signal paths.

Run OI_TP_SL_WALK.py first. This import does not create synthetic returns.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import pandas as pd

from mvp.live_mvp import HORIZON_BARS, database, parse, stamp


def import_trades(path: Path) -> tuple[int, str, str]:
    frame = pd.read_csv(path)
    required = {"symbol", "entry_time", "exit_time", "pnl_usd", "net_return", "exit_type"}
    missing = required - set(frame)
    if missing:
        raise ValueError(f"trades CSV missing: {sorted(missing)}")
    if frame.empty:
        raise ValueError("trades CSV is empty")
    rows = []
    for row in frame.to_dict("records"):
        entry, exit_ = stamp(parse(row["entry_time"])), stamp(parse(row["exit_time"]))
        pnl, net = float(row["pnl_usd"]), float(row["net_return"])
        if not math.isfinite(pnl) or not math.isfinite(net) or parse(exit_) < parse(entry):
            raise ValueError("invalid trade value or time")
        symbol = str(row["symbol"]).upper()
        key = f"backtest|{symbol}|{entry}|{exit_}|{row['exit_type']}"
        rows.append((hashlib.sha256(key.encode()).hexdigest()[:24], symbol,
                     entry, exit_, pnl, net, str(row["exit_type"]), "backtest"))
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM trades WHERE source='backtest'")
        db.executemany("INSERT INTO trades VALUES(?,?,?,?,?,?,?,?)", rows)
    return len(rows), min(r[2] for r in rows), max(r[3] for r in rows)


def import_paths(path: Path) -> int:
    frame = pd.read_csv(path)
    required = {"symbol", "signal_ts", "path_json"}
    missing = required - set(frame)
    if missing:
        raise ValueError(f"paths CSV missing: {sorted(missing)}")
    rows = []
    for row in frame.to_dict("records"):
        path_values = json.loads(row["path_json"])
        if len(path_values) != HORIZON_BARS or not all(math.isfinite(float(x)) for x in path_values):
            raise ValueError("path must contain 12 finite close returns")
        symbol, ts = str(row["symbol"]).upper(), stamp(parse(row["signal_ts"]))
        key = f"path|{symbol}|{ts}"
        rows.append((hashlib.sha256(key.encode()).hexdigest()[:24], symbol,
                     ts, json.dumps([float(x) for x in path_values])))
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM forecast_paths")
        db.executemany("INSERT INTO forecast_paths VALUES(?,?,?,?)", rows)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trades", type=Path, required=True)
    parser.add_argument("--paths", type=Path)
    args = parser.parse_args()
    count, start, end = import_trades(args.trades)
    print(json.dumps({"trades": count, "first_entry_utc": start,
                      "last_exit_utc": end,
                      "observed_days": (parse(end) - parse(start)).days,
                      "full_year_observed": (parse(end) - parse(start)).days >= 365}, indent=2))
    if args.paths:
        print(json.dumps({"forecast_paths": import_paths(args.paths)}))


if __name__ == "__main__":
    main()
