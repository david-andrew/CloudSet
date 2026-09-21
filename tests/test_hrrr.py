import json
from datetime import date, datetime, timezone

import numpy as np

from cloudset.forecast import default_region
import pytest

from cloudset.hrrr import HrrrIngestor, HrrrWeatherProvider, _download_url, _extended_cycle_for, bounds_for_region, parse_index


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


def test_subset_requests_smoke_aerosol_optical_depth():
    url = _download_url(datetime(2026, 8, 19, tzinfo=timezone.utc), 24, (-91, 22, -63, 51))
    assert "var_AOTK=on" in url
    assert "lev_entire_atmosphere=on" in url
    assert "lev_entire_atmosphere_%28considered_as_a_single_layer%29=on" in url


IDX = """1:0:d=2026092018:REFC:entire atmosphere:6 hour fcst:
5:2235751:d=2026092018:VIS:surface:6 hour fcst:
6:3000000:d=2026092018:REFD:1000 m above ground:6 hour fcst:
83:62696012:d=2026092018:PRATE:surface:6 hour fcst:
84:63000000:d=2026092018:APCP:surface:0-6 hour acc fcst:
111:79376078:d=2026092018:AOTK:entire atmosphere (considered as a single layer):6 hour fcst:
112:80000000:d=2026092018:TCDC:entire atmosphere:6 hour fcst:
116:83846564:d=2026092018:LCDC:low cloud layer:6 hour fcst:
117:84721097:d=2026092018:MCDC:middle cloud layer:6 hour fcst:
118:85266728:d=2026092018:HCDC:high cloud layer:6 hour fcst:
"""


def test_index_parsing_selects_only_needed_messages():
    ranges = parse_index(IDX)
    assert ranges == [
        (2235751, 3000000),
        (62696012, 63000000),
        (79376078, 80000000),
        (83846564, 84721097),
        (84721097, 85266728),
        (85266728, None),
    ]
    with pytest.raises(RuntimeError):
        parse_index(IDX.replace("HCDC", "XXXX"))


def test_download_prefers_aws_and_falls_back_to_nomads(tmp_path, monkeypatch):
    ingestor = HrrrIngestor(tmp_path)
    cycle = datetime(2026, 9, 20, 18, tzinfo=timezone.utc)
    monkeypatch.setattr(HrrrIngestor, "_download_aws", staticmethod(lambda c, l: b"GRIB" + b"a" * 200_000))
    path, source = ingestor._download(cycle, 6, (-91, 22, -63, 51))
    assert source == "NOAA HRRR via AWS"
    assert path.read_bytes().startswith(b"GRIB")
    # Cached copy remembers where it came from.
    assert ingestor._download(cycle, 6, (-91, 22, -63, 51))[1] == "NOAA HRRR via AWS"

    def boom(c, l):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(HrrrIngestor, "_download_aws", staticmethod(boom))
    monkeypatch.setattr(HrrrIngestor, "_download_nomads", staticmethod(lambda c, l, b: b"GRIB" + b"n" * 200_000))
    path, source = ingestor._download(cycle, 12, (-91, 22, -63, 51))
    assert source == "NOAA HRRR via NOMADS"


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


def test_live_provider_blends_recent_goes_cloud_observation(tmp_path):
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

    class Observations:
        @staticmethod
        def sample(latitudes, _longitudes, _day):
            return np.full_like(latitudes, 0.2), 0.5

        @staticmethod
        def correction(_day):
            return {"observed_utc": "2026-08-18T22:00:00+00:00", "weight": 0.5}

        @staticmethod
        def cache_key(_day):
            return "observed"

    provider = HrrrWeatherProvider(tmp_path, Observations())
    field = provider.field(np.array([40.02]), np.array([-73.98]), day)
    assert np.isclose(field.mid_cloud[0], 0.4)
    assert np.isclose(field.high_cloud[0], 0.5)
    assert "GOES corrected" in provider.metadata(day)["model_run"]
