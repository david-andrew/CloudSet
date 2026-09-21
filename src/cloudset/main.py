from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .config import Settings, get_settings, validate_settings
from .email_map import render_email_map
from .forecast import REGION_BOUNDS, ForecastEngine, default_region
from .goes import GoesIngestor, GoesObservationProvider, should_refresh_goes
from .hrrr import HrrrIngestor, HrrrWeatherProvider
from .mailer import (
    plain_text_content,
    send_admin_alert,
    send_confirmation,
    send_downgrade,
    send_forecast,
    subscription_links,
)
from .solar import sunset_utc
from .store import Store
from .tiles import LAYERS, ForecastTilePyramid, cache_key, quantize_min
from .tokens import read_token

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)
settings = get_settings()
for problem in validate_settings(settings):
    if settings.production:
        raise SystemExit(f"Refusing to start in production: {problem}")
    log.warning("Configuration: %s", problem)

store = Store(settings.db_path)
goes_observations = GoesObservationProvider(settings.root / "data" / "goes" / "fields")
live_provider = HrrrWeatherProvider(settings.root / "data" / "hrrr" / "fields", goes_observations)
engine = ForecastEngine(live_provider if settings.forecast_mode in {"auto", "live"} else None)
tile_pyramid = ForecastTilePyramid(settings.root / "data" / "tile_cache", engine)
hrrr_ingestor = HrrrIngestor(settings.root / "data" / "hrrr")
goes_ingestor = GoesIngestor(settings.root / "data" / "goes")
FORECAST_TIMEZONE = ZoneInfo(settings.timezone)
EMAIL_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


def _active_region() -> list[str]:
    return store.get_json("active_region", default_region())


def _today() -> date:
    return datetime.now(FORECAST_TIMEZONE).date()


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


# --- rate limiting -----------------------------------------------------------


class RateLimiter:
    """Small in-memory sliding window keyed by client IP. Caddy adds a second layer."""

    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] < now - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if len(self._hits) > 5000:
                stale = [k for k, v in self._hits.items() if not v or v[-1] < now - self.window]
                for k in stale:
                    del self._hits[k]
            return True


