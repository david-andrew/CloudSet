/* global L */
const state = {
  location: { latitude: 40.7128, longitude: -74.0060, label: 'New York City' },
  day: 0,
  offset: 0,
  forecastLayer: null,
  marker: null,
  revision: '',
};

const map = L.map('map', { zoomControl: false, minZoom: 3, maxZoom: 12, preferCanvas: true }).setView([39.2, -74.8], 6);
L.control.zoom({ position: 'topright' }).addTo(map);
const roadLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '© OpenStreetMap contributors',
});
const earthLayer = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
  maxZoom: 19,
  attribution: 'Esri World Imagery',
}).addTo(map);

function goesTimestamp() {
  const d = new Date(Date.now() - 60 * 60 * 1000);
  d.setUTCMinutes(Math.floor(d.getUTCMinutes() / 10) * 10, 0, 0);
  return d.toISOString().replace('.000', '');
}

const goesTime = goesTimestamp();
const goesLayer = L.tileLayer(
  `https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/GOES-East_ABI_GeoColor/default/${goesTime}/GoogleMapsCompatible_Level7/{z}/{y}/{x}.jpg`,
  { maxNativeZoom: 7, maxZoom: 12, opacity: 0.78, attribution: 'NASA EOSDIS GIBS · NOAA GOES-East' },
).addTo(map);
document.querySelector('#satellite-age').innerHTML = `<i></i> GOES-East · ${new Date(goesTime).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`;

const markerIcon = L.divIcon({ className: 'location-marker', iconSize: [18, 18], iconAnchor: [9, 9] });
state.marker = L.marker([state.location.latitude, state.location.longitude], { icon: markerIcon, zIndexOffset: 1000 }).addTo(map);

function placeName(lat, lon) {
  const places = [
    ['New York City', 40.713, -74.006], ['Boston', 42.36, -71.059], ['Philadelphia', 39.953, -75.165],
    ['Washington, DC', 38.907, -77.037], ['Richmond', 37.54, -77.436], ['Raleigh', 35.78, -78.639],
    ['Charleston', 32.777, -79.932], ['Savannah', 32.081, -81.091], ['Jacksonville', 30.332, -81.656],
    ['Miami', 25.762, -80.192], ['Portland, Maine', 43.66, -70.257], ['Pittsburgh', 40.44, -79.996],
  ];
  let nearest = null; let distance = Infinity;
  places.forEach(([name, y, x]) => { const d = (lat - y) ** 2 + (lon - x) ** 2; if (d < distance) [nearest, distance] = [name, d]; });
  return distance < 1.1 ? nearest : `${Math.abs(lat).toFixed(2)}° ${lat >= 0 ? 'N' : 'S'}, ${Math.abs(lon).toFixed(2)}° ${lon >= 0 ? 'E' : 'W'}`;
}

function describe(score) {
  if (score >= 82) return ['The sky could catch fire', 'A strong reflecting cloud deck and open western light path line up near sunset.'];
  if (score >= 68) return ['A vivid glow is possible', 'Good color ingredients are coming together. Keep an eye on the western horizon.'];
  if (score >= 48) return ['A warm glow may break through', 'Some useful cloud texture is present, though one ingredient may limit the show.'];
  return ['A quieter horizon tonight', 'The cloud and light-path pattern is not favorable for widespread fiery color.'];
}

function setBar(id, value) {
  document.querySelector(id).style.width = `${Math.max(2, Math.min(100, value))}%`;
}

