from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .config import Settings
from .tokens import make_token

log = logging.getLogger(__name__)


def _zone(settings: Settings) -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def _location(forecast: dict, subscriber: dict) -> tuple[str, float, float]:
    coordinates = forecast.get("geometry", {}).get("coordinates", [0, 0])
    longitude, latitude = float(coordinates[0]), float(coordinates[1])
    return subscriber.get("label") or "your saved location", latitude, longitude


def _sunset_label(settings: Settings, value: str) -> str:
    sunset = datetime.fromisoformat(value).astimezone(_zone(settings))
    return sunset.strftime("%A, %B %-d at %-I:%M %p %Z")


def countdown_label(sunset_utc: str, now: datetime | None = None) -> str:
    """Human phrasing for how far away the sunset is, for subjects and headlines."""
    now = now or datetime.now(timezone.utc)
    delta = datetime.fromisoformat(sunset_utc) - now
    minutes = int(round(delta.total_seconds() / 60))
    if minutes < -30:
        return "earlier this evening"
    if minutes <= 5:
        return "right now"
    if minutes < 90:
        return f"in {minutes} minutes"
    hours = minutes / 60
    if hours < 20:
        rounded = round(hours * 2) / 2
        text = f"{rounded:g}"
        return f"in {text} hour{'s' if rounded != 1 else ''}"
    if hours < 36:
        return "tomorrow evening"
    days = round(hours / 24)
    return f"in {days} days"


def _interactive_url(settings: Settings, location: str, latitude: float, longitude: float, sunset_utc: str) -> str:
    forecast_date = datetime.fromisoformat(sunset_utc).astimezone(_zone(settings)).date().isoformat()
    query = urlencode({"lat": f"{latitude:.5f}", "lon": f"{longitude:.5f}", "date": forecast_date, "label": location})
    return f"{settings.public_url}/?{query}"


def subscription_links(settings: Settings, subscriber: dict) -> dict[str, str]:
    """Signed URLs for one subscription. Empty strings when signing is impossible."""
    subscription_id = subscriber.get("id")
    if subscription_id is None or not settings.secret_key:
        return {"confirm": "", "manage": "", "unsubscribe": ""}
    email = subscriber["email"]
    base = settings.public_url
    return {
        "confirm": f"{base}/confirm?token={make_token(settings.secret_key, 'confirm', int(subscription_id), email)}",
        "manage": f"{base}/manage?token={make_token(settings.secret_key, 'manage', int(subscription_id), email)}",
        "unsubscribe": f"{base}/unsubscribe?token={make_token(settings.secret_key, 'unsubscribe', int(subscription_id), email)}",
    }


def _apply_list_headers(message: EmailMessage, links: dict[str, str]) -> None:
    if links.get("unsubscribe"):
        # RFC 8058 one-click: the client POSTs to this URL with List-Unsubscribe=One-Click.
        message["List-Unsubscribe"] = f"<{links['unsubscribe']}>"
        message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"


def _footer_text(settings: Settings, links: dict[str, str]) -> str:
    lines = ["Cloudset is experimental. Look outside before making a trip."]
    if links.get("manage"):
        lines.append(f"Change this watch (location, timing, threshold): {links['manage']}")
        lines.append(f"Unsubscribe: {links['unsubscribe']}")
    if settings.donate_url:
        lines.append(f"Support the project: {settings.donate_url}")
    return "\n".join(lines)


def _footer_html(settings: Settings, links: dict[str, str]) -> str:
    parts = []
    if links.get("manage"):
        parts.append(
            f'<a href="{escape(links["manage"], quote=True)}" style="color:#8a837c">Change this watch</a>'
            f' &nbsp;·&nbsp; <a href="{escape(links["unsubscribe"], quote=True)}" style="color:#8a837c">Unsubscribe</a>'
        )
    if settings.donate_url:
        parts.append(f'<a href="{escape(settings.donate_url, quote=True)}" style="color:#8a837c">Support Cloudset</a>')
    line = " &nbsp;·&nbsp; ".join(parts)
    return (
        '<p style="margin:20px 0 0;color:#8a837c;font-size:11px;line-height:1.6">'
        "The colored overlay shows Cloudset's fiery-sky potential derived from the cloud forecast. "
        "Map data © OpenStreetMap contributors; label cartography © CARTO. "
        "Cloudset is experimental. Look outside before making a trip.</p>"
        + (f'<p style="margin:10px 0 0;color:#8a837c;font-size:11px">{line}</p>' if line else "")
    )


