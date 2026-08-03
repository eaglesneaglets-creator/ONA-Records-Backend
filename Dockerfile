# ONA Records — backend image
#
# Multi-stage: dependencies are built once and copied into a lean runtime,
# so the shipped image has no compilers and no build caches.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app


# ---------------------------------------------------------------------------
FROM base AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt


# ---------------------------------------------------------------------------
FROM base AS production

# libpq5 only — the client library, not the build headers.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Never run as root.
RUN useradd --create-home --shell /bin/bash ona
COPY --chown=ona:ona . .
RUN chmod +x start.sh start-worker.sh

# Static files are collected at build time so the container starts fast.
# A dummy key is enough: collectstatic touches no secrets and no database.
RUN SECRET_KEY=build-only-dummy \
    DATABASE_URL=postgres://u:p@localhost:5432/d \
    DJANGO_SETTINGS_MODULE=config.settings.production \
    python manage.py collectstatic --noinput

USER ona

EXPOSE 8000
CMD ["./start.sh"]
