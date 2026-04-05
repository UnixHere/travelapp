# Travel Archive — Docker Deployment Guide

## What you get

| Container port | Host port | Purpose |
|---|---|---|
| `8000` | `8000` | Web app (viewer + admin) |
| `22` | `2222` | SSH emergency access into container |

Your video files and POI data are stored **outside** the container in bind-mounted folders, so they survive rebuilds and updates.

---

## File layout

Put all these files in one folder on your server, e.g. `/opt/travel-archive`:

```
/opt/travel-archive/
├── server.py
├── world_map_app.html
├── viewer.html
├── Dockerfile
├── docker-entrypoint.sh
├── docker-compose.yml
├── Caddyfile              ← only needed if you want HTTPS with a domain
├── setup.sh               ← optional one-shot installer
└── data/                  ← created automatically, back this up
    └── media/             ← uploaded video/image files live here
```

---

## Step 1 — Install Docker

On Ubuntu/Debian:

```bash
curl -fsSL https://get.docker.com | bash
```

On any other distro follow https://docs.docker.com/engine/install/

Verify:
```bash
docker --version
docker compose version
```

---

## Step 2 — Edit `docker-compose.yml`

Open `docker-compose.yml` and change these three things:

### 2a — Your films folder path

Find this line:
```yaml
- /path/to/your/films:/films:ro
```

Change the left side to where your videos actually live, for example:
```yaml
- /mnt/nas/movies:/films:ro
```

Inside the container (and in the admin file browser) your films will appear under `/films`.  
You can add as many extra mounts as you need:
```yaml
- /mnt/nas/movies:/films:ro
- /home/user/recordings:/recordings:ro
```

### 2b — Ports (optional)

Default is web on `8000`, SSH on `2222`. Change the **left** numbers if those ports are already in use:
```yaml
ports:
  - "9000:8000"   # now accessible on port 9000
  - "2223:22"     # SSH on port 2223
```

### 2c — SSH public key

So you can SSH into the container for emergency access, paste your public key:

```yaml
environment:
  AUTHORIZED_KEYS: "ssh-ed25519 AAAAC3Nza... your-name@machine"
```

Get your public key with:
```bash
cat ~/.ssh/id_ed25519.pub   # or id_rsa.pub
```

If you don't have an SSH key pair yet:
```bash
ssh-keygen -t ed25519 -C "travel-archive"
cat ~/.ssh/id_ed25519.pub
```

---

## Step 3 — Build and start

```bash
cd /opt/travel-archive

# Make the entrypoint executable
chmod +x docker-entrypoint.sh

# Build image and start (first build takes ~2 minutes — downloads ffmpeg)
docker compose up -d --build

# Check it started OK
docker compose logs -f
```

Open in browser: `http://your-server-ip:8000/admin`

---

## Step 4 — Autostart on reboot (systemd)

```bash
# Create the service
sudo tee /etc/systemd/system/travel-archive.service > /dev/null << 'EOF'
[Unit]
Description=Travel Archive
After=docker.service
Requires=docker.service

[Service]
WorkingDirectory=/opt/travel-archive
ExecStart=/usr/bin/docker compose up --build
ExecStop=/usr/bin/docker compose down
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now travel-archive
```

---

## Step 5 — HTTPS with a domain (optional but recommended for public servers)

You need: a domain name pointing at your server's IP (`A` record in your DNS).

### 5a — Edit `Caddyfile`

Replace `travel.example.com` with your actual domain:
```
travel.yourdomain.com {
    reverse_proxy travel-archive:8000
    ...
}
```

### 5b — Uncomment Caddy in `docker-compose.yml`

Find the commented-out `caddy:` block and uncomment the whole thing (remove the `#` from each line).

### 5c — Open ports 80 and 443

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```

### 5d — Restart

```bash
docker compose up -d
```

Caddy automatically gets a free Let's Encrypt certificate. Your site will be at `https://travel.yourdomain.com`.

---

## SSH emergency access

If the web server crashes or you can't reach the admin panel, SSH directly into the container:

```bash
ssh root@your-server-ip -p 2222
```

Inside the container you can:
```bash
# Check what's running
ps aux

# Restart the Python server manually
python3 /app/server.py

# Check logs
cat /var/log/...

# Look at your data
ls /app/data/
ls /films/
```

The SSH host key is persisted in the `ssh-host-keys` Docker volume, so you won't get fingerprint warnings after rebuilds.

---

## Day-to-day commands

```bash
# View live logs
docker compose logs -f

# Restart the app (e.g. after updating server.py)
docker compose restart travel-archive

# Rebuild after changing server.py or HTML files
docker compose up -d --build

# Stop everything
docker compose down

# Stop and delete all data volumes (DESTRUCTIVE — wipes media too)
docker compose down -v
```

---

## Updating the app

```bash
cd /opt/travel-archive

# Copy new server.py / HTML files here, then:
docker compose up -d --build
```

Data in `./data/` is never touched by rebuilds.

---

## Backup

The only folder you need to back up is `./data/`:

```bash
# Simple tar backup
tar -czf travel-archive-backup-$(date +%Y%m%d).tar.gz /opt/travel-archive/data/

# Or rsync to another machine
rsync -av /opt/travel-archive/data/ user@backup-server:/backups/travel-archive/
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Port already in use | Change the left port number in `docker-compose.yml` |
| Films not showing in browser | Check the bind-mount path in `docker-compose.yml` — left side must exist on host |
| Video won't play (AVI/MKV) | ffmpeg is included — check `docker compose logs` for ffmpeg errors |
| SSH connection refused | Make sure `AUTHORIZED_KEYS` is set and port 2222 is open in your firewall |
| Site not accessible | Run `docker compose ps` to check container is running; check firewall with `ufw status` |
| HTTPS not working | Make sure your domain's A record points at this server's IP; ports 80+443 must be open |

---

## Do you actually need Docker?

**Without Docker** — your Python server already handles multiple users simultaneously (uses `ThreadingMixIn`). Just run `python3 server.py` and it works fine for personal/small-team use.

**With Docker you additionally get:**
- Automatic restart if the server crashes
- Starts automatically on server reboot (via systemd)
- Isolated ffmpeg version — won't conflict with anything else on the server
- SSH emergency access into the container
- Easy HTTPS via Caddy
- Cleaner updates — just copy new files and `docker compose up -d --build`
