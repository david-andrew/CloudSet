from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Settings:
    root: Path
    db_path: Path
    host: str
    port: int
    public_url: str
    admin_token: str
    admin_email: str
    secret_key: str
    environment: str
    forecast_mode: str
    timezone: str
    donate_url: str
    max_subscriptions_per_email: int
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    smtp_tls: bool

    @property
    def production(self) -> bool:
        return self.environment == "production"


def _bool(value: str) -> bool:
    return value.lower() in {"1", "true", "yes"}


def get_settings() -> Settings:
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env")
    db_value = os.getenv("CLOUDSET_DB", "data/cloudset.db")
    db_path = Path(db_value)
    if not db_path.is_absolute():
        db_path = root / db_path
    port = int(os.getenv("CLOUDSET_PORT", "8080"))
    return Settings(
        root=root,
        db_path=db_path,
        host=os.getenv("CLOUDSET_HOST", "127.0.0.1"),
        port=port,
        public_url=os.getenv("CLOUDSET_PUBLIC_URL", f"http://127.0.0.1:{port}").rstrip("/"),
        admin_token=os.getenv("CLOUDSET_ADMIN_TOKEN", ""),
        admin_email=os.getenv("CLOUDSET_ADMIN_EMAIL", ""),
        secret_key=os.getenv("CLOUDSET_SECRET_KEY", ""),
        environment=os.getenv("CLOUDSET_ENV", "development").lower(),
        forecast_mode=os.getenv("CLOUDSET_FORECAST_MODE", "demo").lower(),
        timezone=os.getenv("CLOUDSET_TIMEZONE", "America/New_York"),
        donate_url=os.getenv("CLOUDSET_DONATE_URL", ""),
        max_subscriptions_per_email=int(os.getenv("CLOUDSET_MAX_SUBSCRIPTIONS_PER_EMAIL", "5")),
        smtp_host=os.getenv("CLOUDSET_SMTP_HOST", ""),
        smtp_port=int(os.getenv("CLOUDSET_SMTP_PORT", "587")),
        smtp_user=os.getenv("CLOUDSET_SMTP_USER", ""),
        smtp_password=os.getenv("CLOUDSET_SMTP_PASSWORD", ""),
        smtp_from=os.getenv("CLOUDSET_SMTP_FROM", "Cloudset <sunsets@example.com>"),
        smtp_tls=_bool(os.getenv("CLOUDSET_SMTP_TLS", "true")),
    )


def validate_settings(settings: Settings) -> list[str]:
    """Return configuration problems. In production any problem is fatal."""
    problems: list[str] = []
    if not settings.secret_key:
        problems.append("CLOUDSET_SECRET_KEY is empty; confirmation and unsubscribe links cannot be signed")
    elif len(settings.secret_key) < 32:
        problems.append("CLOUDSET_SECRET_KEY should be at least 32 characters")
    if not settings.admin_token:
        problems.append("CLOUDSET_ADMIN_TOKEN is empty; the control room would be open to anyone who can reach it")
    if settings.production:
        if not settings.public_url.startswith("https://"):
            problems.append(f"CLOUDSET_PUBLIC_URL should be https in production (got {settings.public_url})")
        if not settings.smtp_host:
            problems.append("CLOUDSET_SMTP_HOST is empty; confirmations and alerts would only be logged")
        if "example.com" in settings.smtp_from:
            problems.append("CLOUDSET_SMTP_FROM still uses the example sender address")
        if not settings.admin_email:
            problems.append("CLOUDSET_ADMIN_EMAIL is empty; nobody will hear about failed ingests or sends")
        if settings.forecast_mode not in {"auto", "live"}:
            problems.append("CLOUDSET_FORECAST_MODE should be auto or live in production")
    return problems
