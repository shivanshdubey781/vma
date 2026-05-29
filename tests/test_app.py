from app import app


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
