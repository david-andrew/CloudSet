from cloudset.tokens import make_token, read_token

SECRET = "0123456789abcdef0123456789abcdef"


def test_round_trip_and_purpose_check():
    token = make_token(SECRET, "manage", 42, "Sky@Example.com")
    payload = read_token(SECRET, token)
    assert payload is not None
    assert payload.purpose == "manage"
    assert payload.subscription_id == 42
    assert payload.email == "sky@example.com"
    assert read_token(SECRET, token, "manage") is not None
    assert read_token(SECRET, token, "confirm") is None


def test_tampering_and_wrong_secret_fail():
    token = make_token(SECRET, "unsubscribe", 7, "a@example.com")
    payload, signature = token.split(".")
    assert read_token(SECRET, f"{payload}x.{signature}") is None
    assert read_token(SECRET, f"{payload}.{signature[:-1]}A") is None
    assert read_token("another-secret-another-secret-00", token) is None
    assert read_token("", token) is None
    assert read_token(SECRET, "") is None
    assert read_token(SECRET, "no-dot") is None
