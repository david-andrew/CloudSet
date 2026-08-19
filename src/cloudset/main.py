from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .config import Settings, get_settings
from .forecast import REGION_BOUNDS, ForecastEngine, default_region
from .mailer import send_forecast
from .store import Store
from .tiles import ForecastTilePyramid, cache_key

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)
settings = get_settings()
store = Store(settings.db_path)
engine = ForecastEngine()
tile_pyramid = ForecastTilePyramid(settings.root / "data" / "tile_cache", engine)
FORECAST_TIMEZONE = ZoneInfo("America/New_York")


def _active_region() -> list[str]:
    return store.get_json("active_region", default_region())


_cache: dict[tuple, dict] = {}


def forecast_for(day: date, minute_offset: int = 0) -> dict:
    active = _active_region()
    key = (day.isoformat(), minute_offset, tuple(active), engine.provider.name)
    if key not in _cache:
        _cache[key] = engine.calculate(active, day, minute_offset)
        if len(_cache) > 30:
            _cache.pop(next(iter(_cache)))
    return _cache[key]


def forecast_meta(day: date, minute_offset: int = 0) -> dict:
    active = _active_region()
    return {
        "day": day.isoformat(),
        "minute_offset": minute_offset,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": engine.provider.name,
        "mode": "demo",
        "model_run": "DEMO · deterministic atmospheric field",
        "resolution_degrees": 0,
        "revision": cache_key(active, engine.provider.name, day, minute_offset),
        "tile_resolutions_degrees": {"z3-5": 0.5, "z6": 0.25, "z7": 0.125, "z8+": 0.0625},
    }


def dispatch_notifications(force: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    sent = skipped = 0
    errors: list[str] = []
    for subscriber in store.subscriptions():
        local_now = now + timedelta(hours=float(subscriber["longitude"]) / 15)
        if not force and not 12 <= local_now.hour < 17:
            skipped += 1
            continue
        day = local_now.date()
        if store.notification_exists(int(subscriber["id"]), day.isoformat()):
            skipped += 1
            continue
        feature = engine.point(
            _active_region(),
            float(subscriber["latitude"]),
            float(subscriber["longitude"]),
            day,
        )
        if not feature or feature["properties"]["score"] < subscriber["threshold"]:
            skipped += 1
            continue
        try:
            result = send_forecast(settings, subscriber, feature)
            store.record_notification(int(subscriber["id"]), day.isoformat(), feature["properties"]["score"], result)
            sent += 1
        except Exception as exc:  # SMTP failures must not stop other recipients.
            log.exception("Notification failed")
            errors.append(f"subscription {subscriber['id']}: {exc}")
    return {"sent": sent, "skipped": skipped, "errors": errors}


async def scheduler() -> None:
    while True:
        try:
            await asyncio.to_thread(dispatch_notifications)
        except Exception:
            log.exception("Scheduled notification pass failed")
        await asyncio.sleep(15 * 60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(scheduler())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Cloudset", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type", "X-Admin-Token"],
)
app.mount("/assets", StaticFiles(directory=settings.root / "static"), name="assets")


def require_admin(x_admin_token: str = Header(default="")) -> None:
    if settings.admin_token and x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Invalid admin token")


class RegionUpdate(BaseModel):
    active_cells: list[str] = Field(max_length=2000)

    @field_validator("active_cells")
    @classmethod
    def validate_cells(cls, cells: list[str]) -> list[str]:
        cleaned: set[str] = set()
        for item in cells:
            if not re.fullmatch(r"-?\d{1,2}\.\d,-?\d{1,3}\.\d", item):
                raise ValueError(f"Invalid cell: {item}")
            lat, lon = map(float, item.split(","))
            if not (REGION_BOUNDS["south"] <= lat < REGION_BOUNDS["north"] and REGION_BOUNDS["west"] <= lon < REGION_BOUNDS["east"]):
                raise ValueError(f"Cell outside supported bounds: {item}")
            cleaned.add(item)
        if not cleaned:
            raise ValueError("At least one forecast cell is required")
        return sorted(cleaned)


class SubscriptionCreate(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    label: str = Field(default="", max_length=100)
    threshold: int = Field(default=70, ge=40, le=95)

    @field_validator("email")
    @classmethod
    def validate_email(cls, email: str) -> str:
        email = email.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("Enter a valid email address")
        return email


@app.get("/", include_in_schema=False)
def public_ui() -> FileResponse:
    return FileResponse(settings.root / "static" / "index.html")


@app.get("/admin", include_in_schema=False)
def admin_ui() -> FileResponse:
    return FileResponse(settings.root / "static" / "admin.html")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "mode": settings.forecast_mode, "version": app.version}


@app.get("/api/config")
def config() -> dict:
    return {
        "mode": settings.forecast_mode,
        "admin_auth": bool(settings.admin_token),
        "smtp_configured": bool(settings.smtp_host),
        "region_bounds": REGION_BOUNDS,
        "satellite": {
            "provider": "NASA GIBS / GOES-East ABI GeoColor",
            "attribution": "NASA EOSDIS GIBS · NOAA",
        },
        "forecast_tiles": {
            "format": "PNG tile pyramid",
            "resolutions_degrees": {"z3-5": 0.5, "z6": 0.25, "z7": 0.125, "z8+": 0.0625},
        },
    }


