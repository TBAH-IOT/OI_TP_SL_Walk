from datetime import datetime, timedelta, timezone
import json

from fastapi.testclient import TestClient

from mvp import live_mvp as m


def test_trigger_entry_close_and_live_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(m, "INGEST_TOKEN", "test-token")
    monkeypatch.setattr(m, "SYMBOLS", {"BTC"})
    client = TestClient(m.app)
    now_floor = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now_floor -= timedelta(minutes=now_floor.minute % 5)
    base = now_floor - timedelta(minutes=70 + 5 * 506)
    def iso(ts):
        return ts.isoformat().replace("+00:00", "Z")
    with m.database() as db:
        db.executemany("INSERT INTO bars VALUES(?,?,?,?,?,?,?,?)", [
            ("BTC", iso(base + timedelta(minutes=5*i)), 1000.0 * (1.0001 ** i), 100.0,
             100.0, 100.0, 100.0, 100.0) for i in range(506)])
        db.execute("INSERT INTO forecast_paths VALUES(?,?,?,?)",
                   ("path1", "BTC", iso(base), json.dumps([.001] * 12)))
    trigger = base + timedelta(minutes=5*506)
    body = {"symbol": "BTC", "ts": iso(trigger), "oi_usd": 900,
            "mark_close": 100, "open": 100, "high": 100, "low": 100, "close": 100}
    h = {"Authorization": "Bearer test-token"}
    response = client.post("/v1/bar-close", json=body, headers=h)
    assert response.status_code == 200, response.text
    assert response.json()["triggered"] is True
    assert client.post("/v1/bar-close", json=body, headers=h).json()["accepted"] is False
    item = client.get("/v1/notifications").json()["items"][0]
    assert item["entry_price"] is None
    assert item["forecast_n"] == 1
    entry_ts = trigger + timedelta(minutes=5)
    assert client.post("/v1/bar-open", json={"symbol": "BTC", "ts": iso(entry_ts), "open": 100}, headers=h).json()["matched"]
    item = client.get("/v1/notifications").json()["items"][0]
    assert item["forecast"][0]["price"] == 100.1
    assert item["scheduled_exit_ts"] == iso(entry_ts + timedelta(hours=1))
    assert "take_profit_price" not in item
    for i in range(12):
        body["ts"] = iso(entry_ts + timedelta(minutes=5*i))
        # A price candle must still settle the trade when GFI OI is absent.
        body["oi_usd"] = None
        body["mark_close"] = None
        body["high"] = 105 if i == 0 else 100
        body["close"] = 105 if i == 0 else 100
        response = client.post("/v1/bar-close", json=body, headers=h)
        assert response.status_code == 200, response.text
        if i == 0:
            assert client.get("/v1/notifications/" + item["id"]).json()["status"] == "OPEN"
    detail = client.get("/v1/notifications/" + item["id"]).json()
    assert detail["status"] == "CLOSED"
    assert detail["exit_type"] == "TIME_60M"
    assert detail["backtest"]["trades"] == 1
    assert detail["backtest"]["gross_direction_accuracy_pct"] == 0
    assert detail["backtest"]["roi_pct"] < 0


def test_historical_warmup_never_emits_a_notification(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "DB_PATH", tmp_path / "warmup.sqlite3")
    monkeypatch.setattr(m, "INGEST_TOKEN", "test-token")
    monkeypatch.setattr(m, "SYMBOLS", {"kPEPE"})
    client = TestClient(m.app)
    now_floor = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now_floor -= timedelta(minutes=now_floor.minute % 5)
    base = now_floor - timedelta(minutes=70 + 5 * 506)
    def iso(ts):
        return ts.isoformat().replace("+00:00", "Z")
    with m.database() as db:
        db.executemany("INSERT INTO bars VALUES(?,?,?,?,?,?,?,?)", [
            ("kPEPE", iso(base + timedelta(minutes=5*i)), 1000.0 * (1.0001 ** i),
             1.0, 1.0, 1.0, 1.0, 1.0) for i in range(506)
        ])
    response = client.post("/v1/bar-close", json={
        "symbol": "kPEPE", "ts": iso(base + timedelta(minutes=5*506)),
        "oi_usd": 900, "mark_close": 1, "open": 1, "high": 1,
        "low": 1, "close": 1, "historical": True,
    }, headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": True, "triggered": False}
    assert client.get("/v1/notifications").json()["items"] == []


def test_five_user_signals_preserve_trade_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "DB_PATH", tmp_path / "test.sqlite3")
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    with m.database() as db:
        for i in range(7):
            ts = m.stamp(base + timedelta(hours=i))
            db.execute("INSERT INTO signals(id,symbol,signal_ts,entry_ts,status,created_at) "
                       "VALUES(?,?,?,?,'CLOSED',?)",
                       (str(i), "BTC", ts, ts, ts))
            db.execute("INSERT INTO trades VALUES(?,?,?,?,?,?,?,?)",
                       (str(i), "BTC", ts, ts, 1, .001, "TP", "live"))
        m.trim_signals(db)
    assert len(m.notifications()["items"]) == 5
    with m.database() as db:
        assert db.execute("SELECT count(*) FROM trades").fetchone()[0] == 7
