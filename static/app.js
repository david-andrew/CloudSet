/* global L */
const STORAGE_KEY = 'cloudset-state-v1';
const linkParams = new URLSearchParams(window.location.search);
const linkedLatitudeValue = linkParams.get('lat');
const linkedLongitudeValue = linkParams.get('lon');
const linkedLatitude = Number(linkedLatitudeValue);
const linkedLongitude = Number(linkedLongitudeValue);
const hasLinkedLocation = linkParams.has('lat') && linkParams.has('lon')
  && linkedLatitudeValue.trim() !== '' && linkedLongitudeValue.trim() !== ''
  && Number.isFinite(linkedLatitude) && Number.isFinite(linkedLongitude)
  && linkedLatitude >= -90 && linkedLatitude <= 90 && linkedLongitude >= -180 && linkedLongitude <= 180;

function linkedDayOffset(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return 0;
  const target = new Date(`${value}T12:00:00`);
  const today = new Date(); today.setHours(12, 0, 0, 0);
  if (Number.isNaN(target.getTime())) return 0;
  return Math.max(0, Math.min(3, Math.round((target - today) / 86400000)));
}

function loadSaved() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
    if (!saved || typeof saved !== 'object') return null;
    return saved;
  } catch { return null; }
}

const saved = loadSaved() || {};
const defaultLocation = { latitude: 40.7128, longitude: -74.0060, label: 'New York City' };
const savedLocation = saved.location && Number.isFinite(saved.location.latitude) && Number.isFinite(saved.location.longitude)
  ? { latitude: saved.location.latitude, longitude: saved.location.longitude, label: String(saved.location.label || '').slice(0, 100) || placeName(saved.location.latitude, saved.location.longitude) }
  : null;

const state = {
  location: hasLinkedLocation
    ? { latitude: linkedLatitude, longitude: linkedLongitude, label: linkParams.get('label')?.slice(0, 100) || placeName(linkedLatitude, linkedLongitude) }
    : (savedLocation || defaultLocation),
  day: hasLinkedLocation ? linkedDayOffset(linkParams.get('date')) : 0,
  offset: 0,
  overlay: ['potential', 'clouds', 'none'].includes(saved.overlay) ? saved.overlay : 'potential',
  minScore: Number.isFinite(saved.minScore) ? Math.max(0, Math.min(95, Math.round(saved.minScore / 5) * 5)) : 60,
  baseLayer: saved.baseLayer === 'map' ? 'map' : 'satellite',
  forecastLayer: null,
  marker: null,
  revision: '',
  donateUrl: '',
};

function persist() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      location: state.location,
      overlay: state.overlay,
      minScore: state.minScore,
      baseLayer: state.baseLayer,
      view: { center: map.getCenter(), zoom: map.getZoom() },
    }));
  } catch { /* private mode or storage disabled; nothing to do */ }
}

const initialView = hasLinkedLocation
  ? { center: [state.location.latitude, state.location.longitude], zoom: 9 }
  : (saved.view && saved.view.center ? { center: [saved.view.center.lat, saved.view.center.lng], zoom: saved.view.zoom || 6 } : { center: [39.2, -74.8], zoom: 6 });

const map = L.map('map', { zoomControl: false, minZoom: 3, maxZoom: 12, preferCanvas: true }).setView(initialView.center, initialView.zoom);
L.control.zoom({ position: 'topright' }).addTo(map);
const roadLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '© OpenStreetMap contributors' });
const earthLayer = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', { maxZoom: 19, attribution: 'Esri World Imagery' });

function goesTimestamp() {
  const d = new Date(Date.now() - 60 * 60 * 1000);
  d.setUTCMinutes(Math.floor(d.getUTCMinutes() / 10) * 10, 0, 0);
  return d.toISOString().replace('.000', '');
}

const goesTime = goesTimestamp();
const goesLayer = L.tileLayer(
  `https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/GOES-East_ABI_GeoColor/default/${goesTime}/GoogleMapsCompatible_Level7/{z}/{y}/{x}.jpg`,
  { maxNativeZoom: 7, maxZoom: 12, opacity: 0.78, attribution: 'NASA EOSDIS GIBS · NOAA GOES-East' },
);
document.querySelector('#satellite-age').innerHTML = `<i></i> GOES-East · ${new Date(goesTime).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`;

