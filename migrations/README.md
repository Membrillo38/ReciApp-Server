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
```

`001_init.sql` is the bootstrap schema. `002_row_level_security.sql` forces RLS on app tables. The API sets `app.actor` and `app.user_id` per transaction. `005_pro_monthly_default.sql` sets fair-use default to monthlyized weekly mid-band (~$9.99/wk). `006_pro_margin_40.sql` raises Pro fair-use margin to 40%.

`007_free_yearly_limit_3.sql` finalizes the Free product rule at 3 new recipes per UTC calendar year and clears stale per-profile overrides.

`008_extract_delivery_idempotency.sql` adds an optional client delivery UUID to extract jobs. Apply it before deploying an API build that accepts `client_delivery_id`.

`009_refresh_rotation_replay.sql` adds nullable replay metadata for idempotent refresh retries. Apply after migration 008 and before deploying an API build that reads or writes these columns. The migration is additive; old clients that omit `request_id` retain one-time refresh-token rotation.

`010_apns_recipe_completion.sql` adds per-device APNs registrations, a durable completion-delivery queue, and worker heartbeat. Apply before deploying code that registers push devices. It is additive and safe to leave in place during a code rollback. `/ready` verifies the 010 tables, completion trigger, logout cleanup function, and a fresh worker heartbeat when durable-worker mode is enabled. To enable sends, configure `APNS_ENABLED=true`, `APNS_TEAM_ID`, `APNS_KEY_ID`, and `APNS_AUTH_KEY` on both API and worker; `APNS_TOPIC` must match the app bundle identifier and defaults to `com.membri.reciapp`. Keep the `.p8` key in a secret store, never in the repository. Push title/body follow the selected ReciApp language using `app/apns_localizations.json`, generated from `ReciApp/Localizable.xcstrings` with `python scripts/sync_apns_localizations.py`. The API reports push enabled only when APNs credentials and a fresh durable worker heartbeat are both available; otherwise iOS keeps its local-notification fallback. The worker retries transient APNs failures, removes unregistered tokens, and never stores successful device-token copies in the delivery queue. Migration 010 is safe to rerun after partial application and creates `app_is_service()` / `app_user_id()` if an older database never applied migration 002.
