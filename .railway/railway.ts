// Railway settings for the ONA API and its Celery worker, applied with the
// Railway CLI (v5+):
//
//   railway config plan  --environment Staging      # preview
//   railway config apply --environment Staging      # then Production
//
// PowerShell: the SDK checks the CLI version by running $_, which PowerShell
// never sets, so first point it at the real binary (CLI installed with npm):
//   $env:_ = "$env:APPDATA\npm\node_modules\@railway\cli\bin\railway.exe"
//
// Railway does not read this file during deploys: `apply` writes these
// values onto the services, and deploys use what is stored there. It
// replaces railway.json, which Railway stops reading on 2026-12-01.
//
// This repo owns only these two services (the `partial` below). The website
// repo owns ONA-Portal-Frontend; Postgres, Redis, the volumes and the bucket
// are owned by no file, so nothing here can delete them. No variables: they
// stay managed in Railway.

import { defineRailway, github, project, service } from "railway/iac";

export const partial = "ona-backend";

// The branch each environment deploys. `source` must be declared: leaving it
// out does not mean "unchanged", the plan disconnects the GitHub repo.
const BRANCH: Record<string, string> = { Staging: "develop", Production: "main" };

export default defineRailway((ctx) => {
  const branch = BRANCH[ctx.environment];
  if (!branch) throw new Error(`No branch set for environment "${ctx.environment}" in .railway/railway.ts`);
  const source = github("eaglesneaglets-creator/ONA-Records-Backend", { branch, checkSuites: true });

  // The Dockerfile's last stage is `production`, so no build target is needed.
  const dockerfile = { builder: "DOCKERFILE", dockerfilePath: "Dockerfile" } as const;

  const api = service("ONA-Records-Backend", {
    source,
    build: dockerfile,
    // Waits for Postgres, runs migrations, then starts gunicorn.
    start: "./start.sh",
    healthcheck: "/api/v1/health/",
    // Covers the database wait (up to 60s) plus migrations plus gunicorn
    // boot. The default 60s is not enough on a cold Postgres.
    healthcheckTimeout: 180,
    // Restart on failure is Railway's default (stored as unset); cap the retries.
    deploy: { restartPolicyMaxRetries: 3 },
  });

  const worker = service("worker", {
    source,
    build: dockerfile,
    // Celery worker + beat from the same image. No health check and no
    // public domain: it serves no HTTP.
    start: "./start-worker.sh",
    deploy: { restartPolicyType: "ALWAYS" },
  });

  return project("ONA-Records", {
    variables: { managed: false },
    resources: [api, worker],
  });
});
