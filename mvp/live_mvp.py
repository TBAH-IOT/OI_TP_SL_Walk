"""Reference live API for the OI q01 LONG baseline, held exactly 60 minutes.

Market adapters send *closed* OI/OHLC candles and next-candle open prices. No
exchange orders are placed. Times are UTC, and candle timestamps are starts.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from OI_TP_SL_WALK import (
    BAR, FEE, HORIZON_BARS, INITIAL_CAPITAL, LOOKBACK_BARS, MIN_PERIODS,
    POSITION_PCT, metrics,
)
from mvp.strategy import gross_return, oi_drop_onset, schedule

UTC = timezone.utc
DB_PATH = Path(os.getenv("MVP_DB_PATH", "mvp.sqlite3"))
INGEST_TOKEN = os.getenv("MVP_INGEST_TOKEN", "")
SYMBOLS = {symbol.strip() for symbol in os.getenv("MVP_SYMBOLS", "").split(",")
           if symbol.strip()}
BAR_SECONDS = 300
HORIZON_SECONDS = HORIZON_BARS * BAR_SECONDS
app = FastAPI(title="GFI OI Baseline MVP reference API", version="2.0.0")


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(UTC)


def stamp(value: datetime) -> str:
    return utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def finite_positive(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("price and OI values must be finite and positive")
    return value


class ClosedBar(BaseModel):
    symbol: str = Field(min_length=1, max_length=50, pattern=r"^[^\s/]+$")
    ts: datetime  # beginning of a fully closed 5-minute bar
    oi_usd: float | None = None
    mark_close: float | None = None
    open: float
    high: float
    low: float
    close: float
    historical: bool = False  # warmup/catch-up bars never create notifications

    @field_validator("ts")
    @classmethod
    def check_ts(cls, value: datetime) -> datetime:
        value = utc(value)
        if int(value.timestamp()) % BAR_SECONDS:
            raise ValueError("ts must be aligned to a 5-minute UTC candle")
        return value

    @field_validator("oi_usd", "mark_close", "open", "high", "low", "close")
    @classmethod
    def check_positive(cls, value: float | None) -> float | None:
        return finite_positive(value) if value is not None else None


class BarOpen(BaseModel):
    symbol: str = Field(min_length=1, max_length=50, pattern=r"^[^\s/]+$")
    ts: datetime  # beginning of the new candle
    open: float

    @field_validator("ts")
    @classmethod
    def check_ts(cls, value: datetime) -> datetime:
        value = utc(value)
        if int(value.timestamp()) % BAR_SECONDS:
            raise ValueError("ts must be aligned to a 5-minute UTC candle")
        return value

    @field_validator("open")
    @classmethod
    def check_positive(cls, value: float) -> float:
        return finite_positive(value)


class PriceTick(BaseModel):
    symbol: str = Field(min_length=1, max_length=50, pattern=r"^[^\s/]+$")
    ts: datetime
    price: float

    @field_validator("ts")
    @classmethod
    def check_ts(cls, value: datetime) -> datetime:
        return utc(value)

    @field_validator("price")
    @classmethod
    def check_positive(cls, value: float) -> float:
        return finite_positive(value)


def authorize(authorization: str | None = Header(default=None)) -> None:
    if not INGEST_TOKEN:
        raise HTTPException(503, "Set MVP_INGEST_TOKEN before ingesting data")
    if authorization != f"Bearer {INGEST_TOKEN}":
        raise HTTPException(401, "Invalid ingestion token")


def authorized_symbol(symbol: str) -> None:
    if not SYMBOLS:
        raise HTTPException(503, "Set MVP_SYMBOLS to the trained coin universe")
    if symbol not in SYMBOLS:
        raise HTTPException(422, "Symbol is outside the trained coin universe")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS bars (
      symbol TEXT NOT NULL, ts TEXT NOT NULL, oi_usd REAL,
      mark_close REAL, open REAL NOT NULL, high REAL NOT NULL,
      low REAL NOT NULL, close REAL NOT NULL, PRIMARY KEY(symbol,ts));
    CREATE TABLE IF NOT EXISTS signals (
      id TEXT PRIMARY KEY, symbol TEXT NOT NULL, signal_ts TEXT NOT NULL,
      entry_ts TEXT NOT NULL, entry_price REAL, status TEXT NOT NULL, exit_ts TEXT,
      exit_type TEXT, exit_price REAL, net_return REAL, pnl_usd REAL,
      forecast_json TEXT, forecast_n INTEGER NOT NULL DEFAULT 0,
      actual_json TEXT NOT NULL DEFAULT '[]', last_price REAL,
      last_price_ts TEXT, created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS signals_recent ON signals(signal_ts DESC);
    CREATE TABLE IF NOT EXISTS trades (
      id TEXT PRIMARY KEY, symbol TEXT NOT NULL, entry_time TEXT NOT NULL,
      exit_time TEXT NOT NULL, pnl_usd REAL NOT NULL, net_return REAL NOT NULL,
      exit_type TEXT NOT NULL, source TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS trades_recent ON trades(exit_time DESC,symbol);
    CREATE TABLE IF NOT EXISTS forecast_paths (
      id TEXT PRIMARY KEY, symbol TEXT NOT NULL, signal_ts TEXT NOT NULL,
      path_json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS forecast_asof ON forecast_paths(symbol,signal_ts);
    """)
    return db


