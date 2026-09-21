import io
from dataclasses import replace
from datetime import date, datetime, timezone

from PIL import Image

import cloudset.main as main
from cloudset.email_map import MAP_HEIGHT, MAP_WIDTH, render_email_map
from cloudset.mailer import build_confirmation_message, build_downgrade_message, build_forecast_message, countdown_label, plain_text_content


def png(color, mode="RGBA"):
    image = Image.new(mode, (256, 256), color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class FakePyramid:
    def render(self, *_):
        return png((220, 70, 50, 100)), False, 0.0625


def test_email_map_composites_forecast_and_marks_location(tmp_path):
    def basemap_loader(*_):
        return png((235, 235, 230, 255))

    def label_loader(*_):
        return png((0, 0, 0, 0))

    content = render_email_map(
        FakePyramid(),
        ["40.0,-74.0"],
        date(2026, 8, 19),
        40.7128,
        -74.006,
        tmp_path,
        basemap_loader=basemap_loader,
        label_loader=label_loader,
    )
    with Image.open(io.BytesIO(content)) as image:
        assert image.size == (MAP_WIDTH, MAP_HEIGHT)
        # The marker has a white center and a warm-red body.
        assert image.getpixel((MAP_WIDTH // 2, MAP_HEIGHT // 2)) == (255, 255, 255)
        red = image.getpixel((MAP_WIDTH // 2 + 8, MAP_HEIGHT // 2))
        assert red[0] > red[1]


def test_forecast_email_has_location_html_map_and_plain_fallback():
    forecast = {
        "geometry": {"type": "Point", "coordinates": [-74.006, 40.7128]},
        "properties": {
            "score": 91,
            "tier": "fire",
            "sunset_utc": "2026-08-19T23:48:10+00:00",
            "mid_cloud": 42,
            "high_cloud": 76,
            "western_clearance": 88,
            "aerosol_optical_depth": 0.12,
        },
    }
    settings = replace(
        main.settings, smtp_host="", public_url="https://cloudset.example", secret_key="k" * 40, donate_url="https://ko-fi.com/x"
    )
    message = build_forecast_message(
        settings,
        {"id": 3, "email": "watch@example.com", "label": "Brooklyn rooftop"},
        forecast,
        "Morning sunset outlook",
        png((240, 240, 240, 255)),
        png((230, 230, 230, 255)),
        now=datetime(2026, 8, 19, 20, 48, tzinfo=timezone.utc),
    )
    plain = plain_text_content(message)
    html = message.get_body(preferencelist=("html",)).get_content()
    assert "Location: Brooklyn rooftop (40.7128, -74.0060)" in plain
    assert "Wednesday, August 19 at 7:48 PM EDT" in plain
    assert "https://cloudset.example/?lat=40.71280&lon=-74.00600&date=2026-08-19&label=Brooklyn+rooftop" in plain
    assert "Brooklyn rooftop" in html
    assert "Open interactive outlook" in html
    assert "https://cloudset.example/?lat=40.71280&amp;lon=-74.00600" in html
    assert 'src="cid:cloudset-map-detail"' in html
    assert 'src="cid:cloudset-map-regional"' in html
    assert sum(part.get_content_type() == "image/png" for part in message.walk()) == 2
    assert message["Subject"] == "Cloudset: 91% sunset potential in 3 hours near Brooklyn rooftop"
    assert message["List-Unsubscribe"].startswith("<https://cloudset.example/unsubscribe?token=")
    assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "https://cloudset.example/manage?token=" in plain
    assert "https://ko-fi.com/x" in html
    assert "https://cloudset.example/rate?token=" in plain
    assert plain.count("&rating=") == 5
    assert "HOW WAS IT?" in html


def test_countdown_phrasing():
    sunset = "2026-08-19T23:48:00+00:00"
    at = lambda h, m: datetime(2026, 8, 19, h, m, tzinfo=timezone.utc)  # noqa: E731
    assert countdown_label(sunset, at(23, 46)) == "right now"
    assert countdown_label(sunset, at(23, 3)) == "in 45 minutes"
    assert countdown_label(sunset, at(20, 48)) == "in 3 hours"
    assert countdown_label(sunset, at(21, 18)) == "in 2.5 hours"
    assert countdown_label(sunset, datetime(2026, 8, 18, 22, 0, tzinfo=timezone.utc)) == "tomorrow evening"


def test_confirmation_and_downgrade_messages():
    settings = replace(main.settings, smtp_host="", public_url="https://cloudset.example", secret_key="k" * 40)
    subscriber = {"id": 9, "email": "new@example.com", "label": "Harpers Ferry", "latitude": 39.32, "longitude": -77.74, "threshold": 70}
    confirmation = build_confirmation_message(settings, subscriber)
    assert "Confirm your Cloudset sunset watch for Harpers Ferry" == confirmation["Subject"]
    assert "https://cloudset.example/confirm?token=" in plain_text_content(confirmation)
    assert confirmation["List-Unsubscribe"] is None
    forecast = {
        "geometry": {"type": "Point", "coordinates": [-77.74, 39.32]},
        "properties": {"score": 41, "tier": "quiet", "sunset_utc": "2026-08-19T23:48:10+00:00", "mid_cloud": 10, "high_cloud": 5, "western_clearance": 30, "aerosol_optical_depth": 0.05},
    }
    downgrade = build_downgrade_message(settings, subscriber, forecast, 84, now=datetime(2026, 8, 19, 22, 18, tzinfo=timezone.utc))
    plain = plain_text_content(downgrade)
    assert "dropped to 41%" in downgrade["Subject"]
    assert "Earlier today we said 84%" in plain
    assert "in 1.5 hours" in plain
