# syntax=docker/dockerfile:1
#
# Baked, read-only serving image: the versioned data/malaria.db is copied in, opened
# immutable (-i), and served by Datasette. The collector never runs here; the weekly
# GitHub workflow rebakes the DB into git, which is what produces a new image.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Datasette resolves /malaria-<hash> from the DB content hash and serves the whole API
# with a one-year immutable cache header (datasette-hashed-urls). Restart on a new DB ->
# new hash -> new URLs, so caches we don't control never serve stale data.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install dependencies first for layer caching: this layer only busts when the lock changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# App code, serving config, and the baked database.
COPY src/ ./src/
COPY plugins/ ./plugins/
COPY web/ ./web/
COPY reference/ ./reference/
COPY metadata.yaml ./
COPY data/malaria.db ./data/malaria.db

# Editable install of the project so the locate plugin's `import malaria_tracker` resolves
# and config.PROJECT_ROOT points at /app (its data/ dir is where the geocode cache is written).
RUN uv sync --frozen --no-dev

# Runtime-written geocode cache. Defaulted to a path meant for a mounted volume, so attaching
# a Railway volume at /app/var persists it across deploys with no extra config. Without a
# volume this is ephemeral, which is harmless (the cache rebuilds from GeoNames on demand).
ENV GEOCODE_CACHE_PATH=/app/var/geocode_cache.sqlite

# Railway injects $PORT; default for local `docker run`.
ENV PORT=8765
EXPOSE 8765

# -i opens malaria.db immutable (required for hashed-urls). The plugins dir loads both the
# locate endpoint and the static cache-header plugin; datasette-hashed-urls loads via its
# entry point. Bind 0.0.0.0 so the container is reachable.
CMD ["sh", "-c", "uv run --no-sync datasette -i data/malaria.db -m metadata.yaml --static web:web/ --plugins-dir plugins -h 0.0.0.0 -p ${PORT}"]