function applyBaseLayer() {
  document.querySelectorAll('.layer-switch button').forEach((b) => b.classList.toggle('active', b.dataset.layer === state.baseLayer));
  if (state.baseLayer === 'map') {
    map.removeLayer(earthLayer); map.removeLayer(goesLayer); roadLayer.addTo(map);
    document.querySelector('#satellite-age').style.display = 'none';
  } else {
    map.removeLayer(roadLayer); earthLayer.addTo(map); goesLayer.addTo(map);
    document.querySelector('#satellite-age').style.display = '';
  }
  if (state.forecastLayer) state.forecastLayer.bringToFront();
}
applyBaseLayer();

const markerIcon = L.divIcon({ className: 'location-marker', iconSize: [18, 18], iconAnchor: [9, 9] });
state.marker = L.marker([state.location.latitude, state.location.longitude], { icon: markerIcon, zIndexOffset: 1000 }).addTo(map);

function placeName(lat, lon) {
  const places = [
    ['New York City', 40.713, -74.006], ['Boston', 42.36, -71.059], ['Philadelphia', 39.953, -75.165],
    ['Washington, DC', 38.907, -77.037], ['Richmond', 37.54, -77.436], ['Raleigh', 35.78, -78.639],
    ['Charleston', 32.777, -79.932], ['Savannah', 32.081, -81.091], ['Jacksonville', 30.332, -81.656],
    ['Miami', 25.762, -80.192], ['Portland, Maine', 43.66, -70.257], ['Pittsburgh', 40.44, -79.996],
    ['Baltimore', 39.29, -76.612], ['Harpers Ferry', 39.325, -77.739], ['Norfolk', 36.851, -76.286],
  ];
  let nearest = null; let distance = Infinity;
  places.forEach(([name, y, x]) => { const d = (lat - y) ** 2 + (lon - x) ** 2; if (d < distance) [nearest, distance] = [name, d]; });
  return distance < 0.08 ? nearest : `${Math.abs(lat).toFixed(2)}° ${lat >= 0 ? 'N' : 'S'}, ${Math.abs(lon).toFixed(2)}° ${lon >= 0 ? 'E' : 'W'}`;
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

function coordText(lat, lon, digits = 3) {
  return `${Math.abs(lat).toFixed(digits)}° ${lat >= 0 ? 'N' : 'S'} · ${Math.abs(lon).toFixed(digits)}° ${lon >= 0 ? 'E' : 'W'}`;
}

function updatePanel(feature) {
  const coords = state.location;
  document.querySelector('#location-name').textContent = state.location.label;
  document.querySelector('#coordinates').textContent = coordText(coords.latitude, coords.longitude);
  if (!feature) {
    document.querySelector('#score').textContent = '—';
    document.querySelector('#headline').textContent = 'Outside the forecast area';
    document.querySelector('#summary').textContent = 'Cloudset currently covers the US East Coast. Pick a spot inside the shaded region.';
    document.querySelector('#tier').textContent = 'NO COVERAGE';
    document.querySelector('#score-ring').style.setProperty('--score', 0);
    document.querySelector('#sunset-time').textContent = '—';
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
  const aerosolLabel = p.aerosol_optical_depth < 0.05 ? 'very clear'
    : p.aerosol_optical_depth <= 0.25 ? 'color-friendly'
      : p.aerosol_optical_depth <= 0.5 ? 'hazy' : 'dense smoke / haze';
  document.querySelector('#aerosol-value').textContent = `${p.aerosol_optical_depth.toFixed(2)} AOD · ${aerosolLabel}`;
  setBar('#cloud-bar', cloud);
  setBar('#clear-bar', p.western_clearance);
  setBar('#rain-bar', 100 - p.precip_risk);
  setBar('#aerosol-bar', Math.min(100, p.aerosol_optical_depth * 200));
}

function updateNotifyLocationPreview() {
  const { latitude, longitude, label } = state.location;
  document.querySelector('#notify-current-location').textContent = `${label} · ${coordText(latitude, longitude)}`;
  if (document.querySelector('#use-current-location').checked) {
    document.querySelector('#notification-location').value = `${label} — ${latitude.toFixed(4)}, ${longitude.toFixed(4)}`;
  }
}

function customCoordinates(value) {
  const parts = value.split(',').map((part) => Number(part.trim()));
  if (parts.length !== 2 || !parts.every(Number.isFinite)) return null;
  const [latitude, longitude] = parts;
  if (latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) return null;
  return { latitude, longitude };
}

// --- overlays -----------------------------------------------------------------

function renderForecastTiles(revision) {
  if (state.forecastLayer) { map.removeLayer(state.forecastLayer); state.forecastLayer = null; }
  if (state.overlay === 'none') return;
  const min = state.overlay === 'potential' ? state.minScore : 0;
  const template = `/api/forecast/tiles/${state.overlay}/${state.day}/${state.offset}/{z}/{x}/{y}.png?min=${min}&v=${encodeURIComponent(revision)}`;
  state.forecastLayer = L.tileLayer(template, {
    minZoom: 3, maxZoom: 12, tileSize: 256, opacity: 0.88, keepBuffer: 3, updateWhenZooming: false, attribution: 'Cloudset forecast',
  }).addTo(map);
  state.forecastLayer.bringToFront();
  state.marker.setZIndexOffset(1000);
}

function applyOverlayUI() {
  document.querySelectorAll('.overlay-switch button').forEach((b) => b.classList.toggle('active', b.dataset.overlay === state.overlay));
  document.querySelector('#legend-potential').hidden = state.overlay !== 'potential';
  document.querySelector('#legend-clouds').hidden = state.overlay !== 'clouds';
  document.querySelector('#min-slider').value = state.minScore;
  document.querySelector('#min-label').textContent = state.minScore > 0 ? `showing ${state.minScore}+` : 'showing all';
}

document.querySelectorAll('.overlay-switch button').forEach((button) => button.addEventListener('click', () => {
  state.overlay = button.dataset.overlay;
  applyOverlayUI();
  renderForecastTiles(state.revision);
  persist();
}));

let minTimer;
document.querySelector('#min-slider').addEventListener('input', (event) => {
  state.minScore = Number(event.target.value);
  document.querySelector('#min-label').textContent = state.minScore > 0 ? `showing ${state.minScore}+` : 'showing all';
  clearTimeout(minTimer);
  minTimer = setTimeout(() => { renderForecastTiles(state.revision); persist(); }, 150);
});

// --- forecast loading -----------------------------------------------------------

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
    const live = meta.mode === 'live';
    document.querySelector('#data-status-text').textContent = live ? `Live · ${meta.model_run}` : 'Demo fallback · live solar geometry';
    document.querySelector('#weather-source-note').innerHTML = live
      ? `<strong>Current weather data:</strong> ${meta.model_run}, spatially normalized to the 5–7 km forecast layer. ${meta.data_source.satellite_correction ? 'Recent GOES-East infrared observations are correcting cloud placement near sunset.' : 'GOES-East imagery remains live in the map; observed correction activates during the final three hours.'}`
      : '<strong>Demo fallback:</strong> a current HRRR snapshot is unavailable. Scores use deterministic demo weather and remain clearly labeled. Email alerts pause until live data returns.';
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

function setLocation(latitude, longitude, label, { pan = false, zoom = null } = {}) {
  state.location = { latitude, longitude, label: label || placeName(latitude, longitude) };
  state.marker.setLatLng([latitude, longitude]);
  updateNotifyLocationPreview();
  if (pan) {
    if (zoom) map.setView([latitude, longitude], zoom); else map.panTo([latitude, longitude]);
  }
  persist();
  loadPoint().catch((error) => { document.querySelector('#summary').textContent = error.message; });
}

map.on('click', ({ latlng }) => setLocation(latlng.lat, latlng.lng));
map.on('moveend zoomend', () => { updateResolutionIndicator(); persist(); });

function updateResolutionIndicator() {
  const zoom = map.getZoom();
  const degrees = zoom <= 5 ? 0.5 : zoom === 6 ? 0.25 : zoom === 7 ? 0.125 : 0.0625;
  const km = Math.round(degrees * 111 * Math.cos(map.getCenter().lat * Math.PI / 180));
  document.querySelector('#resolution-indicator').textContent = `~${km} km prediction cells`;
}
updateResolutionIndicator();

document.querySelectorAll('.layer-switch button').forEach((button) => button.addEventListener('click', () => {
  state.baseLayer = button.dataset.layer;
  applyBaseLayer();
  persist();
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

// --- place search and geolocation -------------------------------------------------

const searchForm = document.querySelector('#search-form');
const searchInput = document.querySelector('#search-input');
const searchResults = document.querySelector('#search-results');

function hideResults() { searchResults.hidden = true; searchResults.innerHTML = ''; }

function shortName(item) {
  const a = item.address || {};
  const primary = a.city || a.town || a.village || a.hamlet || a.suburb || a.county || item.name || item.display_name.split(',')[0];
  const region = a.state ? `, ${a.state}` : '';
  const named = item.name && item.name !== primary ? `${item.name}, ${primary}` : primary;
  return `${named}${region}`.slice(0, 100);
}

async function searchPlaces(query) {
  const params = new URLSearchParams({
    q: query, format: 'jsonv2', limit: '6', addressdetails: '1', countrycodes: 'us',
    viewbox: '-90,48,-64,24', bounded: '0',
  });
  const response = await fetch(`https://nominatim.openstreetmap.org/search?${params}`, { headers: { Accept: 'application/json' } });
  if (!response.ok) throw new Error('Place search is unavailable right now');
  return response.json();
}

searchForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const query = searchInput.value.trim();
  if (!query) return;
  const direct = customCoordinates(query);
  if (direct) { setLocation(direct.latitude, direct.longitude, null, { pan: true, zoom: 9 }); hideResults(); return; }
  searchResults.hidden = false;
  searchResults.innerHTML = '<li class="search-status">Searching…</li>';
  try {
    const items = await searchPlaces(query);
    if (!items.length) { searchResults.innerHTML = '<li class="search-status">No places found. Try a town name or “lat, lon”.</li>'; return; }
    searchResults.innerHTML = '';
    items.forEach((item) => {
      const li = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.innerHTML = `<strong></strong><small></small>`;
      button.querySelector('strong').textContent = shortName(item);
      button.querySelector('small').textContent = item.display_name;
      button.addEventListener('click', () => {
        setLocation(Number(item.lat), Number(item.lon), shortName(item), { pan: true, zoom: 9 });
        searchInput.value = '';
        hideResults();
        collapseSheet();
      });
      li.appendChild(button);
      searchResults.appendChild(li);
    });
  } catch (error) {
    searchResults.innerHTML = `<li class="search-status">${error.message}</li>`;
  }
});
document.addEventListener('click', (event) => { if (!searchForm.contains(event.target)) hideResults(); });
searchInput.addEventListener('keydown', (event) => { if (event.key === 'Escape') hideResults(); });

document.querySelector('#locate-button').addEventListener('click', () => {
  if (!navigator.geolocation) { document.querySelector('#summary').textContent = 'Your browser does not offer location access.'; return; }
  const button = document.querySelector('#locate-button');
  button.classList.add('busy');
  navigator.geolocation.getCurrentPosition(
    ({ coords }) => { button.classList.remove('busy'); setLocation(coords.latitude, coords.longitude, null, { pan: true, zoom: 9 }); },
    () => { button.classList.remove('busy'); document.querySelector('#summary').textContent = 'Location access was declined. Click the map or search instead.'; },
    { timeout: 10000, maximumAge: 300000 },
  );
});

// --- dialogs -------------------------------------------------------------------

const notifyDialog = document.querySelector('#notify-dialog');
['#notify-top', '#notify-card'].forEach((selector) => document.querySelector(selector).addEventListener('click', () => {
  updateNotifyLocationPreview();
  const button = document.querySelector('#subscribe-button');
  button.textContent = 'Start watching the sky';
  notifyDialog.showModal();
}));
document.querySelector('#notify-close').addEventListener('click', () => notifyDialog.close());
document.querySelectorAll('dialog').forEach((dialog) => dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); }));
document.querySelector('#use-current-location').addEventListener('change', (event) => {
  const field = document.querySelector('#notification-location');
  field.disabled = event.target.checked;
  if (event.target.checked) {
    field.dataset.customValue = field.value;
    updateNotifyLocationPreview();
  } else {
    field.value = field.dataset.customValue?.includes(',') ? field.dataset.customValue : '';
    field.placeholder = '40.7128, -74.0060';
    field.focus();
  }
});
document.querySelector('#notification-location').addEventListener('input', (event) => { event.target.dataset.customValue = event.target.value; });
document.querySelector('#custom-time-toggle').addEventListener('change', (event) => {
  document.querySelector('#custom-time').hidden = !event.target.checked;
  document.querySelector('#custom-hours').required = event.target.checked;
});
document.querySelector('#about-button').addEventListener('click', () => document.querySelector('#about-dialog').showModal());
document.querySelector('#details-button').addEventListener('click', () => document.querySelector('#about-dialog').showModal());
document.querySelector('#privacy-button').addEventListener('click', () => document.querySelector('#privacy-dialog').showModal());
document.querySelector('#threshold').addEventListener('input', (event) => { document.querySelector('#threshold-output').textContent = event.target.value; });
document.querySelector('#notify-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = document.querySelector('#subscribe-button'); const note = document.querySelector('#form-note');
  button.disabled = true; button.textContent = 'Saving your watch…';
  try {
    const useCurrent = document.querySelector('#use-current-location').checked;
    const custom = useCurrent ? null : customCoordinates(document.querySelector('#notification-location').value);
    if (!useCurrent && !custom) throw new Error('Enter the custom location as latitude, longitude');
    const latitude = useCurrent ? state.location.latitude : custom.latitude;
    const longitude = useCurrent ? state.location.longitude : custom.longitude;
    const notificationTimes = [...document.querySelectorAll('input[name="notification-time"]:checked')].map((input) => input.value);
    if (!notificationTimes.length) throw new Error('Choose at least one notification time');
    const customMinutes = notificationTimes.includes('custom') ? Math.round(Number(document.querySelector('#custom-hours').value) * 60) : null;
    if (notificationTimes.includes('custom') && (!Number.isFinite(customMinutes) || customMinutes < 30 || customMinutes > 2160)) throw new Error('Choose a custom lead time between 0.5 and 36 hours');
    const response = await fetch('/api/subscriptions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
      email: document.querySelector('#email').value,
      label: useCurrent ? state.location.label : placeName(latitude, longitude),
      latitude,
      longitude,
      threshold: Number(document.querySelector('#threshold').value),
      notification_times: notificationTimes,
      custom_minutes: customMinutes,
    }) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail?.[0]?.msg || result.detail || 'Could not create watch');
    button.textContent = result.status === 'active' ? 'Watch updated ✓' : 'Check your email ✓';
    note.textContent = result.message;
    setTimeout(() => notifyDialog.close(), result.status === 'active' ? 1400 : 4000);
  } catch (error) { note.textContent = error.message; button.textContent = 'Try again'; }
  finally { button.disabled = false; }
});

