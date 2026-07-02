import importlib

from app import app


app_module = importlib.import_module("app")


def test_health_endpoint_reports_status():
    client = app.test_client()

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert "mongo_configured" in payload
    assert "angel_configured" in payload


def test_root_serves_dashboard():
    client = app.test_client()

    response = client.get("/")

    assert response.status_code == 200
    assert b"VMA Dual Crossover Dashboard" in response.data


def test_assets_route_serves_css():
    client = app.test_client()

    response = client.get("/assets/index.css")

    assert response.status_code == 200
    assert "text/css" in response.content_type


def test_dual_vma_rejects_invalid_lengths():
    client = app.test_client()

    response = client.get("/api/dual-vma?short_len=21&long_len=9")

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False


def test_vma_trades_rejects_non_list_payload():
    client = app.test_client()

    response = client.post("/api/vma-trades", json={"trades": "bad"})

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False


def test_active_trade_rejects_missing_position():
    client = app.test_client()

    response = client.post("/api/vma-active-trade", json={"session_id": "test-session", "status": "ACTIVE"})

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False


def test_active_trade_rejects_missing_session_id():
    client = app.test_client()

    response = client.post("/api/vma-active-trade", json={"status": "CLOSED"})

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False


def test_unknown_api_route_returns_json():
    client = app.test_client()

    response = client.get("/api/does-not-exist")

    assert response.status_code == 404
    assert "application/json" in response.content_type
    payload = response.get_json()
    assert payload["ok"] is False


def test_bearish_micro_cross_not_overridden(monkeypatch):
    rows = [
        {"timestamp": "t0", "open": 10, "high": 11, "low": 9, "close": 10},
        {"timestamp": "t1", "open": 12, "high": 13, "low": 11, "close": 12},
    ]

    def fake_vma_series(_rows, length):
        return [10.0, 9.0] if length == 5 else [9.0, 9.1]

    monkeypatch.setattr(app_module, "_vma_series", fake_vma_series)
    monkeypatch.setattr(app_module, "_atr_series", lambda _rows, _period: [1.0, 1.0])
    monkeypatch.setattr(app_module, "_rsi_series", lambda _rows, _period: [50.0, 60.0])

    result = app_module.compute_dual_vma(rows, short_len=5, long_len=9)

    assert result[1]["signal"] == "PE"


def test_same_bar_reversal_allowed():
    # Setup _sim state
    app_module._sim.last_exit_ts = "2026-07-02 09:18:00"
    app_module._sim.last_trade_type = "PE"
    app_module._sim.position = None
    app_module._sim.params = {"confirmCandle": False, "minQuality": 0}

    bar = {
        "timestamp": "2026-07-02 09:18:00",
        "signal": "CE",
        "confirm_signal": "NONE",
        "quality": 2,
        "is_sideways": False
    }

    # Verify that opposite direction (CE) on same bar is allowed (not skipped)
    sig = app_module._sim_get_entry_signal(bar, app_module._sim.params)
    assert sig == "CE"


def test_sim_complete_trade_deletes_active_trade(monkeypatch):
    called_with = []
    def fake_save_active_vma_trade(payload):
        called_with.append(payload)
        return {"session_id": payload.get("session_id"), "status": payload.get("status")}

    monkeypatch.setattr(app_module, "save_active_vma_trade", fake_save_active_vma_trade)
    monkeypatch.setattr(app_module, "save_vma_trades", lambda payload: {"inserted": 1, "updated": 0, "total": 1})

    # Setup sim state with a fake position
    app_module._sim.position = {
        "type": "CE",
        "entry": 100.0,
        "entry_ts": "t0",
        "init_sl": 90.0,
        "cur_sl": 90.0,
        "tgt": 110.0,
        "lot_size": 1,
    }
    app_module._sim.session_id = "test-session-complete"
    app_module._sim.params = {"lotSize": 1}
    app_module._sim.trades = []

    app_module._sim_complete_trade(90.0, "t1", "SL")

    # Verify save_active_vma_trade was called with CLOSED status
    assert len(called_with) == 1
    assert called_with[0]["session_id"] == "test-session-complete"
    assert called_with[0]["status"] == "CLOSED"