@contextmanager
def database():
    db = connect()
    try:
        with db:
            yield db
    finally:
        db.close()


def signal_id(symbol: str, signal_ts: str) -> str:
    return hashlib.sha256(f"OI_BASELINE_LONG|{symbol}|{signal_ts}".encode()).hexdigest()[:24]


def signal_onset(db: sqlite3.Connection, symbol: str, ts: str) -> bool:
    rows = db.execute(
        "SELECT ts,oi_usd,mark_close FROM bars WHERE symbol=? AND ts<=? "
        "ORDER BY ts DESC LIMIT ?", (symbol, ts, LOOKBACK_BARS + 4)
    ).fetchall()
    if len(rows) < MIN_PERIODS + 2 or rows[0]["oi_usd"] is None:
        return False
    rows = list(reversed(rows))
    # The upstream model splits at gaps. A gap resets its rolling OI window.
    cut = 0
    for i in range(1, len(rows)):
        if ((parse(rows[i]["ts"]) - parse(rows[i-1]["ts"])).total_seconds() != BAR_SECONDS
                or rows[i]["oi_usd"] is None or rows[i-1]["oi_usd"] is None):
            cut = i
    rows = rows[cut:]
    if len(rows) < MIN_PERIODS + 2 or rows[-1]["oi_usd"] is None:
        return False
    return oi_drop_onset([r["oi_usd"] for r in rows],
                         lookback_bars=LOOKBACK_BARS, min_periods=MIN_PERIODS)


def forecast_for(db: sqlite3.Connection, symbol: str, signal_ts: str) -> tuple[list[float] | None, int]:
    # Closed historical paths only. Each path is 12 future 5m close returns
    # relative to the real next-candle open. No future path enters a forecast.
    cutoff = stamp(parse(signal_ts) - timedelta(seconds=HORIZON_SECONDS + BAR_SECONDS))
    rows = db.execute(
        "SELECT path_json FROM forecast_paths WHERE symbol=? AND signal_ts<=? "
        "ORDER BY signal_ts DESC LIMIT 500", (symbol, cutoff)
    ).fetchall()
    paths = []
    for row in rows:
        path = json.loads(row["path_json"])
        if len(path) == HORIZON_BARS and all(math.isfinite(float(x)) for x in path):
            paths.append(path)
    if not paths:
        return None, 0
    return np.mean(np.asarray(paths, dtype=float), axis=0).tolist(), len(paths)


def trim_signals(db: sqlite3.Connection) -> None:
    # Keep five user-facing signals. Trades and forecast paths stay as an
    # independent ledger so performance remains correct after pruning.
    db.execute("DELETE FROM signals WHERE status!='OPEN' AND id NOT IN "
               "(SELECT id FROM signals ORDER BY signal_ts DESC,id DESC LIMIT 5)")