subscribe_limiter = RateLimiter(limit=8, window_seconds=3600)
manage_limiter = RateLimiter(limit=60, window_seconds=3600)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _limit(limiter: RateLimiter, request: Request) -> None:
    if not limiter.check(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many requests from this address. Try again in a little while.")


# --- monitoring ---------------------------------------------------------------

STALE_HRRR_HOURS = 4
ALERT_COOLDOWN_HOURS = 6


class Monitor:
    """In-memory record of the last time each background job succeeded or failed.

    Health reporting and the daily heartbeat read from here; alert emails go out
    when something has been failing long enough to matter, at most once per cooldown.
    """

    def __init__(self):
        self.started_at = datetime.now(timezone.utc)
        self.hrrr_ok_at: datetime | None = None
        self.hrrr_error: str | None = None
        self.hrrr_failures = 0
        self.goes_error: str | None = None
        self.notify_at: datetime | None = None
        self.notify_errors: list[str] = []
        self.last_alert_at: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def hrrr_success(self) -> None:
        with self._lock:
            self.hrrr_ok_at = datetime.now(timezone.utc)
            self.hrrr_error = None
            self.hrrr_failures = 0

    def hrrr_failure(self, error: str) -> None:
        with self._lock:
            self.hrrr_error = error[:300]
            self.hrrr_failures += 1

    def notify_pass(self, errors: list[str]) -> None:
        with self._lock:
            self.notify_at = datetime.now(timezone.utc)
            self.notify_errors = errors[:10]

    def hrrr_stale(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        reference = self.hrrr_ok_at or self.started_at
        return now - reference > timedelta(hours=STALE_HRRR_HOURS)

    def problems(self) -> list[str]:
        items = []
        mode = engine.provider_metadata(_today())["mode"]
        if settings.forecast_mode in {"auto", "live"} and mode != "live":
            items.append("Forecast is on the demo fallback (no live HRRR snapshot for today)")
        if settings.forecast_mode in {"auto", "live"} and self.hrrr_stale():
            items.append(f"HRRR has not refreshed in over {STALE_HRRR_HOURS} hours" + (f": {self.hrrr_error}" if self.hrrr_error else ""))
        if self.notify_errors:
            items.append(f"Last notification pass had {len(self.notify_errors)} error(s): {self.notify_errors[0]}")
        return items

    def should_alert(self, key: str) -> bool:
        with self._lock:
            last = self.last_alert_at.get(key)
            if last and datetime.now(timezone.utc) - last < timedelta(hours=ALERT_COOLDOWN_HOURS):
                return False
            self.last_alert_at[key] = datetime.now(timezone.utc)
            return True


monitor = Monitor()


def _alert_if_needed() -> None:
    """Email the admin about a sustained failure, at most once per cooldown per kind."""
    if settings.forecast_mode in {"auto", "live"} and monitor.hrrr_failures >= 3 and monitor.should_alert("hrrr"):
        send_admin_alert(
            settings,
            "HRRR ingestion is failing",
            f"{monitor.hrrr_failures} consecutive hourly refreshes have failed.\nLast error: {monitor.hrrr_error}\n"
            f"Last success: {monitor.hrrr_ok_at.isoformat() if monitor.hrrr_ok_at else 'never since start'}\n\n"
            "Alerts pause while the forecast is on the demo fallback. Check `docker compose logs app` on the droplet.",
        )
    if monitor.notify_errors and monitor.should_alert("notify"):
        send_admin_alert(
            settings,
            "Notification sends are failing",
            "Errors from the latest pass:\n" + "\n".join(monitor.notify_errors) + "\n\nIf these are SMTP timeouts, see the deploy README about outbound ports.",
        )


def heartbeat_body() -> str:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    sends = store.sends_since(since)
    counts = store.status_counts()
    outcomes = store.outcome_summary()
    meta = engine.provider_metadata(_today())
    problems = monitor.problems()
    lines = [
        f"Cloudset daily heartbeat · {datetime.now(FORECAST_TIMEZONE):%A %B %-d, %-I:%M %p %Z}",
        "",
        "STATUS: " + ("all clear" if not problems else "attention needed"),
        *[f"  ! {item}" for item in problems],
        "",
        f"Forecast: {meta.get('model_run')} (mode {meta.get('mode')})",
        f"HRRR last success: {monitor.hrrr_ok_at.isoformat(timespec='minutes') if monitor.hrrr_ok_at else 'none since start'}",
        f"Emails last 24h: {sends.get('sent', 0)} sent, {sends.get('logged', 0)} logged" + (f", other: {sum(v for k, v in sends.items() if k not in ('sent', 'logged'))}" if any(k not in ('sent', 'logged') for k in sends) else ""),
        f"Subscribers: {counts['active']} active, {counts['pending']} pending, {counts['unsubscribed']} unsubscribed",
        f"Outcome ratings: {outcomes['count']} total" + (f", average {outcomes['average_rating']:.1f}/5 against a mean predicted score of {outcomes['average_predicted_score']:.0f}" if outcomes['count'] else ""),
        "",
        f"Control room: {settings.public_url}/admin",
    ]
    return "\n".join(lines)


# --- notifications -----------------------------------------------------------

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


def _send_alert(subscriber: dict, feature: dict, forecast_day: date, event_key: str, event_label: str) -> str:
    latitude = float(subscriber["latitude"])
    longitude = float(subscriber["longitude"])
    detail_map_png = email_map_for(forecast_day, latitude, longitude)
    regional_map_png = email_map_for(forecast_day, latitude, longitude, zoom=8, tile_scale=1, forecast_blur=3)
    result, _ = send_forecast(settings, subscriber, feature, event_label, detail_map_png, regional_map_png)
    store.record_notification(int(subscriber["id"]), forecast_day.isoformat(), event_key, feature["properties"]["score"], result)
    return result


def _process_subscriber(subscriber: dict, now: datetime, force: bool) -> tuple[int, int, list[str]]:
    sent = skipped = 0
    errors: list[str] = []
    for event_key, forecast_day, event_label in _due_events(subscriber, now, force):
        subscription_id = int(subscriber["id"])
        day_key = forecast_day.isoformat()
        if store.notification_exists(subscription_id, day_key, event_key):
            skipped += 1
            continue
        if engine.provider_metadata(forecast_day)["mode"] != "live" and not force:
            skipped += 1
            continue
        feature = engine.point(_active_region(), float(subscriber["latitude"]), float(subscriber["longitude"]), forecast_day)
        if not feature:
            skipped += 1
            continue
        score = feature["properties"]["score"]
        try:
            if score >= subscriber["threshold"]:
                _send_alert(subscriber, feature, forecast_day, event_key, event_label)
                sent += 1
                continue
            # The outlook fell below the threshold after we already promised a
            # show. Say so once, at the next reminder time the person chose.
            earlier = [e for e in store.alerts_for_date(subscription_id, day_key) if e["score"] >= subscriber["threshold"]]
            if earlier and not store.notification_exists(subscription_id, day_key, "downgrade"):
                result, _ = send_downgrade(settings, subscriber, feature, max(e["score"] for e in earlier))
                store.record_notification(subscription_id, day_key, "downgrade", score, result)
                sent += 1
            else:
                skipped += 1
        except Exception as exc:  # SMTP failures must not stop other recipients.
            log.exception("Notification failed")
            errors.append(f"subscription {subscription_id} ({event_key}): {exc}")
    return sent, skipped, errors


def dispatch_notifications(force: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    sent = skipped = 0
    errors: list[str] = []
    subscribers = store.subscriptions()
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="notify") as pool:
        for result in pool.map(lambda s: _process_subscriber(s, now, force), subscribers):
            sent += result[0]
            skipped += result[1]
            errors.extend(result[2])
    monitor.notify_pass(errors)
    return {"sent": sent, "skipped": skipped, "errors": errors, "subscribers": len(subscribers)}


async def scheduler() -> None:
    while True:
        try:
            await asyncio.to_thread(dispatch_notifications)
            await asyncio.to_thread(_alert_if_needed)
        except Exception:
            log.exception("Scheduled notification pass failed")
        await asyncio.sleep(15 * 60)


HEARTBEAT_HOUR = 7


async def heartbeat_scheduler() -> None:
    """One summary email a day so silence never means 'probably fine'."""
    while True:
        local = datetime.now(FORECAST_TIMEZONE)
        target = local.replace(hour=HEARTBEAT_HOUR, minute=30, second=0, microsecond=0)
        if target <= local:
            target += timedelta(days=1)
        await asyncio.sleep((target - local).total_seconds())
        try:
            await asyncio.to_thread(send_admin_alert, settings, "Daily heartbeat", heartbeat_body())
        except Exception:
            log.exception("Heartbeat email failed")


async def hrrr_scheduler() -> None:
    # Let startup and short-lived test processes settle; existing snapshots are
    # immediately available, and an admin can always trigger a refresh now.
    await asyncio.sleep(30)
    while True:
        try:
            results = await asyncio.to_thread(hrrr_ingestor.refresh, _active_region(), _today())
            live_provider.invalidate()
            _cache.clear()
            monitor.hrrr_success()
            log.info("HRRR refresh complete: %s", results)
        except Exception as exc:
            monitor.hrrr_failure(f"{type(exc).__name__}: {exc}")
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
        except Exception as exc:
            monitor.goes_error = f"{type(exc).__name__}: {exc}"[:300]
            log.exception("GOES-East correction refresh failed; HRRR remains active")
        await asyncio.sleep(10 * 60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = [asyncio.create_task(scheduler())]
    if settings.admin_email:
        tasks.append(asyncio.create_task(heartbeat_scheduler()))
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


app = FastAPI(title="Cloudset", version="0.2.0", lifespan=lifespan, docs_url=None if settings.production else "/docs", redoc_url=None)
app.mount("/assets", StaticFiles(directory=settings.root / "static"), name="assets")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response


def require_admin(x_admin_token: str = Header(default="")) -> None:
    if not settings.admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Invalid admin token")


# --- models -------------------------------------------------------------------


def _clean_email(email: str) -> str:
    email = email.strip().lower()
    if not EMAIL_PATTERN.fullmatch(email):
        raise ValueError("Enter a valid email address")
    return email


def _clean_times(values: list[str]) -> list[str]:
    allowed = {"day_before", "morning", "final_90", "custom"}
    unique = list(dict.fromkeys(values))
    if not unique or any(value not in allowed for value in unique):
        raise ValueError("Choose at least one valid notification time")
    return unique


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


class WatchSettings(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    label: str = Field(default="", max_length=100)
    threshold: int = Field(default=70, ge=40, le=95)
    notification_times: list[str] = Field(default_factory=lambda: ["morning", "final_90"], min_length=1, max_length=4)
    custom_minutes: int | None = Field(default=None, ge=30, le=2160)

    @field_validator("notification_times")
    @classmethod
    def validate_notification_times(cls, values: list[str]) -> list[str]:
        return _clean_times(values)

    def check_custom(self) -> None:
        if "custom" in self.notification_times and self.custom_minutes is None:
            raise HTTPException(status_code=422, detail="Choose how many hours before sunset for the custom reminder")


class SubscriptionCreate(WatchSettings):
    email: str = Field(min_length=5, max_length=254)

    @field_validator("email")
    @classmethod
    def validate_email(cls, email: str) -> str:
        return _clean_email(email)


class TokenBody(BaseModel):
    token: str = Field(min_length=10, max_length=600)
    everything: bool = False


class EmailTest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    latitude: float = Field(default=40.7128, ge=-90, le=90)
    longitude: float = Field(default=-74.006, ge=-180, le=180)

    @field_validator("email")
    @classmethod
    def validate_email(cls, email: str) -> str:
        return _clean_email(email)


# --- pages --------------------------------------------------------------------


@app.get("/", include_in_schema=False)
def public_ui() -> FileResponse:
    return FileResponse(settings.root / "static" / "index.html")


@app.get("/admin", include_in_schema=False)
def admin_ui() -> FileResponse:
    return FileResponse(settings.root / "static" / "admin.html")


@app.get("/manage", include_in_schema=False)
@app.get("/confirm", include_in_schema=False)
@app.get("/unsubscribe", include_in_schema=False)
def manage_ui() -> FileResponse:
    return FileResponse(settings.root / "static" / "manage.html")


@app.get("/rate", include_in_schema=False)
def rate_ui() -> FileResponse:
    return FileResponse(settings.root / "static" / "rate.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(settings.root / "static" / "favicon.svg", media_type="image/svg+xml")


@app.get("/robots.txt", include_in_schema=False)
def robots() -> Response:
    return Response("User-agent: *\nDisallow: /admin\nDisallow: /manage\nDisallow: /confirm\nDisallow: /unsubscribe\nDisallow: /rate\nDisallow: /api/\n", media_type="text/plain")


# --- public API ---------------------------------------------------------------


@app.get("/api/health")
def health(response: Response) -> dict:
    problems = monitor.problems()
    if problems:
        response.status_code = 503
    return {
        "status": "degraded" if problems else "ok",
        "problems": problems,
        "mode": engine.provider_metadata(_today())["mode"],
        "configured_mode": settings.forecast_mode,
        "hrrr_last_success": monitor.hrrr_ok_at.isoformat() if monitor.hrrr_ok_at else None,
        "version": app.version,
    }


@app.get("/api/config")
def config() -> dict:
    return {
        "mode": engine.provider_metadata(_today())["mode"],
        "smtp_configured": bool(settings.smtp_host),
        "region_bounds": REGION_BOUNDS,
        "timezone": settings.timezone,
        "donate_url": settings.donate_url,
        "satellite": {"provider": "NASA GIBS / GOES-East ABI GeoColor", "attribution": "NASA EOSDIS GIBS · NOAA"},
        "forecast_tiles": {
            "format": "PNG tile pyramid",
            "layers": list(LAYERS),
            "resolutions_degrees": {"z3-5": 0.5, "z6": 0.25, "z7": 0.125, "z8+": 0.0625},
        },
    }


@app.get("/api/forecast")
def forecast(day: int = Query(default=0, ge=-1, le=5), offset: int = Query(default=0, ge=-60, le=60)) -> dict:
    return forecast_for(_today() + timedelta(days=day), offset)


@app.get("/api/forecast/point")
def point_forecast(
    latitude: float = Query(ge=-90, le=90),
    longitude: float = Query(ge=-180, le=180),
    day: int = Query(default=0, ge=-1, le=5),
    offset: int = Query(default=0, ge=-60, le=60),
) -> dict:
    target = _today() + timedelta(days=day)
    feature = engine.point(_active_region(), latitude, longitude, target, offset)
    if not feature:
        raise HTTPException(status_code=404, detail="Location is outside the active forecast area")
    return {"location": {"latitude": latitude, "longitude": longitude}, "forecast": feature, "meta": forecast_meta(target, offset)}


@app.get("/api/forecast/meta")
def get_forecast_meta(day: int = Query(default=0, ge=-1, le=5), offset: int = Query(default=0, ge=-60, le=60)) -> dict:
    return forecast_meta(_today() + timedelta(days=day), offset)


@app.get("/api/forecast/tiles/{layer}/{day_offset}/{minute_offset}/{zoom}/{tile_x}/{tile_y}.png")
def forecast_tile(
    layer: str,
    day_offset: int,
    minute_offset: int,
    zoom: int,
    tile_x: int,
    tile_y: int,
    min: int = Query(default=0, ge=0, le=95),
) -> Response:
    if layer not in LAYERS:
        raise HTTPException(status_code=404, detail="Unknown overlay layer")
    if not -1 <= day_offset <= 5 or not -60 <= minute_offset <= 60 or not 3 <= zoom <= 12:
        raise HTTPException(status_code=422, detail="Tile coordinate outside the supported forecast range")
    scale = 2**zoom
    if not 0 <= tile_x < scale or not 0 <= tile_y < scale:
        raise HTTPException(status_code=404, detail="Tile does not exist")
    target = _today() + timedelta(days=day_offset)
    content, cached, resolution = tile_pyramid.render(
        _active_region(), target, minute_offset, zoom, tile_x, tile_y, layer=layer, minimum=quantize_min(min)
    )
    return Response(
        content=content,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600", "X-Forecast-Resolution": str(resolution), "X-Tile-Cache": "HIT" if cached else "MISS"},
    )


# --- subscriptions ------------------------------------------------------------


def _public_subscription(record: dict) -> dict:
    links = subscription_links(settings, record)
    return {
        "id": record["id"],
        "email": record["email"],
        "latitude": record["latitude"],
        "longitude": record["longitude"],
        "label": record["label"],
        "threshold": record["threshold"],
        "notification_times": record["notification_times"],
        "custom_minutes": record["custom_minutes"],
        "status": record["status"],
        "created_at": record["created_at"],
        "confirmed_at": record.get("confirmed_at"),
        "manage_token": links["manage"].split("token=")[-1] if links["manage"] else "",
    }


@app.post("/api/subscriptions", status_code=201)
def subscribe(payload: SubscriptionCreate, request: Request) -> dict:
    _limit(subscribe_limiter, request)
    payload.check_custom()
    existing = next(
        (s for s in store.subscriptions_for_email(payload.email) if s["latitude"] == payload.latitude and s["longitude"] == payload.longitude),
        None,
    )
    if existing is None and store.count_for_email(payload.email) >= settings.max_subscriptions_per_email:
        raise HTTPException(
            status_code=409,
            detail=f"That address already has {settings.max_subscriptions_per_email} watches. Use the manage link in any alert to change them.",
        )
    subscriber_id = store.subscribe(
        payload.email, payload.latitude, payload.longitude, payload.label, payload.threshold, payload.notification_times, payload.custom_minutes
    )
    record = store.subscription(subscriber_id)
    log.info("Subscription %s created/updated (%s) from %s", subscriber_id, record["status"], _client_ip(request))
    if record["status"] == "active":
        return {"ok": True, "id": subscriber_id, "status": "active", "message": "Your watch is updated. No confirmation needed."}
    try:
        delivery, _ = send_confirmation(settings, record)
    except Exception as exc:
        log.exception("Confirmation email failed")
        raise HTTPException(status_code=502, detail="We couldn't send the confirmation email right now. Please try again in a few minutes.") from exc
    store.record_notification(subscriber_id, _today().isoformat(), "confirmation", 0, delivery)
    return {
        "ok": True,
        "id": subscriber_id,
        "status": "pending",
        "message": "Check your inbox and tap the confirmation link to start watching the sky.",
    }


def _resolve(token: str, purpose: str | None = None) -> dict:
    payload = read_token(settings.secret_key, token, purpose)
    if payload is None:
        raise HTTPException(status_code=400, detail="This link is invalid or was signed with an older key.")
    record = store.subscription(payload.subscription_id)
    if record is None or record["email"] != payload.email:
        raise HTTPException(status_code=404, detail="This watch no longer exists.")
    return record


@app.post("/api/confirm")
def confirm(body: TokenBody, request: Request) -> dict:
    _limit(manage_limiter, request)
    record = _resolve(body.token, "confirm")
    if record["status"] == "unsubscribed":
        raise HTTPException(status_code=410, detail="This watch was unsubscribed. Sign up again from the map if you'd like alerts.")
    store.confirm(int(record["id"]))
    return {"ok": True, "subscription": _public_subscription(store.subscription(int(record["id"])))}


def _unsubscribe(token: str, everything: bool) -> dict:
    record = _resolve(token)  # Any purpose: manage and unsubscribe tokens both allow leaving.
    if everything:
        count = store.unsubscribe_email(record["email"])
        return {"ok": True, "unsubscribed": count, "email": record["email"]}
    store.unsubscribe(int(record["id"]))
    return {"ok": True, "unsubscribed": 1, "email": record["email"], "label": record["label"]}


@app.post("/api/unsubscribe")
def unsubscribe(body: TokenBody, request: Request) -> dict:
    _limit(manage_limiter, request)
    return _unsubscribe(body.token, body.everything)


@app.post("/unsubscribe", include_in_schema=False)
async def one_click_unsubscribe(request: Request, token: str = Query(default="")) -> Response:
    """RFC 8058 one-click target: mail clients POST here without showing a page."""
    _limit(manage_limiter, request)
    if not token:
        raise HTTPException(status_code=400, detail="Missing token")
    _unsubscribe(token, everything=False)
    return JSONResponse({"ok": True})


@app.get("/api/manage")
def manage(request: Request, token: str = Query(min_length=10, max_length=600)) -> dict:
    _limit(manage_limiter, request)
    record = _resolve(token)
    return {
        "email": record["email"],
        "subscriptions": [_public_subscription(item) for item in store.subscriptions_for_email(record["email"])],
    }


@app.put("/api/manage/{subscription_id}")
def update_watch(subscription_id: int, payload: WatchSettings, request: Request, token: str = Query(min_length=10, max_length=600)) -> dict:
    _limit(manage_limiter, request)
    payload.check_custom()
    owner = _resolve(token)
    target = store.subscription(subscription_id)
    if target is None or target["email"] != owner["email"] or target["status"] == "unsubscribed":
        raise HTTPException(status_code=404, detail="This watch no longer exists.")
    try:
        store.update_subscription(
            subscription_id,
            latitude=payload.latitude,
            longitude=payload.longitude,
            label=payload.label,
            threshold=payload.threshold,
            notification_times=payload.notification_times,
            custom_minutes=payload.custom_minutes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "subscription": _public_subscription(store.subscription(subscription_id))}


@app.delete("/api/manage/{subscription_id}")
def delete_watch(subscription_id: int, request: Request, token: str = Query(min_length=10, max_length=600)) -> dict:
    _limit(manage_limiter, request)
    owner = _resolve(token)
    target = store.subscription(subscription_id)
    if target is None or target["email"] != owner["email"]:
        raise HTTPException(status_code=404, detail="This watch no longer exists.")
    store.unsubscribe(subscription_id)
    return {"ok": True}


# --- outcomes -----------------------------------------------------------------


class OutcomeBody(BaseModel):
    token: str = Field(min_length=10, max_length=600)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=500)


@app.post("/api/outcome")
def record_outcome(body: OutcomeBody, request: Request) -> dict:
    _limit(manage_limiter, request)
    record = _resolve(body.token, "outcome")
    alerts = store.alerts_for_date(int(record["id"]), body.date)
    if not alerts:
        raise HTTPException(status_code=404, detail="We didn't send you an alert for that day, so there's nothing to rate.")
    predicted = max(float(a["score"]) for a in alerts)
    store.record_outcome(int(record["id"]), body.date, body.rating, predicted, body.comment.strip())
    return {"ok": True, "rating": body.rating, "predicted_score": predicted, "label": record["label"], "date": body.date}


# --- admin --------------------------------------------------------------------


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
    results = hrrr_ingestor.refresh(_active_region(), _today())
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
    result = forecast_for(_today())
    duration = (time.perf_counter() - then) * 1000
    finished = datetime.now(timezone.utc)
    actual_mode = engine.provider_metadata(_today())["mode"]
    run_id = store.add_run(started.isoformat(), finished.isoformat(), actual_mode, len(result["features"]), duration, "Forecast field generated")
    return {"ok": True, "run_id": run_id, "cells": len(result["features"]), "duration_ms": round(duration, 1)}


@app.post("/api/admin/notifications/test", dependencies=[Depends(require_admin)])
def test_notifications() -> dict:
    return dispatch_notifications(force=False)


@app.post("/api/admin/email/test", dependencies=[Depends(require_admin)])
def send_test_email(payload: EmailTest) -> dict:
    today = _today()
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
    regional_map_png = email_map_for(today, payload.latitude, payload.longitude, zoom=8, tile_scale=1, forecast_blur=3)
    delivery, message = send_forecast(settings, subscriber, feature, "This is a Cloudset test email · no alert was triggered", detail_map_png, regional_map_png)
    return {
        "ok": True,
        "delivery": delivery,
        "smtp_configured": bool(settings.smtp_host),
        "preview": {
            "to": payload.email,
            "subject": str(message["Subject"]),
            "body": plain_text_content(message),
            "map_embedded": detail_map_png is not None or regional_map_png is not None,
            "maps_embedded": {"detail": detail_map_png is not None, "regional": regional_map_png is not None},
        },
    }


@app.get("/api/admin/subscriptions", dependencies=[Depends(require_admin)])
def admin_subscriptions() -> dict:
    return {"counts": store.status_counts(), "subscriptions": [_public_subscription(s) for s in store.subscriptions(status=None)]}


@app.post("/api/admin/subscriptions/{subscription_id}/send", dependencies=[Depends(require_admin)])
def admin_send_now(subscription_id: int) -> dict:
    record = store.subscription(subscription_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown subscription")
    today = _today()
    feature = engine.point(_active_region(), float(record["latitude"]), float(record["longitude"]), today)
    if not feature:
        raise HTTPException(status_code=404, detail="Subscriber location is outside the active forecast area")
    result = _send_alert(record, feature, today, f"manual_{int(time.time())}", EVENT_LABELS["manual"])
    return {"ok": True, "delivery": result, "score": feature["properties"]["score"]}


@app.delete("/api/admin/subscriptions/{subscription_id}", dependencies=[Depends(require_admin)])
def admin_delete(subscription_id: int) -> dict:
    if not store.delete_subscription(subscription_id):
        raise HTTPException(status_code=404, detail="Unknown subscription")
    return {"ok": True}


@app.get("/api/admin/notifications", dependencies=[Depends(require_admin)])
def admin_notifications(limit: int = Query(default=50, ge=1, le=500)) -> dict:
    return {"notifications": store.recent_notifications(limit)}


@app.get("/api/admin/outcomes", dependencies=[Depends(require_admin)])
def admin_outcomes(limit: int = Query(default=200, ge=1, le=1000)) -> dict:
    return {"summary": store.outcome_summary(), "outcomes": store.outcomes(limit)}


@app.post("/api/admin/heartbeat", dependencies=[Depends(require_admin)])
def admin_heartbeat() -> dict:
    body = heartbeat_body()
    return {"ok": True, "delivery": send_admin_alert(settings, "Daily heartbeat (manual)", body), "body": body}


@app.get("/api/admin/status", dependencies=[Depends(require_admin)])
def admin_status() -> dict:
    today = _today()
    provider_meta = engine.provider_metadata(today)
    counts = store.status_counts()
    return {
        "mode": provider_meta["mode"],
        "configured_mode": settings.forecast_mode,
        "environment": settings.environment,
        "public_url": settings.public_url,
        "model_run": provider_meta["model_run"],
        "data_source": provider_meta,
        "goes": goes_observations.status(today),
        "provider": provider_meta.get("source", "demo-atmosphere"),
        "active_cells": len(_active_region()),
        "forecast_cells": len(_active_region()) * 4,
        "subscribers": counts["active"],
        "subscriber_counts": counts,
        "smtp_configured": bool(settings.smtp_host),
        "admin_auth": bool(settings.admin_token),
        "warnings": validate_settings(settings),
        "problems": monitor.problems(),
        "hrrr_last_success": monitor.hrrr_ok_at.isoformat() if monitor.hrrr_ok_at else None,
        "admin_email": settings.admin_email,
        "last_run": store.last_run(),
    }


def run() -> None:
    import uvicorn

    uvicorn.run(
        "cloudset.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        workers=1,
        proxy_headers=settings.production,
        forwarded_allow_ips="*" if settings.production else None,
    )


if __name__ == "__main__":
    run()