RATING_LABELS = {1: "Nothing", 2: "A little colour", 3: "Decent", 4: "Really good", 5: "On fire"}


def _rating_base(settings: Settings, subscriber: dict, sunset_utc: str) -> str | None:
    if subscriber.get("id") is None or not settings.secret_key:
        return None
    forecast_date = datetime.fromisoformat(sunset_utc).astimezone(_zone(settings)).date().isoformat()
    token = make_token(settings.secret_key, "outcome", int(subscriber["id"]), subscriber["email"])
    return f"{settings.public_url}/rate?token={token}&date={forecast_date}"


def _rating_text(base: str | None) -> str:
    if not base:
        return ""
    lines = [f"  {n} · {RATING_LABELS[n]}: {base}&rating={n}" for n in range(1, 6)]
    return "After sunset, tap one to tell us how it actually was (it helps calibrate the forecast):\n" + "\n".join(lines) + "\n\n"


def _rating_html(base: str | None) -> str:
    if not base:
        return ""
    buttons = "".join(
        f'<a href="{escape(base + "&rating=" + str(n), quote=True)}" style="display:inline-block;margin:3px;padding:9px 11px;background:{"#d95f35" if n >= 4 else "#eee9e0"};'
        f'color:{"#fff" if n >= 4 else "#3d3833"};text-decoration:none;font-size:12px;font-weight:bold;border-radius:7px">{n} · {RATING_LABELS[n]}</a>'
        for n in range(1, 6)
    )
    return (
        '<div style="margin-top:22px;padding:16px;background:#f7f3ec;border-radius:8px;text-align:center">'
        '<div style="font-size:11px;font-weight:bold;letter-spacing:1.2px;color:#8a837c">AFTER SUNSET · HOW WAS IT?</div>'
        '<p style="margin:6px 0 10px;color:#514c47;font-size:13px">One tap. Your answer helps calibrate the forecast for everyone.</p>'
        f"{buttons}</div>"
    )


def _shell(inner: str) -> str:
    return (
        '<!doctype html><html><body style="margin:0;background:#f2efe9;color:#292622;font-family:Arial,sans-serif">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f2efe9;padding:24px 10px"><tr><td align="center">'
        '<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="width:100%;max-width:600px;background:#fbfaf7;border-radius:12px;overflow:hidden">'
        f"{inner}</table></td></tr></table></body></html>"
    )


def _button(url: str, label: str) -> str:
    return (
        f'<a href="{escape(url, quote=True)}" style="display:inline-block;padding:13px 21px;background:#d95f35;color:#ffffff;'
        f'text-decoration:none;font-size:14px;font-weight:bold;border-radius:7px">{escape(label)}</a>'
    )


