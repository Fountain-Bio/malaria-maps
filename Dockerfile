# syntax=docker/dockerfile:1
#
# Baked, read-only serving image: the versioned data/malaria.db is copied in, opened
# immutable (-i), and served by Datasette. The collector never runs here; the weekly
# GitHub workflow rebakes the DB into git, which is what produces a new image.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# The API is served at the stable /malaria path with a one-hour Cache-Control TTL, so a
# rebaked DB is visible everywhere within the hour and URLs never change between builds.
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

# Opt this baked image into far-future immutable caching of /web/* assets. The image is the
# unit of versioning (a rebake is a new deploy), so the cache never needs purging. Left unset
# in local dev, where the same files change in place and must stay uncached (cache_headers.py).
ENV IMMUTABLE_ASSETS=1

# Runtime-written geocode cache. Defaulted to a path meant for a mounted volume, so attaching
# a Railway volume at /app/var persists it across deploys with no extra config. Without a
# volume this is ephemeral, which is harmless (the cache rebuilds from GeoNames on demand).
ENV GEOCODE_CACHE_PATH=/app/var/geocode_cache.sqlite

# Railway injects $PORT; default for local `docker run`.
ENV PORT=8765
EXPOSE 8765

# -i opens malaria.db immutable (read-only, no locking). The plugins dir loads the locate
# endpoint, cache headers, robots.txt, sitemap.xml, legacy-URL redirects and the AI crawler
# rate limit; datasette-gzip loads via its entry point. Bind 0.0.0.0 so the container is
# reachable.
CMD ["sh", "-c", "uv run --no-sync datasette -i data/malaria.db -m metadata.yaml --static web:web/ --plugins-dir plugins --setting default_cache_ttl 3600 -h 0.0.0.0 -p ${PORT}"]