def expire_missing(db: sqlite3.Connection) -> None:
    # Allow ten minutes for delayed final candles, then stop stale open records
    # from blocking later signals indefinitely. Never assign fabricated PnL.
    cutoff = stamp(datetime.now(UTC) - timedelta(seconds=HORIZON_SECONDS + 600))
    db.execute("UPDATE signals SET status='DATA_MISSING' WHERE status='OPEN' AND entry_ts<=?",
               (cutoff,))
    trim_signals(db)


def serialize_signal(row: sqlite3.Row) -> dict:
    data = dict(row)
    returns = json.loads(data.pop("forecast_json")) if data.get("forecast_json") else None
    actual = json.loads(data.pop("actual_json"))
    data["side"] = "LONG"
    window_ended = datetime.now(UTC) >= parse(data["entry_ts"]) + timedelta(seconds=HORIZON_SECONDS)
    data["status_label"] = "Đã kết thúc" if data["status"] != "OPEN" or window_ended else "Đang mở"
    data["data_gap"] = data["status"] == "DATA_MISSING" or (data["status"] == "OPEN" and window_ended)
    data["entry_zone"] = [data["entry_price"], data["entry_price"]] if data["entry_price"] else None
    data["scheduled_exit_ts"] = stamp(schedule(parse(data["signal_ts"]))[2])
    data["forecast"] = (
        [{"ts": stamp(parse(data["entry_ts"]) + timedelta(minutes=5 * i)),
          "price": data["entry_price"] * (1 + value)}
         for i, value in enumerate(returns, 1)]
        if returns is not None and data["entry_price"] else None
    )
    data["actual"] = actual
    return data


def close_trade(db: sqlite3.Connection, row: sqlite3.Row, bar: ClosedBar) -> None:
    if row["entry_price"] is None:
        return
    entry = float(row["entry_price"])
    elapsed = (bar.ts - parse(row["entry_ts"])).total_seconds()
    if elapsed < 0:
        return
    if elapsed > HORIZON_SECONDS - BAR_SECONDS:
        db.execute("UPDATE signals SET status='DATA_MISSING' WHERE id=?", (row["id"],))
        return
    if elapsed < HORIZON_SECONDS - BAR_SECONDS:
        return
    first_ts = row["entry_ts"]
    last_ts = stamp(bar.ts)
    bars = db.execute(
        "SELECT ts,close FROM bars WHERE symbol=? AND ts>=? AND ts<=? ORDER BY ts",
        (row["symbol"], first_ts, last_ts),
    ).fetchall()
    if len(bars) != HORIZON_BARS or any(
        (parse(bars[i]["ts"]) - parse(bars[i-1]["ts"])).total_seconds() != BAR_SECONDS
        for i in range(1, len(bars))
    ):
        db.execute("UPDATE signals SET status='DATA_MISSING' WHERE id=?", (row["id"],))
        return
    exit_type, exit_price = "TIME_60M", float(bars[-1]["close"])
    gross = gross_return(entry, exit_price)
    exit_ts = stamp(bar.ts + timedelta(minutes=5))
    net = gross - FEE
    pnl = INITIAL_CAPITAL * POSITION_PCT * net
    db.execute(
        "UPDATE signals SET status='CLOSED',exit_ts=?,exit_type=?,exit_price=?,"
        "net_return=?,pnl_usd=? WHERE id=?",
        (exit_ts, exit_type, exit_price, net, pnl, row["id"]),
    )
    db.execute(
        "INSERT OR IGNORE INTO trades(id,symbol,entry_time,exit_time,pnl_usd,net_return,exit_type,source) "
        "VALUES(?,?,?,?,?,?,?,'live')",
        (row["id"], row["symbol"], row["entry_ts"], exit_ts, pnl, net, exit_type),
    )
    db.execute("INSERT OR IGNORE INTO forecast_paths VALUES(?,?,?,?)",
               (row["id"], row["symbol"], row["signal_ts"],
                json.dumps([float(x["close"]) / entry - 1.0 for x in bars])))


