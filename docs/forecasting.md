# Forecasting design

Cloudset is an inverse forecast: compute a spatial field first, then sample it for each subscriber. It does not run a separate weather model query for every person.

## What makes a sky look “on fire”

The useful ingredients are more specific than generic cloud cover:

1. The Sun must be just below or near the observer's horizon. Solar position and the local sunset are deterministic.
2. Mid/high cloud must provide a broken reflecting canvas. A solid overcast or a cloudless sky both score poorly.
3. The path toward and beyond the setting Sun must be open enough for low-angle light to reach the cloud base. This requires sampling an upstream wedge, not only the observer's grid cell.
4. Low cloud, fog, heavy precipitation, and terrain near the western horizon can block the view.
5. Moderate aerosol loading can deepen reds; extreme smoke/dust can instead extinguish the light. This is a secondary signal and should be quality-controlled.

The current engine implements that scoring structure with live solar geometry and narrowly cropped NOAA HRRR fields. It downloads one representative sunset-valid lead for tonight and tomorrow, then resamples the requested low/mid/high cloud, visibility, precipitation, and smoke AOD variables to a compact 0.0625° runtime snapshot. In the final three hours, two cropped GOES-East clean-infrared frames provide observed middle/high-cloud probability and a bounded bulk-motion estimate; that field is advected toward sunset and blended conservatively into HRRR. `meta.mode` and the UI switch explicitly to demo only when a live model snapshot is absent.

## Data priority

### Phase 1 — highest value per unit effort

- **[HRRR (0–48 hours)](https://registry.opendata.aws/noaa-hrrr-pds/) — implemented baseline:** subset low/mid/high cloud cover, visibility, and precipitation over the active region plus its western buffer using NOAA's GRIB filter. HRRR is hourly, 3 km, and assimilates radar. The ingest retrieves only a sunset-valid lead rather than an entire model cycle.
- **Solar geometry:** compute sunset, solar elevation/azimuth, and the upstream illumination wedge locally. This is already implemented without a network dependency.
- **[GOES-East ABI](https://www.nesdis.noaa.gov/our-satellites/currently-flying/goes-east-west/goes-r-series-data-products) — implemented Band 13 nowcast:** two NASA GIBS clean-infrared frames correct model timing and cloud-edge position during roughly the final 0–3 hours. The ingest converts the official brightness-temperature palette into a compact cloud-probability field, estimates bounded bulk motion with phase correlation, and preserves HRRR as the vertical-structure/future-growth baseline. Cloud height/phase/[optical depth](https://goes-r.noaa.gov/products/baseline-cloud-opt-depth.html) remain later refinements.

### Phase 2 — useful refinements

- **HRRR smoke AOD — implemented:** ingest the whole-atmosphere `AOTK` field with the sunset HRRR subset. The scoring curve peaks around modest optical depth and adds an explicit extinction penalty above 0.35 AOD. This field represents forecast smoke rather than all aerosol species; the UI and email therefore report the value directly instead of overstating certainty.
- **NEXRAD:** precipitation/virga and opaque low-cloud veto near forecast time. Radar is valuable defensively but has less direct predictive power for colorful clouds than HRRR and ABI.
- **GFS:** day 3–5 fallback and upstream context outside HRRR coverage. Downweight confidence at the longer horizon.
- **Terrain/land mask:** score whether the subscriber has an unobstructed western view and avoid recommending inaccessible/offshore cells.

### Defer

- **[ECMWF open data](https://www.ecmwf.int/en/forecasts/datasets/open-data):** valuable for ensemble/model disagreement and expansion beyond North America, but the extra ingest and compute burden is not the best Pi-first investment.
- **Full-disk raw ABI archives:** retain compact features, not raw imagery. The UI can request NASA GIBS tiles directly.
- **Machine learning:** first collect predictions, observed imagery, and user “was it fiery?” feedback. A calibrated model needs labeled outcomes; fitting one before that would give false precision.

## Proposed operational score

For each 3–6 km output cell and candidate time:

```text
potential = reflecting_cloud_fit
          × clear_low_level_view
          × upstream_light_corridor
          × precipitation_veto
          × solar_elevation_window
          + bounded_aerosol_bonus
```

The upstream corridor follows the reverse solar azimuth and samples multiple distances/heights. Forecast confidence is separate from potential: a high score with stale satellite data or disagreeing models should remain high-potential but low-confidence.

## Pi budget

- Keep one forecast cycle plus compact prior-run features in memory.
- Crop at the provider before download. The visible admin cells create a western/northern/southern buffer automatically.
- Compute with `float32` NumPy arrays and reuse masks/solar geometry.
- Refresh HRRR hourly, GOES features every 10 minutes only in the sunset window, and emails every 15 minutes.
- Store configuration, subscribers, runs, and sent-message idempotency in SQLite.
- Let the public site fetch [NASA GIBS GOES-East GeoColor](https://gibs.earthdata.nasa.gov/layer-metadata/v1.0/GOES-East_ABI_GeoColor.json), OSM, and Esri tiles from their providers; the Pi serves only compact GeoJSON and subscription requests.

The notification sweep retains a cheap 0.5° field. The public map uses a cached prediction pyramid: 0.5° at zoom 3–5, 0.25° at zoom 6, 0.125° at zoom 7, and 0.0625° (about 5–7 km) from zoom 8 onward. Only visible 256×256 PNG tiles are evaluated and transferred. Operational HRRR output can replace the finest demo level with aggregated 3–6 km model data without changing the tile API.
