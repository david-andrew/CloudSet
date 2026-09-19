from datetime import date

import numpy as np

from cloudset.forecast import ForecastEngine, WeatherField, default_region


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


def test_dense_smoke_reduces_score_instead_of_boosting_color():
    class AerosolProvider:
        name = "aerosol-test"

        def __init__(self, aerosol):
            self.aerosol = aerosol

        def field(self, latitude, _longitude, _day):
            shape = latitude.shape
            return WeatherField(
                low_cloud=np.full(shape, 0.15),
                mid_cloud=np.full(shape, 0.45),
                high_cloud=np.full(shape, 0.35),
                aerosol=np.full(shape, self.aerosol),
                precip=np.zeros(shape),
                texture=np.full(shape, 0.7),
                western_clearance=np.full(shape, 0.9),
                visibility_clarity=np.full(shape, 0.9),
            )

    coordinates = np.array([40.0])
    moderate, _ = ForecastEngine(AerosolProvider(0.12)).score_field(coordinates, np.array([-74.0]), date(2026, 8, 19))
    dense, _ = ForecastEngine(AerosolProvider(1.2)).score_field(coordinates, np.array([-74.0]), date(2026, 8, 19))
    assert dense[0] < moderate[0] * 0.75
