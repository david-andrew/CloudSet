# Deploying Cloudset

One small VM runs everything: the app container (FastAPI plus the notification
and weather schedulers) and a Caddy container that terminates HTTPS. SQLite and
cached weather data live in `./data` on the host. Nothing else is required.

Tested target: DigitalOcean droplet, Ubuntu 24.04, 1 GB RAM plus 2 GB swap.

## 1. One-time server setup

SSH in as root the first time (DigitalOcean gives you root with your SSH key).

```bash
# Create an unprivileged user that owns the app.
adduser --disabled-password --gecos "" cloudset
usermod -aG sudo cloudset
mkdir -p /home/cloudset/.ssh
cp ~/.ssh/authorized_keys /home/cloudset/.ssh/
chown -R cloudset:cloudset /home/cloudset/.ssh
echo "cloudset ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/cloudset

# Swap: the HRRR decode step is the memory peak and 1 GB alone is tight.
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
sysctl vm.swappiness=10 && echo 'vm.swappiness=10' >> /etc/sysctl.conf

# Firewall: only SSH and web.
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp && ufw --force enable

# Unattended security updates.
apt-get update && apt-get install -y unattended-upgrades git
dpkg-reconfigure -f noninteractive unattended-upgrades

# Docker.
curl -fsSL https://get.docker.com | sh
usermod -aG docker cloudset
```

Log out and back in as `cloudset` from now on:

```bash
ssh cloudset@137.184.96.145
```

## 2. Install the app

```bash
git clone https://github.com/YOUR_USER/cloudset.git ~/cloudset
cd ~/cloudset
cp .env.example .env
nano .env        # paste the real values (see the checklist below)
mkdir -p data
docker compose up -d --build
```

The first build takes a few minutes. Watch it come up:

```bash
docker compose logs -f app
```

You should see "Application startup complete" and then, within a minute or so,
"HRRR refresh complete". Caddy fetches the certificate automatically the first
time someone visits the domain, which can take about ten seconds.

Check from your laptop:

```bash
curl https://cloudset.dev/api/health
```

## 3. `.env` checklist

| Variable | What to put |
| --- | --- |
| `CLOUDSET_ENV` | `production` |
| `CLOUDSET_DOMAIN` | `cloudset.dev` |
| `CLOUDSET_PUBLIC_URL` | `https://cloudset.dev` |
| `CLOUDSET_ADMIN_TOKEN` | `openssl rand -hex 24` |
| `CLOUDSET_SECRET_KEY` | `openssl rand -hex 24`, never change it once emails have gone out |
| `CLOUDSET_ADMIN_ALLOW` | your home IP, e.g. `73.132.82.112/32` (find it with `curl -4 ifconfig.me`) |
| `CLOUDSET_FORECAST_MODE` | `auto` |
| `CLOUDSET_SMTP_PASSWORD` | the Resend API key |
| `CLOUDSET_DONATE_URL` | your Ko-fi page |

In production the app refuses to start if a secret is missing or the public
URL isn't https. The reason is printed in `docker compose logs app`.

## 4. First checks after deploy

1. Open `https://cloudset.dev` and confirm the score overlay appears and the
   status pill says "Live".
2. Open `https://cloudset.dev/admin` from home, paste the admin token when
   asked, and send a test email to yourself. Check it lands in the inbox with
   both maps.
3. Sign up for a watch with your own address and click the confirmation link.
4. Add `https://cloudset.dev/api/health` to UptimeRobot.
5. Install the backup cron:

   ```bash
   crontab -e
   # add:
   15 4 * * * /home/cloudset/cloudset/deploy/backup.sh >> /home/cloudset/backups/backup.log 2>&1
   ```

## 5. Everyday operations

Deploy a new version:

```bash
cd ~/cloudset && git pull && docker compose up -d --build
```

Logs:

```bash
docker compose logs -f app       # application, schedulers, email sends
docker compose logs -f caddy     # HTTP access and certificate events
```

Restart just the app:

```bash
docker compose restart app
```

Your home IP changed and `/admin` gives a 404:

```bash
nano .env                        # update CLOUDSET_ADMIN_ALLOW
docker compose up -d caddy       # Caddy re-reads .env on recreate
```

Locked out anyway? The app port is bound to the droplet's loopback, so an SSH
tunnel bypasses the IP allowlist:

```bash
ssh -L 8080:127.0.0.1:8080 cloudset@137.184.96.145
# then open http://localhost:8080/admin on your laptop
```

Restore a backup:

```bash
docker compose stop app
gunzip -c ~/backups/cloudset-YYYYMMDD-HHMMSS.db.gz > data/cloudset.db
docker compose start app
```

## 6. Sizing notes

- The app container is capped at 900 MB. If `docker stats` shows it hitting
  that during the hourly HRRR refresh, resize the droplet to 2 GB.
- `data/` grows to roughly 100 MB and prunes itself (old HRRR files, tile
  cache namespaces, GOES frames). The SQLite database stays tiny.
- Run exactly one app container. The schedulers live inside it; two copies
  would send every email twice.
