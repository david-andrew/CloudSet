# Cloudset

Cloudset is a small self-hosted app for finding the East Coast skies most likely to turn red, orange, magenta, and purple around sunset. It computes a spatial forecast first and then maps people and email watches onto it.

This repository is a working vertical slice:

- public GOES/satellite + adaptive forecast-tile map, day/time scrubber, click-to-relocate, and email signup with selectable reminder times;
- local control room with a paintable forecast footprint, upstream-data buffer, run controls, delivery status, and test-email preview/send;
- vectorized scoring, solar geometry, SQLite persistence, SMTP delivery, and a 15-minute notification scheduler;
- live NOAA HRRR cloud/smoke snapshots, near-sunset GOES-East infrared cloud-motion correction, and deterministic demo fallback when live data is unavailable.

With `CLOUDSET_FORECAST_MODE=auto`, the service pulls the six needed NOAA HRRR fields by byte range from the AWS Open Data mirror (falling back to NOMADS) low/mid/high cloud, visibility, precipitation, and smoke aerosol optical depth fields for tonight and tomorrow. It normalizes each valid time to a compact 0.0625° NumPy snapshot and refreshes hourly. Moderate smoke AOD receives only a bounded color bonus; dense smoke/haze incurs an extinction penalty. During the final hours before sunset, two small NASA GIBS GOES-East Band 13 frames provide an observed cloud field and motion correction at the same resolution. If the upstream model cycle is unavailable, the UI and API explicitly switch to the deterministic demo fallback rather than presenting stale/demo data as live.

## Run locally

```bash
uv sync --extra dev
uv run cloudset
```

Open <http://127.0.0.1:8080> and <http://127.0.0.1:8080/admin>. The API schema is at <http://127.0.0.1:8080/docs>.

Configuration is via the variables in [`.env.example`](.env.example). Without SMTP, eligible notifications are logged rather than sent; the admin test-email control still renders the exact message preview. `CLOUDSET_ADMIN_TOKEN` and `CLOUDSET_SECRET_KEY` are required in production and warned about in development.

Notification emails include the saved location and coordinates, a plain-text fallback, embedded close-up and regional OpenStreetMap images composited with the Cloudset forecast overlay, high-contrast labels, and location marker, plus a deep link back to the interactive outlook. Set `CLOUDSET_PUBLIC_URL` to the LAN address or public domain recipients should open. Basemap tiles are cached under ignored runtime data so repeated alerts remain lightweight.

Fetch live model snapshots immediately with `uv run cloudset-ingest`, or use **Fetch HRRR now** in the admin control room. **Fetch GOES now** exercises the satellite correction manually; automatically it polls every ten minutes only from four hours before through one hour after sunset. The HRRR background refresh remains hourly in `auto`/`live` mode.

## Verify

```bash
uv run pytest
curl http://127.0.0.1:8080/api/health
```

The broad notification field takes roughly 1–2 seconds for about 1,800 demo output cells. The public map uses a separate on-demand tile pyramid: 0.5° at wide zooms, refining through 0.25° and 0.125° to 0.0625° (about 5–7 km) when zoomed in. Each 256×256 PNG tile is cached on disk by day, time offset, provider, and forecast-footprint revision. A 1 GB VM is comfortable for this workload; live HRRR ingestion crops and aggregates data as described in [the forecasting design](docs/forecasting.md).

## Deploy

Production runs as two containers (the app and Caddy for HTTPS) on one small VM. The runbook, including first-time server setup, the `.env` checklist, backups, and how to recover admin access when your home IP changes, is in [`deploy/README.md`](deploy/README.md). The overall launch plan and rationale is in [`docs/launch-plan.md`](docs/launch-plan.md).

Key behaviours worth knowing before going public:

- Signups are double opt-in. A watch stays `pending` and receives nothing until its confirmation link is opened.
- Every alert carries signed manage and unsubscribe links plus `List-Unsubscribe` headers, so recipients can leave with one click from their mail client.
- If an alert went out and a later check finds the score below the threshold, a single "the outlook has faded" notice is sent at the next reminder time the person chose.
- The control room at `/admin` requires the admin token and, in production, is only reachable from the IPs in `CLOUDSET_ADMIN_ALLOW`.
- Every alert email ends with five one-tap "how was it?" buttons for that day's sunset. Ratings are visible in the control room and are the ground truth for future calibration.
- The admin address gets a daily heartbeat plus alerts on sustained HRRR or SMTP failures, and `/api/health` returns 503 when degraded.
- Leaflet and the web fonts are vendored under `static/vendor`, so the page loads nothing from third-party CDNs.
- Run one app process. The notification, HRRR, and GOES schedulers live inside it.

## Next implementation milestone

Add observed-outcome collection and calibration, followed by NEXRAD precipitation/virga veto refinement. The reasoning, ordering, and resource constraints are in [the forecasting design](docs/forecasting.md). The `WeatherProvider` boundary keeps those additions independent from either UI and notification logic.

## Data attribution

Map imagery is loaded in the browser from NASA EOSDIS GIBS/NOAA GOES-East, Esri World Imagery, and OpenStreetMap. NOAA weather data is public; NASA/Esri/OSM attribution remains visible on the map. See each provider's current terms before a public launch.
