from __future__ import annotations

import json
import logging
import math
import threading
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from eccodes import (
    codes_get,
    codes_get_array,
    codes_get_values,
    codes_grib_new_from_file,
    codes_release,
)
from scipy.spatial import cKDTree

from .forecast import DemoWeatherProvider, WeatherField
from .solar import sunset_utc

log = logging.getLogger(__name__)
NOMADS_FILTER = "https://nomads.ncep.noaa.gov/cgi-bin/filter_hrrr_2d.pl"
GRID_RESOLUTION = 0.0625
FIELD_NAMES = {"lcc", "mcc", "hcc", "vis", "prate"}


def bounds_for_region(active: list[str]) -> tuple[float, float, float, float]:
    points = [tuple(map(float, item.split(","))) for item in active]
    latitudes = [point[0] for point in points]
    longitudes = [point[1] for point in points]
    return (
        max(-130.0, math.floor(min(longitudes) - 6)),
        max(20.0, math.floor(min(latitudes) - 2)),
        min(-60.0, math.ceil(max(longitudes) + 2)),
        min(55.0, math.ceil(max(latitudes) + 4)),
    )


def _extended_cycle_for(target: datetime, now: datetime) -> tuple[datetime, int]:
    available = min(target, now - timedelta(hours=3))
    cycle_hour = (available.hour // 6) * 6
    cycle = available.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)
    lead = round((target - cycle).total_seconds() / 3600)
    if not 0 <= lead <= 48:
        raise ValueError(f"No HRRR extended cycle covers {target.isoformat()}")
    return cycle, lead


def _download_url(cycle: datetime, lead: int, bounds: tuple[float, float, float, float]) -> str:
    west, south, east, north = bounds
    params = {
        "file": f"hrrr.t{cycle:%H}z.wrfsfcf{lead:02d}.grib2",
        "lev_low_cloud_layer": "on",
        "lev_middle_cloud_layer": "on",
        "lev_high_cloud_layer": "on",
        "lev_surface": "on",
        "lev_entire_atmosphere": "on",
        "var_LCDC": "on",
        "var_MCDC": "on",
        "var_HCDC": "on",
        "var_VIS": "on",
        "var_PRATE": "on",
        "subregion": "",
        "leftlon": str(west),
        "rightlon": str(east),
        "toplat": str(north),
        "bottomlat": str(south),
        "dir": f"/hrrr.{cycle:%Y%m%d}/conus",
    }
    return f"{NOMADS_FILTER}?{urllib.parse.urlencode(params)}"


