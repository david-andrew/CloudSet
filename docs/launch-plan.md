# Launch plan

Goal: take the working prototype and make it something strangers can use, at a cost of roughly one coffee a month, with no new infrastructure beyond one small VM.

Current state (2026-09-18): 25 tests pass. Uncommitted work includes the GOES-East correction, smoke AOD scoring, and UI tweaks. Nothing below assumes those are thrown away.

## Hosting recommendation

**Use one small cloud VM, not the Raspberry Pi.** The Pi would work technically (the app was designed for it), but three things make it a poor public host:

- Residential IPs are blocked or spam-scored by most mail receivers, so notification emails would need to relay through a provider anyway.
- Exposing a home network means port forwarding or a tunnel, plus dealing with ISP outages and dynamic IPs. Cloudflare Tunnel mitigates the exposure but adds a moving part.
- If the Pi reboots or the SD card dies, subscribers silently stop getting alerts.

**DigitalOcean droplet, 1 vCPU / 1 GB, $6 per month, plus 2 GB of swap.** Upgrade to the 2 GB tier ($12) only if the HRRR decode step gets OOM-killed. Hetzner CX22 is about half the price if you don't care about the DO ecosystem. Avoid DO App Platform: it has no persistent disk, and this app keeps SQLite and cached weather fields on disk.

Expected monthly cost:

| Item | Cost |
| --- | --- |
| Droplet | $6 |
| Domain | ~$1 (billed yearly) |
| Email provider | $0 on free tier |
| Uptime monitoring | $0 (UptimeRobot free) |

### Deployment shape

- Single `Dockerfile` (uv-based) plus `docker-compose.yml` with two services: the app and Caddy for automatic HTTPS. Caddy also gives us per-IP rate limiting and keeps `/admin` off the public internet by IP allowlist or basic auth.
- `data/` mounted as a named volume. That is the only state.
- **Exactly one uvicorn worker.** The notification, HRRR, and GOES schedulers run inside the web process and use in-memory caches. A second worker would double-send emails and double-download from NOMADS.
- A nightly cron on the host runs `sqlite3 data/cloudset.db ".backup ..."` and keeps the last 14 copies. The database is tiny. Optionally rclone them to a DO Space.
- Logs stay in journald / `docker logs`. No log shipping.
- `deploy/` gets a short runbook: create droplet, install Docker, clone, copy `.env`, `docker compose up -d`, point DNS.

### Email sending

Keep the current smtplib code. Change only the credentials to a transactional provider that offers SMTP:

- **Resend** (3,000 per month free, 100 per day) or **Brevo** (300 per day free). Either is fine. Amazon SES is cheapest at scale but has more setup.
- Do not use a Gmail app password. Gmail throttles and may lock the account.
- Set SPF, DKIM, and DMARC records for the sending domain. Without these, HTML emails with embedded images land in spam.
- Keep sending in-process on the 15-minute loop. At the current scale (dozens to low hundreds of subscribers) there is no reason for a queue or worker service. The one change worth making: run each subscriber's send in a small thread pool with a per-send timeout so one slow SMTP call cannot stall the whole pass.

## Phase 0: commit what exists

Commit the pending GOES, AOD, and UI changes. Tag it `v0.1-prototype` so there is a clean point to compare against.

## Phase 1: production hardening (blocking for public launch)

1. **Admin token is mandatory.** Today an empty `CLOUDSET_ADMIN_TOKEN` leaves the control room wide open. Refuse to start in non-demo mode without one, or bind admin routes to localhost only.
2. **Tighten CORS** to the public origin. The API is same-origin now, so `*` buys nothing.
3. **Rate limit `/api/subscriptions`** (Caddy or a small in-memory limiter). Also cap active subscriptions per email address.
4. **Double opt-in.** Signup sends a confirmation email with a signed link. Nothing is delivered until confirmed. This is the single biggest protection against someone signing up strangers, and providers require it for a clean sender reputation.
5. **Signed subscription tokens.** An HMAC over the subscription id with a server secret, embedded in every email as a manage link and an unsubscribe link. No passwords, no accounts.
6. **`List-Unsubscribe` and `List-Unsubscribe-Post` headers** on every notification. Gmail and Yahoo require this for bulk senders and it makes the one-click unsubscribe button appear.
7. **Manage page** (`/manage?token=...`): shows every subscription for that email, lets the user edit location, threshold, and times, pause, or delete.
8. **Config validation at startup**: warn loudly if `CLOUDSET_PUBLIC_URL` is still a LAN address, if SMTP is unset in live mode, etc.
9. Privacy note and a "what we store" line in the about dialog. Only email and coordinates are stored.

