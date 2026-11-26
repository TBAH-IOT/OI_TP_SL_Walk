from datetime import datetime, timedelta, timezone

from mvp.live_feed import Config, Feed, iso


class FakeFeed(Feed):
    def __init__(self, config, oi_value, triggered):
        super().__init__(config)
        self.oi_value = oi_value
        self.triggered = triggered
        self.sent = []

    def _get_api(self, path):
        if path == "/health":
            return {"latest_bars": [{"symbol": "kPEPE", "latest": "2026-11-26T09:55:00Z"}]}
        return {"items": []}

    def oi_rows(self, symbol, start, end_exclusive):
        return {start: {"oi_usd": self.oi_value, "mark_close": self.oi_value}}

    def candles(self, symbol, start, last_needed):
        candle = {"o": "1", "h": "1.1", "l": "0.9", "c": "1.01"}
        return {start: candle, start + timedelta(minutes=5): candle}

    def _post_api(self, path, payload):
        self.sent.append((path, payload))
        return {"accepted": True, "triggered": self.triggered} if path == "/v1/bar-close" else {"matched": True}


def test_feed_keeps_price_bar_when_oi_missing_and_uses_any_configured_coin():
    config = Config(symbols=("kPEPE",), gfi_key="test", ingest_token="test")
    feed = FakeFeed(config, oi_value=None, triggered=False)
    result = feed.run_once(datetime(2026, 11, 26, 10, 7, tzinfo=timezone.utc))
    assert result["bars_sent"] == 1
    assert result["missing_oi"] == 1
    assert feed.sent[0][0] == "/v1/bar-close"
    assert feed.sent[0][1]["symbol"] == "kPEPE"
    assert feed.sent[0][1]["oi_usd"] is None
    assert feed.sent[0][1]["historical"] is False


def test_feed_sets_next_candle_open_when_triggered():
    config = Config(symbols=("kPEPE",), gfi_key="test", ingest_token="test")
    feed = FakeFeed(config, oi_value=1000, triggered=True)
    result = feed.run_once(datetime(2026, 11, 26, 10, 7, tzinfo=timezone.utc))
    assert result["triggers"] == 1
    assert feed.sent[1][0] == "/v1/bar-open"
    assert feed.sent[1][1] == {
        "symbol": "kPEPE", "ts": iso(datetime(2026, 11, 26, 10, 5, tzinfo=timezone.utc)),
        "open": 1.0,
    }


def test_fresh_bootstrap_is_historical_even_for_last_closed_bar():
    class BootstrapFeed(FakeFeed):
        def _get_api(self, path):
            return {"latest_bars": []} if path == "/health" else {"items": []}

    config = Config(symbols=("kPEPE",), gfi_key="test", ingest_token="test", bootstrap_days=1)
    feed = BootstrapFeed(config, oi_value=1000, triggered=False)
    feed.run_once(datetime(2026, 11, 26, 10, 7, tzinfo=timezone.utc))
    assert feed.sent[0][0] == "/v1/bar-close"
    assert feed.sent[0][1]["historical"] is True
