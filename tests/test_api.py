from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import cloudset.main as main
from cloudset.store import Store
from cloudset.tokens import make_token

SECRET = "test-secret-key-that-is-long-enough-0123456789"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "store", Store(tmp_path / "api.db"))
    monkeypatch.setattr(
        main,
        "settings",
        replace(main.settings, smtp_host="", secret_key=SECRET, admin_token="admin-token", public_url="https://cloudset.example"),
    )
    monkeypatch.setattr(main, "email_map_for", lambda *_, **__: None)
    monkeypatch.setattr(main, "subscribe_limiter", main.RateLimiter(100, 3600))
    monkeypatch.setattr(main, "manage_limiter", main.RateLimiter(100, 3600))
    main._cache.clear()
    with TestClient(main.app) as test_client:
        yield test_client


ADMIN = {"X-Admin-Token": "admin-token"}


def test_public_api_and_tiles(client):
    assert client.get("/api/health").json()["status"] in {"ok", "degraded"}
    forecast = client.get("/api/forecast?day=0&offset=0")
    assert forecast.status_code == 200
    assert forecast.json()["meta"]["mode"] in {"demo", "live"}
    for layer in ("potential", "clouds"):
        tile = client.get(f"/api/forecast/tiles/{layer}/0/0/8/75/96.png?min=60")
        assert tile.status_code == 200
        assert tile.headers["content-type"] == "image/png"
        assert tile.headers["x-forecast-resolution"] == "0.0625"
    assert client.get("/api/forecast/tiles/nope/0/0/8/75/96.png").status_code == 404
    config = client.get("/api/config").json()
    assert config["forecast_tiles"]["layers"] == ["potential", "clouds"]
    assert "admin_auth" not in config


def test_signup_requires_confirmation_then_manage_and_unsubscribe(client):
    signup = client.post(
        "/api/subscriptions",
        json={"email": "Watch@Example.com", "latitude": 40.71, "longitude": -74.01, "label": "Roof", "threshold": 75},
    )
    assert signup.status_code == 201
    assert signup.json()["status"] == "pending"
    record = main.store.subscription(signup.json()["id"])
    assert record["status"] == "pending"
    assert main.store.subscriptions() == []  # nothing deliverable until confirmed

    bad = client.post("/api/confirm", json={"token": "abcdefghijklmnop.qrstuvwxyz"})
    assert bad.status_code == 400
    token = make_token(SECRET, "confirm", record["id"], "watch@example.com")
    confirmed = client.post("/api/confirm", json={"token": token})
    assert confirmed.status_code == 200
    assert confirmed.json()["subscription"]["status"] == "active"
    assert main.store.subscriptions()[0]["email"] == "watch@example.com"

    # Re-submitting the same watch keeps it active and just updates settings.
    again = client.post(
        "/api/subscriptions",
        json={"email": "watch@example.com", "latitude": 40.71, "longitude": -74.01, "label": "Roof", "threshold": 60},
    )
    assert again.json()["status"] == "active"
    assert main.store.subscription(record["id"])["threshold"] == 60

    manage_token = make_token(SECRET, "manage", record["id"], "watch@example.com")
    listing = client.get(f"/api/manage?token={manage_token}")
    assert listing.status_code == 200
    assert listing.json()["email"] == "watch@example.com"
    assert len(listing.json()["subscriptions"]) == 1

    updated = client.put(
        f"/api/manage/{record['id']}?token={manage_token}",
        json={"latitude": 39.3, "longitude": -77.7, "label": "Harpers Ferry", "threshold": 80, "notification_times": ["final_90"]},
    )
    assert updated.status_code == 200
    assert updated.json()["subscription"]["label"] == "Harpers Ferry"
    assert main.store.subscription(record["id"])["notification_times"] == ["final_90"]

    # Someone else's token cannot touch this watch.
    other = client.post("/api/subscriptions", json={"email": "other@example.com", "latitude": 41, "longitude": -73})
    other_token = make_token(SECRET, "manage", other.json()["id"], "other@example.com")
    assert client.put(f"/api/manage/{record['id']}?token={other_token}", json={"latitude": 1, "longitude": 1}).status_code == 404

    unsub_token = make_token(SECRET, "unsubscribe", record["id"], "watch@example.com")
    gone = client.post(f"/unsubscribe?token={unsub_token}", data={"List-Unsubscribe": "One-Click"})
    assert gone.status_code == 200
    assert main.store.subscription(record["id"])["status"] == "unsubscribed"
    assert client.post("/api/confirm", json={"token": token}).status_code == 410


