from datetime import datetime, timezone

from cloudset.main import _due_events


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
