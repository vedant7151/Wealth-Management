# Multi-stage build. Base image pinned by DIGEST (python:3.12.2-slim, the
# multi-arch index digest -- matches this project's measured Python
# version). Refresh deliberately via Dependabot's docker ecosystem. See docs/DEPLOY.md for the full build/run story.

FROM python:3.15.2-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS builder

WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PRISMA_BINARY_CACHE_DIR=/opt/prisma-cache

# prisma-client-py downloads a prebuilt Node (via nodeenv) to run the Prisma CLI;
# that Node binary links libatomic, which python:*-slim does not ship. Builder
# stage only -- the runtime stage copies just the generated client + engine.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*

# requirements.lock (pip-compile output) pins every TRANSITIVE dependency;
# used as a constraints file so the image can't drift from what CI tested.
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock

COPY schema.prisma .
# `prisma py fetch` downloads the query-engine binary for THIS platform at
# build time, so the runtime container never needs outbound network just
# to generate a client or open a connection; `prisma generate` then builds
# the actual Python client against schema.prisma. Needs a syntactically
# valid DATABASE_URL to run (never connects) -- a placeholder is enough.
# PRISMA_BINARY_CACHE_DIR is set explicitly (rather than relying on
# root's implicit $HOME/.cache) so the runtime stage's COPY below is
# pinned to a known, explicit path instead of an assumption about where
# prisma-client-py's default config resolves a home directory to.
ENV DATABASE_URL="postgresql://build:build@localhost:5432/build"
RUN python -m prisma py fetch && python -m prisma generate --schema=schema.prisma


FROM python:3.15.2-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS runtime

# Non-root user -- the app never needs root inside the container.
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app

WORKDIR /app
ENV PRISMA_BINARY_CACHE_DIR=/opt/prisma-cache
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
# The fetched query-engine binary lives here (see the builder stage's
# explicit PRISMA_BINARY_CACHE_DIR) -- copied so the runtime image never
# needs outbound network just to find/re-fetch it, and so the non-root
# `app` user (no write access to re-fetch anywhere) can still read it.
COPY --from=builder /opt/prisma-cache /opt/prisma-cache

COPY . .
RUN chown -R app:app /app /opt/prisma-cache && chmod +x docker-entrypoint.sh

USER 10001:10001
ENV PORT=8010 \
    WEB_CONCURRENCY=1 \
    GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS=25 \
    PYTHONUNBUFFERED=1

EXPOSE 8010

# Dependency-free liveness check (GET /healthz does no DB/network I/O --
# see api.py). start-period gives the DB-wait loop in the entrypoint room
# to run before the first probe counts against the container.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8010\")}/healthz', timeout=3).status==200 else 1)"

ENTRYPOINT ["./docker-entrypoint.sh"]
