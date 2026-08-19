from fastapi.testclient import TestClient

import cloudset.main as main
from cloudset.store import Store


def test_public_api_and_subscription(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "store", Store(tmp_path / "api.db"))
    main._cache.clear()
    with TestClient(main.app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        forecast = client.get("/api/forecast?day=0&offset=0")
        assert forecast.status_code == 200
        assert forecast.json()["meta"]["mode"] == "demo"
        tile = client.get("/api/forecast/tiles/0/0/8/75/96.png")
        assert tile.status_code == 200
        assert tile.headers["content-type"] == "image/png"
        assert tile.headers["x-forecast-resolution"] == "0.0625"
        signup = client.post(
            "/api/subscriptions",
            json={"email": "watch@example.com", "latitude": 40.71, "longitude": -74.01, "label": "Roof", "threshold": 75},
        )
        assert signup.status_code == 201
        assert main.store.subscriptions()[0]["email"] == "watch@example.com"


def test_subscription_rejects_invalid_email(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "store", Store(tmp_path / "api.db"))
    with TestClient(main.app) as client:
        response = client.post(
            "/api/subscriptions",
            json={"email": "not-an-email", "latitude": 40, "longitude": -74, "threshold": 70},
        )
    assert response.status_code == 422