class HrrrIngestor:
    def __init__(self, root: Path):
        self.root = root
        self.raw_root = root / "raw"
        self.fields_root = root / "fields"
        self._target_indices: np.ndarray | None = None
        self._target_shape: tuple[int, int] | None = None
        self._grid_axes: tuple[np.ndarray, np.ndarray] | None = None
        self._lock = threading.Lock()

    def _download(self, cycle: datetime, lead: int, bounds: tuple[float, float, float, float]) -> Path:
        bounds_key = "_".join(str(int(value)) for value in bounds)
        path = self.raw_root / f"hrrr_{cycle:%Y%m%d_%H}_f{lead:02d}_{bounds_key}.grib2"
        if path.exists() and path.stat().st_size > 100_000:
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(_download_url(cycle, lead, bounds), headers={"User-Agent": "cloudset/0.1"})
        with urllib.request.urlopen(request, timeout=120) as response:
            content = response.read()
        if not content.startswith(b"GRIB"):
            raise RuntimeError("NOAA returned a non-GRIB response; the requested HRRR cycle may not be ready")
        temporary = path.with_suffix(".part")
        temporary.write_bytes(content)
        temporary.replace(path)
        self._prune_raw()
        return path

    def _prune_raw(self, retain: int = 6) -> None:
        files = sorted(self.raw_root.glob("*.grib2"), key=lambda item: item.stat().st_mtime, reverse=True)
        for expired in files[retain:]:
            expired.unlink(missing_ok=True)

    def _prepare_target_grid(
        self, source_lat: np.ndarray, source_lon: np.ndarray, bounds: tuple[float, float, float, float]
    ) -> None:
        west, south, east, north = bounds
        lat_axis = np.arange(south + GRID_RESOLUTION / 2, north, GRID_RESOLUTION, dtype=np.float32)
        lon_axis = np.arange(west + GRID_RESOLUTION / 2, east, GRID_RESOLUTION, dtype=np.float32)
        lon_grid, lat_grid = np.meshgrid(lon_axis, lat_axis)
        scale = math.cos(math.radians((south + north) / 2))
        source = np.column_stack((source_lat.astype(np.float32), source_lon.astype(np.float32) * scale))
        target = np.column_stack((lat_grid.ravel(), lon_grid.ravel() * scale))
        tree = cKDTree(source)
        _, indices = tree.query(target, workers=-1)
        self._target_indices = indices
        self._target_shape = lat_grid.shape
        self._grid_axes = lat_axis, lon_axis

    def _decode(
        self, path: Path, bounds: tuple[float, float, float, float]
    ) -> dict[str, np.ndarray]:
        raw_fields: dict[str, np.ndarray] = {}
        source_lat = source_lon = None
        with path.open("rb") as stream:
            while (message := codes_grib_new_from_file(stream)) is not None:
                try:
                    name = str(codes_get(message, "shortName"))
                    if name not in FIELD_NAMES:
                        continue
                    raw_fields[name] = np.asarray(codes_get_values(message), dtype=np.float32)
                    if source_lat is None:
                        source_lat = np.asarray(codes_get_array(message, "latitudes"), dtype=np.float32)
                        source_lon = np.asarray(codes_get_array(message, "longitudes"), dtype=np.float32)
                        source_lon = np.where(source_lon > 180, source_lon - 360, source_lon)
                finally:
                    codes_release(message)
        missing = FIELD_NAMES - raw_fields.keys()
        if missing or source_lat is None or source_lon is None:
            raise RuntimeError(f"HRRR subset is missing fields: {', '.join(sorted(missing))}")
        if self._target_indices is None:
            self._prepare_target_grid(source_lat, source_lon, bounds)
        assert self._target_indices is not None and self._target_shape is not None
        return {name: values[self._target_indices].reshape(self._target_shape) for name, values in raw_fields.items()}

    def ingest_day(
        self,
        target_day: date,
        active: list[str],
        now: datetime | None = None,
    ) -> dict:
        now = now or datetime.now(timezone.utc)
        target = sunset_utc(target_day, 38.0, -75.0)
        if target is None:
            raise RuntimeError("Could not calculate representative East Coast sunset")
        rounded_target = target.replace(minute=0, second=0, microsecond=0) + (
            timedelta(hours=1) if target.minute >= 30 else timedelta()
        )
        target = rounded_target
        cycle, lead = _extended_cycle_for(target, now)
        bounds = bounds_for_region(active)
        raw = self._download(cycle, lead, bounds)
        fields = self._decode(raw, bounds)
        cloud_canvas = fields["mcc"] * 0.64 + fields["hcc"] * 0.53
        grad_y, grad_x = np.gradient(cloud_canvas / 100.0)
        texture = np.clip(0.30 + np.hypot(grad_x, grad_y) * 7.5, 0, 1).astype(np.float32)
        assert self._grid_axes is not None
        lat_axis, lon_axis = self._grid_axes
        metadata = {
            "source": "NOAA HRRR",
            "cycle_utc": cycle.isoformat(),
            "forecast_hour": lead,
            "valid_utc": target.isoformat(),
            "downloaded_at": now.isoformat(),
            "bounds": bounds,
            "resolution_degrees": GRID_RESOLUTION,
            "raw_bytes": raw.stat().st_size,
        }
        self.fields_root.mkdir(parents=True, exist_ok=True)
        destination = self.fields_root / f"{target_day.isoformat()}.npz"
        temporary = destination.with_suffix(".tmp.npz")
        np.savez_compressed(
            temporary,
            latitude=lat_axis,
            longitude=lon_axis,
            low_cloud=fields["lcc"].astype(np.float32) / 100,
            mid_cloud=fields["mcc"].astype(np.float32) / 100,
            high_cloud=fields["hcc"].astype(np.float32) / 100,
            visibility=fields["vis"].astype(np.float32),
            precip_rate=fields["prate"].astype(np.float32),
            texture=texture,
            metadata=np.asarray(json.dumps(metadata)),
        )
        temporary.replace(destination)
        return metadata

    def refresh(self, active: list[str], start_day: date, day_count: int = 2) -> list[dict]:
        with self._lock:
            self._target_indices = None
            self._target_shape = None
            self._grid_axes = None
            results = []
            for offset in range(day_count):
                results.append(self.ingest_day(start_day + timedelta(days=offset), active))
            return results


