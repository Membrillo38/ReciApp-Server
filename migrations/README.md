# ReciApp PostgreSQL Migrations

Apply the bootstrap schema to a fresh self-hosted PostgreSQL database:

```sh
psql "$DATABASE_URL" -f migrations/001_init.sql
```

Apply in order:

```sh
psql "$DATABASE_URL" -f migrations/001_init.sql
psql "$DATABASE_URL" -f migrations/002_row_level_security.sql
psql "$DATABASE_URL" -f migrations/003_free_yearly_limit.sql
psql "$DATABASE_URL" -f migrations/004_apple_provider_tokens.sql
psql "$DATABASE_URL" -f migrations/005_pro_monthly_default.sql
psql "$DATABASE_URL" -f migrations/006_pro_margin_40.sql
psql "$DATABASE_URL" -f migrations/007_free_yearly_limit_3.sql
psql "$DATABASE_URL" -f migrations/008_extract_delivery_idempotency.sql
psql "$DATABASE_URL" -f migrations/009_refresh_rotation_replay.sql
psql "$DATABASE_URL" -f migrations/010_apns_recipe_completion.sql
psql "$DATABASE_URL" -f migrations/011_row_level_security_hardening.sql
psql "$DATABASE_URL" -f migrations/012_user_library_state.sql
psql "$DATABASE_URL" -f migrations/013_user_library_state_history.sql
psql "$DATABASE_URL" -f migrations/014_runtime_role_grants.sql
```

Antes de la migración 014, crea `reciapp_runtime` desde una conexión administrativa: `CREATE ROLE reciapp_runtime LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '<secreto>'`. Usa esa contraseña solo en el `DATABASE_URL` de Coolify para API y worker. Ejecuta las migraciones con el usuario propietario/administrador, no con `reciapp_runtime`; no copies su URL privilegiada a los servicios.

`001_init.sql` is the bootstrap schema. `002_row_level_security.sql` forces RLS on app tables. The API sets `app.actor` and `app.user_id` per transaction. `005_pro_monthly_default.sql` sets fair-use default to monthlyized weekly mid-band (~$9.99/wk). `006_pro_margin_40.sql` raises Pro fair-use margin to 40%.

`007_free_yearly_limit_3.sql` finalizes the Free product rule at 3 new recipes per UTC calendar year and clears stale per-profile overrides.

`008_extract_delivery_idempotency.sql` adds an optional client delivery UUID to extract jobs. Apply it before deploying an API build that accepts `client_delivery_id`.

`009_refresh_rotation_replay.sql` adds nullable replay metadata for idempotent refresh retries. Apply after migration 008 and before deploying an API build that reads or writes these columns. The migration is additive; old clients that omit `request_id` retain one-time refresh-token rotation.

`010_apns_recipe_completion.sql` adds per-device APNs registrations, a durable completion-delivery queue, and worker heartbeat. Apply before deploying code that registers push devices. It is additive and safe to leave in place during a code rollback. `/ready` verifies the 010 tables, completion trigger, logout cleanup function, and a fresh worker heartbeat when durable-worker mode is enabled. To enable sends, configure `APNS_ENABLED=true`, `APNS_TEAM_ID`, `APNS_KEY_ID`, and `APNS_AUTH_KEY` on both API and worker; `APNS_TOPIC` must match the app bundle identifier and defaults to `com.membri.reciapp`. Keep the `.p8` key in a secret store, never in the repository. Push title/body follow the selected ReciApp language using `app/apns_localizations.json`, generated from `ReciApp/Localizable.xcstrings` with `python scripts/sync_apns_localizations.py`. The API reports push enabled only when APNs credentials and a fresh durable worker heartbeat are both available; otherwise iOS keeps its local-notification fallback. The worker retries transient APNs failures, removes unregistered tokens, and never stores successful device-token copies in the delivery queue. Migration 010 is safe to rerun after partial application and creates `app_is_service()` / `app_user_id()` if an older database never applied migration 002.

`011_row_level_security_hardening.sql` reapplies FORCE RLS policies without replacing business functions. It restricts extract-job rows to their owner or explicit shared access, and keeps anonymized usage/spend rows inaccessible to ordinary users. Apply it before removing superuser/BYPASSRLS from the `reciapp` role.

`012_user_library_state.sql` adds one revisioned JSONB snapshot per account for iOS library organization and other user-owned client state. It is additive and RLS-protected for service-role access. Apply it before deploying the matching API/iOS sync code; `/ready` verifies the table, columns, FORCE RLS, and service policy. Rollback by reverting application code; retain the table and snapshots to avoid data loss.

`013_user_library_state_history.sql` adds a bounded history of the three previous server snapshots. Apply before deploying API code that returns or records history; `/ready` verifies the JSONB column and its array constraint. Each successful compare-and-swap update archives the prior snapshot atomically. Rollback by reverting API code; keep the column and history data.

`014_runtime_role_grants.sql` verifies `reciapp_runtime` is a login role without superuser or RLS-bypass privileges, then grants the table, sequence, and exact function permissions used by the API/worker. FORCE RLS still applies. Apply with a database owner/admin after migrations 001–013 and before changing Coolify `DATABASE_URL`.
