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
from .email_map import render_email_map
from .forecast import REGION_BOUNDS, ForecastEngine, default_region
from .goes import GoesIngestor, GoesObservationProvider, should_refresh_goes
from .hrrr import HrrrIngestor, HrrrWeatherProvider
from .mailer import plain_text_content, send_forecast
from .solar import sunset_utc
from .store import Store
from .tiles import ForecastTilePyramid, cache_key

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)
settings = get_settings()
store = Store(settings.db_path)
goes_observations = GoesObservationProvider(settings.root / "data" / "goes" / "fields")
live_provider = HrrrWeatherProvider(settings.root / "data" / "hrrr" / "fields", goes_observations)
engine = ForecastEngine(live_provider if settings.forecast_mode in {"auto", "live"} else None)
tile_pyramid = ForecastTilePyramid(settings.root / "data" / "tile_cache", engine)
hrrr_ingestor = HrrrIngestor(settings.root / "data" / "hrrr")
goes_ingestor = GoesIngestor(settings.root / "data" / "goes")
FORECAST_TIMEZONE = ZoneInfo("America/New_York")


def _active_region() -> list[str]:
    return store.get_json("active_region", default_region())


_cache: dict[tuple, dict] = {}


def forecast_for(day: date, minute_offset: int = 0) -> dict:
    active = _active_region()
    key = (day.isoformat(), minute_offset, tuple(active), engine.provider_key(day))
    if key not in _cache:
        _cache[key] = engine.calculate(active, day, minute_offset)
        if len(_cache) > 30:
            _cache.pop(next(iter(_cache)))
    return _cache[key]


def forecast_meta(day: date, minute_offset: int = 0) -> dict:
    active = _active_region()
    provider_meta = engine.provider_metadata(day)
    return {
        "day": day.isoformat(),
        "minute_offset": minute_offset,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": provider_meta.get("source", "demo-atmosphere"),
        "mode": provider_meta["mode"],
        "model_run": provider_meta["model_run"],
        "data_source": provider_meta,
        "resolution_degrees": 0,
        "revision": cache_key(active, engine.provider_key(day), day, minute_offset),
        "tile_resolutions_degrees": {"z3-5": 0.5, "z6": 0.25, "z7": 0.125, "z8+": 0.0625},
    }


def email_map_for(
    forecast_day: date,
    latitude: float,
    longitude: float,
    *,
    zoom: int = 10,
    tile_scale: float = 1.25,
    forecast_blur: float = 5,
) -> bytes | None:
    try:
        return render_email_map(
            tile_pyramid,
            _active_region(),
            forecast_day,
            latitude,
            longitude,
            settings.root / "data" / "email_map_cache",
            zoom=zoom,
            tile_scale=tile_scale,
            forecast_blur=forecast_blur,
        )
    except Exception:
        # A basemap outage should never suppress a useful forecast notification.
        log.exception("Email map rendering failed; sending the alert without a map")
        return None


EVENT_LABELS = {
    "day_before": "Early outlook · the evening before",
    "morning": "Morning sunset outlook",
    "final_90": "Final call · about 90 minutes before sunset",
    "custom": "Your custom sunset reminder",
    "manual": "Manual notification check",
}


def _due_events(subscriber: dict, now: datetime, force: bool = False) -> list[tuple[str, date, str]]:
    local_now = now.astimezone(FORECAST_TIMEZONE)
    today = local_now.date()
    if force:
        return [("manual", today, EVENT_LABELS["manual"])]
    selected = set(subscriber.get("notification_times") or ["morning", "final_90"])
    events: list[tuple[str, date, str]] = []
    if "morning" in selected and 8 <= local_now.hour < 12:
        events.append(("morning", today, EVENT_LABELS["morning"]))
    if "day_before" in selected and 18 <= local_now.hour <= 23:
        events.append(("day_before", today + timedelta(days=1), EVENT_LABELS["day_before"]))
    latitude = float(subscriber["latitude"])
    longitude = float(subscriber["longitude"])
    if "final_90" in selected:
        sunset = sunset_utc(today, latitude, longitude)
        if sunset and sunset - timedelta(minutes=90) <= now < sunset:
            events.append(("final_90", today, EVENT_LABELS["final_90"]))
    if "custom" in selected and subscriber.get("custom_minutes"):
        minutes = int(subscriber["custom_minutes"])
        for offset in range(2):
            forecast_day = today + timedelta(days=offset)
            sunset = sunset_utc(forecast_day, latitude, longitude)
            if sunset and sunset - timedelta(minutes=minutes) <= now < sunset:
                label = f"Custom reminder · {minutes / 60:g} hours before sunset"
                events.append((f"custom_{minutes}", forecast_day, label))
                break
    return events