@app.get("/api/forecast")
def forecast(
    day: int = Query(default=0, ge=-1, le=5),
    offset: int = Query(default=0, ge=-60, le=60),
) -> dict:
    target = datetime.now(FORECAST_TIMEZONE).date() + timedelta(days=day)
    return forecast_for(target, offset)


@app.get("/api/forecast/point")
def point_forecast(
    latitude: float = Query(ge=-90, le=90),
    longitude: float = Query(ge=-180, le=180),
    day: int = Query(default=0, ge=-1, le=5),
    offset: int = Query(default=0, ge=-60, le=60),
) -> dict:
    target = datetime.now(FORECAST_TIMEZONE).date() + timedelta(days=day)
    active = _active_region()
    feature = engine.point(active, latitude, longitude, target, offset)
    if not feature:
        raise HTTPException(status_code=404, detail="Location is outside the active forecast area")
    return {
        "location": {"latitude": latitude, "longitude": longitude},
        "forecast": feature,
        "meta": forecast_meta(target, offset),
    }


@app.get("/api/forecast/meta")
def get_forecast_meta(
    day: int = Query(default=0, ge=-1, le=5),
    offset: int = Query(default=0, ge=-60, le=60),
) -> dict:
    target = datetime.now(FORECAST_TIMEZONE).date() + timedelta(days=day)
    return forecast_meta(target, offset)


@app.get("/api/forecast/tiles/{day_offset}/{minute_offset}/{zoom}/{tile_x}/{tile_y}.png")
def forecast_tile(
    day_offset: int,
    minute_offset: int,
    zoom: int,
    tile_x: int,
    tile_y: int,
) -> Response:
    if not -1 <= day_offset <= 5 or not -60 <= minute_offset <= 60 or not 3 <= zoom <= 12:
        raise HTTPException(status_code=422, detail="Tile coordinate outside the supported forecast range")
    scale = 2**zoom
    if not 0 <= tile_x < scale or not 0 <= tile_y < scale:
        raise HTTPException(status_code=404, detail="Tile does not exist")
    target = datetime.now(FORECAST_TIMEZONE).date() + timedelta(days=day_offset)
    content, cached, resolution = tile_pyramid.render(
        _active_region(), target, minute_offset, zoom, tile_x, tile_y
    )
    return Response(
        content=content,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Forecast-Resolution": str(resolution),
            "X-Tile-Cache": "HIT" if cached else "MISS",
        },
    )


@app.post("/api/subscriptions", status_code=201)
def subscribe(payload: SubscriptionCreate, request: Request) -> dict:
    # SQLite uniqueness makes retries idempotent. A reverse proxy should add a
    # per-IP rate limit before exposing this endpoint broadly.
    subscriber_id = store.subscribe(payload.email, payload.latitude, payload.longitude, payload.label, payload.threshold)
    log.info("Subscription %s created/updated from %s", subscriber_id, request.client.host if request.client else "unknown")
    return {"ok": True, "id": subscriber_id, "message": "You're on the sunset watchlist."}


@app.get("/api/admin/region", dependencies=[Depends(require_admin)])
def get_region() -> dict:
    return {"active_cells": _active_region(), "bounds": REGION_BOUNDS, "resolution": 1.0}


@app.put("/api/admin/region", dependencies=[Depends(require_admin)])
def update_region(payload: RegionUpdate) -> dict:
    store.set_json("active_region", payload.active_cells)
    _cache.clear()
    return {"ok": True, "active_count": len(payload.active_cells), "forecast_cells": len(payload.active_cells) * 4}


@app.post("/api/admin/run", dependencies=[Depends(require_admin)])
def run_forecast() -> dict:
    _cache.clear()
    started = datetime.now(timezone.utc)
    then = time.perf_counter()
    result = forecast_for(datetime.now(FORECAST_TIMEZONE).date())
    duration = (time.perf_counter() - then) * 1000
    finished = datetime.now(timezone.utc)
    run_id = store.add_run(started.isoformat(), finished.isoformat(), settings.forecast_mode, len(result["features"]), duration, "Forecast field generated")
    return {"ok": True, "run_id": run_id, "cells": len(result["features"]), "duration_ms": round(duration, 1)}


@app.post("/api/admin/notifications/test", dependencies=[Depends(require_admin)])
def test_notifications() -> dict:
    return dispatch_notifications(force=True)


@app.get("/api/admin/status", dependencies=[Depends(require_admin)])
def admin_status() -> dict:
    return {
        "mode": settings.forecast_mode,
        "provider": engine.provider.name,
        "active_cells": len(_active_region()),
        "forecast_cells": len(_active_region()) * 4,
        "subscribers": len(store.subscriptions()),
        "smtp_configured": bool(settings.smtp_host),
        "admin_auth": bool(settings.admin_token),
        "last_run": store.last_run(),
    }


def run() -> None:
    import uvicorn

    uvicorn.run("cloudset.main:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    run()
