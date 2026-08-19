from datetime import date, datetime, timezone

from cloudset.solar import solar_position, sunset_utc


def test_solar_noon_equinox_near_equator():
    elevation, _ = solar_position(datetime(2026, 3, 20, 12, tzinfo=timezone.utc), 0, 0)
    assert elevation > 87


def test_new_york_summer_sunset_is_plausible():
    result = sunset_utc(date(2026, 6, 21), 40.7128, -74.006)
    assert result is not None
    assert result.date() == date(2026, 6, 22)
    assert 0 <= result.hour <= 1
    assert abs(solar_position(result, 40.7128, -74.006)[0] + 0.833) < 0.02
