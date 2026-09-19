const params = new URLSearchParams(window.location.search);
const token = params.get('token') || '';
const page = window.location.pathname.replace(/\/+$/, '') || '/manage';
const statusKicker = document.querySelector('#status-kicker');
const statusTitle = document.querySelector('#status-title');
const statusText = document.querySelector('#status-text');
const statusActions = document.querySelector('#status-actions');
const watchList = document.querySelector('#watch-list');

function setStatus(kicker, title, text, actions = []) {
  statusKicker.textContent = kicker;
  statusTitle.textContent = title;
  statusText.textContent = text;
  statusActions.innerHTML = '';
  statusActions.hidden = actions.length === 0;
  actions.forEach(({ label, href, onClick, primary }) => {
    const el = document.createElement(href ? 'a' : 'button');
    el.className = primary ? 'primary-button' : 'ghost-button dark';
    el.textContent = label;
    if (href) el.href = href; else { el.type = 'button'; el.addEventListener('click', onClick); }
    statusActions.appendChild(el);
  });
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail?.[0]?.msg || body.detail || `Request failed (${response.status})`);
  return body;
}

function coords(lat, lon) {
  return `${Math.abs(lat).toFixed(3)}° ${lat >= 0 ? 'N' : 'S'}, ${Math.abs(lon).toFixed(3)}° ${lon >= 0 ? 'E' : 'W'}`;
}

function renderWatch(watch) {
  const node = document.querySelector('#watch-template').content.firstElementChild.cloneNode(true);
  node.dataset.id = watch.id;
  node.querySelector('.watch-label').textContent = watch.label || 'Unnamed spot';
  node.querySelector('.watch-coords').textContent = coords(watch.latitude, watch.longitude);
  const status = node.querySelector('.watch-status');
  status.textContent = watch.status === 'active' ? 'ACTIVE' : 'AWAITING CONFIRMATION';
  status.classList.toggle('muted', watch.status !== 'active');
  node.querySelector('[name=label]').value = watch.label || '';
  node.querySelector('[name=latitude]').value = watch.latitude;
  node.querySelector('[name=longitude]').value = watch.longitude;
  node.querySelector('[name=threshold]').value = watch.threshold;
  node.querySelector('.threshold-output').textContent = watch.threshold;
  node.querySelectorAll('[name=notification-time]').forEach((input) => { input.checked = watch.notification_times.includes(input.value); });
  const customBox = node.querySelector('.custom-time');
  customBox.hidden = !watch.notification_times.includes('custom');
  if (watch.custom_minutes) node.querySelector('[name=custom-hours]').value = watch.custom_minutes / 60;

  node.querySelector('[name=threshold]').addEventListener('input', (event) => { node.querySelector('.threshold-output').textContent = event.target.value; });
  node.querySelector('.custom-toggle').addEventListener('change', (event) => { customBox.hidden = !event.target.checked; });
  const note = node.querySelector('.watch-note');

  node.addEventListener('submit', async (event) => {
    event.preventDefault();
    const times = [...node.querySelectorAll('[name=notification-time]:checked')].map((input) => input.value);
    if (!times.length) { note.textContent = 'Choose at least one notification time.'; return; }
    const customMinutes = times.includes('custom') ? Math.round(Number(node.querySelector('[name=custom-hours]').value) * 60) : null;
    const button = node.querySelector('[type=submit]');
    button.disabled = true; note.textContent = 'Saving…';
    try {
      const result = await api(`/api/manage/${watch.id}?token=${encodeURIComponent(token)}`, {
        method: 'PUT',
        body: JSON.stringify({
          label: node.querySelector('[name=label]').value.trim(),
          latitude: Number(node.querySelector('[name=latitude]').value),
          longitude: Number(node.querySelector('[name=longitude]').value),
          threshold: Number(node.querySelector('[name=threshold]').value),
          notification_times: times,
          custom_minutes: customMinutes,
        }),
      });
      node.querySelector('.watch-label').textContent = result.subscription.label || 'Unnamed spot';
      node.querySelector('.watch-coords').textContent = coords(result.subscription.latitude, result.subscription.longitude);
      note.textContent = 'Saved.';
    } catch (error) { note.textContent = error.message; }
    finally { button.disabled = false; }
  });

  node.querySelector('[data-action=remove]').addEventListener('click', async () => {
    if (!window.confirm(`Stop watching ${watch.label || 'this spot'}?`)) return;
    try {
      await api(`/api/manage/${watch.id}?token=${encodeURIComponent(token)}`, { method: 'DELETE' });
      node.remove();
      if (!document.querySelector('.watch-card')) setStatus('SUNSET WATCH', 'No watches left', 'You will not receive any more alerts. Sign up again from the map any time.', [{ label: 'Back to the map', href: '/', primary: true }]);
    } catch (error) { note.textContent = error.message; }
  });
  return node;
}

