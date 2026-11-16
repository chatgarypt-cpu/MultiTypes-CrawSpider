# Deploy on CentOS Linux 8.4.2105 (x86_64)

CentOS Linux 8 reached EOL on 2021-12-31. The default mirrors are gone, so the
first step is to point DNF at `vault.centos.org`. The crawler itself runs in a
container built from `deploy/Dockerfile` (Debian bookworm + Python 3.12), so the
host only needs a container runtime.

## Quick start (recommended)

No Docker, no npm, no Node.js on the host: the image builds the UI inside a
Node stage, and Podman runs the result.

```bash
git clone git@github.com:chatgarypt-cpu/MultiTypes-CrawSpider.git
cd MultiTypes-CrawSpider
git checkout deploy/centos8
sudo bash deploy/centos8/install.sh
```

The script:

1. points DNF at `vault.centos.org` for CentOS 8.4;
2. installs Podman;
3. generates an admin password in `/etc/rea-crawler/web.env` (0600);
4. builds `rea-crawler:latest` and starts container `rea-crawler`;
5. enables `podman-restart.service` so the container comes back after reboot;
6. waits for `/healthz` and prints the URL.

Use `CRAWL_BIND=0.0.0.0` / `CRAWL_PORT=8788` before the script to change the
bind address/port. Data stays in the `crawler-data` volume across rebuilds.

Why not npm/qnpm: npm is only used to build the frontend, which already happens
inside the image. Installing Node/Python on CentOS 8.4 natively is fragile
(glibc/OpenSSL/EOL repos) and unnecessary. Why not Docker: Docker CE's CentOS 8
repo is archived; Podman is native and sufficient.

## 1. Fix DNF repos (vault)

```bash
sed -i 's/^mirrorlist=/#mirrorlist=/; s|^#baseurl=http://mirror.centos.org|baseurl=http://vault.centos.org|' /etc/yum.repos.d/CentOS-*.repo
sed -i 's/\$releasever/8.4.2105/g' /etc/yum.repos.d/CentOS-*.repo
dnf clean all && dnf makecache
```

Optional EPEL (also from vault):

```bash
dnf install -y epel-release
```

## 2. Container runtime: Podman

CentOS 8 AppStream ships Podman, which avoids the archived Docker CE repo.

```bash
dnf install -y podman
podman --version
```

> Docker CE for CentOS 8 is archived. If you must use Docker, use its static
> x86_64 binaries or a supported host; Podman is the lower-risk path here.

## 3. Manual build and run (same steps as the script)

```bash
set -a; . /etc/rea-crawler/web.env; set +a

podman build -f deploy/Dockerfile -t rea-crawler:latest .
podman volume exists crawler-data || podman volume create crawler-data
podman rm -f rea-crawler 2>/dev/null || true
podman run -d --name rea-crawler --restart=unless-stopped \
  -p 127.0.0.1:8787:8787 \
  -e CRAWL_WEB_PASSWORD \
  -v crawler-data:/data \
  rea-crawler:latest
curl -fsS http://127.0.0.1:8787/healthz
```

The container binds `127.0.0.1:8787` by default. Put an HTTPS reverse proxy
(nginx/Apache) in front; do not expose plain HTTP with the admin password.

To expose on a trusted LAN only, use `-p 0.0.0.0:8787:8787` and open the port:

```bash
firewall-cmd --permanent --add-port=8787/tcp
firewall-cmd --reload
```

`deploy/compose.yaml` is still available as an optional `podman-compose` path;
the direct `podman run` above has fewer moving parts.

Firewall (only if CRAWL_BIND is not 127.0.0.1):

```bash
firewall-cmd --permanent --add-port=8787/tcp
firewall-cmd --reload
```

## 4. Nginx reverse proxy (example)

```nginx
server {
    listen 443 ssl;
    server_name crawler.example.com;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # SSE progress stream: disable buffering
    location /api/stream {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header Connection '';
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
}
```

## 5. Data and backup

- Data lives in the named volume `crawler-data` (mounted at `/data`).
- Do **not** run `podman-compose down -v` unless you intend to delete data.
- Backup while the service is stopped:

```bash
podman volume export crawler-data -o crawler-data-$(date +%F).tar
```

Restore with `podman volume import crawler-data <file>`.

## 6. Health and logs

```bash
curl -fsS http://127.0.0.1:8787/healthz
podman-compose -f deploy/compose.yaml logs -f
```

## 7. Browser channels (Playwright / Camoufox)

The default image intentionally does **not** include Playwright or Camoufox
(they add large browser downloads and OS libraries). Sites that need a real
browser (`via: playwright` / `via: camoufox`) require a derived image:

```dockerfile
FROM rea-crawler:latest
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
      libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
      libgbm1 libasound2 && rm -rf /var/lib/apt/lists/*
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN /app/.venv/bin/python -m playwright install --with-deps chromium
USER crawler
```

Camoufox needs a Firefox binary and `playwright==1.62.0` (already pinned in
`pyproject.toml`); install it in the same derived image only if a source
actually uses `via: camoufox`. Validate with:

```bash
podman exec -it <container> /app/.venv/bin/python collector/collect.py --check-site <domain>
```

## 8. Notes

- Images are multi-arch; building on this host produces `linux/amd64`.
- `CRAWL_DATA_DIR=/data` is already set by the image.
- Only one web scheduler may use a data directory at a time.
- After upgrading the image, interrupted tasks show "interrupted" in the UI and
  can be resumed; queued tasks are scheduled automatically.
