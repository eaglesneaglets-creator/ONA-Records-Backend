# Going live: production runbook

How to stand up ONA Records production on Railway, check it, and keep it
healthy. Staging (`ona-records-*-staging.up.railway.app`) is the model:
production is the same services with its own data and secrets.

## 1. Create the environment

In the ONA-Records Railway project, add a **Production** environment with its own:

| Service | Notes |
|---|---|
| Postgres | Production's own database. Turn on backups (step 5). |
| Redis | Celery broker and the shared cache. |
| Storage bucket | Invoices, uploads. Never share staging's. |
| Backend (this repo, `main` or the release branch) | Healthcheck `/api/v1/health/`. |
| Worker (same repo) | Start command `./start-worker.sh`, `CELERY_BEAT=1`, **no** public domain. |
| Frontend (`ONA-Portal-Frontend`) | Public domain = the website. |

Build, start, health check and restart settings for the backend and worker live in
`.railway/railway.ts` (the website's in its own repo's `.railway/railway.ts`). Railway does not
read them during deploys: apply them with the Railway CLI v5 (`railway config plan`, then
`railway config apply --environment Production`) after any change. They replace
`railway.json`, which Railway stopped reading on 2026-12-01. In PowerShell, first run
`$env:_ = "$env:APPDATA\npm\node_modules\@railway\cli\bin\railway.exe"`: the SDK checks
the CLI version by running `$_`, which PowerShell never sets.

Point the company domain at the frontend (e.g. `onarecords.com`) and an API
subdomain at the backend (e.g. `api.onarecords.com`).

## 2. Variables

Set on **both backend and worker** unless marked. Never copy staging secrets.

| Variable | Production value |
|---|---|
| `DJANGO_SETTINGS_MODULE` | `config.settings.production` |
| `SECRET_KEY` | new, 50+ random characters |
| `DATABASE_URL`, `REDIS_URL` | references to production's Postgres and Redis |
| `S3_*` | production bucket |
| `ALLOWED_HOSTS` | `api.<domain>,healthcheck.railway.app` |
| `FRONTEND_URL` | `https://<domain>`. The app refuses to start without an https value: every email link and Paystack's return address use it. |
| `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS` | `https://<domain>` |
| `ADMIN_URL` | a new private path, not `admin/` |
| `LOG_LEVEL` | `INFO` |
| `PAYSTACK_SECRET_KEY` | the **live** key `sk_live_…` |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` | the company mailbox (Google Workspace app password) |
| `KYC_API_URL`, `KYC_API_KEY`, `KYC_WEBHOOK_SECRET` | a **new** KYC platform key whose webhook URL is `https://api.<domain>/api/v1/kyc/webhook/` |
| `SENTRY_DSN` | so production errors are visible (web **and** worker; the backend's own Sentry project). Check with `python manage.py ops_check --sentry-test` |
| `CELERY_BEAT=1` | worker only |

Frontend: `NEXT_PUBLIC_API_URL=https://api.<domain>/api/v1` and
`NEXT_PUBLIC_ENVIRONMENT=production`. These are baked in at build time:
redeploy the frontend after changing them.

Frontend error reporting (its own Sentry project, not the backend's):
`NEXT_PUBLIC_SENTRY_DSN` (baked in at build), plus `SENTRY_AUTH_TOKEN`,
`SENTRY_ORG`, `SENTRY_PROJECT` so the build uploads source maps. In Sentry,
for both projects: Security & Privacy → keep the default scrubbers on and turn
on "Prevent Storing of IP Addresses"; Client Keys → set a rate limit; Allowed
Domains → the production site only.

Paystack dashboard (live mode): webhook URL
`https://api.<domain>/api/v1/payments/paystack/webhook/`.

## 3. First deploy

The backend runs `migrate` on every start. Then, in the backend's shell:

```bash
python manage.py createsuperuser          # the first staff login
python manage.py load_ona_catalogue       # rooms, engineers, services, packages
python manage.py launch_check             # must end "Ready: no blockers"
```

In the staff console:

- **Questions & contact**: phone, WhatsApp, email, address (launch_check blocks until set).
- **Business settings**: deposit, hold time, notice, studio hours, fees, invoice
  details (TIN, bank, contact). Turn tax on only once ONA is registered, with
  the rates from ONA's accountant; then a TIN is required.
- **Staff accounts**: add the rest of the team.

## 4. Before announcing it

- ONA has approved `/legal/privacy` and `/legal/terms`.
- One real booking end to end: sign up → confirm email (link opens the live
  site) → book → pay the deposit with live Paystack → receipt arrives →
  staff see the payment → refund it from the console.
- `https://<domain>/robots.txt` allows indexing (production only).

## 5. Keep it healthy

- **Backups:** enable Railway Postgres backups; once a month restore the latest
  into a scratch database and run `python manage.py check` against it.
- **Errors:** Sentry alerts to a person who will act on them.
- **Deploys:** CI must pass before Railway deploys (`develop` → staging; the
  release branch → production).
- **Secrets:** rotate any key that was ever pasted into chat or a log.
