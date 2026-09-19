from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

import numpy as np

from .solar import solar_position, sunset_utc

REGION_RESOLUTION = 1.0
FORECAST_RESOLUTION = 0.5
REGION_BOUNDS = {"west": -90, "south": 24, "east": -64, "north": 48}


def cell_id(lat: float, lon: float) -> str:
    return f"{lat:.1f},{lon:.1f}"


def default_region() -> list[str]:
    """A deliberately broad East Coast seed region, editable from the admin map."""
    cells: list[str] = []
    for lat in np.arange(24.0, 48.0, REGION_RESOLUTION):
        if lat < 30:
            west = -83
        elif lat < 35:
            west = -85
        elif lat < 40:
            west = -83
        elif lat < 44:
            west = -81
        else:
            west = -80
        for lon in np.arange(west, -64.0, REGION_RESOLUTION):
            cells.append(cell_id(float(lat), float(lon)))
    return cells


@dataclass(slots=True)
class WeatherField:
    low_cloud: np.ndarray
    mid_cloud: np.ndarray
    high_cloud: np.ndarray
    aerosol: np.ndarray
    precip: np.ndarray
    texture: np.ndarray
    western_clearance: np.ndarray
    visibility_clarity: np.ndarray


class WeatherProvider(Protocol):
    name: str

    def field(self, latitudes: np.ndarray, longitudes: np.ndarray, day: date) -> WeatherField: ...


class DemoWeatherProvider:
    """Deterministic spatial fields used to exercise the full product safely.

    This is intentionally labeled demo everywhere. It is not random per request,
    making screenshots, tests, and user comparisons stable.
    """

    name = "demo-atmosphere"

    @staticmethod
    def _raw(lat: np.ndarray, lon: np.ndarray, day_number: int) -> tuple[np.ndarray, ...]:
        phase = day_number * 0.37
        front = np.sin((lon + 75) * 0.55 + (lat - 36) * 0.31 + phase)
        cross = np.cos((lon + 77) * 0.24 - (lat - 38) * 0.62 - phase * 0.7)
        cellular = np.sin((lon + lat) * 1.7 + phase * 2.1) * np.cos((lon - lat) * 0.8)
        low = np.clip(0.34 + 0.29 * front - 0.16 * cross + 0.08 * cellular, 0, 1)
        mid = np.clip(0.47 - 0.22 * front + 0.25 * cross + 0.11 * cellular, 0, 1)
        high = np.clip(0.43 - 0.11 * front + 0.27 * np.sin((lon + 70) * 0.31 + phase), 0, 1)
        return low, mid, high, cellular

    def field(self, latitudes: np.ndarray, longitudes: np.ndarray, day: date) -> WeatherField:
        n = day.toordinal()
        low, mid, high, cellular = self._raw(latitudes, longitudes, n)
        west_low, _, _, _ = self._raw(latitudes, longitudes - 4.0, n)
        aerosol = np.clip(0.09 + 0.055 * np.sin((latitudes - 30) * 0.35 - n * 0.13) + 0.025 * cellular, 0.01, 0.35)
        precip = np.clip((low - 0.64) * 1.7 + np.maximum(0, mid - 0.82), 0, 1)
        texture = np.clip(0.48 + 0.45 * np.abs(cellular), 0, 1)
        return WeatherField(low, mid, high, aerosol, precip, texture, 1 - west_low, 1 - precip * 0.7)


def _expanded_points(active: list[str]) -> tuple[np.ndarray, np.ndarray]:
    points: list[tuple[float, float]] = []
    offsets = (0.0, 0.5)
    for item in active:
        try:
            lat, lon = (float(part) for part in item.split(","))
        except (ValueError, AttributeError):
            continue
        for dy in offsets:
            for dx in offsets:
                points.append((lat + dy, lon + dx))
    points = sorted(set(points))
    if not points:
        return np.array([], dtype=np.float32), np.array([], dtype=np.float32)
    values = np.asarray(points, dtype=np.float32)
    return values[:, 0], values[:, 1]


def _tier(score: float) -> str:
    if score >= 82:
        return "fire"
    if score >= 68:
        return "vivid"
    if score >= 48:
        return "glow"
    return "quiet"


