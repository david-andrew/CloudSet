from datetime import date

from cloudset.forecast import ForecastEngine, default_region


def test_forecast_is_deterministic_and_bounded():
    engine = ForecastEngine()
    region = default_region()[:4]
    first = engine.calculate(region, date(2026, 8, 18))
    second = engine.calculate(region, date(2026, 8, 18))
    assert len(first["features"]) == 16
    assert [f["properties"]["score"] for f in first["features"]] == [f["properties"]["score"] for f in second["features"]]
    assert all(0 <= f["properties"]["score"] <= 100 for f in first["features"])


def test_nearest_rejects_locations_outside_footprint():
    engine = ForecastEngine()
    result = engine.calculate(default_region()[:2], date(2026, 8, 18))
    assert engine.nearest(result, 0, 0) is None

