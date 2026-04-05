#!/bin/bash
# setup.sh — Deploy Travel Archive on a fresh Ubuntu/Debian server
# Run as root or with sudo.
#
# Usage:
#   curl -fsSL https://your-server/setup.sh | bash
#   OR
#   chmod +x setup.sh && sudo ./setup.sh

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[setup]${NC} $*"; }
ok()    { echo -e "${GREEN}[ok]${NC} $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC} $*"; }
die()   { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

# ── Root check ───────────────────────────────────────────────────────────────
[ "$(id -u)" -eq 0 ] || die "Run as root or with sudo"

# ── Config ───────────────────────────────────────────────────────────────────
APP_DIR="${APP_DIR:-/opt/travel-archive}"
APP_USER="${APP_USER:-travel}"
WEB_PORT="${WEB_PORT:-8000}"
SSH_PORT="${SSH_PORT:-2222}"
FILMS_PATH="${FILMS_PATH:-/mnt/films}"

info "=== Travel Archive — Server Setup ==="
info "Install dir : $APP_DIR"
info "Web port    : $WEB_PORT"
info "SSH port    : $SSH_PORT  (container SSH — for emergency access)"
info "Films path  : $FILMS_PATH"
echo ""

# ── Install Docker ───────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    info "Installing Docker…"
    apt-get update -qq
    apt-get install -y --no-install-recommends ca-certificates curl gnupg lsb-release
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
        gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable --now docker
    ok "Docker installed"
else
    ok "Docker already installed ($(docker --version))"
fi

# ── Create app directory ─────────────────────────────────────────────────────
info "Creating $APP_DIR…"
mkdir -p "$APP_DIR"/data/media
mkdir -p "$FILMS_PATH"

# Copy app files if running from the repo directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for f in server.py world_map_app.html viewer.html Dockerfile docker-entrypoint.sh docker-compose.yml Caddyfile; do
    if [ -f "$SCRIPT_DIR/$f" ]; then
        cp "$SCRIPT_DIR/$f" "$APP_DIR/$f"
        ok "Copied $f"
    else
        warn "$f not found in $SCRIPT_DIR — make sure to copy it manually to $APP_DIR"
    fi
done

# ── Patch docker-compose.yml with actual paths and ports ────────────────────
info "Patching docker-compose.yml…"
sed -i "s|8000:8000|${WEB_PORT}:8000|g"                 "$APP_DIR/docker-compose.yml"
sed -i "s|2222:22|${SSH_PORT}:22|g"                      "$APP_DIR/docker-compose.yml"
sed -i "s|/path/to/your/films|${FILMS_PATH}|g"           "$APP_DIR/docker-compose.yml"
ok "docker-compose.yml patched"

# ── SSH key for container access ─────────────────────────────────────────────
info "SSH key setup…"
echo ""
echo -e "${YELLOW}Paste your SSH public key for container emergency access${NC}"
echo -e "${YELLOW}(contents of ~/.ssh/id_ed25519.pub or id_rsa.pub on your local machine)${NC}"
echo -e "${YELLOW}Leave empty to skip — you can set AUTHORIZED_KEYS in docker-compose.yml later${NC}"
echo ""
read -rp "Public key: " SSH_PUBKEY
if [ -n "$SSH_PUBKEY" ]; then
    # Inject into docker-compose.yml AUTHORIZED_KEYS env
    # Escape special chars for sed
    ESCAPED=$(printf '%s\n' "$SSH_PUBKEY" | sed 's/[\/&]/\\&/g')
    sed -i "s|AUTHORIZED_KEYS: \"\"|AUTHORIZED_KEYS: \"${ESCAPED}\"|" "$APP_DIR/docker-compose.yml"
    ok "SSH key injected into docker-compose.yml"
else
    warn "No SSH key set — edit AUTHORIZED_KEYS in $APP_DIR/docker-compose.yml before deploying"
fi

# ── Firewall (ufw) ───────────────────────────────────────────────────────────
if command -v ufw &>/dev/null; then
    info "Configuring firewall (ufw)…"
    ufw --force enable
    ufw allow 22/tcp    comment "Host SSH"
    ufw allow "$WEB_PORT"/tcp comment "Travel Archive web"
    ufw allow "$SSH_PORT"/tcp comment "Travel Archive container SSH"
    ok "Firewall configured"
else
    warn "ufw not found — make sure port $WEB_PORT and $SSH_PORT are open on your server"
fi

# ── systemd service for auto-start on boot ───────────────────────────────────
info "Installing systemd service…"
cat > /etc/systemd/system/travel-archive.service << EOF
[Unit]
Description=Travel Archive (Docker Compose)
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=/usr/bin/docker compose up --build
ExecStop=/usr/bin/docker compose down
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable travel-archive
ok "systemd service installed — starts on boot"

# ── Build and start ──────────────────────────────────────────────────────────
info "Building Docker image (this takes ~2 min on first run)…"
cd "$APP_DIR"
docker compose build

info "Starting services…"
docker compose up -d

# Wait for health check
info "Waiting for server to be healthy…"
for i in $(seq 1 20); do
    if curl -sf "http://localhost:${WEB_PORT}/api/ping" &>/dev/null; then
        ok "Server is up!"
        break
    fi
    sleep 2
done

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   Travel Archive — Deployed Successfully ✓        ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════╝${NC}"
echo ""
SERVER_IP=$(curl -sf https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
echo -e "  Viewer : ${CYAN}http://${SERVER_IP}:${WEB_PORT}/${NC}"
echo -e "  Admin  : ${CYAN}http://${SERVER_IP}:${WEB_PORT}/admin${NC}"
echo -e "  SSH    : ${CYAN}ssh root@${SERVER_IP} -p ${SSH_PORT}${NC}"
echo -e "  Data   : ${CYAN}${APP_DIR}/data${NC}  ← back this up!"
echo -e "  Films  : ${CYAN}${FILMS_PATH}${NC}  ← mounted as /films in container"
echo ""
echo -e "  Logs   : ${CYAN}docker compose -f ${APP_DIR}/docker-compose.yml logs -f${NC}"
echo -e "  Stop   : ${CYAN}systemctl stop travel-archive${NC}"
echo -e "  Restart: ${CYAN}systemctl restart travel-archive${NC}"
echo ""
warn "Remember to set up your domain + HTTPS if this server is public:"
warn "  Edit Caddyfile, uncomment caddy service in docker-compose.yml, run:"
warn "  docker compose up -d"
echo ""