@app.post("/v1/bar-close", dependencies=[Depends(authorize)])
def bar_close(bar: ClosedBar) -> dict:
    authorized_symbol(bar.symbol)
    if not (bar.low <= min(bar.open, bar.close) <= bar.high
            and bar.low <= max(bar.open, bar.close) <= bar.high):
        raise HTTPException(422, "Invalid OHLC range")
    now = datetime.now(UTC)
    if bar.ts + timedelta(minutes=5) > now + timedelta(seconds=30):
        raise HTTPException(422, "Candle has not closed")
    ts = stamp(bar.ts)
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        expire_missing(db)
        existing = db.execute("SELECT 1 FROM bars WHERE symbol=? AND ts=?", (bar.symbol, ts)).fetchone()
        if existing:
            return {"accepted": False, "reason": "duplicate"}
        latest = db.execute("SELECT max(ts) AS ts FROM bars WHERE symbol=?", (bar.symbol,)).fetchone()["ts"]
        if latest and ts < latest:
            raise HTTPException(409, "Out-of-order candle; use offline backfill")
        db.execute("INSERT INTO bars VALUES(?,?,?,?,?,?,?,?)",
                   (bar.symbol, ts, bar.oi_usd, bar.mark_close,
                    bar.open, bar.high, bar.low, bar.close))
        # If the open event was missed, recover the actual entry from this
        # closed candle. The card was already created at signal time.
        pending = db.execute(
            "SELECT id FROM signals WHERE symbol=? AND entry_ts=? AND entry_price IS NULL",
            (bar.symbol, ts),
        ).fetchone()
        if pending:
            _set_entry(db, pending["id"], bar.open)
        open_signal = db.execute(
            "SELECT * FROM signals WHERE symbol=? AND status='OPEN' AND entry_ts<=?",
            (bar.symbol, ts),
        ).fetchone()
        if open_signal:
            actual = json.loads(open_signal["actual_json"])
            actual.append({"ts": stamp(bar.ts + timedelta(minutes=5)), "price": bar.close})
            db.execute("UPDATE signals SET actual_json=?,last_price=?,last_price_ts=? WHERE id=?",
                       (json.dumps(actual), bar.close, stamp(bar.ts + timedelta(minutes=5)), open_signal["id"]))
            close_trade(db, open_signal, bar)
            trim_signals(db)
        triggered = False
        if not bar.historical and bar.oi_usd is not None and signal_onset(db, bar.symbol, ts):
            active = db.execute("SELECT 1 FROM signals WHERE symbol=? AND status='OPEN' AND entry_ts>?",
                                (bar.symbol, stamp(bar.ts - timedelta(seconds=HORIZON_SECONDS)))).fetchone()
            if not active:
                sid = signal_id(bar.symbol, ts)
                forecast, n = forecast_for(db, bar.symbol, ts)
                entry_ts = stamp(schedule(bar.ts)[0])
                previous = list(reversed(db.execute(
                    "SELECT ts,close FROM bars WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT 24",
                    (bar.symbol, ts)).fetchall()))
                actual = [{"ts": stamp(parse(x["ts"]) + timedelta(minutes=5)),
                           "price": x["close"]} for x in previous]
                db.execute("INSERT OR IGNORE INTO signals "
                           "(id,symbol,signal_ts,entry_ts,status,"
                           "forecast_json,forecast_n,actual_json,created_at) "
                           "VALUES(?,?,?,?,'OPEN',?,?,?,?)",
                           (sid, bar.symbol, ts, entry_ts,
                            json.dumps(forecast) if forecast else None,
                            n, json.dumps(actual), stamp(now)))
                trim_signals(db)
                triggered = True
        return {"accepted": True, "triggered": triggered}


def _set_entry(db: sqlite3.Connection, sid: str, price: float) -> None:
    db.execute("UPDATE signals SET entry_price=? WHERE id=? AND entry_price IS NULL", (price, sid))


