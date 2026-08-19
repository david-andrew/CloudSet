import json
from datetime import date, datetime, timezone

import numpy as np

from cloudset.forecast import default_region
from cloudset.hrrr import HrrrWeatherProvider, _extended_cycle_for, bounds_for_region


def test_cycle_selection_uses_latest_available_extended_run():
    target = datetime(2026, 8, 20, 0, tzinfo=timezone.utc)
    now = datetime(2026, 8, 19, 3, 30, tzinfo=timezone.utc)
    cycle, lead = _extended_cycle_for(target, now)
    assert cycle == datetime(2026, 8, 19, 0, tzinfo=timezone.utc)
    assert lead == 24


def test_region_bounds_include_upstream_buffer():
    west, south, east, north = bounds_for_region(default_region())
    assert west <= -91
    assert south <= 22
    assert east >= -63
    assert north >= 51


def test_live_provider_reads_compact_snapshot(tmp_path):
    day = date(2026, 8, 18)
    metadata = {
        "cycle_utc": "2026-08-18T18:00:00+00:00",
        "forecast_hour": 6,
        "valid_utc": "2026-08-19T00:00:00+00:00",
    }
    shape = (2, 2)
    np.savez_compressed(
        tmp_path / f"{day.isoformat()}.npz",
        latitude=np.array([40.0, 40.0625], dtype=np.float32),
        longitude=np.array([-74.0, -73.9375], dtype=np.float32),
        low_cloud=np.full(shape, 0.2, dtype=np.float32),
        mid_cloud=np.full(shape, 0.6, dtype=np.float32),
        high_cloud=np.full(shape, 0.8, dtype=np.float32),
        visibility=np.full(shape, 10_000, dtype=np.float32),
        precip_rate=np.zeros(shape, dtype=np.float32),
        texture=np.full(shape, 0.7, dtype=np.float32),
        metadata=np.asarray(json.dumps(metadata)),
    )
    provider = HrrrWeatherProvider(tmp_path)
    field = provider.field(np.array([40.02]), np.array([-73.98]), day)
    assert provider.metadata(day)["mode"] == "live"
    assert np.isclose(field.mid_cloud[0], 0.6)
    assert np.isclose(field.high_cloud[0], 0.8)
    assert field.visibility_clarity[0] == 1
