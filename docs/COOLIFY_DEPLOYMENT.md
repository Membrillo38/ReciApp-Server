# ReciApp deployment through Coolify

`docker-compose.coolify.yml` is the target Coolify Git resource. It runs API and worker from this repository while reusing the existing `reciapp-internal` network, PostgreSQL, Redis, and `/var/lib/reciapp/covers`. It does not create, initialize, or migrate a database.

## Production readiness correction — 2026-10-05

Read-only inspection of the live API found two confirmed Coolify settings that make `/ready` fail:

1. `DATABASE_URL` connects as `reciapp`, a PostgreSQL superuser with `BYPASSRLS`. Migration 014 has already created `reciapp_runtime` as `LOGIN`, `NOSUPERUSER`, `NOBYPASSRLS` and granted the app permissions. In the existing API resource, change only the username/password portion of `DATABASE_URL` to use `reciapp_runtime`; preserve the existing host, port, database, and query parameters. If its password is unknown, set a new password with the database admin and save it only as a Coolify secret.
2. The live API has `APNS_ENABLED=true` and `WORKER_ENABLED=false`. The separate worker is enabled and has a recent heartbeat. Set `WORKER_ENABLED=true` on the API as well.

The live API and worker currently use different commits. Redeploy both from the same reviewed server commit. Change the Coolify API health check from `/health` to `/ready`; `/health` alone reports process liveness and hid this dependency failure. Keep `APPLE_ENVIRONMENT=Production`; do not rerun migrations or change APNs secrets for this incident. A read-only simulation using the runtime role, service RLS context, and worker enabled passed the complete readiness probe.

After redeploy, verify public `/reciapp/ready` returns HTTP 200 and `status=ready`. Keep both services pointed to the existing PostgreSQL/database and cover volume; do not create a new database or expose secret values in logs/screenshots.

## Coolify runtime variables

Create runtime Environment Variables/Secrets on the single ReciApp Coolify Compose resource. Compose passes shared values to both API and worker. The user creates the OpenAI key; never put it in GitHub, iOS, build arguments, or logs.

Required:

- `OPENAI_API_KEY` — new ReciApp OpenAI project key.
- `API_KEY` — ReciApp server-only operator key.
- `DATABASE_URL` — existing ReciApp PostgreSQL connection, using the restricted `reciapp_runtime` role (LOGIN, NOSUPERUSER, NOBYPASSRLS). Preserve the database; do not connect the app as the migration/admin role.
- `REDIS_URL` — existing ReciApp Redis connection.
- `AUTH_JWT_SECRET` — existing signing secret; preserve it so current sessions remain valid.
- `APPLE_ROOT_CA_PEM` — optional override for the bundled Apple Root CA G3 certificate used to verify App Store transactions and notifications. This is a public certificate, not the APNs `.p8` private key. The current server image bundles the root certificate; leave this unset unless Apple rotates the trust anchor.
- `APPLE_ENVIRONMENT` — choose `Production` for App Store transactions. TestFlight uses `Sandbox`; current server accepts only one environment, so do not point a mixed TestFlight/App Store population at this deployment until environment isolation is implemented.
- `APNS_ENABLED=true` — required to turn on remote recipe-completion notifications. Also set APNs Team ID, Key ID, `.p8` key, and production environment for TestFlight/App Store distribution builds.

Set existing non-empty integration secrets too when enabled: `SUPERWALL_WEBHOOK_SECRET`, `DASHBOARD_PASSWORD`, `DASHBOARD_SESSION_SECRET`, `DASHBOARD_TOTP_SECRET`, Apple authentication/encryption values, and APNs values. Keep any existing values unchanged during the initial cutover. Optional unset integrations remain disabled. Get Apple root certificates from [Apple PKI](https://www.apple.com/certificateauthority/) and use the root that matches the App Store JWS chain.

Configure `PUBLIC_API_BASE_URL=https://api.acasillas.com/reciapp` and `CORS_ORIGINS=https://api.acasillas.com`. Set `WORKER_ENABLED=true` on both API and worker: API uses durable queue mode and checks the worker heartbeat; only the separate worker process consumes jobs. Both use the same OpenAI key through the Coolify resource environment. `APNS_ENVIRONMENT=production` selects APNs delivery host; it does not control StoreKit transaction validation. `APPLE_ENVIRONMENT=Production` accepts only App Store Production transactions. TestFlight transactions and notifications use Apple Sandbox, so they will be rejected by this production-only server configuration; don't change it to Sandbox on a server that also serves App Store users.

## Safe cutover

1. Do not create a second API resource on the same Traefik route. Production currently uses an API Coolify resource and a separate `reciapp-worker`; this differs from the target single Compose resource above. Update the existing resources or plan a coordinated swap.
2. In Coolify, prepare the Git resource and runtime variables; inspect resolved Compose configuration without printing secret values.
3. Confirm `coolify` and `reciapp-internal` Docker networks exist; confirm the current PostgreSQL, Redis, and covers volume/path have backups and remain attached/available.
4. Choose a controlled redeploy window. Keep the existing database and cover volume attached. Redeploy API and worker from the same server commit, then check API `/health` and `/ready`, worker heartbeat, authentication, recipe extraction, covers, billing guard, and Superwall/Apple webhook signatures.
5. Keep the old deployment definition and encrypted backups for rollback. If any check fails, stop the Coolify resource and restore the old API/worker with its prior environment and route. Do not run schema initialization or destructive migrations as part of this switch.

The Compose manifest is source preparation only. It does not prove Coolify variables, runtime network access, backup restore, deployment, DNS, HTTPS, or production flows. Verify each at cutover.