function updatePanel(feature) {
  const coords = state.location;
  document.querySelector('#location-name').textContent = state.location.label;
  document.querySelector('#coordinates').textContent = `${Math.abs(coords.latitude).toFixed(3)}° ${coords.latitude >= 0 ? 'N' : 'S'} · ${Math.abs(coords.longitude).toFixed(3)}° ${coords.longitude >= 0 ? 'E' : 'W'}`;
  if (!feature) {
    document.querySelector('#score').textContent = '—';
    document.querySelector('#headline').textContent = 'Outside the search area';
    document.querySelector('#summary').textContent = 'The control room can add this location to the active forecast footprint.';
    document.querySelector('#tier').textContent = 'NO COVERAGE';
    document.querySelector('#score-ring').style.setProperty('--score', 0);
    return;
  }
  const p = feature.properties;
  const rounded = Math.round(p.score);
  const [headline, summary] = describe(rounded);
  document.querySelector('#score').textContent = rounded;
  document.querySelector('#score-ring').style.setProperty('--score', rounded);
  document.querySelector('#headline').textContent = headline;
  document.querySelector('#summary').textContent = summary;
  document.querySelector('#tier').textContent = p.tier === 'fire' ? 'HIGH POTENTIAL' : p.tier.toUpperCase();
  document.querySelector('#sunset-time').textContent = new Date(p.sunset_utc).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const offsetText = state.offset === 0 ? 'Peak color near sunset' : `${Math.abs(state.offset)} min ${state.offset < 0 ? 'before' : 'after'} sunset`;
  document.querySelector('#sunset-note').textContent = offsetText;
  const cloud = Math.round((p.mid_cloud + p.high_cloud) / 2);
  document.querySelector('#cloud-value').textContent = `${cloud}% mid / high`;
  document.querySelector('#clear-value').textContent = `${p.western_clearance}% open`;
  document.querySelector('#rain-value').textContent = p.precip_risk < 15 ? 'Low' : `${p.precip_risk}% risk`;
  setBar('#cloud-bar', cloud);
  setBar('#clear-bar', p.western_clearance);
  setBar('#rain-bar', 100 - p.precip_risk);
}

function updateNotifyLocationPreview() {
  const { latitude, longitude, label } = state.location;
  document.querySelector('#notify-current-location').textContent = `${label} · ${Math.abs(latitude).toFixed(3)}° ${latitude >= 0 ? 'N' : 'S'}, ${Math.abs(longitude).toFixed(3)}° ${longitude >= 0 ? 'E' : 'W'}`;
}

function renderForecastTiles(revision) {
  if (state.forecastLayer) map.removeLayer(state.forecastLayer);
  const template = `/api/forecast/tiles/${state.day}/${state.offset}/{z}/{x}/{y}.png?v=${encodeURIComponent(revision)}`;
  state.forecastLayer = L.tileLayer(template, {
    minZoom: 3,
    maxZoom: 12,
    tileSize: 256,
    opacity: 0.88,
    keepBuffer: 3,
    updateWhenZooming: false,
    attribution: 'Cloudset forecast',
  }).addTo(map);
  state.forecastLayer.bringToFront();
  state.marker.setZIndexOffset(1000);
}

async function loadPoint() {
  const params = new URLSearchParams({ latitude: state.location.latitude, longitude: state.location.longitude, day: state.day, offset: state.offset });
  const response = await fetch(`/api/forecast/point?${params}`);
  if (response.status === 404) { updatePanel(null); return null; }
  if (!response.ok) throw new Error(`Point forecast failed (${response.status})`);
  const result = await response.json();
  updatePanel(result.forecast);
  return result;
}

async function loadForecast() {
  document.querySelector('#data-status-text').textContent = 'Updating forecast';
  try {
    const metaResponse = await fetch(`/api/forecast/meta?day=${state.day}&offset=${state.offset}`);
    if (!metaResponse.ok) throw new Error(`Forecast metadata failed (${metaResponse.status})`);
    const meta = await metaResponse.json();
    state.revision = meta.revision;
    renderForecastTiles(meta.revision);
    await loadPoint();
    document.querySelector('#data-status-text').textContent = meta.mode === 'demo' ? 'Demo weather · live solar geometry' : 'Forecast updated';
    document.querySelector('#selected-date').textContent = state.day === 0 ? 'TODAY' : new Date(Date.now() + state.day * 86400000).toLocaleDateString([], { month: 'short', day: 'numeric' }).toUpperCase();
  } catch (error) {
    document.querySelector('#data-status-text').textContent = 'Forecast unavailable';
    document.querySelector('#headline').textContent = 'The forecast is offline';
    document.querySelector('#summary').textContent = error.message;
  }
}

function updateDayLabels() {
  document.querySelectorAll('#day-tabs button').forEach((button) => {
    const day = Number(button.dataset.day);
    const d = new Date(Date.now() + day * 86400000);
    button.querySelector('strong').textContent = d.toLocaleDateString([], { weekday: 'short', day: 'numeric' });
  });
}

map.on('click', ({ latlng }) => {
  state.location = { latitude: latlng.lat, longitude: latlng.lng, label: placeName(latlng.lat, latlng.lng) };
  state.marker.setLatLng(latlng);
  updateNotifyLocationPreview();
  loadPoint().catch((error) => { document.querySelector('#summary').textContent = error.message; });
});