def build_forecast_message(
    settings: Settings,
    subscriber: dict,
    forecast: dict,
    event_label: str,
    detail_map_png: bytes | None = None,
    regional_map_png: bytes | None = None,
    now: datetime | None = None,
) -> EmailMessage:
    props = forecast["properties"]
    location, latitude, longitude = _location(forecast, subscriber)
    sunset = _sunset_label(settings, props["sunset_utc"])
    countdown = countdown_label(props["sunset_utc"], now)
    score = round(props["score"])
    interactive_url = _interactive_url(settings, location, latitude, longitude, props["sunset_utc"])
    links = subscription_links(settings, subscriber)
    rating_base = _rating_base(settings, subscriber, props["sunset_utc"])
    message = EmailMessage()
    message["Subject"] = f"Cloudset: {score}% sunset potential {countdown} near {location}"
    message["From"] = settings.smtp_from
    message["To"] = subscriber["email"]
    _apply_list_headers(message, links)
    message.set_content(
        f"{score}% sunset potential {countdown}\n"
        f"{event_label}\n\n"
        f"Location: {location} ({latitude:.4f}, {longitude:.4f})\n"
        f"Sunset: {sunset}\n"
        f"The fiery-sky score is {score}/100 ({props['tier']}).\n\n"
        f"Mid/high cloud: {props['mid_cloud']}% / {props['high_cloud']}%\n"
        f"Clear western light path: {props['western_clearance']}%\n"
        f"Smoke aerosol optical depth: {props['aerosol_optical_depth']:.2f}\n\n"
        "The maps in the HTML version show close-up and regional views of the Cloudset forecast overlay and your marked location.\n\n"
        f"Open your interactive outlook: {interactive_url}\n\n"
        f"{_rating_text(rating_base)}"
        f"{_footer_text(settings, links)}"
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
    inner = f"""
<tr><td style="padding:28px 30px 20px">
<div style="font-size:11px;font-weight:bold;letter-spacing:1.4px;color:#d65f35">SUNSET WATCH</div>
<h1 style="margin:8px 0 4px;font-size:27px;line-height:1.2">{score}% sunset potential</h1>
<div style="margin:0 0 6px;font-size:18px;color:#3d3833">Sunset is <strong>{escape(countdown)}</strong></div>
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
{_button(interactive_url, "Open interactive outlook →")}
<div style="margin-top:9px;color:#8a837c;font-size:10px">Opens Cloudset centered on {escape(location)}</div>
</td></tr>
<tr><td style="padding:22px 30px 30px">
<p style="margin:0 0 16px;font-size:16px"><strong>Sunset:</strong> {escape(sunset)}</p>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>
<td style="padding:12px;background:#f2efe9;border-radius:7px"><span style="font-size:11px;color:#817a73">MID / HIGH CLOUD</span><br><strong>{props['mid_cloud']}% / {props['high_cloud']}%</strong></td>
<td width="10"></td>
<td style="padding:12px;background:#f2efe9;border-radius:7px"><span style="font-size:11px;color:#817a73">WESTERN LIGHT PATH</span><br><strong>{props['western_clearance']}% clear</strong></td>
</tr></table>
<p style="margin:12px 0 0;padding:12px;background:#f2efe9;border-radius:7px;font-size:13px"><span style="font-size:11px;color:#817a73">SMOKE AEROSOL OPTICAL DEPTH</span><br><strong>{props['aerosol_optical_depth']:.2f} AOD</strong></p>
{_rating_html(rating_base)}
{_footer_html(settings, links)}
</td></tr>"""
    message.add_alternative(_shell(inner), subtype="html")
    html_part = message.get_payload()[-1]
    if detail_map_png:
        html_part.add_related(detail_map_png, maintype="image", subtype="png", cid="<cloudset-map-detail>", disposition="inline", filename="cloudset-forecast-detail.png")
    if regional_map_png:
        html_part.add_related(regional_map_png, maintype="image", subtype="png", cid="<cloudset-map-regional>", disposition="inline", filename="cloudset-forecast-regional.png")
    return message


def build_downgrade_message(
    settings: Settings,
    subscriber: dict,
    forecast: dict,
    previous_score: float,
    now: datetime | None = None,
) -> EmailMessage:
    """Sent when an earlier alert went out and the outlook has since fallen below the threshold."""
    props = forecast["properties"]
    location, latitude, longitude = _location(forecast, subscriber)
    sunset = _sunset_label(settings, props["sunset_utc"])
    countdown = countdown_label(props["sunset_utc"], now)
    score = round(props["score"])
    interactive_url = _interactive_url(settings, location, latitude, longitude, props["sunset_utc"])
    links = subscription_links(settings, subscriber)
    rating_base = _rating_base(settings, subscriber, props["sunset_utc"])
    message = EmailMessage()
    message["Subject"] = f"Cloudset update: sunset potential near {location} dropped to {score}%"
    message["From"] = settings.smtp_from
    message["To"] = subscriber["email"]
    _apply_list_headers(message, links)
    message.set_content(
        f"Update: the outlook for sunset {countdown} has faded.\n\n"
        f"Location: {location} ({latitude:.4f}, {longitude:.4f})\n"
        f"Sunset: {sunset}\n"
        f"Earlier today we said {round(previous_score)}%. The latest forecast is {score}%, below your alert threshold of {subscriber.get('threshold', 70)}.\n\n"
        f"Mid/high cloud: {props['mid_cloud']}% / {props['high_cloud']}%\n"
        f"Clear western light path: {props['western_clearance']}%\n\n"
        f"Open your interactive outlook: {interactive_url}\n\n"
        f"{_rating_text(rating_base)}"
        f"{_footer_text(settings, links)}"
    )
    inner = f"""
<tr><td style="padding:28px 30px 30px">
<div style="font-size:11px;font-weight:bold;letter-spacing:1.4px;color:#6f7a8a">SUNSET WATCH · UPDATE</div>
<h1 style="margin:8px 0 4px;font-size:25px;line-height:1.2">The outlook has faded</h1>
<div style="margin:0 0 18px;font-size:16px;color:#3d3833">Sunset is <strong>{escape(countdown)}</strong> · now <strong>{score}%</strong>, down from {round(previous_score)}%</div>
<div style="padding:14px 16px;background:#e9edf1;border-radius:8px;margin-bottom:18px">
<strong style="display:block;font-size:16px">{escape(location)}</strong>
<span style="color:#756e67;font-size:12px">{latitude:.4f}, {longitude:.4f} · {escape(sunset)}</span>
</div>
<p style="margin:0 0 18px;color:#514c47;font-size:14px;line-height:1.5">The latest model run shows less favorable cloud or light-path conditions than this morning, and the score is now below your threshold of {subscriber.get('threshold', 70)}. We'll send another alert if it recovers before sunset.</p>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>
<td style="padding:12px;background:#f2efe9;border-radius:7px"><span style="font-size:11px;color:#817a73">MID / HIGH CLOUD</span><br><strong>{props['mid_cloud']}% / {props['high_cloud']}%</strong></td>
<td width="10"></td>
<td style="padding:12px;background:#f2efe9;border-radius:7px"><span style="font-size:11px;color:#817a73">WESTERN LIGHT PATH</span><br><strong>{props['western_clearance']}% clear</strong></td>
</tr></table>
<div style="text-align:center;margin-top:22px">{_button(interactive_url, "Open interactive outlook →")}</div>
{_rating_html(rating_base)}
{_footer_html(settings, links)}
</td></tr>"""
    message.add_alternative(_shell(inner), subtype="html")
    return message


def build_confirmation_message(settings: Settings, subscriber: dict) -> EmailMessage:
    links = subscription_links(settings, subscriber)
    location = subscriber.get("label") or "your chosen location"
    message = EmailMessage()
    message["Subject"] = f"Confirm your Cloudset sunset watch for {location}"
    message["From"] = settings.smtp_from
    message["To"] = subscriber["email"]
    message.set_content(
        f"Someone (hopefully you) asked Cloudset to watch for fiery sunsets near {location} "
        f"({float(subscriber['latitude']):.4f}, {float(subscriber['longitude']):.4f}).\n\n"
        f"Confirm to start receiving alerts: {links['confirm']}\n\n"
        "If you didn't request this, ignore this email and nothing will be sent.\n\n"
        f"{_footer_text(settings, {})}"
    )
    inner = f"""
<tr><td style="padding:28px 30px 30px">
<div style="font-size:11px;font-weight:bold;letter-spacing:1.4px;color:#d65f35">SUNSET WATCH</div>
<h1 style="margin:8px 0 12px;font-size:25px;line-height:1.2">One tap to start watching the sky</h1>
<p style="margin:0 0 18px;color:#514c47;font-size:14px;line-height:1.5">Someone, hopefully you, asked Cloudset to email when the sunset near <strong>{escape(location)}</strong> looks likely to catch fire. Confirm below and we'll start watching.</p>
<div style="text-align:center;margin:8px 0 22px">{_button(links['confirm'], "Confirm my sunset watch")}</div>
<p style="margin:0;color:#8a837c;font-size:12px;line-height:1.5">If you didn't request this, ignore this email and nothing will be sent. Cloudset stores only your email address and the coordinates you chose.</p>
{_footer_html(settings, {})}
</td></tr>"""
    message.add_alternative(_shell(inner), subtype="html")
    return message


def build_admin_alert(settings: Settings, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = f"[Cloudset] {subject}"
    message["From"] = settings.smtp_from
    message["To"] = settings.admin_email
    message.set_content(body)
    return message


def send_admin_alert(settings: Settings, subject: str, body: str) -> str:
    if not settings.admin_email:
        log.warning("Admin alert suppressed (no CLOUDSET_ADMIN_EMAIL): %s", subject)
        return "skipped"
    return deliver(settings, build_admin_alert(settings, subject, body))


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
    message = build_forecast_message(settings, subscriber, forecast, event_label, detail_map_png, regional_map_png)
    return deliver(settings, message), message


def send_downgrade(settings: Settings, subscriber: dict, forecast: dict, previous_score: float) -> tuple[str, EmailMessage]:
    message = build_downgrade_message(settings, subscriber, forecast, previous_score)
    return deliver(settings, message), message


def send_confirmation(settings: Settings, subscriber: dict) -> tuple[str, EmailMessage]:
    message = build_confirmation_message(settings, subscriber)
    return deliver(settings, message), message