class ForecastEngine:
    def __init__(self, provider: WeatherProvider | None = None):
        self.provider = provider or DemoWeatherProvider()

    def provider_metadata(self, day: date) -> dict:
        metadata = getattr(self.provider, "metadata", None)
        if metadata:
            return metadata(day)
        return {"mode": "demo", "model_run": "DEMO · deterministic atmospheric field", "confidence": 42}

    def provider_key(self, day: date) -> str:
        key = getattr(self.provider, "cache_key", None)
        return key(day) if key else self.provider.name

    def score_field(
        self, lat: np.ndarray, lon: np.ndarray, day: date, minute_offset: int = 0
    ) -> tuple[np.ndarray, WeatherField]:
        """Score arbitrary coordinate arrays; shapes are preserved."""
        field = self.provider.field(lat, lon, day)
        reflector = np.clip(field.mid_cloud * 0.64 + field.high_cloud * 0.53, 0, 1)
        reflector_fit = np.exp(-((reflector - 0.62) / 0.32) ** 2)
        low_clear = 1 - field.low_cloud**1.35
        aerosol_fit = np.exp(-((field.aerosol - 0.12) / 0.10) ** 2)
        # A modest aerosol layer can deepen warm colors, while dense smoke or
        # haze attenuates the already-long optical path near the horizon.
        aerosol_extinction = np.clip((field.aerosol - 0.35) / 0.65, 0, 1)
        dry = (1 - field.precip) * (0.72 + 0.28 * field.visibility_clarity)
        ingredients = (
            0.34 * reflector_fit * np.clip(reflector / 0.45, 0, 1)
            + 0.22 * field.western_clearance
            + 0.16 * low_clear
            + 0.12 * field.texture
            + 0.09 * aerosol_fit
            + 0.07 * dry
        )
        # Low clouds and precipitation can block even an otherwise ideal deck.
        scores = np.clip(
            100
            * ingredients
            * (1 - 0.48 * field.precip)
            * (0.68 + 0.32 * low_clear)
            * (1 - 0.35 * aerosol_extinction),
            0,
            99,
        )
        if minute_offset:
            scores *= math.exp(-((minute_offset - 4) / 47) ** 2)
        return scores, field

    def calculate(self, active: list[str], day: date, minute_offset: int = 0) -> dict:
        lat, lon = _expanded_points(active)
        scores, field = self.score_field(lat, lon, day, minute_offset)

        provider_meta = self.provider_metadata(day)
        features: list[dict] = []
        sunsets: list[datetime] = []
        for i in range(len(lat)):
            sunset = sunset_utc(day, float(lat[i] + 0.25), float(lon[i] + 0.25))
            if sunset is None:
                continue
            sunsets.append(sunset)
            moment = sunset + timedelta(minutes=minute_offset)
            elevation, azimuth = solar_position(moment, float(lat[i] + 0.25), float(lon[i] + 0.25))
            score = round(float(scores[i]), 1)
            x, y = float(lon[i]), float(lat[i])
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Polygon", "coordinates": [[[x, y], [x + 0.5, y], [x + 0.5, y + 0.5], [x, y + 0.5], [x, y]]]},
                    "properties": {
                        "score": score,
                        "tier": _tier(score),
                        "sunset_utc": sunset.isoformat(),
                        "valid_utc": moment.isoformat(),
                        "sun_azimuth": round(azimuth, 1),
                        "sun_elevation": round(elevation, 2),
                        "confidence": provider_meta["confidence"],
                        "low_cloud": round(float(field.low_cloud[i] * 100)),
                        "mid_cloud": round(float(field.mid_cloud[i] * 100)),
                        "high_cloud": round(float(field.high_cloud[i] * 100)),
                        "western_clearance": round(float(field.western_clearance[i] * 100)),
                        "aerosol_optical_depth": round(float(field.aerosol[i]), 3),
                        "precip_risk": round(float(field.precip[i] * 100)),
                    },
                }
            )
        return {
            "type": "FeatureCollection",
            "features": features,
            "meta": {
                "day": day.isoformat(),
                "minute_offset": minute_offset,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "provider": self.provider.name,
                "mode": provider_meta["mode"],
                "model_run": provider_meta["model_run"],
                "cell_count": len(features),
                "resolution_degrees": FORECAST_RESOLUTION,
                "valid_window_utc": [min(sunsets).isoformat(), max(sunsets).isoformat()] if sunsets else [],
            },
        }

    def point(
        self,
        active: list[str],
        latitude: float,
        longitude: float,
        day: date,
        minute_offset: int = 0,
    ) -> dict | None:
        """Evaluate the selected coordinate directly, not at a display-grid center."""
        parent = cell_id(float(math.floor(latitude)), float(math.floor(longitude)))
        if parent not in set(active):
            return None
        lat = np.asarray([latitude], dtype=np.float32)
        lon = np.asarray([longitude], dtype=np.float32)
        scores, field = self.score_field(lat, lon, day, minute_offset)
        provider_meta = self.provider_metadata(day)
        sunset = sunset_utc(day, latitude, longitude)
        if sunset is None:
            return None
        moment = sunset + timedelta(minutes=minute_offset)
        elevation, azimuth = solar_position(moment, latitude, longitude)
        score = round(float(scores[0]), 1)
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [longitude, latitude]},
            "properties": {
                "score": score,
                "tier": _tier(score),
                "sunset_utc": sunset.isoformat(),
                "valid_utc": moment.isoformat(),
                "sun_azimuth": round(azimuth, 1),
                "sun_elevation": round(elevation, 2),
                "confidence": provider_meta["confidence"],
                "low_cloud": round(float(field.low_cloud[0] * 100)),
                "mid_cloud": round(float(field.mid_cloud[0] * 100)),
                "high_cloud": round(float(field.high_cloud[0] * 100)),
                "western_clearance": round(float(field.western_clearance[0] * 100)),
                "aerosol_optical_depth": round(float(field.aerosol[0]), 3),
                "precip_risk": round(float(field.precip[0] * 100)),
            },
        }

    @staticmethod
    def nearest(collection: dict, latitude: float, longitude: float) -> dict | None:
        best = None
        distance = float("inf")
        for feature in collection["features"]:
            ring = feature["geometry"]["coordinates"][0]
            center_lon = (ring[0][0] + ring[2][0]) / 2
            center_lat = (ring[0][1] + ring[2][1]) / 2
            candidate = (center_lat - latitude) ** 2 + ((center_lon - longitude) * math.cos(math.radians(latitude))) ** 2
            if candidate < distance:
                distance, best = candidate, feature
        if best is None or distance > 2.25:
            return None
        return best
