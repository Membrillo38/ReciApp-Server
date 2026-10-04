# ReciApp security runbook

## Security boundary

- `API_KEY`, `OPENAI_API_KEY`, `AUTH_JWT_SECRET`, dashboard secrets, webhook secrets and Sign in with Apple private key material stay on the VPS. Never put them in iOS, logs, CI output or dashboard responses.
- The admin `API_KEY` is server-only.
- Never log `identity_token`, `authorization_code`, Apple refresh tokens or the Apple `client_secret` JWT.

## Deploy order

1. Apply `migrations/001_init.sql` on self-hosted Postgres. For API-only deployment keep `WORKER_ENABLED=false`; the Coolify Compose stack runs API and worker together with the durable worker enabled.
2. Set `DASHBOARD_TOTP_SECRET`, `DASHBOARD_SESSION_SECRET`, `BILLING_GUARD_ENABLED=true` and budget variables on the VPS.
3. Configure Apple App Store Server Notifications V2 at `/v1/webhooks/apple`. The public Apple Root CA - G3 is bundled; set `APPLE_ROOT_CA_PEM` only when rotating or overriding that trust anchor.
4. Apply `migrations/004_apple_provider_tokens.sql`. Set `APPLE_TEAM_ID`, `APPLE_KEY_ID` and `APPLE_PRIVATE_KEY` so `POST /v1/auth/apple` can exchange an optional `authorization_code` and encrypt the Apple refresh token.
5. Configure Superwall webhook signing. Unsigned events are rejected.
6. Configure provider-side spend limits and alerts at 50%, 75%, 90% and 100%.
7. Deploy the web service and run `/health`, `/ready`, then the recipe matrix with `AUTH_JWT_SECRET`. Keep `authorization_code` optional until the iOS client is updated; do not require the field.

## Dashboard

- Dashboard requires password, session secret and TOTP; without all three it stays disabled.
- Session cookie: `HttpOnly`, `Secure`, `SameSite=Strict`, path `/dashboard`.

## Operator controls

- Kill switch: set `BILLING_GUARD_ENABLED=false` only to deliberately stop protected extraction; the API fails closed with `503`.
- Daily/monthly/user budgets: `DAILY_API_BUDGET_CENTS`, `MONTHLY_API_BUDGET_CENTS`, `USER_MONTHLY_BUDGET_CENTS`.

## Securitymaxxing audit

See `docs/SECURITYMAXXING_CHECKLIST.md` (mapped from millee.md reel `DbMkXnBuTcb`).

## Data lifecycle and recovery

- Encrypted Postgres backups on the VPS (`~/Desktop/Server/deploy/postgres-backup.sh`). Test restore on a disposable database.
- Account deletion revokes the stored Apple refresh token (`POST https://appleid.apple.com/auth/revoke`), deletes recipes/library data, revokes app refresh tokens and soft-closes the profile. `apple_sub` is kept so the same Apple ID reactivates the same profile and cannot reset the free yearly recipe quota by delete+recreate. `usage_events` stay attached for quota. A second `DELETE /v1/me` is idempotent. Closed sessions return `ACCOUNT_DELETED` or `ACCOUNT_UNAVAILABLE`.
- Accounts that signed in before an Apple refresh token was stored cannot be revoked at Apple; keep the iOS manual-recovery path until those sessions age out.
