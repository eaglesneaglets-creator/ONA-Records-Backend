#!/usr/bin/env bash
# ONA Records — web entrypoint.
#
# Order matters here. Migrations run before gunicorn binds a port, which means
# anything that hangs or fails in this script surfaces as "service
# unavailable" on the health check rather than as a useful error. So every
# step announces itself and the database wait is bounded.
set -euo pipefail

echo "==> DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE:-UNSET}"
echo "==> PORT=${PORT:-8000}"

# When the settings module is missing, print what the container DID receive.
# Railway distinguishes project-level shared variables from service-level
# ones, and a shared variable does not reach a service unless it is
# explicitly referenced — so "I set it" and "the container has it" are
# different claims. This shows which is true.
#
# Only variable NAMES are printed, never values: this log is not a safe
# place for SECRET_KEY or database credentials.
if [ -z "${DJANGO_SETTINGS_MODULE:-}" ]; then
    echo "--- environment variable names visible to this container ---"
    env | cut -d= -f1 | sort | tr '\n' ' '
    echo ""
    echo "--- Railway-injected variables (these prove which env we are in) ---"
    echo "    RAILWAY_ENVIRONMENT_NAME=${RAILWAY_ENVIRONMENT_NAME:-<absent>}"
    echo "    RAILWAY_SERVICE_NAME=${RAILWAY_SERVICE_NAME:-<absent>}"
    if [ -n "${DATABASE_URL:-}" ]; then
        echo "    DATABASE_URL=<set>"
    else
        echo "    DATABASE_URL=<absent>"
    fi
    echo "-----------------------------------------------------------"
fi

# A deployed container must never fall back to manage.py's default, which is
# config.settings.local — DEBUG=True, no SSL redirect, no HSTS, permissive
# CORS. That failure is silent: the app boots and serves traffic, it is just
# unprotected. Refuse to start instead.
case "${DJANGO_SETTINGS_MODULE:-}" in
    config.settings.staging|config.settings.production)
        ;;
    "")
        echo "!!! DJANGO_SETTINGS_MODULE is not set."
        echo "    Without it Django falls back to config.settings.local, which"
        echo "    runs with DEBUG=True and no security middleware. Set it to"
        echo "    config.settings.staging or config.settings.production in the"
        echo "    Railway Variables tab for THIS environment."
        exit 1
        ;;
    *)
        echo "!!! DJANGO_SETTINGS_MODULE='${DJANGO_SETTINGS_MODULE}' is not a"
        echo "    deployable settings module. Expected config.settings.staging"
        echo "    or config.settings.production."
        exit 1
        ;;
esac

if [ -z "${DATABASE_URL:-}" ]; then
    echo "!!! DATABASE_URL is not set."
    echo "    On Railway this is injected automatically once a Postgres service"
    echo "    exists in the SAME environment. If it is missing, either the"
    echo "    database was never added or it lives in another environment."
    exit 1
fi

# Wait for Postgres rather than letting `migrate` block indefinitely. Railway
# can start the app before the database finishes provisioning, and an
# unbounded migrate is indistinguishable from a crash to the health check.
echo "==> Waiting for the database..."
python - <<'PY'
import os
import sys
import time

import dj_database_url
import psycopg

cfg = dj_database_url.parse(os.environ["DATABASE_URL"])
dsn = "host={h} port={p} dbname={n} user={u} password={pw}".format(
    h=cfg.get("HOST") or "localhost",
    p=cfg.get("PORT") or 5432,
    n=cfg.get("NAME") or "postgres",
    u=cfg.get("USER") or "postgres",
    pw=cfg.get("PASSWORD") or "",
)

deadline = time.time() + 60
attempt = 0
while time.time() < deadline:
    attempt += 1
    try:
        with psycopg.connect(dsn, connect_timeout=5):
            print("    connected on attempt %d" % attempt)
            sys.exit(0)
    except Exception as exc:
        print("    attempt %d: %s: %s" % (attempt, type(exc).__name__, str(exc).strip()[:120]))
        time.sleep(3)

print("!!! Database unreachable after 60s. Check that a Postgres service exists")
print("    in this Railway environment and that DATABASE_URL points at it.")
sys.exit(1)
PY

echo "==> Running migrations..."
python manage.py migrate --noinput

echo "==> Starting gunicorn on 0.0.0.0:${PORT:-8000}"
exec gunicorn config.wsgi:application \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers "${WEB_CONCURRENCY:-3}" \
    --timeout 60 \
    --access-logfile - \
    --error-logfile -
