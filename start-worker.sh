#!/usr/bin/env bash
# ONA Records — Celery worker entrypoint.
# Deploy as a SEPARATE Railway service using the same image.
set -euo pipefail

# Same guard as start.sh. config/celery.py falls back to production settings,
# so a staging worker missing this variable would run production behaviour
# against staging data — and production settings accept a live Paystack key.
case "${DJANGO_SETTINGS_MODULE:-}" in
    config.settings.staging|config.settings.production)
        echo "==> Worker: DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE} CELERY_BEAT=${CELERY_BEAT:-1}"
        ;;
    *)
        echo "!!! DJANGO_SETTINGS_MODULE='${DJANGO_SETTINGS_MODULE:-}' — set it to"
        echo "    config.settings.staging or config.settings.production on THIS service."
        exit 1
        ;;
esac

if [ -z "${REDIS_URL:-}${CELERY_BROKER_URL:-}" ]; then
    echo "!!! No REDIS_URL (or CELERY_BROKER_URL). Add a Redis service to this"
    echo "    environment and reference its REDIS_URL from this service."
    exit 1
fi

# Beat (the scheduler for reminders) runs inside the worker. Run exactly ONE
# worker with CELERY_BEAT=1, or reminders are swept twice (still sent once —
# the log dedupes — but wastefully). Set CELERY_BEAT=0 on extra replicas.
BEAT_ARGS=()
if [ "${CELERY_BEAT:-1}" = "1" ]; then
    BEAT_ARGS=(--beat --schedule=/tmp/celerybeat-schedule)
fi

exec celery -A config.celery worker \
    --loglevel="${CELERY_LOG_LEVEL:-info}" \
    --concurrency="${CELERY_CONCURRENCY:-2}" \
    "${BEAT_ARGS[@]}"
