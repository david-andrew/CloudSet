from dataclasses import replace
from datetime import datetime, timezone

import cloudset.main as main
from cloudset.main import _due_events
from cloudset.store import Store


def subscriber(times, custom_minutes=None):
    return {
        "latitude": 40.7128,
        "longitude": -74.006,
        "notification_times": times,
        "custom_minutes": custom_minutes,
    }


def test_morning_and_evening_before_windows():
    morning = datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc)  # 9am EDT
    evening = datetime(2026, 8, 19, 22, 0, tzinfo=timezone.utc)  # 6pm EDT
    assert _due_events(subscriber(["morning"]), morning)[0][0] == "morning"
    event = _due_events(subscriber(["day_before"]), evening)[0]
    assert event[0] == "day_before"
    assert event[1].isoformat() == "2026-08-20"


def test_force_produces_one_manual_event():
    now = datetime(2026, 8, 19, 4, 0, tzinfo=timezone.utc)
    events = _due_events(subscriber(["morning", "final_90"]), now, force=True)
    assert len(events) == 1
    assert events[0][0] == "manual"


def _feature(score):
    return {
        "geometry": {"type": "Point", "coordinates": [-74.006, 40.7128]},
        "properties": {
            "score": score,
            "tier": "vivid",
            "sunset_utc": "2026-08-19T23:48:10+00:00",
            "mid_cloud": 40,
            "high_cloud": 60,
            "western_clearance": 80,
            "aerosol_optical_depth": 0.1,
            "precip_risk": 5,
        },
    }


def test_downgrade_is_sent_once_after_an_alert(tmp_path, monkeypatch):
    store = Store(tmp_path / "n.db")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "settings", replace(main.settings, smtp_host="", secret_key="x" * 40))
    monkeypatch.setattr(main, "email_map_for", lambda *_, **__: None)
    sub_id = store.subscribe("sky@example.com", 40.7128, -74.006, "Home", 70, ["morning", "final_90"])
    store.confirm(sub_id)
    record = store.subscription(sub_id)

    class FakeEngine:
        score = 85

        def provider_metadata(self, _day):
            return {"mode": "live"}

        def point(self, *_args, **_kwargs):
            return _feature(self.score)

    engine = FakeEngine()
    monkeypatch.setattr(main, "engine", engine)
    sent = []
    monkeypatch.setattr(main, "send_forecast", lambda *a, **k: (sent.append(("alert", a[3])) or ("logged", None)))
    monkeypatch.setattr(main, "send_downgrade", lambda *a, **k: (sent.append(("downgrade", a[3])) or ("logged", None)))

    morning = datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc)
    assert main._process_subscriber(record, morning, False)[0] == 1
    assert sent == [("alert", "Morning sunset outlook")]

    # Same window again: nothing new.
    assert main._process_subscriber(record, morning, False)[0] == 0

    # The final call finds the score below threshold: one downgrade notice.
    engine.score = 40
    final_call = datetime(2026, 8, 19, 23, 0, tzinfo=timezone.utc)
    assert main._process_subscriber(record, final_call, False)[0] == 1
    assert sent[-1] == ("downgrade", 85)
    assert store.notification_exists(sub_id, "2026-08-19", "downgrade")

    # A fresh subscriber who was never alerted gets nothing when the score is low.
    quiet_id = store.subscribe("quiet@example.com", 40.7128, -74.006, "Home", 70, ["final_90"])
    store.confirm(quiet_id)
    assert main._process_subscriber(store.subscription(quiet_id), final_call, False) == (0, 1, [])


def test_outcome_requests_go_out_after_sunset_once(tmp_path, monkeypatch):
    store = Store(tmp_path / "o.db")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "settings", replace(main.settings, smtp_host="", secret_key="x" * 40))
    sub_id = store.subscribe("sky@example.com", 40.7128, -74.006, "Home", 70, ["morning"])
    store.confirm(sub_id)
    day = "2026-08-19"
    store.record_notification(sub_id, day, "morning", 83, "sent")
    # Sunset at NYC on 2026-08-19 is 23:48 UTC. Too early first, then in the window.
    early = datetime(2026, 8, 19, 23, 50, tzinfo=timezone.utc)
    assert main.dispatch_outcome_requests(early)["sent"] == 0
    later = datetime(2026, 8, 20, 0, 45, tzinfo=timezone.utc)
    assert main.dispatch_outcome_requests(later)["sent"] == 1
    assert store.notification_exists(sub_id, day, "outcome_request")
    assert main.dispatch_outcome_requests(later)["sent"] == 0
    # Someone who only got a downgrade notice is not asked.
    other = store.subscribe("quiet@example.com", 40.7128, -74.006, "Home", 70, ["morning"])
    store.confirm(other)
    store.record_notification(other, day, "downgrade", 30, "sent")
    assert main.dispatch_outcome_requests(later)["sent"] == 0


def test_monitor_flags_stale_hrrr_and_send_errors(monkeypatch):
    m = main.Monitor()
    monkeypatch.setattr(main, "settings", replace(main.settings, forecast_mode="demo"))
    assert m.problems() == []
    m.notify_pass(["subscription 1 (morning): timed out"])
    assert any("error" in p for p in m.problems())
    assert m.should_alert("notify")
    assert not m.should_alert("notify")  # cooldown
    m.started_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert m.hrrr_stale()
    m.hrrr_success()
    assert not m.hrrr_stale()