class HrrrWeatherProvider:
    name = "hrrr-live+demo-fallback"

    def __init__(self, fields_root: Path):
        self.fields_root = fields_root
        self.fallback = DemoWeatherProvider()
        self._cache: OrderedDict[date, dict] = OrderedDict()
        self._lock = threading.RLock()

    def _load(self, day: date) -> dict | None:
        path = self.fields_root / f"{day.isoformat()}.npz"
        if not path.exists():
            return None
        with self._lock:
            if day in self._cache:
                return self._cache[day]
            with np.load(path, allow_pickle=False) as archive:
                data = {name: archive[name] for name in archive.files}
            self._cache[day] = data
            while len(self._cache) > 3:
                self._cache.popitem(last=False)
            return data

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    @staticmethod
    def _sample(data: dict, name: str, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        lat_axis = data["latitude"]
        lon_axis = data["longitude"]
        values = data[name]
        y = np.clip((lat - lat_axis[0]) / GRID_RESOLUTION, 0, len(lat_axis) - 1.001)
        x = np.clip((lon - lon_axis[0]) / GRID_RESOLUTION, 0, len(lon_axis) - 1.001)
        y0 = np.floor(y).astype(np.int32)
        x0 = np.floor(x).astype(np.int32)
        y1 = np.minimum(y0 + 1, len(lat_axis) - 1)
        x1 = np.minimum(x0 + 1, len(lon_axis) - 1)
        fy, fx = y - y0, x - x0
        return (
            values[y0, x0] * (1 - fy) * (1 - fx)
            + values[y1, x0] * fy * (1 - fx)
            + values[y0, x1] * (1 - fy) * fx
            + values[y1, x1] * fy * fx
        )

    def field(self, latitudes: np.ndarray, longitudes: np.ndarray, day: date) -> WeatherField:
        data = self._load(day)
        if data is None:
            return self.fallback.field(latitudes, longitudes, day)
        low = self._sample(data, "low_cloud", latitudes, longitudes)
        mid = self._sample(data, "mid_cloud", latitudes, longitudes)
        high = self._sample(data, "high_cloud", latitudes, longitudes)
        west_low = self._sample(data, "low_cloud", latitudes, longitudes - 4.0)
        precip_rate = self._sample(data, "precip_rate", latitudes, longitudes) * 3600
        visibility = self._sample(data, "visibility", latitudes, longitudes)
        return WeatherField(
            low_cloud=np.clip(low, 0, 1),
            mid_cloud=np.clip(mid, 0, 1),
            high_cloud=np.clip(high, 0, 1),
            aerosol=np.full_like(low, 0.09),
            precip=np.clip(precip_rate / 1.0, 0, 1),
            texture=np.clip(self._sample(data, "texture", latitudes, longitudes), 0, 1),
            western_clearance=np.clip(1 - west_low, 0, 1),
            visibility_clarity=np.clip((visibility - 1000) / 9000, 0, 1),
        )

    def metadata(self, day: date) -> dict:
        data = self._load(day)
        if data is None:
            return {"mode": "demo", "model_run": "DEMO fallback · HRRR snapshot unavailable", "confidence": 42}
        metadata = json.loads(str(data["metadata"]))
        cycle = datetime.fromisoformat(metadata["cycle_utc"])
        return {
            "mode": "live",
            "model_run": f"HRRR {cycle:%Y-%m-%d %HZ} · F{metadata['forecast_hour']:02d}",
            "confidence": round(max(50, 80 - metadata["forecast_hour"] * 0.6)),
            **metadata,
        }

    def cache_key(self, day: date) -> str:
        metadata = self.metadata(day)
        return f"{self.name}-{metadata.get('cycle_utc', 'fallback')}-f{metadata.get('forecast_hour', 0)}"


def run() -> None:
    from zoneinfo import ZoneInfo

    from .config import get_settings
    from .forecast import default_region
    from .store import Store

    settings = get_settings()
    store = Store(settings.db_path)
    active = store.get_json("active_region", default_region())
    today = datetime.now(ZoneInfo("America/New_York")).date()
    results = HrrrIngestor(settings.root / "data" / "hrrr").refresh(active, today)
    print(json.dumps(results, indent=2))
