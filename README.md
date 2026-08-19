# Cloudset

Cloudset is a Pi-friendly app for finding the East Coast skies most likely to turn red, orange, magenta, and purple around sunset. It computes a spatial forecast first and then maps people and email watches onto it.

This repository is a working vertical slice:

- public GOES/satellite + adaptive forecast-tile map, day/time scrubber, click-to-relocate, and email signup;
- local control room with a paintable forecast footprint, upstream-data buffer, run controls, and delivery status;
- vectorized scoring, solar geometry, SQLite persistence, SMTP delivery, and a 15-minute notification scheduler;
- deterministic demo atmosphere so every flow is testable before large weather ingestion is connected.

The atmospheric layer is currently **demo data**. The UI and API say so. Solar position, sunset time, persistence, notification decisions, NASA GOES imagery, and all product interactions are real.

## Run locally

```bash
uv sync --extra dev
uv run cloudset
```

Open <http://127.0.0.1:8080> and <http://127.0.0.1:8080/admin>. The API schema is at <http://127.0.0.1:8080/docs>.

Configuration is via the variables in [`.env.example`](.env.example). Without SMTP, eligible notifications are logged rather than sent. Set `CLOUDSET_ADMIN_TOKEN` before making the admin endpoint accessible to anything beyond a trusted LAN.

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

Connect the provider interface in [`forecast.py`](src/cloudset/forecast.py) to cropped HRRR cloud/visibility/precipitation fields, then add a GOES cloud-motion correction. The reasoning, field list, ordering, and resource constraints are in [the forecasting design](docs/forecasting.md). The `WeatherProvider` boundary lets that work land without changing either UI or notification logic.

## Data attribution

Map imagery is loaded in the browser from NASA EOSDIS GIBS/NOAA GOES-East, Esri World Imagery, and OpenStreetMap. NOAA weather data is public; NASA/Esri/OSM attribution remains visible on the map. See each provider's current terms before a public launch.