def dispatch_notifications(force: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    sent = skipped = 0
    errors: list[str] = []
    for subscriber in store.subscriptions():
        events = _due_events(subscriber, now, force)
        if not events:
            skipped += 1
            continue
        for event_key, forecast_day, event_label in events:
            if store.notification_exists(int(subscriber["id"]), forecast_day.isoformat(), event_key):
                skipped += 1
                continue
            if engine.provider_metadata(forecast_day)["mode"] != "live" and not force:
                skipped += 1
                continue
            feature = engine.point(
                _active_region(),
                float(subscriber["latitude"]),
                float(subscriber["longitude"]),
                forecast_day,
            )
            if not feature or feature["properties"]["score"] < subscriber["threshold"]:
                skipped += 1
                continue
            try:
                detail_map_png = email_map_for(
                    forecast_day,
                    float(subscriber["latitude"]),
                    float(subscriber["longitude"]),
                )
                regional_map_png = email_map_for(
                    forecast_day,
                    float(subscriber["latitude"]),
                    float(subscriber["longitude"]),
                    zoom=8,
                    tile_scale=1,
                    forecast_blur=3,
                )
                result, _ = send_forecast(
                    settings,
                    subscriber,
                    feature,
                    event_label,
                    detail_map_png,
                    regional_map_png,
                )
                store.record_notification(
                    int(subscriber["id"]), forecast_day.isoformat(), event_key, feature["properties"]["score"], result
                )
                sent += 1
            except Exception as exc:  # SMTP failures must not stop other recipients.
                log.exception("Notification failed")
                errors.append(f"subscription {subscriber['id']} ({event_key}): {exc}")
    return {"sent": sent, "skipped": skipped, "errors": errors}


async def scheduler() -> None:
    while True:
        try:
            await asyncio.to_thread(dispatch_notifications)
        except Exception:
            log.exception("Scheduled notification pass failed")
        await asyncio.sleep(15 * 60)


async def hrrr_scheduler() -> None:
    # Let startup and short-lived test processes settle; existing snapshots are
    # immediately available, and an admin can always trigger a refresh now.
    await asyncio.sleep(30)
    while True:
        try:
            today = datetime.now(FORECAST_TIMEZONE).date()
            results = await asyncio.to_thread(hrrr_ingestor.refresh, _active_region(), today)
            live_provider.invalidate()
            _cache.clear()
            log.info("HRRR refresh complete: %s", results)
        except Exception:
            log.exception("HRRR refresh failed; demo fallback remains active")
        await asyncio.sleep(60 * 60)


async def goes_scheduler() -> None:
    await asyncio.sleep(20)
    while True:
        try:
            now = datetime.now(timezone.utc)
            if should_refresh_goes(now):
                result = await asyncio.to_thread(goes_ingestor.refresh, _active_region(), now)
                goes_observations.invalidate()
                live_provider.invalidate()
                _cache.clear()
                log.info("GOES-East correction refresh complete: %s", result)
        except Exception:
            log.exception("GOES-East correction refresh failed; HRRR remains active")
        await asyncio.sleep(10 * 60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = [asyncio.create_task(scheduler())]
    if settings.forecast_mode in {"auto", "live"}:
        tasks.extend((asyncio.create_task(hrrr_scheduler()), asyncio.create_task(goes_scheduler())))
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
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
    notification_times: list[str] = Field(default_factory=lambda: ["morning", "final_90"], min_length=1, max_length=4)
    custom_minutes: int | None = Field(default=None, ge=30, le=2160)

    @field_validator("email")
    @classmethod
    def validate_email(cls, email: str) -> str:
        email = email.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("Enter a valid email address")
        return email

    @field_validator("notification_times")
    @classmethod
    def validate_notification_times(cls, values: list[str]) -> list[str]:
        allowed = {"day_before", "morning", "final_90", "custom"}
        unique = list(dict.fromkeys(values))
        if not unique or any(value not in allowed for value in unique):
            raise ValueError("Choose at least one valid notification time")
        return unique


class EmailTest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    latitude: float = Field(default=40.7128, ge=-90, le=90)
    longitude: float = Field(default=-74.006, ge=-180, le=180)

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
    today = datetime.now(FORECAST_TIMEZONE).date()
    return {"status": "ok", "mode": engine.provider_metadata(today)["mode"], "configured_mode": settings.forecast_mode, "version": app.version}


@app.get("/api/config")
def config() -> dict:
    return {
        "mode": engine.provider_metadata(datetime.now(FORECAST_TIMEZONE).date())["mode"],
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
    if "custom" in payload.notification_times and payload.custom_minutes is None:
        raise HTTPException(status_code=422, detail="Choose how many hours before sunset for the custom reminder")
    subscriber_id = store.subscribe(
        payload.email,
        payload.latitude,
        payload.longitude,
        payload.label,
        payload.threshold,
        payload.notification_times,
        payload.custom_minutes,
    )
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


@app.post("/api/admin/ingest", dependencies=[Depends(require_admin)])
def ingest_hrrr() -> dict:
    if settings.forecast_mode not in {"auto", "live"}:
        raise HTTPException(status_code=409, detail="Set CLOUDSET_FORECAST_MODE=auto or live to enable HRRR")
    today = datetime.now(FORECAST_TIMEZONE).date()
    results = hrrr_ingestor.refresh(_active_region(), today)
    live_provider.invalidate()
    _cache.clear()
    return {"ok": True, "runs": results}


@app.post("/api/admin/goes/ingest", dependencies=[Depends(require_admin)])
def ingest_goes() -> dict:
    if settings.forecast_mode not in {"auto", "live"}:
        raise HTTPException(status_code=409, detail="Set CLOUDSET_FORECAST_MODE=auto or live to enable GOES")
    result = goes_ingestor.refresh(_active_region())
    goes_observations.invalidate()
    live_provider.invalidate()
    _cache.clear()
    return {"ok": True, "observation": result}


@app.post("/api/admin/run", dependencies=[Depends(require_admin)])
def run_forecast() -> dict:
    _cache.clear()
    started = datetime.now(timezone.utc)
    then = time.perf_counter()
    result = forecast_for(datetime.now(FORECAST_TIMEZONE).date())
    duration = (time.perf_counter() - then) * 1000
    finished = datetime.now(timezone.utc)
    actual_mode = engine.provider_metadata(datetime.now(FORECAST_TIMEZONE).date())["mode"]
    run_id = store.add_run(started.isoformat(), finished.isoformat(), actual_mode, len(result["features"]), duration, "Forecast field generated")
    return {"ok": True, "run_id": run_id, "cells": len(result["features"]), "duration_ms": round(duration, 1)}


@app.post("/api/admin/notifications/test", dependencies=[Depends(require_admin)])
def test_notifications() -> dict:
    return dispatch_notifications(force=False)


@app.post("/api/admin/email/test", dependencies=[Depends(require_admin)])
def send_test_email(payload: EmailTest) -> dict:
    today = datetime.now(FORECAST_TIMEZONE).date()
    feature = engine.point(_active_region(), payload.latitude, payload.longitude, today)
    if not feature:
        raise HTTPException(status_code=404, detail="Test location is outside the active forecast area")
    subscriber = {
        "email": payload.email,
        "label": "New York City" if abs(payload.latitude - 40.7128) < 0.1 and abs(payload.longitude + 74.006) < 0.1 else "your test location",
        "latitude": payload.latitude,
        "longitude": payload.longitude,
    }
    detail_map_png = email_map_for(today, payload.latitude, payload.longitude)
    regional_map_png = email_map_for(
        today,
        payload.latitude,
        payload.longitude,
        zoom=8,
        tile_scale=1,
        forecast_blur=3,
    )
    delivery, message = send_forecast(
        settings,
        subscriber,
        feature,
        "This is a Cloudset test email · no alert was triggered",
        detail_map_png,
        regional_map_png,
    )
    return {
        "ok": True,
        "delivery": delivery,
        "smtp_configured": bool(settings.smtp_host),
        "preview": {
            "to": payload.email,
            "subject": str(message["Subject"]),
            "body": plain_text_content(message),
            "map_embedded": detail_map_png is not None or regional_map_png is not None,
            "maps_embedded": {
                "detail": detail_map_png is not None,
                "regional": regional_map_png is not None,
            },
        },
    }


@app.get("/api/admin/status", dependencies=[Depends(require_admin)])
def admin_status() -> dict:
    today = datetime.now(FORECAST_TIMEZONE).date()
    provider_meta = engine.provider_metadata(today)
    return {
        "mode": provider_meta["mode"],
        "configured_mode": settings.forecast_mode,
        "model_run": provider_meta["model_run"],
        "data_source": provider_meta,
        "goes": goes_observations.status(today),
        "provider": provider_meta.get("source", "demo-atmosphere"),
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
