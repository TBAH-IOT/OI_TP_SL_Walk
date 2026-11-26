"""Example live adapter: GFI 5m OI + Hyperliquid 5m candles -> MVP API.

The strategy itself is in ``strategy.py``. This adapter is replaceable by a
product feed. It never sends an unfinished candle, fabricates OI, or emits a
new alert while replaying old bars after a restart.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from mvp.strategy import BAR


UTC = timezone.utc
GFI_SERIES = "https://sh-news-api.gfigroup.io/api/hyperliquid/asset-ctxs/series"
HL_INFO = "https://api.hyperliquid.xyz/info"
CHUNK_BARS = 980  # below the GFI 1,000-row response cap
LOG = logging.getLogger(__name__)


def utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("feed timestamps must include timezone")
    return value.astimezone(UTC)


def iso(value: datetime) -> str:
    return utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def floor_5m(value: datetime) -> datetime:
    value = utc(value).replace(second=0, microsecond=0)
    return value.replace(minute=value.minute - value.minute % 5)


def positive_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


@dataclass(frozen=True)
class Config:
    symbols: tuple[str, ...]
    gfi_key: str
    ingest_token: str
    api_url: str = "http://127.0.0.1:8000"
    bootstrap_days: int = 8
    close_lag_seconds: int = 15
    max_signal_age_seconds: int = 600
    poll_seconds: int = 30

    @classmethod
    def from_env(cls) -> "Config":
        symbols = tuple(s.strip() for s in os.getenv("MVP_SYMBOLS", "").split(",") if s.strip())
        gfi_key = (os.getenv("GFI_API_KEY") or os.getenv("NI_API_KEY") or
                   os.getenv("GFI_NI_API_KEY") or "").strip()
        token = os.getenv("MVP_INGEST_TOKEN", "").strip()
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("MVP_SYMBOLS must list distinct supported coins")
        if not gfi_key or not token:
            raise ValueError("Set GFI_API_KEY and MVP_INGEST_TOKEN outside source control")
        config = cls(
            symbols=symbols,
            gfi_key=gfi_key,
            ingest_token=token,
            api_url=os.getenv("MVP_API_URL", "http://127.0.0.1:8000").rstrip("/"),
            bootstrap_days=int(os.getenv("MVP_BOOTSTRAP_DAYS", "8")),
            close_lag_seconds=int(os.getenv("MVP_CLOSE_LAG_SECONDS", "15")),
            max_signal_age_seconds=int(os.getenv("MVP_MAX_SIGNAL_AGE_SECONDS", "600")),
            poll_seconds=int(os.getenv("MVP_POLL_SECONDS", "30")),
        )
        if min(config.bootstrap_days, config.poll_seconds) < 1:
            raise ValueError("bootstrap days and poll seconds must be positive")
        return config


class Feed:
    def __init__(self, config: Config, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()

    def _json(self, method: str, url: str, **kwargs: Any) -> Any:
        for attempt in range(5):
            response = self.session.request(method, url, timeout=30, **kwargs)
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < 4:
                    try:
                        pause = float(response.headers.get("Retry-After", "0"))
                    except ValueError:
                        pause = 0
                    time.sleep(pause if pause > 0 else min(2 ** attempt, 8))
                    continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("API did not recover after retries")

    def _post_api(self, path: str, payload: dict) -> dict:
        return self._json(
            "POST", self.config.api_url + path,
            headers={"Authorization": f"Bearer {self.config.ingest_token}"},
            json=payload,
        )

    def _get_api(self, path: str) -> dict:
        return self._json("GET", self.config.api_url + path)

    def oi_rows(self, symbol: str, start: datetime, end_exclusive: datetime) -> dict[datetime, dict]:
        rows: dict[datetime, dict] = {}
        cursor = start
        while cursor < end_exclusive:
            chunk_end = min(cursor + CHUNK_BARS * BAR, end_exclusive)
            payload = self._json(
                "GET", GFI_SERIES,
                headers={"X-Api-Key": self.config.gfi_key},
                params={"symbol": symbol, "start": iso(cursor),
                        "end": iso(chunk_end), "limit": 1000},
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
                raise ValueError(f"Unexpected GFI series response for {symbol}")
            for row in payload["rows"]:
                if isinstance(row, dict) and row.get("bucket_5m"):
                    stamp = utc(row["bucket_5m"])
                    if cursor <= stamp < chunk_end:
                        rows[stamp] = row
            cursor = chunk_end
        return rows

    def candles(self, symbol: str, start: datetime, last_needed: datetime) -> dict[datetime, dict]:
        rows: dict[datetime, dict] = {}
        cursor = start
        end_exclusive = last_needed + BAR
        while cursor < end_exclusive:
            chunk_end = min(cursor + CHUNK_BARS * BAR, end_exclusive)
            payload = self._json(
                "POST", HL_INFO,
                json={"type": "candleSnapshot", "req": {
                    "coin": symbol, "interval": "5m",
                    "startTime": int(cursor.timestamp() * 1000),
                    "endTime": int(chunk_end.timestamp() * 1000),
                }},
            )
            if not isinstance(payload, list):
                raise ValueError(f"Unexpected Hyperliquid candles for {symbol}")
            for row in payload:
                if not isinstance(row, dict):
                    continue
                stamp = datetime.fromtimestamp(int(row["t"]) / 1000, UTC)
                if start <= stamp <= last_needed:
                    rows[stamp] = row
            cursor = chunk_end
        return rows

    def live_marks(self) -> dict[str, float]:
        payload = self._json("POST", HL_INFO, json={"type": "metaAndAssetCtxs"})
        if not isinstance(payload, list) or len(payload) != 2:
            raise ValueError("Unexpected Hyperliquid live context response")
        universe = payload[0].get("universe", [])
        contexts = payload[1]
        return {meta["name"]: price for meta, ctx in zip(universe, contexts)
                if (price := positive_or_none(ctx.get("markPx"))) is not None}

    def run_once(self, now: datetime | None = None) -> dict:
        now = utc(now or datetime.now(UTC))
        last_closed = floor_5m(now - timedelta(seconds=self.config.close_lag_seconds)) - BAR
        health = self._get_api("/health")
        latest_by_symbol = {row["symbol"]: utc(row["latest"]) for row in health.get("latest_bars", [])}
        report: dict[str, Any] = {"asof_utc": iso(now), "last_closed_utc": iso(last_closed),
                                  "bars_sent": 0, "missing_oi": 0, "missing_price": 0,
                                  "triggers": 0, "failures": {}}
        candle_cache: dict[str, dict[datetime, dict]] = {}
        for symbol in self.config.symbols:
            try:
                bootstrap = symbol not in latest_by_symbol
                start = (latest_by_symbol[symbol] + BAR if not bootstrap
                         else last_closed - timedelta(days=self.config.bootstrap_days) + BAR)
                if start > last_closed:
                    continue
                oi = self.oi_rows(symbol, start, last_closed + BAR)
                prices = self.candles(symbol, start, last_closed + BAR)
                candle_cache[symbol] = prices
                cursor = start
                while cursor <= last_closed:
                    candle = prices.get(cursor)
                    if candle is None:
                        report["missing_price"] += 1
                        cursor += BAR
                        continue
                    context = oi.get(cursor, {})
                    oi_usd = positive_or_none(context.get("oi_usd"))
                    mark = positive_or_none(context.get("mark_close"))
                    if oi_usd is None:
                        report["missing_oi"] += 1
                    payload = {
                        "symbol": symbol, "ts": iso(cursor),
                        "oi_usd": oi_usd, "mark_close": mark,
                        "open": float(candle["o"]), "high": float(candle["h"]),
                        "low": float(candle["l"]), "close": float(candle["c"]),
                        "historical": bootstrap or cursor + BAR < now - timedelta(
                            seconds=self.config.max_signal_age_seconds),
                    }
                    result = self._post_api("/v1/bar-close", payload)
                    report["bars_sent"] += int(result.get("accepted", False))
                    if result.get("triggered"):
                        report["triggers"] += 1
                        next_bar = prices.get(cursor + BAR)
                        if next_bar is not None:
                            self._post_api("/v1/bar-open", {
                                "symbol": symbol, "ts": iso(cursor + BAR),
                                "open": float(next_bar["o"]),
                            })
                    cursor += BAR
            except (requests.RequestException, ValueError, RuntimeError, KeyError) as exc:
                LOG.exception("Feed failed for %s", symbol)
                report["failures"][symbol] = type(exc).__name__

        notifications = self._get_api("/v1/notifications").get("items", [])
        for item in notifications:
            if item["status"] != "OPEN":
                continue
            symbol = item["symbol"]
            entry_ts = utc(item["entry_ts"])
            candle = candle_cache.get(symbol, {}).get(entry_ts)
            if item["entry_price"] is None and candle is None:
                try:
                    candle = self.candles(symbol, entry_ts, entry_ts).get(entry_ts)
                except (requests.RequestException, ValueError, RuntimeError, KeyError) as exc:
                    LOG.exception("Could not fetch pending entry open for %s", symbol)
                    report["failures"][f"entry_{symbol}"] = type(exc).__name__
            if item["entry_price"] is None and candle is not None:
                self._post_api("/v1/bar-open", {
                    "symbol": symbol, "ts": iso(entry_ts), "open": float(candle["o"]),
                })

        open_items = [item for item in notifications if item["status"] == "OPEN"]
        if open_items:
            try:
                marks = self.live_marks()
                for item in open_items:
                    if (price := marks.get(item["symbol"])) is not None:
                        self._post_api("/v1/price-tick", {
                            "symbol": item["symbol"], "ts": iso(now), "price": price,
                        })
            except (requests.RequestException, ValueError, RuntimeError, KeyError) as exc:
                LOG.exception("Live mark update failed")
                report["failures"]["live_marks"] = type(exc).__name__
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Fetch and ingest one cycle")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = Config.from_env()
    feed = Feed(config)
    while True:
        started = time.monotonic()
        print(json.dumps(feed.run_once(), ensure_ascii=False), flush=True)
        if args.once:
            break
        time.sleep(max(1, config.poll_seconds - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