async function loadWatches(intro) {
  const result = await api(`/api/manage?token=${encodeURIComponent(token)}`);
  document.querySelector('#watch-email').textContent = result.email;
  const container = document.querySelector('#watches');
  container.innerHTML = '';
  result.subscriptions.forEach((watch) => container.appendChild(renderWatch(watch)));
  watchList.hidden = false;
  if (intro) setStatus(intro.kicker, intro.title, intro.text, intro.actions || []);
  document.querySelector('#unsubscribe-all-wrap').hidden = result.subscriptions.length < 2;
}

document.querySelector('#unsubscribe-all').addEventListener('click', async () => {
  if (!window.confirm('Unsubscribe every watch for this email address?')) return;
  try {
    await api('/api/unsubscribe', { method: 'POST', body: JSON.stringify({ token, everything: true }) });
    watchList.hidden = true;
    setStatus('UNSUBSCRIBED', 'You are fully unsubscribed', 'No more emails from Cloudset. Thanks for trying it.', [{ label: 'Back to the map', href: '/', primary: true }]);
  } catch (error) { setStatus('SOMETHING WENT WRONG', 'Could not unsubscribe', error.message); }
});

async function init() {
  if (!token) {
    setStatus('SUNSET WATCH', 'This link is missing its key', 'Open the link from one of your Cloudset emails. Every alert has a “Change this watch” link at the bottom.', [{ label: 'Back to the map', href: '/', primary: true }]);
    return;
  }
  try {
    if (page === '/confirm') {
      const result = await api('/api/confirm', { method: 'POST', body: JSON.stringify({ token }) });
      const s = result.subscription;
      await loadWatches({ kicker: 'YOU’RE ON THE WATCHLIST', title: `Watching ${s.label || 'your spot'} for fiery sunsets`, text: `We’ll email ${s.email} when the score clears ${s.threshold}. You can fine-tune everything below.` });
    } else if (page === '/unsubscribe') {
      setStatus('SUNSET WATCH', 'Unsubscribe from this watch?', 'You can also just adjust the alert threshold or timing instead.', [
        { label: 'Yes, unsubscribe', primary: true, onClick: async () => {
          try {
            const result = await api('/api/unsubscribe', { method: 'POST', body: JSON.stringify({ token }) });
            watchList.hidden = true;
            setStatus('UNSUBSCRIBED', `Stopped watching ${result.label || 'that spot'}`, 'No more alerts for this location. Sign up again from the map any time.', [{ label: 'Back to the map', href: '/', primary: true }]);
          } catch (error) { setStatus('SOMETHING WENT WRONG', 'Could not unsubscribe', error.message); }
        } },
        { label: 'Adjust it instead', onClick: () => loadWatches({ kicker: 'SUNSET WATCH', title: 'Your watches', text: 'Change the location, timing, or threshold and save.' }) },
      ]);
    } else {
      await loadWatches({ kicker: 'SUNSET WATCH', title: 'Your watches', text: 'Change the location, timing, or threshold and save. Changes apply to the next alert.' });
    }
  } catch (error) {
    setStatus('SOMETHING WENT WRONG', 'This link didn’t work', error.message, [{ label: 'Back to the map', href: '/', primary: true }]);
  }
}
init();
