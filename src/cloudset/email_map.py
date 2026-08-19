from __future__ import annotations

import io
import math
import time
import urllib.request
from collections.abc import Callable
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .tiles import TILE_SIZE, ForecastTilePyramid

MAP_WIDTH = 600
MAP_HEIGHT = 340
MAP_ZOOM = 10
MAP_SCALE = 1.25
OSM_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
OSM_TILE_URL = "https://tile.openstreetmap.org/{zoom}/{x}/{y}.png"
LABEL_TILE_URL = "https://basemaps.cartocdn.com/light_only_labels/{zoom}/{x}/{y}@2x.png"


def _world_pixel(latitude: float, longitude: float, zoom: int) -> tuple[float, float]:
    scale = (2**zoom) * TILE_SIZE
    latitude = max(-85.05112878, min(85.05112878, latitude))
    latitude_radians = math.radians(latitude)
    x = (longitude + 180.0) / 360.0 * scale
    y = (1.0 - math.asinh(math.tan(latitude_radians)) / math.pi) / 2.0 * scale
    return x, y


def _fetch_osm_tile(cache_root: Path, zoom: int, tile_x: int, tile_y: int) -> bytes:
    path = cache_root / str(zoom) / str(tile_x) / f"{tile_y}.png"
    if path.exists() and time.time() - path.stat().st_mtime < OSM_MAX_AGE_SECONDS:
        return path.read_bytes()
    request = urllib.request.Request(
        OSM_TILE_URL.format(zoom=zoom, x=tile_x, y=tile_y),
        headers={"User-Agent": "Cloudset/0.1 sunset forecast map (local personal service)"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        content = response.read()
    # Decode before caching so an upstream error document cannot poison the cache.
    with Image.open(io.BytesIO(content)) as image:
        image.verify()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return content


def _fetch_label_tile(cache_root: Path, zoom: int, tile_x: int, tile_y: int) -> bytes:
    path = cache_root / str(zoom) / str(tile_x) / f"{tile_y}.png"
    if path.exists() and time.time() - path.stat().st_mtime < OSM_MAX_AGE_SECONDS:
        return path.read_bytes()
    request = urllib.request.Request(
        LABEL_TILE_URL.format(zoom=zoom, x=tile_x, y=tile_y),
        headers={"User-Agent": "Cloudset/0.1 sunset forecast map (local personal service)"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        content = response.read()
    with Image.open(io.BytesIO(content)) as image:
        image.verify()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return content


def _draw_marker(image: Image.Image, x: float, y: float) -> None:
    draw = ImageDraw.Draw(image, "RGBA")
    draw.ellipse((x - 18, y - 15, x + 18, y + 21), fill=(20, 20, 24, 70))
    draw.ellipse((x - 16, y - 18, x + 16, y + 14), fill=(255, 255, 255, 255))
    draw.ellipse((x - 12, y - 14, x + 12, y + 10), fill=(226, 91, 48, 255))
    draw.ellipse((x - 4, y - 6, x + 4, y + 2), fill=(255, 255, 255, 255))


def _draw_map_labels(image: Image.Image) -> None:
    draw = ImageDraw.Draw(image, "RGBA")
    font = ImageFont.load_default(size=11)
    legend = "FIERY-SKY POTENTIAL"
    attribution = "OpenStreetMap · CARTO"
    draw.rounded_rectangle((12, MAP_HEIGHT - 39, 190, MAP_HEIGHT - 10), radius=5, fill=(29, 29, 35, 220))
    for index, color in enumerate(((241, 176, 63), (233, 93, 45), (167, 45, 89), (91, 43, 126))):
        left = 19 + index * 13
        draw.rectangle((left, MAP_HEIGHT - 30, left + 11, MAP_HEIGHT - 19), fill=(*color, 255))
    draw.text((76, MAP_HEIGHT - 33), legend, fill=(255, 255, 255, 255), font=font)
    box = draw.textbbox((0, 0), attribution, font=font)
    width = box[2] - box[0]
    draw.rounded_rectangle(
        (MAP_WIDTH - width - 19, MAP_HEIGHT - 31, MAP_WIDTH - 7, MAP_HEIGHT - 7),
        radius=4,
        fill=(255, 255, 255, 220),
    )
    draw.text((MAP_WIDTH - width - 13, MAP_HEIGHT - 28), attribution, fill=(45, 45, 48, 255), font=font)


def render_email_map(
    tile_pyramid: ForecastTilePyramid,
    active: list[str],
    forecast_day: date,
    latitude: float,
    longitude: float,
    cache_root: Path,
    *,
    zoom: int = MAP_ZOOM,
    tile_scale: float = MAP_SCALE,
    forecast_blur: float = 5,
    basemap_loader: Callable[[Path, int, int, int], bytes] = _fetch_osm_tile,
    label_loader: Callable[[Path, int, int, int], bytes] = _fetch_label_tile,
) -> bytes:
    """Compose labeled OSM tiles, the Cloudset overlay, and a location marker."""
    center_x, center_y = _world_pixel(latitude, longitude, zoom)
    left = center_x - MAP_WIDTH / (2 * tile_scale)
    top = center_y - MAP_HEIGHT / (2 * tile_scale)
    first_x = math.floor(left / TILE_SIZE)
    last_x = math.floor((left + (MAP_WIDTH - 1) / tile_scale) / TILE_SIZE)
    first_y = math.floor(top / TILE_SIZE)
    last_y = math.floor((top + (MAP_HEIGHT - 1) / tile_scale) / TILE_SIZE)
    scale = 2**zoom
    display_tile_size = round(TILE_SIZE * tile_scale)
    canvas = Image.new("RGBA", (MAP_WIDTH, MAP_HEIGHT), (232, 229, 222, 255))
    forecast_canvas = Image.new("RGBA", (MAP_WIDTH, MAP_HEIGHT), (0, 0, 0, 0))
    label_canvas = Image.new("RGBA", (MAP_WIDTH, MAP_HEIGHT), (0, 0, 0, 0))

    for world_tile_y in range(first_y, last_y + 1):
        if not 0 <= world_tile_y < scale:
            continue
        for world_tile_x in range(first_x, last_x + 1):
            tile_x = world_tile_x % scale
            paste_x = round((world_tile_x * TILE_SIZE - left) * tile_scale)
            paste_y = round((world_tile_y * TILE_SIZE - top) * tile_scale)
            base_bytes = basemap_loader(cache_root / "osm", zoom, tile_x, world_tile_y)
            with Image.open(io.BytesIO(base_bytes)) as base_tile:
                enlarged_base = base_tile.convert("RGBA").resize(
                    (display_tile_size, display_tile_size), Image.Resampling.LANCZOS
                )
                canvas.alpha_composite(enlarged_base, (paste_x, paste_y))
            overlay_bytes, _, _ = tile_pyramid.render(active, forecast_day, 0, zoom, tile_x, world_tile_y)
            with Image.open(io.BytesIO(overlay_bytes)) as overlay_tile:
                enlarged_overlay = overlay_tile.convert("RGBA").resize(
                    (display_tile_size, display_tile_size), Image.Resampling.NEAREST
                )
                forecast_canvas.alpha_composite(enlarged_overlay, (paste_x, paste_y))
            label_bytes = label_loader(cache_root / "carto_labels", zoom, tile_x, world_tile_y)
            with Image.open(io.BytesIO(label_bytes)) as label_tile:
                enlarged_labels = label_tile.convert("RGBA").resize(
                    (display_tile_size, display_tile_size), Image.Resampling.LANCZOS
                )
                label_canvas.alpha_composite(enlarged_labels, (paste_x, paste_y))

    # Preserve the prediction's strong color while softening cell edges. CARTO's
    # transparent label tiles are applied afterward so names and halos remain
    # readable without washing out the forecast or lifting all roads above it.
    if forecast_blur > 0:
        forecast_canvas = forecast_canvas.filter(ImageFilter.GaussianBlur(radius=forecast_blur))
    canvas.alpha_composite(forecast_canvas)
    canvas.alpha_composite(label_canvas)

    _draw_marker(canvas, (center_x - left) * tile_scale, (center_y - top) * tile_scale)
    _draw_map_labels(canvas)
    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()
