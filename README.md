# Travel Archive

A self-hosted travel memory app — pin your trips on an interactive world map, attach photos and videos, and browse everything in a clean viewer. Runs as a single Python server with no external database.

---

## Features

- **Interactive world map** (Leaflet) with Points of Interest (POIs), routes, and folders
- **Admin interface** (`world_map_app.html`) for creating and managing all content
- **Viewer interface** (`viewer.html`) for read-only browsing — shareable with others
- **Media support** — upload images and videos directly, or link to files already on the server
- **Video streaming** — native browser formats served with HTTP range support; AVI/MKV/etc. transcoded on-the-fly via ffmpeg or VLC
- **Permanent transcoding** — convert non-native video files to MP4 via the UI
- **File browser** — navigate server-side directories to import video folders
- **Multi-user** — multiple clients can connect simultaneously to the Python server
- **Docker-ready** — includes Dockerfile, docker-compose, and optional Caddy HTTPS proxy
- **SSH emergency access** — container exposes SSH on port 2222 for remote management

---

## Quick Start (local)

**Requirements:** Python 3.8+, optionally `ffmpeg` for video transcoding.

```bash
git clone <this-repo>
cd travelapp-main
python server.py
```

The app opens automatically at `http://localhost:8000`.

- Admin (create/edit): `http://localhost:8000/`
- Viewer (read-only): `http://localhost:8000/viewer.html`

---

## Docker Deployment

See [DOCKER.md](DOCKER.md) for the full guide. Quick version:

```bash
# 1. Copy your SSH public key into docker-compose.yml → AUTHORIZED_KEYS
# 2. Set the path to your video library in the volumes section
docker compose up -d
```

| Port | Purpose |
|------|---------|
| `8000` | Web interface |
| `2222` | SSH emergency access |

To enable HTTPS with a domain, uncomment the Caddy block in `docker-compose.yml` and fill in `Caddyfile.conf`.

### One-shot installer (Ubuntu/Debian)

```bash
sudo ./setup.sh
```

Installs Docker, creates a `travel` system user, and starts the container.

---

## File Layout

```
travelapp-main/
├── server.py               # Python HTTP server + REST API
├── world_map_app.html      # Admin UI
├── viewer.html             # Public viewer UI
├── data/
│   ├── pois.json           # Points of interest
│   ├── folders.json        # Folder groups
│   ├── routes.json         # Map routes
│   └── media/              # Uploaded files (auto-created)
├── Dockerfile
├── docker-compose.yml
├── Caddyfile.conf          # Optional HTTPS reverse proxy config
├── docker-entrypoint.sh
└── setup.sh                # One-shot server installer
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/ping` | Health check |
| GET | `/api/all` | All folders, POIs, and routes |
| GET/POST | `/api/pois` | List / create POIs |
| PUT/DELETE | `/api/pois/<id>` | Update / delete a POI |
| GET/POST | `/api/folders` | List / create folders |
| DELETE | `/api/folders/<id>` | Delete a folder |
| GET/POST | `/api/routes` | List / create routes |
| PUT/DELETE | `/api/routes/<id>` | Update / delete a route |
| GET | `/api/videos` | List media directory videos |
| GET | `/api/browse/<b64path>` | Browse server directories |
| GET | `/api/scan/<b64path>` | Scan a directory for video files |
| GET | `/api/stream/<b64path>` | Stream a video (range-aware) |
| POST | `/api/link` | Copy a server-side file into media |
| POST | `/api/relink` | Bulk update file path prefixes |
| POST | `/api/transcode` | Permanently transcode video to MP4 |
| POST | `/api/play` | Launch VLC on the server to play locally |
| GET | `/api/is-local` | Check if the client is on localhost |

---

## Video Support

| Format | Handling |
|--------|---------|
| `.mp4`, `.webm`, `.ogg`, `.m4v` | Served directly with HTTP range support |
| `.avi`, `.mkv`, `.mov`, `.flv`, `.wmv`, `.ts`, `.mkv`, and others | Transcoded on-the-fly via ffmpeg (preferred) or VLC (fallback) |

ffmpeg and VLC are auto-detected at startup. On the Docker image, ffmpeg is pre-installed.

---

## Configuration

Environment variables (set in `docker-compose.yml` or shell):

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8000` | HTTP port |
| `APP_DIR` | script directory | App root |
| `AUTHORIZED_KEYS` | _(empty)_ | SSH public key for container access |

Setup script variables:

| Variable | Default |
|----------|---------|
| `APP_DIR` | `/opt/travel-archive` |
| `APP_USER` | `travel` |
| `WEB_PORT` | `8000` |
| `SSH_PORT` | `2222` |
| `FILMS_PATH` | `/mnt/films` |

---

## Tech Stack

- **Backend:** Python 3 standard library (`http.server`) — no framework, no database
- **Frontend:** Vanilla JS + [Leaflet](https://leafletjs.com/) for maps
- **Fonts:** DM Sans, DM Mono, Playfair Display (Google Fonts)
- **Video transcoding:** ffmpeg / VLC
- **Reverse proxy (optional):** Caddy
- **Container:** Docker + Docker Compose
