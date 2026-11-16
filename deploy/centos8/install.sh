#!/usr/bin/env bash
# One-shot CentOS 8.4 x86_64 deployment: Podman build + run, no Docker/npm on host.
set -euo pipefail

if [ "$(id -u)" != "0" ]; then
  echo "run as root (sudo bash deploy/centos8/install.sh)" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

# --- 1. CentOS 8 mirrors are EOL: point DNF at vault -------------------------
if [ -f /etc/centos-release ] && grep -q 'CentOS Linux release 8' /etc/centos-release; then
  sed -i 's/^mirrorlist=/#mirrorlist=/; s|^#baseurl=http://mirror.centos.org|baseurl=http://vault.centos.org|' /etc/yum.repos.d/CentOS-*.repo || true
  sed -i 's/\$releasever/8.4.2105/g' /etc/yum.repos.d/CentOS-*.repo || true
  dnf clean all >/dev/null 2>&1 || true
fi

# --- 2. Podman (native on CentOS 8 AppStream; Docker CE CentOS-8 repo is archived)
if ! command -v podman >/dev/null 2>&1; then
  dnf install -y podman
fi
podman --version

# --- 3. Admin password (generated once, root-only) ---------------------------
ENV_DIR=/etc/rea-crawler
ENV_FILE="$ENV_DIR/web.env"
install -d -m 700 "$ENV_DIR"
if [ ! -s "$ENV_FILE" ]; then
  PW="$(openssl rand -base64 24 | tr -d '/+=' | head -c 20)"
  printf 'CRAWL_WEB_PASSWORD=%s\n' "$PW" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "generated admin password -> $ENV_FILE"
fi
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a
: "${CRAWL_WEB_PASSWORD:?missing CRAWL_WEB_PASSWORD in $ENV_FILE}"
BIND="${CRAWL_BIND:-127.0.0.1}"
PORT="${CRAWL_PORT:-8787}"

# --- 4. Build image (UI is built inside the image; host needs no Node/npm) ---
podman build -f deploy/Dockerfile -t rea-crawler:latest "$ROOT"

# --- 5. Named volume + container --------------------------------------------
podman volume exists crawler-data 2>/dev/null || podman volume create crawler-data
podman rm -f rea-crawler >/dev/null 2>&1 || true
podman run -d --name rea-crawler --restart=unless-stopped \
  -p "${BIND}:${PORT}:8787" \
  -e CRAWL_WEB_PASSWORD \
  -e SITE_WORKERS="${SITE_WORKERS:-4}" \
  -v crawler-data:/data \
  rea-crawler:latest

# restart container after reboot (podman-restart.service ships with podman)
systemctl enable --now podman-restart.service >/dev/null 2>&1 || true

# --- 6. Health check ---------------------------------------------------------
echo "waiting for http://${BIND}:${PORT}/healthz ..."
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    echo "OK"
    break
  fi
  sleep 2
done

cat <<MSG

Deployment ready.
  URL:      http://${BIND}:${PORT}/
  user:     admin
  password: stored in ${ENV_FILE} (root only)
  logs:     podman logs -f rea-crawler
  stop:     podman stop rea-crawler
  start:    podman start rea-crawler
  upgrade:  re-run this script (data volume is kept)

Put an HTTPS reverse proxy in front before exposing it publicly.
MSG