@app.post("/v1/bar-open", dependencies=[Depends(authorize)])
def bar_open(payload: BarOpen) -> dict:
    authorized_symbol(payload.symbol)
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM signals WHERE symbol=? AND entry_ts=? AND status='OPEN'",
                         (payload.symbol, stamp(payload.ts))).fetchone()
        if row is None:
            return {"matched": False}
        if row["entry_price"] is not None and not math.isclose(row["entry_price"], payload.open, rel_tol=1e-9):
            raise HTTPException(409, "Entry open already set to a different price")
        _set_entry(db, row["id"], payload.open)
        return {"matched": True, "signal_id": row["id"]}


@app.post("/v1/price-tick", dependencies=[Depends(authorize)])
def price_tick(tick: PriceTick) -> dict:
    authorized_symbol(tick.symbol)
    with database() as db:
        db.execute("UPDATE signals SET last_price=?,last_price_ts=? WHERE symbol=? "
                   "AND status='OPEN' AND entry_price IS NOT NULL AND "
                   "(last_price_ts IS NULL OR last_price_ts<=?)",
                   (tick.price, stamp(tick.ts), tick.symbol, stamp(tick.ts)))
    return {"ok": True}


@app.get("/v1/notifications")
def notifications() -> dict:
    with database() as db:
        expire_missing(db)
        rows = db.execute("SELECT * FROM signals ORDER BY signal_ts DESC,id DESC LIMIT 5").fetchall()
    return {"items": [serialize_signal(row) for row in rows], "max_items": 5}


def performance(db: sqlite3.Connection, symbol: str | None = None) -> dict:
    cutoff = stamp(datetime.now(UTC) - timedelta(days=365))
    if symbol:
        rows = db.execute("SELECT * FROM trades WHERE exit_time>=? AND symbol=? "
                          "ORDER BY exit_time,entry_time,symbol", (cutoff, symbol)).fetchall()
    else:
        rows = db.execute("SELECT * FROM trades WHERE exit_time>=? "
                          "ORDER BY exit_time,entry_time,symbol", (cutoff,)).fetchall()
    if not rows:
        return {"trades": 0, "wins": 0, "winrate_pct": None, "roi_pct": None,
                "gross_direction_accuracy_pct": None,
                "max_dd_pct": None, "sharpe_ratio": None,
                "equity_curve": [], "window_days": 365,
                "initial_capital_usd": INITIAL_CAPITAL}
    frame = pd.DataFrame([dict(row) for row in rows])
    summary = metrics(frame)
    curve = [{"ts": row["exit_time"], "equity_usd": round(INITIAL_CAPITAL + pnl, 4)}
             for row, pnl in zip(rows, frame["pnl_usd"].cumsum())]
    result = {key: (None if isinstance(value, float) and not math.isfinite(value) else value)
              for key, value in summary.items()}
    result.update(wins=int((frame["pnl_usd"] > 0).sum()), equity_curve=curve,
                  gross_direction_accuracy_pct=float(((frame["net_return"] + FEE) > 0).mean() * 100),
                  window_days=365, initial_capital_usd=INITIAL_CAPITAL)
    return result


@app.get("/v1/performance")
def get_performance(symbol: str | None = None) -> dict:
    with database() as db:
        return performance(db, symbol)


@app.get("/v1/notifications/{sid}")
def notification_detail(sid: str) -> dict:
    with database() as db:
        expire_missing(db)
        row = db.execute("SELECT * FROM signals WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise HTTPException(404, "Only the five latest signals are retained")
        data = serialize_signal(row)
        data["backtest"] = performance(db, row["symbol"])
        return data


@app.get("/health")
def health() -> dict:
    with database() as db:
        bars = db.execute(
            "SELECT symbol,max(ts) AS latest,"
            "max(CASE WHEN oi_usd IS NOT NULL THEN ts END) AS latest_oi "
            "FROM bars GROUP BY symbol"
        ).fetchall()
    return {"ok": True, "latest_bars": [dict(row) for row in bars]}


@app.get("/", include_in_schema=False)
def demo() -> FileResponse:
    return FileResponse(Path(__file__).with_name("demo.html"))
