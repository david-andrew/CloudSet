from cloudset.store import Store


def test_subscription_upsert(tmp_path):
    store = Store(tmp_path / "test.db")
    first = store.subscribe("Sky@Example.com", 40.1, -74.2, "Home", 70)
    second = store.subscribe("sky@example.com", 40.1, -74.2, "Roof", 80)
    assert first == second
    assert store.subscriptions() == []  # pending until confirmed
    assert store.status_counts()["pending"] == 1
    assert store.confirm(first)
    records = store.subscriptions()
    assert len(records) == 1
    assert records[0]["status"] == "active"
    assert records[0]["label"] == "Roof"
    assert records[0]["threshold"] == 80
    assert records[0]["notification_times"] == ["morning", "final_90"]


def test_notification_events_are_deduplicated_independently(tmp_path):
    store = Store(tmp_path / "test.db")
    subscriber = store.subscribe("sky@example.com", 40.1, -74.2, "Home", 70)
    store.record_notification(subscriber, "2026-08-19", "morning", 82, "sent")
    assert store.notification_exists(subscriber, "2026-08-19", "morning")
    assert not store.notification_exists(subscriber, "2026-08-19", "final_90")


def test_unsubscribe_and_resubscribe_requires_new_confirmation(tmp_path):
    store = Store(tmp_path / "test.db")
    sub = store.subscribe("sky@example.com", 40.1, -74.2, "Home", 70)
    store.confirm(sub)
    assert store.unsubscribe(sub)
    assert store.subscription(sub)["status"] == "unsubscribed"
    assert store.subscriptions_for_email("sky@example.com") == []
    again = store.subscribe("sky@example.com", 40.1, -74.2, "Home", 70)
    assert again == sub
    assert store.subscription(sub)["status"] == "pending"
    assert store.count_for_email("sky@example.com") == 1


def test_legacy_rows_are_migrated_to_active(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE subscriptions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
          label TEXT NOT NULL DEFAULT '', threshold INTEGER NOT NULL DEFAULT 70,
          notification_times TEXT NOT NULL DEFAULT '["morning","final_90"]', custom_minutes INTEGER,
          active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, UNIQUE(email, latitude, longitude));
        INSERT INTO subscriptions(email, latitude, longitude, created_at) VALUES('old@example.com', 40, -74, '2026-01-01');
        """
    )
    conn.commit()
    conn.close()
    store = Store(path)
    assert store.subscriptions()[0]["status"] == "active"
