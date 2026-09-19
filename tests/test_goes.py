import json
from datetime import date, timedelta

import numpy as np

from cloudset.goes import GoesObservationProvider, _motion, bounds_for_observation, should_refresh_goes
from cloudset.solar import sunset_utc


def test_observation_bounds_are_cropped_but_buffered():
    bounds = bounds_for_observation(["40.0,-75.0", "41.0,-73.0"])
    assert bounds == (-77, 38, -71, 43)


def test_refresh_window_is_limited_to_hours_near_sunset():
    sunset = sunset_utc(date(2026, 8, 19), 38.0, -75.0)
    assert sunset is not None
    assert should_refresh_goes(sunset - timedelta(hours=2))
    assert not should_refresh_goes(sunset - timedelta(hours=6))


def test_phase_correlation_recovers_cloud_motion():
    previous = np.zeros((64, 64), dtype=np.float32)
    previous[20:28, 30:40] = 1
    previous[45:50, 10:17] = 0.7
    current = np.roll(np.roll(previous, 2, axis=0), -3, axis=1)
    latitude_cells, longitude_cells, confidence = _motion(previous, current, 20)
    assert latitude_cells == -6
    assert longitude_cells == -9
    assert confidence > 0.5


def test_observation_provider_advects_and_weights_recent_field(tmp_path):
    day = date(2026, 8, 19)
    target = sunset_utc(day, 38.0, -75.0)
    assert target is not None
    metadata = {
        "observed_utc": (target - timedelta(hours=1)).isoformat(),
        "previous_utc": (target - timedelta(hours=1, minutes=20)).isoformat(),
        "motion_lat_cells_per_hour": 0,
        "motion_lon_cells_per_hour": 0,
        "motion_confidence": 1,
    }
    np.savez_compressed(
        tmp_path / "latest.npz",
        latitude=np.array([40.0, 40.0625], dtype=np.float32),
        longitude=np.array([-74.0, -73.9375], dtype=np.float32),
        cloud_probability=np.full((2, 2), 0.8, dtype=np.float32),
        metadata=np.asarray(json.dumps(metadata)),
    )
    provider = GoesObservationProvider(tmp_path)
    sampled, weight = provider.sample(np.array([40.02]), np.array([-73.98]), day)
    assert np.isclose(sampled[0], 0.8)
    assert np.isclose(weight, 0.3)
