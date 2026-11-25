# Build the UI once; runtime needs Python only.
FROM docker.io/library/node:22-bookworm-slim AS ui
WORKDIR /src
COPY collector/ui/package.json collector/ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY collector/ui/ ./
RUN npm run build

FROM docker.io/library/python:3.12-slim-bookworm
WORKDIR /app
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN pip install --no-cache-dir uv==0.11.7
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra browser \
    && .venv/bin/python -m playwright install-deps chromium firefox \
    && .venv/bin/python -m playwright install chromium \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home crawler \
    && install -d -o crawler -g crawler /data \
        "$(.venv/bin/python -c 'from camoufox.fpgen_model import fpgen_data_dir; print(fpgen_data_dir())')"
ENV HOME=/home/crawler CRAWL_DATA_DIR=/data CRAWL_SOURCES_FILE=/data/sources.yaml CRAWL_HOST=0.0.0.0 CRAWL_PORT=8787 PYTHONUNBUFFERED=1
COPY deploy/install-camoufox.py /tmp/install-camoufox.py
USER crawler
RUN .venv/bin/python /tmp/install-camoufox.py
USER root
COPY collector/ ./collector/
COPY --from=ui /src/dist ./collector/ui/dist
RUN chmod -R a+rX collector
USER crawler
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=4)"]
CMD ["/app/.venv/bin/python", "collector/serve.py", "--supervisor"]
