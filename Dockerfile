FROM python:3.12-slim-bookworm

# uv installs the locked dependency set into /app/.venv without touching system Python.
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    CLOUDSET_HOST=0.0.0.0 \
    CLOUDSET_PORT=8080

WORKDIR /app

# Dependencies first so code edits don't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY static ./static
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Runtime data (SQLite, weather snapshots, tile cache) lives on a volume.
RUN useradd --create-home --uid 1000 cloudset && mkdir -p /app/data && chown -R cloudset:cloudset /app
USER cloudset
VOLUME ["/app/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status == 200 else 1)"

CMD ["/app/.venv/bin/cloudset"]
