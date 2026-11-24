#!/usr/bin/env bash
# Install or upgrade the CentOS 8.4 server deployment.
set -euo pipefail

if [ "$(id -u)" != "0" ]; then
  echo "run as root (sudo bash deploy/centos8/install.sh)" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

if ! command -v podman >/dev/null 2>&1; then
  if [ -f /etc/centos-release ] && grep -q 'CentOS Linux release 8.4.2105' /etc/centos-release; then
    # Only update the base repositories used by DNF to install Podman.
    sed -i 's/^mirrorlist=/#mirrorlist=/; s|^#\?baseurl=http://mirror.centos.org|baseurl=https://vault.centos.org|; s/\$releasever/8.4.2105/g' \
      /etc/yum.repos.d/CentOS-Linux-{BaseOS,AppStream,Extras}.repo
    dnf clean all
  fi
  dnf install -y podman
fi
podman --version

ENV_DIR=/etc/rea-crawler
ENV_FILE="$ENV_DIR/web.env"
install -d -m 700 "$ENV_DIR"
if [ ! -s "$ENV_FILE" ]; then
  password="$(openssl rand -hex 20)"
  (umask 077; printf 'CRAWL_WEB_PASSWORD=%s\n' "$password" > "$ENV_FILE")
  echo "generated admin password -> $ENV_FILE"
fi
chmod 600 "$ENV_FILE"

# Older Podman filters clone3 with EPERM. ENOSYS lets glibc fall back to clone.
SECCOMP_FILE="$ENV_DIR/seccomp.json"
/usr/libexec/platform-python - "$SECCOMP_FILE" <<'PY'
import errno
import json
import sys
from pathlib import Path

profile = json.loads(Path("/usr/share/containers/seccomp.json").read_text())
rules = profile["syscalls"]
if not any("clone3" in rule["names"] for rule in rules):
    rules.append({"names": ["clone3"], "action": "SCMP_ACT_ERRNO", "errnoRet": errno.ENOSYS})
destination = Path(sys.argv[1])
temporary = destination.with_suffix(".tmp")
temporary.write_text(json.dumps(profile))
temporary.replace(destination)
PY

# Build before stopping a running installation. Podman reuses the named data volume.
podman build --format docker --layers --security-opt "seccomp=$SECCOMP_FILE" \
  -f deploy/Dockerfile -t localhost/rea-crawler:latest "$ROOT"
install -m 644 deploy/centos8/rea-crawler.service /etc/systemd/system/rea-crawler.service
systemctl daemon-reload
systemctl enable rea-crawler.service
systemctl restart rea-crawler.service

echo 'Waiting for the crawler Web service ...'
deadline=$((SECONDS + 60))
while ((SECONDS < deadline)); do
  if address="$(podman port rea-crawler 8787/tcp 2>/dev/null)"; then
    address="${address/#0.0.0.0:/127.0.0.1:}"
    address="${address/#\[::\]:/[::1]:}"
    if curl --noproxy '*' -fsS --max-time 3 "http://${address}/healthz" >/dev/null 2>&1; then
      printf 'Deployment ready at http://%s/\n' "$address"
      printf 'User: admin. Password file: %s\n' "$ENV_FILE"
      exit 0
    fi
  fi
  sleep 2
done

echo 'Crawler failed its startup health check. Run: journalctl -u rea-crawler.service -n 100 --no-pager' >&2
exit 1
