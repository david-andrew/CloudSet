from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .config import Settings

log = logging.getLogger(__name__)


def send_forecast(settings: Settings, subscriber: dict, forecast: dict) -> str:
    props = forecast["properties"]
    message = EmailMessage()
    message["Subject"] = f"Cloudset: {round(props['score'])}% sunset chance near {subscriber['label'] or 'your place'}"
    message["From"] = settings.smtp_from
    message["To"] = subscriber["email"]
    message.set_content(
        f"Tonight's fiery-sky score is {round(props['score'])}/100 ({props['tier']}).\n\n"
        f"Sunset: {props['sunset_utc']}\n"
        f"Mid/high cloud: {props['mid_cloud']}% / {props['high_cloud']}%\n"
        f"Clear western light path: {props['western_clearance']}%\n\n"
        "Cloudset is experimental. Look outside before making a trip."
    )
    if not settings.smtp_host:
        log.info("SMTP not configured; would send to %s: %s", subscriber["email"], message["Subject"])
        return "logged"
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as client:
        if settings.smtp_tls:
            client.starttls()
        if settings.smtp_user:
            client.login(settings.smtp_user, settings.smtp_password)
        client.send_message(message)
    return "sent"
