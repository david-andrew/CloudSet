from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage
from html import escape
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .config import Settings

log = logging.getLogger(__name__)


EMAIL_TIMEZONE = ZoneInfo("America/New_York")


def _location(forecast: dict, subscriber: dict) -> tuple[str, float, float]:
    coordinates = forecast.get("geometry", {}).get("coordinates", [0, 0])
    longitude, latitude = float(coordinates[0]), float(coordinates[1])
    return subscriber.get("label") or "your saved location", latitude, longitude


def _sunset_label(value: str) -> str:
    sunset = datetime.fromisoformat(value).astimezone(EMAIL_TIMEZONE)
    return sunset.strftime("%A, %B %-d at %-I:%M %p %Z")


def _interactive_url(settings: Settings, location: str, latitude: float, longitude: float, sunset_utc: str) -> str:
    forecast_date = datetime.fromisoformat(sunset_utc).astimezone(EMAIL_TIMEZONE).date().isoformat()
    query = urlencode(
        {
            "lat": f"{latitude:.5f}",
            "lon": f"{longitude:.5f}",
            "date": forecast_date,
            "label": location,
        }
    )
    return f"{settings.public_url}/?{query}"


def build_forecast_message(
    settings: Settings,
    subscriber: dict,
    forecast: dict,
    event_label: str,
    detail_map_png: bytes | None = None,
    regional_map_png: bytes | None = None,
) -> EmailMessage:
    props = forecast["properties"]
    location, latitude, longitude = _location(forecast, subscriber)
    sunset = _sunset_label(props["sunset_utc"])
    interactive_url = _interactive_url(settings, location, latitude, longitude, props["sunset_utc"])
    message = EmailMessage()
    message["Subject"] = f"Cloudset: {round(props['score'])}% sunset potential near {location}"
    message["From"] = settings.smtp_from
    message["To"] = subscriber["email"]
    message.set_content(
        f"{event_label}\n\n"
        f"Location: {location} ({latitude:.4f}, {longitude:.4f})\n"
        f"The fiery-sky score is {round(props['score'])}/100 ({props['tier']}).\n\n"
        f"Sunset: {sunset}\n"
        f"Mid/high cloud: {props['mid_cloud']}% / {props['high_cloud']}%\n"
        f"Clear western light path: {props['western_clearance']}%\n\n"
        "The maps in the HTML version show close-up and regional views of the Cloudset forecast overlay and your marked location.\n\n"
        f"Open your interactive outlook: {interactive_url}\n\n"
        "Cloudset is experimental. Look outside before making a trip."
    )
    detail_map_html = (
        '<img src="cid:cloudset-map-detail" width="600" alt="Close-up map of the saved location with the Cloudset fiery-sky forecast overlay" '
        'style="display:block;width:100%;max-width:600px;height:auto;border:0;border-radius:10px">'
        if detail_map_png
        else '<div style="padding:18px;background:#f2efe9;color:#756e67;border-radius:10px">Close-up map is temporarily unavailable.</div>'
    )
    regional_map_html = (
        '<img src="cid:cloudset-map-regional" width="600" alt="Regional map with the Cloudset fiery-sky forecast overlay" '
        'style="display:block;width:100%;max-width:600px;height:auto;border:0;border-radius:10px">'
        if regional_map_png
        else '<div style="padding:18px;background:#f2efe9;color:#756e67;border-radius:10px">Regional map is temporarily unavailable.</div>'
    )
    message.add_alternative(
        f"""<!doctype html>
<html><body style="margin:0;background:#f2efe9;color:#292622;font-family:Arial,sans-serif">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f2efe9;padding:24px 10px"><tr><td align="center">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="width:100%;max-width:600px;background:#fbfaf7;border-radius:12px;overflow:hidden">
<tr><td style="padding:28px 30px 20px">
<div style="font-size:11px;font-weight:bold;letter-spacing:1.4px;color:#d65f35">SUNSET WATCH</div>
<h1 style="margin:8px 0 6px;font-size:27px;line-height:1.2">{round(props['score'])}% sunset potential</h1>
<p style="margin:0 0 20px;color:#746e67;font-size:14px">{escape(event_label)}</p>
<div style="padding:14px 16px;background:#f3e7dd;border-radius:8px">
<strong style="display:block;font-size:16px">{escape(location)}</strong>
<span style="color:#756e67;font-size:12px">{latitude:.4f}, {longitude:.4f}</span>
</div>
</td></tr>
<tr><td style="padding:0 0 22px">
<div style="padding:0 30px 9px"><strong style="font-size:15px">Around your location</strong><br><span style="font-size:11px;color:#817a73">Street and neighborhood detail</span></div>
{detail_map_html}
</td></tr>
<tr><td style="padding:0 0 6px">
<div style="padding:0 30px 9px"><strong style="font-size:15px">Regional outlook</strong><br><span style="font-size:11px;color:#817a73">The wider weather pattern around you</span></div>
{regional_map_html}
</td></tr>
<tr><td align="center" style="padding:22px 30px 4px">
<a href="{escape(interactive_url, quote=True)}" style="display:inline-block;padding:13px 21px;background:#d95f35;color:#ffffff;text-decoration:none;font-size:14px;font-weight:bold;border-radius:7px">Open interactive outlook →</a>
<div style="margin-top:9px;color:#8a837c;font-size:10px">Opens Cloudset centered on {escape(location)}</div>
</td></tr>
<tr><td style="padding:22px 30px 30px">
<p style="margin:0 0 16px;font-size:16px"><strong>Sunset:</strong> {escape(sunset)}</p>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>
<td style="padding:12px;background:#f2efe9;border-radius:7px"><span style="font-size:11px;color:#817a73">MID / HIGH CLOUD</span><br><strong>{props['mid_cloud']}% / {props['high_cloud']}%</strong></td>
<td width="10"></td>
<td style="padding:12px;background:#f2efe9;border-radius:7px"><span style="font-size:11px;color:#817a73">WESTERN LIGHT PATH</span><br><strong>{props['western_clearance']}% clear</strong></td>
</tr></table>
<p style="margin:20px 0 0;color:#8a837c;font-size:11px;line-height:1.5">The colored overlay shows Cloudset's fiery-sky potential derived from the cloud forecast. Map data © OpenStreetMap contributors; label cartography © CARTO. Cloudset is experimental—look outside before making a trip.</p>
</td></tr></table>
</td></tr></table>
</body></html>""",
        subtype="html",
    )
    html_part = message.get_payload()[-1]
    if detail_map_png:
        html_part.add_related(
            detail_map_png,
            maintype="image",
            subtype="png",
            cid="<cloudset-map-detail>",
            filename="cloudset-forecast-detail.png",
        )
    if regional_map_png:
        html_part.add_related(
            regional_map_png,
            maintype="image",
            subtype="png",
            cid="<cloudset-map-regional>",
            filename="cloudset-forecast-regional.png",
        )
    return message


def plain_text_content(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain",))
    return part.get_content() if part else ""


def deliver(settings: Settings, message: EmailMessage) -> str:
    if not settings.smtp_host:
        log.info("SMTP not configured; would send to %s: %s", message["To"], message["Subject"])
        return "logged"
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as client:
        if settings.smtp_tls:
            client.starttls()
        if settings.smtp_user:
            client.login(settings.smtp_user, settings.smtp_password)
        client.send_message(message)
    return "sent"


def send_forecast(
    settings: Settings,
    subscriber: dict,
    forecast: dict,
    event_label: str,
    detail_map_png: bytes | None = None,
    regional_map_png: bytes | None = None,
) -> tuple[str, EmailMessage]:
    message = build_forecast_message(
        settings,
        subscriber,
        forecast,
        event_label,
        detail_map_png,
        regional_map_png,
    )
    return deliver(settings, message), message