function updateResolutionIndicator() {
  const zoom = map.getZoom();
  const degrees = zoom <= 5 ? 0.5 : zoom === 6 ? 0.25 : zoom === 7 ? 0.125 : 0.0625;
  const km = Math.round(degrees * 111 * Math.cos(map.getCenter().lat * Math.PI / 180));
  document.querySelector('#resolution-indicator').textContent = `~${km} km prediction cells`;
}
map.on('zoomend moveend', updateResolutionIndicator);
updateResolutionIndicator();

document.querySelectorAll('.layer-switch button').forEach((button) => button.addEventListener('click', () => {
  document.querySelectorAll('.layer-switch button').forEach((b) => b.classList.toggle('active', b === button));
  if (button.dataset.layer === 'map') {
    map.removeLayer(earthLayer); map.removeLayer(goesLayer); roadLayer.addTo(map); document.querySelector('#satellite-age').style.display = 'none';
  } else {
    map.removeLayer(roadLayer); earthLayer.addTo(map); goesLayer.addTo(map); document.querySelector('#satellite-age').style.display = '';
  }
  if (state.forecastLayer) state.forecastLayer.bringToFront();
}));

document.querySelectorAll('#day-tabs button').forEach((button) => button.addEventListener('click', () => {
  document.querySelectorAll('#day-tabs button').forEach((b) => b.classList.toggle('active', b === button));
  state.day = Number(button.dataset.day); loadForecast();
}));

let sliderTimer;
document.querySelector('#time-slider').addEventListener('input', (event) => {
  state.offset = Number(event.target.value);
  document.querySelector('#offset-label').textContent = state.offset === 0 ? 'At sunset' : `${Math.abs(state.offset)}m ${state.offset < 0 ? 'before' : 'after'}`;
  clearTimeout(sliderTimer); sliderTimer = setTimeout(loadForecast, 180);
});

const notifyDialog = document.querySelector('#notify-dialog');
['#notify-top', '#notify-card'].forEach((selector) => document.querySelector(selector).addEventListener('click', () => {
  updateNotifyLocationPreview();
  notifyDialog.showModal();
}));
document.querySelectorAll('input[name="notify-location"]').forEach((input) => input.addEventListener('change', () => {
  const useCurrent = document.querySelector('input[name="notify-location"]:checked').value === 'current';
  document.querySelector('#custom-location-fields').hidden = useCurrent;
  document.querySelectorAll('.location-choice').forEach((choice) => choice.classList.toggle('active', choice.querySelector('input').checked));
  document.querySelector('#notify-latitude').required = !useCurrent;
  document.querySelector('#notify-longitude').required = !useCurrent;
}));
document.querySelector('#about-button').addEventListener('click', () => document.querySelector('#about-dialog').showModal());
document.querySelector('#details-button').addEventListener('click', () => document.querySelector('#about-dialog').showModal());
document.querySelector('#threshold').addEventListener('input', (event) => { document.querySelector('#threshold-output').textContent = event.target.value; });
document.querySelector('#notify-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = document.querySelector('#subscribe-button'); const note = document.querySelector('#form-note');
  button.disabled = true; button.textContent = 'Saving your watch…';
  try {
    const useCurrent = document.querySelector('input[name="notify-location"]:checked').value === 'current';
    const latitude = useCurrent ? state.location.latitude : Number(document.querySelector('#notify-latitude').value);
    const longitude = useCurrent ? state.location.longitude : Number(document.querySelector('#notify-longitude').value);
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) throw new Error('Enter valid coordinates for the alert location');
    const response = await fetch('/api/subscriptions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
      email: document.querySelector('#email').value,
      label: document.querySelector('#place-label').value || (useCurrent ? state.location.label : placeName(latitude, longitude)),
      latitude,
      longitude,
      threshold: Number(document.querySelector('#threshold').value),
    }) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail?.[0]?.msg || result.detail || 'Could not create watch');
    button.textContent = 'You’re on the watchlist ✓'; note.textContent = `Watching ${state.location.label}. You can close this window.`;
    setTimeout(() => notifyDialog.close(), 1200);
  } catch (error) { note.textContent = error.message; button.textContent = 'Try again'; }
  finally { button.disabled = false; }
});

document.querySelector('#mobile-toggle').addEventListener('click', () => document.querySelector('.forecast-panel').classList.add('open'));
document.querySelector('.location-block').addEventListener('click', () => document.querySelector('.forecast-panel').classList.toggle('open'));
updateDayLabels();
updateNotifyLocationPreview();
loadForecast();
