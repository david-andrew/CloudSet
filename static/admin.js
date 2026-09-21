/* global L */
const state = { active: new Set(), saved: new Set(), tool: 'add', cells: new Map(), token: sessionStorage.getItem('cloudset-admin-token') || '' };
const map = L.map('admin-map', { preferCanvas: true, minZoom: 3, maxZoom: 9 }).setView([37.2, -75.5], 5);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, opacity: 0.74, attribution: '© OpenStreetMap contributors' }).addTo(map);
let bufferLayer;

async function api(path, options = {}) {
  options.headers = { ...(options.headers || {}), ...(state.token ? { 'X-Admin-Token': state.token } : {}) };
  let response = await fetch(path, options);
  if (response.status === 401) {
    const token = window.prompt('Enter the Cloudset admin token');
    if (!token) throw new Error('Admin token required');
    state.token = token; sessionStorage.setItem('cloudset-admin-token', token);
    options.headers['X-Admin-Token'] = token;
    response = await fetch(path, options);
  }
  if (!response.ok) { const body = await response.json().catch(() => ({})); throw new Error(body.detail || `Request failed (${response.status})`); }
  return response.json();
}

function id(lat, lon) { return `${lat.toFixed(1)},${lon.toFixed(1)}`; }
function defaultRegion() {
  const cells = new Set();
  for (let lat = 24; lat < 48; lat += 1) {
    const west = lat < 30 ? -83 : lat < 35 ? -85 : lat < 40 ? -83 : lat < 44 ? -81 : -80;
    for (let lon = west; lon < -64; lon += 1) cells.add(id(lat, lon));
  }
  return cells;
}

function styleCell(cellId) {
  const layer = state.cells.get(cellId); if (!layer) return;
  const on = state.active.has(cellId);
  layer.setStyle({ fillColor: on ? '#e86634' : '#ffffff', fillOpacity: on ? 0.49 : 0.025, color: on ? '#d25428' : '#ada9a2', weight: on ? 0.65 : 0.28, opacity: on ? 0.9 : 0.24 });
}

function dirty() {
  if (state.active.size !== state.saved.size) return true;
  for (const cell of state.active) if (!state.saved.has(cell)) return true;
  return false;
}

function updateUI() {
  document.querySelector('#cell-count').textContent = state.active.size.toLocaleString();
  document.querySelector('#footprint-stat').textContent = `${state.active.size} regions`;
  document.querySelector('#unsaved-count').textContent = dirty() ? '· unsaved' : '';
  if (bufferLayer) map.removeLayer(bufferLayer);
  if (state.active.size) {
    const points = [...state.active].map((value) => value.split(',').map(Number));
    const lats = points.map((p) => p[0]); const lons = points.map((p) => p[1]);
    bufferLayer = L.rectangle([[Math.max(20, Math.min(...lats) - 4.5), Math.max(-95, Math.min(...lons) - 6)], [Math.min(52, Math.max(...lats) + 5.5), Math.min(-60, Math.max(...lons) + 7)]], {
      fill: false, color: '#76516e', weight: 1.3, opacity: 0.8, dashArray: '7 6', interactive: false,
    }).addTo(map);
  }
}

function applyCell(cellId) {
  if (state.tool === 'add') state.active.add(cellId); else state.active.delete(cellId);
  styleCell(cellId); updateUI();
}

let painting = false; let painted = new Set();
for (let lat = 24; lat < 48; lat += 1) {
  for (let lon = -90; lon < -64; lon += 1) {
    const cellId = id(lat, lon);
    const rect = L.rectangle([[lat, lon], [lat + 1, lon + 1]], { className: 'admin-cell' }).addTo(map);
    state.cells.set(cellId, rect);
    rect.on('mousedown', (event) => { L.DomEvent.stopPropagation(event); painting = true; painted = new Set([cellId]); map.dragging.disable(); applyCell(cellId); });
    rect.on('mouseover', () => { if (painting && !painted.has(cellId)) { painted.add(cellId); applyCell(cellId); } });
  }
}
document.addEventListener('mouseup', () => { painting = false; painted.clear(); map.dragging.enable(); });

