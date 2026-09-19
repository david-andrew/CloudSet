from __future__ import annotations

import io
import json
import logging
import math
import re
import shutil
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, maximum_filter
from scipy.spatial import cKDTree

from .hrrr import GRID_RESOLUTION
from .solar import sunset_utc

log = logging.getLogger(__name__)
GIBS_ROOT = "https://gibs.earthdata.nasa.gov"
LAYER = "GOES-East_ABI_Band13_Clean_Infrared"
MATRIX_SET = "GoogleMapsCompatible_Level6"
ZOOM = 5
TILE_SIZE = 256
FRAME_SPACING_MINUTES = 20
COLORMAP_URL = f"{GIBS_ROOT}/colormaps/v1.3/Clean_Longwave_Infrared_Window_Band.xml"
TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def bounds_for_observation(active: list[str]) -> tuple[float, float, float, float]:
    points = [tuple(map(float, item.split(","))) for item in active]
    latitudes = [point[0] for point in points]
    longitudes = [point[1] for point in points]
    return (
        max(-105.0, math.floor(min(longitudes) - 2)),
        max(18.0, math.floor(min(latitudes) - 2)),
        min(-55.0, math.ceil(max(longitudes) + 2)),
        min(58.0, math.ceil(max(latitudes) + 2)),
    )


def should_refresh_goes(now: datetime) -> bool:
    """Limit ten-minute polling to the useful East Coast sunset window."""
    today = now.astimezone(timezone.utc).date()
    sunset = sunset_utc(today, 38.0, -75.0)
    return bool(sunset and sunset - timedelta(hours=4) <= now <= sunset + timedelta(hours=1))


def _world_pixel(latitude: np.ndarray, longitude: np.ndarray, zoom: int) -> tuple[np.ndarray, np.ndarray]:
    scale = (2**zoom) * TILE_SIZE
    latitude = np.clip(latitude, -85.05112878, 85.05112878)
    radians = np.radians(latitude)
    x = (longitude + 180.0) / 360.0 * scale
    y = (1.0 - np.arcsinh(np.tan(radians)) / math.pi) / 2.0 * scale
    return x, y


def _tile_range(bounds: tuple[float, float, float, float], zoom: int) -> tuple[range, range]:
    west, south, east, north = bounds
    x, y = _world_pixel(np.asarray([north, south]), np.asarray([west, east]), zoom)
    return range(math.floor(x.min() / TILE_SIZE), math.floor(x.max() / TILE_SIZE) + 1), range(
        math.floor(y.min() / TILE_SIZE), math.floor(y.max() / TILE_SIZE) + 1
    )