## Phase 2: notification behaviour

1. **Countdown in the subject and headline.** "78% sunset potential in 3 hours" instead of just the score. Compute from sunset time minus send time.
2. **Downgrade updates.** Record the score with each sent event (the table already has it). At each later due event for the same forecast date, if a prior alert was sent and the score is now below threshold, send a short "it fizzled" email instead of silently skipping. Send it at most once per date.
3. **Upgrade updates** are the mirror case and cost nothing extra: if the morning score was below threshold but the final-90 score is above, that already sends today.
4. Move `America/New_York` into a setting so a future West Coast footprint doesn't require code changes. The morning window (8 to 12) should be interpreted in that zone.
5. Email footer: manage link, unsubscribe link, donation link, attribution.

## Phase 3: UI pass

Public map:

1. **Two overlays with two palettes.**
   - Sunset potential: sequential blue to green (cool, clearly "a score"), thresholded so anything below the user's cutoff is fully transparent. Default cutoff 60, adjustable with a slider in the legend.
   - Cloud canvas: mid and high cloud fraction from HRRR in the existing fiery ramp, so the warm colours mean "here are the clouds that would light up".
   - Tiles take a `layer` and a quantised `min` parameter (steps of 5) so the disk cache stays bounded.
2. **Overlay toggle**: potential / clouds / none, so the satellite or road view is usable on its own.
3. **Remember state in localStorage**: last location and label, selected day, base layer, overlay, threshold. URL parameters from emails still win.
4. **Mobile bottom sheet done properly**: a visible drag handle, tap or swipe to open and close, a backdrop that closes it, and the "View forecast" button becomes a toggle instead of open-only.
5. **Type sizes.** Most labels are 8 to 10 px. Raise the floor to 12 px and re-check the panel at 375 px wide.
6. **Location search** by place name (Nominatim, with the required User-Agent) so people don't have to click around the map. Keep the click-to-move behaviour.
7. **Donate button** in the topbar and about dialog: Ko-fi or Buy Me a Coffee link. Ko-fi takes no fee. No integration needed, just a link.
8. Small things: favicon, Open Graph tags, a proper `<title>` per page, replace the "unsubscribe support is next" note, pin the Leaflet CDN with integrity hashes or vendor it under `static/`.

Admin:

1. Subscriber list with confirmed / pending / unsubscribed counts and a per-row "send test" button.
2. Recent notification log (last 50 sends, with result).
3. Nothing else. The control room already does its job.

## Phase 4: launch

1. Buy the domain. Set A record to the droplet and the SPF / DKIM / DMARC records from the email provider.
2. Deploy, then run the admin test email to a Gmail and an Outlook address and check they land in the inbox with images rendering.
3. Add the site to UptimeRobot on `/api/health`.
4. Subscribe yourself at two thresholds and watch a full day cycle before sharing the link.
5. Soft launch: share with a handful of people, watch NOMADS and GIBS error rates in the logs for a week.

## Things to watch but not fix now

- **Basemap terms.** Esri World Imagery without an API key is tolerated but not licensed for production; OSM's public tile servers ask that heavy apps run their own. At small traffic this is fine. If usage grows, switch the road layer to MapTiler or Protomaps (free tiers) and the satellite layer to an Esri key.
- **NOMADS reliability.** It rate-limits and has outages. The demo fallback handles the UI, but the notifier correctly refuses to send on demo data, so an extended outage means silence. The AWS HRRR mirror with byte-range index reads is the fallback if this becomes a problem.
- **Observed-outcome feedback** ("was it fiery?") is the next real product feature after launch and is the prerequisite for any calibration work described in `forecasting.md`.

## Suggested order and rough effort

| Phase | Effort |
| --- | --- |
| 0 commit | minutes |
| 1 hardening | 1 to 2 days |
| 2 notifications | half a day |
| 3 UI | 2 to 3 days |
| 4 launch | half a day plus DNS wait |
