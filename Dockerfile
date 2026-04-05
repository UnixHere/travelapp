FROM python:3.11-slim

# ── System packages ──────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        openssh-server \
        gosu \
        curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── SSH setup ────────────────────────────────────────────────────────────────
RUN mkdir -p /run/sshd /root/.ssh && chmod 700 /root/.ssh

# Harden SSH: disable password auth, only allow key-based login
RUN sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config && \
    sed -i 's/#PubkeyAuthentication yes/PubkeyAuthentication yes/' /etc/ssh/sshd_config && \
    sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config && \
    echo "AllowTcpForwarding yes" >> /etc/ssh/sshd_config && \
    echo "X11Forwarding no" >> /etc/ssh/sshd_config

# ── App ──────────────────────────────────────────────────────────────────────
WORKDIR /app

COPY server.py          ./server.py
COPY world_map_app.html ./world_map_app.html
COPY viewer.html        ./viewer.html

# Data dirs — these are typically bind-mounted from the host
RUN mkdir -p data/media

# ── Entrypoint ───────────────────────────────────────────────────────────────
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000 22

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:8000/api/ping || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]