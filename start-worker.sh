#!/usr/bin/env bash
# ONA Records — Celery worker entrypoint.
# Deploy as a SEPARATE Railway service using the same image.
set -euo pipefail

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
