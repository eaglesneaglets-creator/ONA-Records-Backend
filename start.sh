#!/usr/bin/env bash
# ONA Records — web entrypoint.
set -euo pipefail

echo "Environment: ${DJANGO_SETTINGS_MODULE:-unset}"

# Migrations run before the server accepts traffic. Railway health checks
# tolerate the delay; serving requests against an un-migrated schema does not.
python manage.py migrate --noinput

exec gunicorn config.wsgi:application \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers "${WEB_CONCURRENCY:-3}" \
    --timeout 60 \
    --access-logfile - \
    --error-logfile -
