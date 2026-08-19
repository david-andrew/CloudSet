from dataclasses import replace

from fastapi.testclient import TestClient

import cloudset.main as main
from cloudset.store import Store


def test_public_api_and_subscription(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "store", Store(tmp_path / "api.db"))
    monkeypatch.setattr(main, "settings", replace(main.settings, smtp_host=""))
    monkeypatch.setattr(main, "email_map_for", lambda *_, **__: None)
    main._cache.clear()
    with TestClient(main.app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        forecast = client.get("/api/forecast?day=0&offset=0")
        assert forecast.status_code == 200
        assert forecast.json()["meta"]["mode"] in {"demo", "live"}
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
        assert main.store.subscriptions()[0]["notification_times"] == ["morning", "final_90"]
        custom_signup = client.post(
            "/api/subscriptions",
            json={
                "email": "custom@example.com",
                "latitude": 40.71,
                "longitude": -74.01,
                "notification_times": ["day_before", "custom"],
                "custom_minutes": 360,
            },
        )
        assert custom_signup.status_code == 201
        custom_record = next(item for item in main.store.subscriptions() if item["email"] == "custom@example.com")
        assert custom_record["notification_times"] == ["day_before", "custom"]
        assert custom_record["custom_minutes"] == 360
        missing_custom_time = client.post(
            "/api/subscriptions",
            json={
                "email": "missing@example.com",
                "latitude": 40.71,
                "longitude": -74.01,
                "notification_times": ["custom"],
            },
        )
        assert missing_custom_time.status_code == 422
        test_email = client.post("/api/admin/email/test", json={"email": "preview@example.com"})
        assert test_email.status_code == 200
        assert test_email.json()["delivery"] in {"logged", "sent"}
        assert "Cloudset:" in test_email.json()["preview"]["subject"]


def test_subscription_rejects_invalid_email(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "store", Store(tmp_path / "api.db"))
    with TestClient(main.app) as client:
        response = client.post(
            "/api/subscriptions",
            json={"email": "not-an-email", "latitude": 40, "longitude": -74, "threshold": 70},
        )
    assert response.status_code == 422
