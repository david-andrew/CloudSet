import io
import math
from datetime import date

from PIL import Image

from cloudset.forecast import ForecastEngine, default_region
from cloudset.tiles import ForecastTilePyramid, resolution_for_zoom


def _tile_for(latitude: float, longitude: float, zoom: int) -> tuple[int, int]:
    scale = 2**zoom
    x = int((longitude + 180) / 360 * scale)
    lat_rad = math.radians(latitude)
    y = int((1 - math.asinh(math.tan(lat_rad)) / math.pi) / 2 * scale)
    return x, y


def test_zoom_levels_refine_to_hrrr_scale():
    assert resolution_for_zoom(5) == 0.5
    assert resolution_for_zoom(6) == 0.25
    assert resolution_for_zoom(7) == 0.125
    assert resolution_for_zoom(8) == 0.0625
    assert resolution_for_zoom(12) == 0.0625


def test_tile_is_rendered_and_then_read_from_cache(tmp_path):
    zoom = 8
    x, y = _tile_for(40.7128, -74.006, zoom)
    pyramid = ForecastTilePyramid(tmp_path, ForecastEngine())
    first, first_cached, resolution = pyramid.render(default_region(), date(2026, 8, 18), 0, zoom, x, y)
    second, second_cached, _ = pyramid.render(default_region(), date(2026, 8, 18), 0, zoom, x, y)
    image = Image.open(io.BytesIO(first))
    assert first_cached is False
    assert second_cached is True
    assert first == second
    assert resolution == 0.0625
    assert image.size == (256, 256)
    assert image.getchannel("A").getextrema()[1] > 0


def test_point_forecast_uses_exact_coordinate():
    engine = ForecastEngine()
    result = engine.point(default_region(), 40.7128, -74.006, date(2026, 8, 18))
    assert result is not None
    assert result["geometry"]["coordinates"] == [-74.006, 40.7128]
