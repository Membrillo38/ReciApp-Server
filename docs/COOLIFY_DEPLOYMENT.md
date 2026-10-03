# ReciApp deployment through Coolify

`docker-compose.coolify.yml` is the target Coolify Git resource. It runs API and worker from this repository while reusing the existing `reciapp-internal` network, PostgreSQL, Redis, and `/var/lib/reciapp/covers`. It does not create, initialize, or migrate a database.

## Coolify runtime variables

Create runtime Environment Variables/Secrets on the single ReciApp Coolify Compose resource. Compose passes shared values to both API and worker. The user creates the OpenAI key; never put it in GitHub, iOS, build arguments, or logs.

Required:

- `OPENAI_API_KEY` — new ReciApp OpenAI project key.
- `API_KEY` — ReciApp server-only operator key.
- `DATABASE_URL` — existing ReciApp PostgreSQL connection; preserve current database and role.
- `REDIS_URL` — existing ReciApp Redis connection.
- `AUTH_JWT_SECRET` — existing signing secret; preserve it so current sessions remain valid.

Set existing non-empty integration secrets too when enabled: `SUPERWALL_WEBHOOK_SECRET`, `DASHBOARD_PASSWORD`, `DASHBOARD_SESSION_SECRET`, `DASHBOARD_TOTP_SECRET`, Apple authentication/encryption values, and APNs values. Keep any existing values unchanged during the initial cutover. Optional unset integrations remain disabled.

Configure `PUBLIC_API_BASE_URL=https://api.acasillas.com/reciapp` and `CORS_ORIGINS=https://api.acasillas.com`. The API receives `WORKER_ENABLED=false`; the worker receives `WORKER_ENABLED=true`. Both use the same OpenAI key through the Coolify resource environment.

## Safe cutover

1. Do not deploy this resource beside the current API on the same Traefik route. The current manual Compose deployment still owns that route.
2. In Coolify, prepare the Git resource and runtime variables; inspect resolved Compose configuration without printing secret values.
3. Confirm `coolify` and `reciapp-internal` Docker networks exist; confirm the current PostgreSQL, Redis, and covers volume/path have backups and remain attached/available.
4. Choose a controlled cutover window. Stop the old API/worker only after Coolify is ready to start against the same existing data services. Start Coolify resource, then check API `/health` and `/ready`, worker heartbeat, authentication, recipe extraction, covers, billing guard, and Superwall/Apple webhook signatures.
5. Keep the old deployment definition and encrypted backups for rollback. If any check fails, stop the Coolify resource and restore the old API/worker with its prior environment and route. Do not run schema initialization or destructive migrations as part of this switch.

The Compose manifest is source preparation only. It does not prove Coolify variables, runtime network access, backup restore, deployment, DNS, HTTPS, or production flows. Verify each at cutover.
