from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    root: Path
    db_path: Path
    host: str
    port: int
    public_url: str
    admin_token: str
    forecast_mode: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    smtp_tls: bool


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
        forecast_mode=os.getenv("CLOUDSET_FORECAST_MODE", "demo").lower(),
        smtp_host=os.getenv("CLOUDSET_SMTP_HOST", ""),
        smtp_port=int(os.getenv("CLOUDSET_SMTP_PORT", "587")),
        smtp_user=os.getenv("CLOUDSET_SMTP_USER", ""),
        smtp_password=os.getenv("CLOUDSET_SMTP_PASSWORD", ""),
        smtp_from=os.getenv("CLOUDSET_SMTP_FROM", "Cloudset <sunsets@example.com>"),
        smtp_tls=os.getenv("CLOUDSET_SMTP_TLS", "true").lower() in {"1", "true", "yes"},
    )
