from cloudset.store import Store


def test_subscription_upsert(tmp_path):
    store = Store(tmp_path / "test.db")
    first = store.subscribe("Sky@Example.com", 40.1, -74.2, "Home", 70)
    second = store.subscribe("sky@example.com", 40.1, -74.2, "Roof", 80)
    assert first == second
    records = store.subscriptions()
    assert len(records) == 1
    assert records[0]["label"] == "Roof"
    assert records[0]["threshold"] == 80
    assert records[0]["notification_times"] == ["morning", "final_90"]


def test_notification_events_are_deduplicated_independently(tmp_path):
    store = Store(tmp_path / "test.db")
    subscriber = store.subscribe("sky@example.com", 40.1, -74.2, "Home", 70)
    store.record_notification(subscriber, "2026-08-19", "morning", 82, "sent")
    assert store.notification_exists(subscriber, "2026-08-19", "morning")
    assert not store.notification_exists(subscriber, "2026-08-19", "final_90")
