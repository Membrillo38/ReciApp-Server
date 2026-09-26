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
```

`001_init.sql` is the bootstrap schema. `002_row_level_security.sql` forces RLS on app tables. The API sets `app.actor` and `app.user_id` per transaction. `005_pro_monthly_default.sql` sets fair-use default to monthlyized weekly mid-band (~$9.99/wk). `006_pro_margin_40.sql` raises Pro fair-use margin to 40%.

`007_free_yearly_limit_3.sql` finalizes the Free product rule at 3 new recipes per UTC calendar year and clears stale per-profile overrides.

`008_extract_delivery_idempotency.sql` adds an optional client delivery UUID to extract jobs. Apply it before deploying an API build that accepts `client_delivery_id`.

`009_refresh_rotation_replay.sql` adds nullable replay metadata for idempotent refresh retries. Apply after migration 008 and before deploying an API build that reads or writes these columns. The migration is additive; old clients that omit `request_id` retain one-time refresh-token rotation.
