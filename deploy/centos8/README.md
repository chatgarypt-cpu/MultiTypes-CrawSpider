# Deploy on CentOS Linux 8.4.2105 (x86_64)

CentOS Linux 8 reached EOL on 2021-12-31. The default mirrors are gone, so the
first step is to point DNF at `vault.centos.org`. The crawler itself runs in a
container built from `deploy/Dockerfile` (Debian bookworm + Python 3.12), so the
host only needs a container runtime.

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

## 2. Container runtime: Podman (recommended)

CentOS 8 AppStream still ships Podman, which avoids the archived Docker CE repo.

```bash
dnf install -y podman python3-pip
python3 -m pip install --upgrade pip
python3 -m pip install podman-compose
podman --version
```

> If you prefer Docker CE, its CentOS 8 repo is archived; use the static
> binaries from `https://download.docker.com/linux/static/stable/x86_64/` or
> install Docker on a supported host. Podman is the lower-risk path on 8.4.

## 3. Build and run

From the repository root on the server:

```bash
read -rs -p 'Web admin password: ' CRAWL_WEB_PASSWORD; echo
export CRAWL_WEB_PASSWORD

podman-compose -f deploy/compose.yaml up -d --build
podman-compose -f deploy/compose.yaml ps
curl -fsS http://127.0.0.1:8787/healthz
```

The compose file binds `127.0.0.1:8787` by default. Put an HTTPS reverse proxy
(nginx/Apache) in front; do not expose plain HTTP with the admin password.

To expose on a trusted LAN only:

```bash
CRAWL_BIND=0.0.0.0 CRAWL_PORT=8787 podman-compose -f deploy/compose.yaml up -d
```

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
