# Build the UI once; runtime needs Python only.
FROM node:22-bookworm-slim AS ui
WORKDIR /src
COPY collector/ui/package.json collector/ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY collector/ui/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
WORKDIR /app
RUN pip install --no-cache-dir uv==0.11.7
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY collector/ ./collector/
COPY --from=ui /src/dist ./collector/ui/dist
RUN useradd --create-home crawler && mkdir /data && chown crawler:crawler /data
USER crawler
ENV CRAWL_DATA_DIR=/data CRAWL_HOST=0.0.0.0 CRAWL_PORT=8787 PYTHONUNBUFFERED=1
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=4)"]
CMD ["/app/.venv/bin/python", "collector/serve.py"]
