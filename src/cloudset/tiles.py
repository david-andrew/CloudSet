from __future__ import annotations

import hashlib
import io
import math
import shutil
from datetime import date
from pathlib import Path

import numpy as np
from PIL import Image

from .forecast import ForecastEngine

TILE_SIZE = 256
ZOOM_RESOLUTIONS = ((5, 0.5), (6, 0.25), (7, 0.125), (30, 0.0625))


def resolution_for_zoom(zoom: int) -> float:
    for maximum, resolution in ZOOM_RESOLUTIONS:
        if zoom <= maximum:
            return resolution
    return 0.0625


def _mercator_lat(tile_y: np.ndarray, scale: int) -> np.ndarray:
    return np.degrees(np.arctan(np.sinh(math.pi * (1 - 2 * tile_y / scale))))


def _active_codes(active: list[str]) -> np.ndarray:
    codes: list[int] = []
    for item in active:
        try:
            lat, lon = map(float, item.split(","))
        except (ValueError, AttributeError):
            continue
        codes.append((int(math.floor(lat)) + 90) * 360 + int(math.floor(lon)) + 180)
    return np.asarray(codes, dtype=np.int32)


def cache_key(active: list[str], provider: str, day: date, minute_offset: int) -> str:
    footprint = hashlib.sha1("|".join(sorted(active)).encode(), usedforsecurity=False).hexdigest()[:12]
    return f"{day.isoformat()}_{minute_offset:+03d}_{provider}_{footprint}"


def _rgba(scores: np.ndarray, mask: np.ndarray) -> np.ndarray:
    stops = np.asarray([0, 35, 48, 68, 82, 99], dtype=np.float32)
    colors = np.asarray(
        [[122, 132, 128], [174, 172, 139], [241, 176, 63], [233, 93, 45], [167, 45, 89], [91, 43, 126]],
        dtype=np.float32,
    )
    rgba = np.zeros((*scores.shape, 4), dtype=np.uint8)
    for channel in range(3):
        rgba[..., channel] = np.interp(scores, stops, colors[:, channel]).astype(np.uint8)
    rgba[..., 3] = np.where(mask, np.clip(42 + scores * 1.55, 48, 196), 0).astype(np.uint8)
    return rgba


class ForecastTilePyramid:
    def __init__(self, root: Path, engine: ForecastEngine):
        self.root = root
        self.engine = engine

    def _prepare_namespace(self, key: str, retain: int = 64) -> Path:
        namespace = self.root / key
        if namespace.exists():
            return namespace
        namespace.mkdir(parents=True, exist_ok=True)
        others = sorted(
            (path for path in self.root.iterdir() if path.is_dir() and path != namespace),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for expired in others[max(0, retain - 1) :]:
            shutil.rmtree(expired, ignore_errors=True)
        return namespace

    def render(
        self,
        active: list[str],
        day: date,
        minute_offset: int,
        zoom: int,
        tile_x: int,
        tile_y: int,
    ) -> tuple[bytes, bool, float]:
        """Return PNG bytes, whether they came from disk cache, and data resolution."""
        resolution = resolution_for_zoom(zoom)
        key = cache_key(active, self.engine.provider.name, day, minute_offset)
        namespace = self._prepare_namespace(key)
        path = namespace / str(zoom) / str(tile_x) / f"{tile_y}.png"
        if path.exists():
            return path.read_bytes(), True, resolution

        scale = 2**zoom
        pixel_x = tile_x + (np.arange(TILE_SIZE, dtype=np.float32) + 0.5) / TILE_SIZE
        pixel_y = tile_y + (np.arange(TILE_SIZE, dtype=np.float32) + 0.5) / TILE_SIZE
        longitudes = pixel_x / scale * 360.0 - 180.0
        latitudes = _mercator_lat(pixel_y, scale)
        lon, lat = np.meshgrid(longitudes, latitudes)

        parent_codes = ((np.floor(lat).astype(np.int32) + 90) * 360 + np.floor(lon).astype(np.int32) + 180)
        mask = np.isin(parent_codes, _active_codes(active))
        if mask.any():
            # Each zoom samples a level of the prediction pyramid. The finest
            # level is close to HRRR's display scale without sending raw 3 km data.
            sample_lat = (np.floor(lat / resolution) + 0.5) * resolution
            sample_lon = (np.floor(lon / resolution) + 0.5) * resolution
            scores, _ = self.engine.score_field(sample_lat, sample_lon, day, minute_offset)
        else:
            scores = np.zeros_like(lat, dtype=np.float32)

        image = Image.fromarray(_rgba(scores, mask), mode="RGBA")
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        content = output.getvalue()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return content, False, resolution