def _motion(previous: np.ndarray, current: np.ndarray, minutes: int) -> tuple[float, float, float]:
    """Return latitude/longitude grid-cell motion per hour and confidence."""
    previous_centered = previous - np.mean(previous)
    current_centered = current - np.mean(current)
    if np.std(previous_centered) < 0.025 or np.std(current_centered) < 0.025:
        return 0.0, 0.0, 0.0
    cross = np.fft.fft2(current_centered) * np.conj(np.fft.fft2(previous_centered))
    cross /= np.maximum(np.abs(cross), 1e-7)
    correlation = np.abs(np.fft.ifft2(cross))
    peak_index = np.unravel_index(np.argmax(correlation), correlation.shape)
    shifts = []
    for index, size in zip(peak_index, correlation.shape):
        shifts.append(float(index if index <= size // 2 else index - size))
    shift_y, shift_x = shifts
    # More than 0.75° in twenty minutes is not a plausible bulk cloud motion.
    maximum_cells = 0.75 / GRID_RESOLUTION
    if abs(shift_y) > maximum_cells or abs(shift_x) > maximum_cells:
        return 0.0, 0.0, 0.0
    peak_ratio = float(correlation[peak_index] / max(np.mean(correlation), 1e-7))
    confidence = float(np.clip((peak_ratio - 3.0) / 12.0, 0, 1))
    hourly = 60.0 / minutes
    # Array y grows southward, so invert it to obtain latitude motion.
    return -shift_y * hourly, shift_x * hourly, confidence


class GoesIngestor:
    def __init__(self, root: Path):
        self.root = root
        self.raw_root = root / "raw"
        self.fields_root = root / "fields"
        self._lock = threading.Lock()
        self._palette_tree: cKDTree | None = None
        self._palette_values: np.ndarray | None = None

    def _latest_time(self, now: datetime) -> datetime:
        for offset in range(2):
            query_day = (now - timedelta(days=offset)).date()
            url = (
                f"{GIBS_ROOT}/wmts/epsg3857/best/1.0.0/{LAYER}/default/"
                f"{MATRIX_SET}/all/{query_day.isoformat()}.xml"
            )
            try:
                with urllib.request.urlopen(url, timeout=15) as response:
                    root = ET.fromstring(response.read())
            except (urllib.error.URLError, ET.ParseError):
                continue
            domain = " ".join(element.text or "" for element in root.iter() if element.tag.endswith("Domain"))
            timestamps = TIMESTAMP_PATTERN.findall(domain)
            if timestamps:
                return max(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) for value in timestamps)
        raise RuntimeError("NASA GIBS did not report a current GOES-East observation")

    def _load_palette(self) -> tuple[cKDTree, np.ndarray]:
        if self._palette_tree is not None and self._palette_values is not None:
            return self._palette_tree, self._palette_values
        path = self.root / "Clean_Longwave_Infrared_Window_Band.xml"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            request = urllib.request.Request(COLORMAP_URL, headers={"User-Agent": "Cloudset/0.1"})
            with urllib.request.urlopen(request, timeout=20) as response:
                content = response.read()
            ET.fromstring(content)
            path.write_bytes(content)
        root = ET.parse(path).getroot()
        colors: list[tuple[int, int, int]] = []
        temperatures: list[float] = []
        for entry in root.iter("ColorMapEntry"):
            rgb = entry.attrib.get("rgb")
            interval = entry.attrib.get("sourceValue", "")
            numbers = re.findall(r"-?\d+(?:\.\d+)?", interval)
            if not rgb or len(numbers) < 2 or entry.attrib.get("transparent") == "true":
                continue
            colors.append(tuple(map(int, rgb.split(","))))
            temperatures.append((float(numbers[0]) + float(numbers[1])) / 2)
        if not colors:
            raise RuntimeError("GOES infrared colormap contained no usable entries")
        self._palette_tree = cKDTree(np.asarray(colors, dtype=np.float32))
        self._palette_values = np.asarray(temperatures, dtype=np.float32)
        return self._palette_tree, self._palette_values

    def _tile_url(self, observed_at: datetime, tile_x: int, tile_y: int) -> str:
        timestamp = observed_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        return (
            f"{GIBS_ROOT}/wmts/epsg3857/best/{LAYER}/default/{timestamp}/"
            f"{MATRIX_SET}/{ZOOM}/{tile_y}/{tile_x}.png"
        )

    def _download_tile(self, observed_at: datetime, tile_x: int, tile_y: int) -> tuple[int, int, bytes]:
        path = self.raw_root / observed_at.strftime("%Y%m%dT%H%MZ") / str(tile_x) / f"{tile_y}.png"
        if path.exists():
            return tile_x, tile_y, path.read_bytes()
        request = urllib.request.Request(self._tile_url(observed_at, tile_x, tile_y), headers={"User-Agent": "Cloudset/0.1"})
        with urllib.request.urlopen(request, timeout=20) as response:
            content = response.read()
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return tile_x, tile_y, content

    def _frame(
        self,
        observed_at: datetime,
        bounds: tuple[float, float, float, float],
        lat_axis: np.ndarray,
        lon_axis: np.ndarray,
    ) -> tuple[np.ndarray, int]:
        x_range, y_range = _tile_range(bounds, ZOOM)
        coordinates = [(x, y) for y in y_range for x in x_range]
        with ThreadPoolExecutor(max_workers=6) as pool:
            tiles = list(pool.map(lambda pair: self._download_tile(observed_at, *pair), coordinates))
        tree, temperatures = self._load_palette()
        mosaic = np.zeros((len(y_range) * TILE_SIZE, len(x_range) * TILE_SIZE, 3), dtype=np.uint8)
        valid = np.ones(mosaic.shape[:2], dtype=bool)
        raw_bytes = 0
        for tile_x, tile_y, content in tiles:
            with Image.open(io.BytesIO(content)) as image:
                rgba = np.asarray(image.convert("RGBA"))
            left = (tile_x - x_range.start) * TILE_SIZE
            top = (tile_y - y_range.start) * TILE_SIZE
            mosaic[top : top + TILE_SIZE, left : left + TILE_SIZE] = rgba[..., :3]
            valid[top : top + TILE_SIZE, left : left + TILE_SIZE] = rgba[..., 3] > 0
            raw_bytes += len(content)
        _, palette_indices = tree.query(mosaic.reshape(-1, 3).astype(np.float32), workers=-1)
        temperature = temperatures[palette_indices].reshape(mosaic.shape[:2])
        lon_grid, lat_grid = np.meshgrid(lon_axis, lat_axis)
        world_x, world_y = _world_pixel(lat_grid, lon_grid, ZOOM)
        sample_x = np.clip(np.rint(world_x - x_range.start * TILE_SIZE).astype(np.int32), 0, mosaic.shape[1] - 1)
        sample_y = np.clip(np.rint(world_y - y_range.start * TILE_SIZE).astype(np.int32), 0, mosaic.shape[0] - 1)
        sampled = temperature[sample_y, sample_x]
        sampled_valid = valid[sample_y, sample_x]
        warm_background = maximum_filter(sampled, size=9, mode="nearest")
        anomaly = np.clip((warm_background - sampled - 2.0) / 13.0, 0, 1)
        cold_cloud = np.clip((-sampled - 5.0) / 35.0, 0, 1)
        probability = gaussian_filter(np.maximum(anomaly * 0.82, cold_cloud), sigma=0.65)
        return np.where(sampled_valid, probability, 0).astype(np.float32), raw_bytes

    def _prune_raw(self, retain: int = 4) -> None:
        if not self.raw_root.exists():
            return
        directories = sorted((path for path in self.raw_root.iterdir() if path.is_dir()), reverse=True)
        for expired in directories[retain:]:
            shutil.rmtree(expired, ignore_errors=True)

    def refresh(self, active: list[str], now: datetime | None = None) -> dict:
        with self._lock:
            now = now or datetime.now(timezone.utc)
            bounds = bounds_for_observation(active)
            west, south, east, north = bounds
            lat_axis = np.arange(south + GRID_RESOLUTION / 2, north, GRID_RESOLUTION, dtype=np.float32)
            lon_axis = np.arange(west + GRID_RESOLUTION / 2, east, GRID_RESOLUTION, dtype=np.float32)
            latest = self._latest_time(now)
            last_error: Exception | None = None
            for lag in range(3):
                observed_at = latest - timedelta(minutes=lag * 10)
                previous_at = observed_at - timedelta(minutes=FRAME_SPACING_MINUTES)
                try:
                    current, current_bytes = self._frame(observed_at, bounds, lat_axis, lon_axis)
                    previous, previous_bytes = self._frame(previous_at, bounds, lat_axis, lon_axis)
                    break
                except (urllib.error.HTTPError, urllib.error.URLError) as exc:
                    last_error = exc
            else:
                raise RuntimeError("Recent GOES-East tiles are not ready") from last_error
            motion_lat, motion_lon, motion_confidence = _motion(previous, current, FRAME_SPACING_MINUTES)
            metadata = {
                "source": "NASA GIBS / NOAA GOES-East ABI Band 13",
                "observed_utc": observed_at.isoformat(),
                "previous_utc": previous_at.isoformat(),
                "downloaded_at": now.isoformat(),
                "bounds": bounds,
                "resolution_degrees": GRID_RESOLUTION,
                "motion_lat_cells_per_hour": round(motion_lat, 3),
                "motion_lon_cells_per_hour": round(motion_lon, 3),
                "motion_confidence": round(motion_confidence, 3),
                "raw_bytes": current_bytes + previous_bytes,
            }
            self.fields_root.mkdir(parents=True, exist_ok=True)
            destination = self.fields_root / "latest.npz"
            temporary = destination.with_suffix(".tmp.npz")
            np.savez_compressed(
                temporary,
                latitude=lat_axis,
                longitude=lon_axis,
                cloud_probability=current,
                metadata=np.asarray(json.dumps(metadata)),
            )
            temporary.replace(destination)
            self._prune_raw()
            return metadata


class GoesObservationProvider:
    def __init__(self, fields_root: Path):
        self.fields_root = fields_root
        self._data: dict | None = None
        self._mtime_ns = -1
        self._lock = threading.RLock()

    def _load(self) -> dict | None:
        path = self.fields_root / "latest.npz"
        if not path.exists():
            return None
        mtime = path.stat().st_mtime_ns
        with self._lock:
            if self._data is not None and mtime == self._mtime_ns:
                return self._data
            with np.load(path, allow_pickle=False) as archive:
                self._data = {name: archive[name] for name in archive.files}
            self._mtime_ns = mtime
            return self._data

    def invalidate(self) -> None:
        with self._lock:
            self._data = None
            self._mtime_ns = -1

    def status(self, day: date | None = None) -> dict:
        data = self._load()
        if data is None:
            return {"available": False, "correction": None}
        metadata = json.loads(str(data["metadata"]))
        observed = datetime.fromisoformat(metadata["observed_utc"])
        age_minutes = (datetime.now(timezone.utc) - observed).total_seconds() / 60
        return {
            "available": True,
            "age_minutes": round(age_minutes, 1),
            **metadata,
            "correction": self.correction(day) if day else None,
        }

    def correction(self, day: date) -> dict | None:
        data = self._load()
        if data is None:
            return None
        metadata = json.loads(str(data["metadata"]))
        observed = datetime.fromisoformat(metadata["observed_utc"])
        target = sunset_utc(day, 38.0, -75.0)
        if target is None:
            return None
        lead_hours = (target - observed).total_seconds() / 3600
        if not -0.5 <= lead_hours <= 3.0:
            return None
        freshness = float(np.clip(1 - max(0, lead_hours) / 3.0, 0, 1))
        weight = 0.45 * freshness * (0.75 + 0.25 * float(metadata["motion_confidence"]))
        return {**metadata, "lead_hours": round(lead_hours, 2), "weight": round(weight, 3)}

    def sample(self, lat: np.ndarray, lon: np.ndarray, day: date) -> tuple[np.ndarray, float] | None:
        data = self._load()
        correction = self.correction(day)
        if data is None or correction is None:
            return None
        lead_hours = float(correction["lead_hours"])
        source_lat = lat - float(correction["motion_lat_cells_per_hour"]) * GRID_RESOLUTION * lead_hours
        source_lon = lon - float(correction["motion_lon_cells_per_hour"]) * GRID_RESOLUTION * lead_hours
        lat_axis = data["latitude"]
        lon_axis = data["longitude"]
        values = data["cloud_probability"]
        y = np.clip((source_lat - lat_axis[0]) / GRID_RESOLUTION, 0, len(lat_axis) - 1.001)
        x = np.clip((source_lon - lon_axis[0]) / GRID_RESOLUTION, 0, len(lon_axis) - 1.001)
        y0, x0 = np.floor(y).astype(np.int32), np.floor(x).astype(np.int32)
        y1, x1 = np.minimum(y0 + 1, len(lat_axis) - 1), np.minimum(x0 + 1, len(lon_axis) - 1)
        fy, fx = y - y0, x - x0
        sampled = (
            values[y0, x0] * (1 - fy) * (1 - fx)
            + values[y1, x0] * fy * (1 - fx)
            + values[y0, x1] * (1 - fy) * fx
            + values[y1, x1] * fy * fx
        )
        return sampled.astype(np.float32), float(correction["weight"])

    def cache_key(self, day: date) -> str:
        correction = self.correction(day)
        return correction["observed_utc"] if correction else "no-goes-correction"