def test_subscription_limits_and_validation(client):
    assert client.post("/api/subscriptions", json={"email": "not-an-email", "latitude": 40, "longitude": -74}).status_code == 422
    missing_custom = client.post(
        "/api/subscriptions",
        json={"email": "missing@example.com", "latitude": 40.71, "longitude": -74.01, "notification_times": ["custom"]},
    )
    assert missing_custom.status_code == 422
    for index in range(main.settings.max_subscriptions_per_email):
        response = client.post("/api/subscriptions", json={"email": "many@example.com", "latitude": 40 + index * 0.1, "longitude": -74})
        assert response.status_code == 201
    too_many = client.post("/api/subscriptions", json={"email": "many@example.com", "latitude": 45, "longitude": -74})
    assert too_many.status_code == 409


def test_rate_limit_blocks_bursts(client, monkeypatch):
    monkeypatch.setattr(main, "subscribe_limiter", main.RateLimiter(2, 3600))
    payload = {"email": "burst@example.com", "latitude": 40.7, "longitude": -74.0}
    assert client.post("/api/subscriptions", json=payload).status_code == 201
    assert client.post("/api/subscriptions", json=payload).status_code == 201
    assert client.post("/api/subscriptions", json=payload).status_code == 429


def test_admin_requires_token_and_lists_subscribers(client):
    assert client.get("/api/admin/status").status_code == 401
    status = client.get("/api/admin/status", headers=ADMIN)
    assert status.status_code == 200
    assert "subscriber_counts" in status.json()
    client.post("/api/subscriptions", json={"email": "a@example.com", "latitude": 40.7, "longitude": -74.0})
    listing = client.get("/api/admin/subscriptions", headers=ADMIN).json()
    assert listing["counts"]["pending"] == 1
    assert listing["subscriptions"][0]["email"] == "a@example.com"
    log = client.get("/api/admin/notifications", headers=ADMIN).json()["notifications"]
    assert log[0]["event_key"] == "confirmation"
    test_email = client.post("/api/admin/email/test", json={"email": "preview@example.com"}, headers=ADMIN)
    assert test_email.status_code == 200
    assert test_email.json()["delivery"] == "logged"
    assert "Cloudset:" in test_email.json()["preview"]["subject"]
    assert client.get("/robots.txt").text.startswith("User-agent")
    assert client.get("/manage").status_code == 200


def test_outcome_rating_and_health(client, monkeypatch):
    signup = client.post("/api/subscriptions", json={"email": "r@example.com", "latitude": 40.71, "longitude": -74.01, "label": "Roof"})
    sub_id = signup.json()["id"]
    main.store.confirm(sub_id)
    token = make_token(SECRET, "outcome", sub_id, "r@example.com")
    nothing = client.post("/api/outcome", json={"token": token, "date": "2026-08-19", "rating": 4})
    assert nothing.status_code == 404
    main.store.record_notification(sub_id, "2026-08-19", "morning", 77, "sent")
    rated = client.post("/api/outcome", json={"token": token, "date": "2026-08-19", "rating": 4, "comment": "great"})
    assert rated.status_code == 200
    assert rated.json()["predicted_score"] == 77
    wrong_purpose = make_token(SECRET, "manage", sub_id, "r@example.com")
    assert client.post("/api/outcome", json={"token": wrong_purpose, "date": "2026-08-19", "rating": 4}).status_code == 400
    outcomes = client.get("/api/admin/outcomes", headers=ADMIN).json()
    assert outcomes["summary"]["count"] == 1
    assert outcomes["outcomes"][0]["comment"] == "great"
    assert client.get("/rate").status_code == 200

    health = client.get("/api/health")
    assert health.status_code in {200, 503}
    main.monitor.notify_pass(["boom"])
    degraded = client.get("/api/health")
    assert degraded.status_code == 503
    assert degraded.json()["status"] == "degraded"
    main.monitor.notify_pass([])
    heartbeat = client.post("/api/admin/heartbeat", headers=ADMIN)
    assert heartbeat.status_code == 200
    assert "Daily heartbeat" not in heartbeat.json()["body"] or "Cloudset daily heartbeat" in heartbeat.json()["body"]
