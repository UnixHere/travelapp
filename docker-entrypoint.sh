#!/bin/bash
set -e

# ── SSH authorized key injection ─────────────────────────────────────────────
# Set AUTHORIZED_KEYS env var (or mount a file at /run/secrets/authorized_keys)
# to enable SSH access into the container.

AUTH_KEY_FILE="/run/secrets/authorized_keys"
if [ -n "$AUTHORIZED_KEYS" ]; then
    echo "$AUTHORIZED_KEYS" >> /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
    echo "[entrypoint] SSH key injected from AUTHORIZED_KEYS env var"
elif [ -f "$AUTH_KEY_FILE" ]; then
    cat "$AUTH_KEY_FILE" >> /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
    echo "[entrypoint] SSH key injected from secret file"
else
    echo "[entrypoint] WARNING: No SSH key provided — SSH login will not work."
    echo "             Set AUTHORIZED_KEYS env var or mount your public key at:"
    echo "             /run/secrets/authorized_keys"
fi

# Regenerate host keys if missing (first boot in fresh volume)
for type in rsa ecdsa ed25519; do
    keyfile="/etc/ssh/ssh_host_${type}_key"
    if [ ! -f "$keyfile" ]; then
        ssh-keygen -q -N "" -t "$type" -f "$keyfile"
        echo "[entrypoint] Generated SSH host key: $keyfile"
    fi
done

# ── Start sshd in background ─────────────────────────────────────────────────
/usr/sbin/sshd -D &
SSHD_PID=$!
echo "[entrypoint] sshd started (PID $SSHD_PID)"

# ── Trap signals for clean shutdown ─────────────────────────────────────────
_term() {
    echo "[entrypoint] Caught signal — shutting down…"
    kill -TERM "$SERVER_PID" 2>/dev/null || true
    kill -TERM "$SSHD_PID"   2>/dev/null || true
    wait "$SERVER_PID" "$SSHD_PID" 2>/dev/null
    exit 0
}
trap _term SIGTERM SIGINT

# ── Start Travel Archive server ──────────────────────────────────────────────
cd /app
PORT=${PORT:-8000}
echo "[entrypoint] Starting Travel Archive on port $PORT…"
python3 -u server.py &
SERVER_PID=$!

echo "[entrypoint] Server PID $SERVER_PID — all systems up"
echo "[entrypoint] Web  → http://localhost:$PORT/"
echo "[entrypoint] SSH  → ssh root@<host> -p <ssh_port>"

# Wait for either process to exit
wait -n "$SERVER_PID" "$SSHD_PID" 2>/dev/null || true

# If server crashed, restart it in a loop
while true; do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "[entrypoint] Server exited — restarting in 3s…"
        sleep 3
        python3 -u server.py &
        SERVER_PID=$!
        echo "[entrypoint] Server restarted as PID $SERVER_PID"
    fi
    sleep 5
done
