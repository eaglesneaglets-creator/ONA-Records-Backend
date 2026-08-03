#!/usr/bin/env bash
# ONA Records — Celery worker entrypoint.
# Deploy as a SEPARATE Railway service using the same image.
set -euo pipefail

exec celery -A config.celery worker \
    --loglevel="${CELERY_LOG_LEVEL:-info}" \
    --concurrency="${CELERY_CONCURRENCY:-2}"
