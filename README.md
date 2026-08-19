# Cloudset

Cloudset is a Pi-friendly app for finding the East Coast skies most likely to turn red, orange, magenta, and purple around sunset. It computes a spatial forecast first and then maps people and email watches onto it.

This repository is a working vertical slice:

- public GOES/satellite + adaptive forecast-tile map, day/time scrubber, click-to-relocate, and email signup with selectable reminder times;
- local control room with a paintable forecast footprint, upstream-data buffer, run controls, delivery status, and test-email preview/send;
- vectorized scoring, solar geometry, SQLite persistence, SMTP delivery, and a 15-minute notification scheduler;
- live NOAA HRRR sunset snapshots with deterministic demo fallback when NOAA data is unavailable.

With `CLOUDSET_FORECAST_MODE=auto`, the service downloads narrowly cropped NOAA HRRR low/mid/high cloud, visibility, and precipitation fields for tonight and tomorrow. It normalizes each valid time to a compact 0.0625° NumPy snapshot and refreshes hourly. If the upstream cycle is unavailable, the UI and API explicitly switch to the deterministic demo fallback rather than presenting stale/demo data as live.

## Run locally

```bash
uv sync --extra dev
uv run cloudset
```

Open <http://127.0.0.1:8080> and <http://127.0.0.1:8080/admin>. The API schema is at <http://127.0.0.1:8080/docs>.

Configuration is via the variables in [`.env.example`](.env.example). Without SMTP, eligible notifications are logged rather than sent; the admin test-email control still renders the exact message preview. Set `CLOUDSET_ADMIN_TOKEN` before making the admin endpoint accessible to anything beyond a trusted LAN.

Notification emails include the saved location and coordinates, a plain-text fallback, embedded close-up and regional OpenStreetMap images composited with the Cloudset forecast overlay, high-contrast labels, and location marker, plus a deep link back to the interactive outlook. Set `CLOUDSET_PUBLIC_URL` to the LAN address or public domain recipients should open. Basemap tiles are cached under ignored runtime data so repeated alerts remain lightweight.

Fetch live snapshots immediately with `uv run cloudset-ingest`, or use **Fetch HRRR now** in the admin control room. The background scheduler performs the same refresh hourly in `auto`/`live` mode.

## Verify

```bash
uv run pytest
curl http://127.0.0.1:8080/api/health
```

The broad notification field takes roughly 1–2 seconds for about 1,800 demo output cells. The public map uses a separate on-demand tile pyramid: 0.5° at wide zooms, refining through 0.25° and 0.125° to 0.0625° (about 5–7 km) when zoomed in. Each 256×256 PNG tile is cached on disk by day, time offset, provider, and forecast-footprint revision. A Pi 4 should be entirely comfortable with this product shell; live HRRR ingestion must crop and aggregate data as described in [the forecasting design](docs/forecasting.md).

## Raspberry Pi 4

Install `uv` in the Pi user's account, copy the repository to `~/cloudset`, then:

```bash
cd ~/cloudset
uv sync
cp .env.example .env
mkdir -p ~/.config/systemd/user
cp deploy/cloudset.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cloudset
```

For boot without an interactive login, enable linger for that Pi user (`loginctl enable-linger USER`) as an administrator. Put Caddy or Cloudflare Tunnel in front of only the public routes if the Pi accepts internet traffic; keep `/admin` LAN-only. A static GitHub Pages frontend is also possible: set an API base URL in the JavaScript and configure HTTPS/CORS on the Pi-facing API.

## Next implementation milestone

Add a GOES cloud-motion correction to the live HRRR baseline, followed by aerosol/smoke input and observed-outcome calibration. The reasoning, ordering, and resource constraints are in [the forecasting design](docs/forecasting.md). The `WeatherProvider` boundary keeps those additions independent from either UI and notification logic.

## Data attribution

Map imagery is loaded in the browser from NASA EOSDIS GIBS/NOAA GOES-East, Esri World Imagery, and OpenStreetMap. NOAA weather data is public; NASA/Esri/OSM attribution remains visible on the map. See each provider's current terms before a public launch.