function toast(message) { const el = document.querySelector('#toast'); el.textContent = message; el.classList.add('show'); setTimeout(() => el.classList.remove('show'), 2600); }

document.querySelectorAll('[data-tool]').forEach((button) => button.addEventListener('click', () => {
  document.querySelectorAll('[data-tool]').forEach((b) => b.classList.toggle('active', b === button)); state.tool = button.dataset.tool;
}));
document.querySelector('#clear-region').addEventListener('click', () => { state.active.clear(); state.cells.forEach((_, key) => styleCell(key)); updateUI(); });
document.querySelector('#reset-region').addEventListener('click', () => { state.active = defaultRegion(); state.cells.forEach((_, key) => styleCell(key)); updateUI(); toast('East Coast footprint restored locally'); });
document.querySelector('#save-region').addEventListener('click', async () => {
  if (!state.active.size) return toast('Add at least one cell before saving');
  try {
    const result = await api('/api/admin/region', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ active_cells: [...state.active] }) });
    state.saved = new Set(state.active); updateUI(); toast(`Saved ${result.active_count} admin cells`);
  } catch (error) { toast(error.message); }
});
document.querySelector('#run-now').addEventListener('click', async (event) => {
  const button = event.currentTarget; button.disabled = true; button.textContent = 'Running…';
  try { const result = await api('/api/admin/run', { method: 'POST' }); document.querySelector('#runtime-stat').textContent = `${result.duration_ms} ms`; document.querySelector('#run-detail').textContent = `Run #${result.run_id} completed ${result.cells} forecast cells in ${result.duration_ms} ms.`; toast('Forecast field refreshed'); }
  catch (error) { toast(error.message); } finally { button.disabled = false; button.textContent = 'Run now'; }
});
document.querySelector('#ingest-now').addEventListener('click', async (event) => {
  const button = event.currentTarget; button.disabled = true; button.textContent = 'Fetching NOAA data…';
  try {
    const result = await api('/api/admin/ingest', { method: 'POST' });
    const latest = result.runs[0];
    document.querySelector('#hrrr-detail').textContent = `Loaded ${result.runs.length} sunset snapshots. Latest: ${latest.cycle_utc.slice(0, 13).replace('T', ' ')}Z · F${String(latest.forecast_hour).padStart(2, '0')}.`;
    document.querySelector('#mode-stat').textContent = 'LIVE';
    document.querySelector('#provider-stat').textContent = 'NOAA HRRR';
    toast('Live HRRR snapshots refreshed');
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = 'Fetch HRRR now'; }
});
document.querySelector('#ingest-goes').addEventListener('click', async (event) => {
  const button = event.currentTarget; button.disabled = true; button.textContent = 'Fetching GOES tiles…';
  try {
    const result = await api('/api/admin/goes/ingest', { method: 'POST' });
    const observation = result.observation;
    document.querySelector('#goes-detail').textContent = `Observed ${new Date(observation.observed_utc).toLocaleString()} · motion confidence ${Math.round(observation.motion_confidence * 100)}% · ${(observation.raw_bytes / 1048576).toFixed(1)} MB.`;
    toast('GOES-East cloud correction refreshed');
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = 'Fetch GOES now'; }
});
document.querySelector('#notification-pass').addEventListener('click', async (event) => {
  const button = event.currentTarget; button.disabled = true;
  try { const result = await api('/api/admin/notifications/test', { method: 'POST' }); toast(`${result.sent} sent · ${result.skipped} skipped${result.errors.length ? ` · ${result.errors.length} errors` : ''}`); loadLog(); }
  catch (error) { toast(error.message); } finally { button.disabled = false; }
});
document.querySelector('#send-test-email').addEventListener('click', async (event) => {
  const button = event.currentTarget; const email = document.querySelector('#test-email-address').value.trim();
  if (!email) return toast('Enter the test recipient email address');
  button.disabled = true; button.textContent = 'Sending…';
  try {
    const result = await api('/api/admin/email/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email }) });
    const preview = document.querySelector('#email-preview');
    preview.hidden = false;
    const maps = result.preview.maps_embedded;
    const mapStatus = maps
      ? `Maps: ${maps.detail ? 'close-up ✓' : 'close-up unavailable'} · ${maps.regional ? 'regional ✓' : 'regional unavailable'}\n`
      : (result.preview.map_embedded ? 'Map: embedded forecast map included\n' : 'Map: unavailable for this message\n');
    preview.textContent = `To: ${result.preview.to}\nSubject: ${result.preview.subject}\n${mapStatus}\n${result.preview.body}`;
    toast(result.delivery === 'sent' ? 'Test email sent' : 'SMTP is not configured · preview generated');
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = 'Send test email'; }
});
document.querySelectorAll('[data-scroll]').forEach((button) => button.addEventListener('click', () => document.querySelector(`#${button.dataset.scroll}`).scrollIntoView({ behavior: 'smooth' })));
window.addEventListener('beforeunload', (event) => { if (dirty()) { event.preventDefault(); event.returnValue = ''; } });

function cell(text, className) { const td = document.createElement('td'); td.textContent = text; if (className) td.className = className; return td; }

async function loadSubscribers() {
  const result = await api('/api/admin/subscriptions');
  const counts = result.counts;
  document.querySelector('#subscriber-counts').textContent = `· ${counts.active} active · ${counts.pending} pending · ${counts.unsubscribed} unsubscribed`;
  document.querySelector('#subscriber-stat').textContent = counts.active;
  const rows = document.querySelector('#subscriber-rows');
  rows.innerHTML = '';
  if (!result.subscriptions.length) { rows.innerHTML = '<tr><td colspan="7">No subscribers yet.</td></tr>'; return; }
  result.subscriptions.forEach((s) => {
    const tr = document.createElement('tr');
    tr.appendChild(cell(s.email));
    tr.appendChild(cell(`${s.label || '—'} (${s.latitude.toFixed(3)}, ${s.longitude.toFixed(3)})`));
    tr.appendChild(cell(String(s.threshold)));
    tr.appendChild(cell(s.notification_times.map((t) => (t === 'custom' && s.custom_minutes ? `custom ${s.custom_minutes / 60}h` : t)).join(', ')));
    tr.appendChild(cell(s.status, `status-${s.status}`));
    tr.appendChild(cell(new Date(s.created_at).toLocaleDateString()));
    const actions = document.createElement('td');
    const send = document.createElement('button'); send.textContent = 'Send now';
    send.addEventListener('click', async () => {
      send.disabled = true;
      try { const r = await api(`/api/admin/subscriptions/${s.id}/send`, { method: 'POST' }); toast(`Sent (${r.delivery}) · score ${Math.round(r.score)}`); loadLog(); }
      catch (error) { toast(error.message); } finally { send.disabled = false; }
    });
    const remove = document.createElement('button'); remove.textContent = 'Delete';
    remove.addEventListener('click', async () => {
      if (!window.confirm(`Delete ${s.email} at ${s.label || 'this spot'}? This removes the record entirely.`)) return;
      try { await api(`/api/admin/subscriptions/${s.id}`, { method: 'DELETE' }); toast('Deleted'); loadSubscribers(); }
      catch (error) { toast(error.message); }
    });
    actions.appendChild(send); actions.appendChild(remove);
    tr.appendChild(actions);
    rows.appendChild(tr);
  });
}

async function loadLog() {
  const result = await api('/api/admin/notifications?limit=100');
  const rows = document.querySelector('#log-rows');
  rows.innerHTML = '';
  if (!result.notifications.length) { rows.innerHTML = '<tr><td colspan="6">Nothing sent yet.</td></tr>'; return; }
  result.notifications.forEach((n) => {
    const tr = document.createElement('tr');
    tr.appendChild(cell(new Date(n.sent_at).toLocaleString()));
    tr.appendChild(cell(n.email || `#${n.subscription_id}`));
    tr.appendChild(cell(n.label || '—'));
    tr.appendChild(cell(`${n.event_key} · ${n.forecast_date}`));
    tr.appendChild(cell(n.event_key === 'confirmation' ? '—' : String(Math.round(n.score))));
    tr.appendChild(cell(n.result));
    rows.appendChild(tr);
  });
}

async function loadOutcomes() {
  const result = await api('/api/admin/outcomes');
  const s = result.summary;
  document.querySelector('#outcome-summary').textContent = s.count
    ? `· ${s.count} ratings · average ${s.average_rating.toFixed(1)}/5 · mean predicted ${Math.round(s.average_predicted_score)}`
    : '· none yet';
  const rows = document.querySelector('#outcome-rows');
  rows.innerHTML = '';
  if (!result.outcomes.length) { rows.innerHTML = '<tr><td colspan="7">No ratings yet. Requests go out about 40 minutes after sunset to anyone who was alerted that day.</td></tr>'; return; }
  result.outcomes.forEach((o) => {
    const tr = document.createElement('tr');
    tr.appendChild(cell(new Date(o.created_at).toLocaleString()));
    tr.appendChild(cell(o.email || `#${o.subscription_id}`));
    tr.appendChild(cell(o.label || '—'));
    tr.appendChild(cell(o.forecast_date));
    tr.appendChild(cell(o.predicted_score == null ? '—' : String(Math.round(o.predicted_score))));
    tr.appendChild(cell(`${o.rating} / 5`));
    tr.appendChild(cell(o.comment || ''));
    rows.appendChild(tr);
  });
}

document.querySelector('#send-heartbeat').addEventListener('click', async (event) => {
  const button = event.currentTarget; button.disabled = true;
  try {
    const result = await api('/api/admin/heartbeat', { method: 'POST' });
    const preview = document.querySelector('#email-preview'); preview.hidden = false; preview.textContent = result.body;
    toast(result.delivery === 'sent' ? 'Heartbeat sent' : `Heartbeat ${result.delivery} (set CLOUDSET_ADMIN_EMAIL)`);
  } catch (error) { toast(error.message); } finally { button.disabled = false; }
});

async function init() {
  try {
    const [region, status] = await Promise.all([api('/api/admin/region'), api('/api/admin/status')]);
    state.active = new Set(region.active_cells); state.saved = new Set(region.active_cells);
    state.cells.forEach((_, key) => styleCell(key)); updateUI();
    document.querySelector('#subscriber-stat').textContent = status.subscribers;
    const warnings = document.querySelector('#config-warnings');
    if (status.warnings && status.warnings.length) { warnings.hidden = false; warnings.textContent = `Configuration: ${status.warnings.join(' · ')}`; }
    const problems = document.querySelector('#problems');
    if (status.problems && status.problems.length) { problems.hidden = false; problems.textContent = `Needs attention: ${status.problems.join(' · ')}`; }
    loadSubscribers().catch((error) => toast(error.message));
    loadLog().catch((error) => toast(error.message));
    loadOutcomes().catch((error) => toast(error.message));
    document.querySelector('#mode-stat').textContent = status.mode.toUpperCase();
    document.querySelector('#provider-stat').textContent = status.provider;
    document.querySelector('#hrrr-detail').textContent = status.mode === 'live'
      ? `${status.model_run}. Compact snapshot is ready for the active forecast footprint.`
      : 'No current HRRR snapshot is loaded. The public map is using its labeled demo fallback.';
    document.querySelector('#goes-detail').textContent = status.goes.available
      ? `Latest observation ${new Date(status.goes.observed_utc).toLocaleString()} · ${Math.round(status.goes.age_minutes)} minutes old${status.goes.correction ? ` · active at ${Math.round(status.goes.correction.weight * 100)}% weight` : ' · retained for the next sunset window'}.`
      : 'No compact GOES observation has been ingested yet. HRRR remains the active baseline.';
    document.querySelector('#smtp-detail').textContent = status.smtp_configured ? 'SMTP is configured. The scheduler checks eligible subscribers every 15 minutes.' : 'SMTP is not configured. Notification messages are logged safely instead of sent.';
    if (status.last_run) { document.querySelector('#runtime-stat').textContent = `${Math.round(status.last_run.duration_ms)} ms`; document.querySelector('#run-detail').textContent = `Last run #${status.last_run.id} completed ${status.last_run.cells} cells.`; }
    else document.querySelector('#runtime-stat').textContent = 'Not run';
  } catch (error) { toast(error.message); }
}
init();