// --- mobile bottom sheet ----------------------------------------------------------

const panel = document.querySelector('#forecast-panel');
const backdrop = document.querySelector('#sheet-backdrop');
function setSheet(open) {
  panel.classList.toggle('open', open);
  backdrop.classList.toggle('show', open);
  document.querySelector('#sheet-label').textContent = open ? 'Back to the map ↓' : 'Tap for the full forecast ↑';
  if (!open) panel.scrollTop = 0;
}
function isMobile() { return window.matchMedia('(max-width: 900px)').matches; }
function collapseSheet() { if (isMobile()) setSheet(false); }
document.querySelector('#sheet-handle').addEventListener('click', () => setSheet(!panel.classList.contains('open')));
document.querySelector('.location-block').addEventListener('click', (event) => {
  if (isMobile() && !panel.classList.contains('open') && !event.target.closest('form, button')) setSheet(true);
});
backdrop.addEventListener('click', () => setSheet(false));
let touchStartY = null;
panel.addEventListener('touchstart', (event) => { touchStartY = event.touches[0].clientY; }, { passive: true });
panel.addEventListener('touchend', (event) => {
  if (touchStartY === null) return;
  const delta = event.changedTouches[0].clientY - touchStartY;
  touchStartY = null;
  if (delta > 70 && panel.scrollTop <= 0) setSheet(false);
  else if (delta < -70 && !panel.classList.contains('open')) setSheet(true);
}, { passive: true });

// --- config and startup -------------------------------------------------------------

async function loadConfig() {
  try {
    const config = await (await fetch('/api/config')).json();
    if (config.donate_url) {
      ['#donate-top', '#donate-panel', '#donate-about'].forEach((selector) => {
        const link = document.querySelector(selector); link.href = config.donate_url; link.hidden = false;
      });
    }
  } catch { /* the map still works without config */ }
}

updateDayLabels();
document.querySelectorAll('#day-tabs button').forEach((button) => button.classList.toggle('active', Number(button.dataset.day) === state.day));
applyOverlayUI();
updateNotifyLocationPreview();
loadConfig();
loadForecast();
