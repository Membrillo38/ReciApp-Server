# ReciApp API

Backend: extract recipes from TikTok / YouTube / Instagram / Facebook.

- **Cache** by normalized URL (no duplicate OpenAI calls)
- **Postgres** on the VPS (users, recipes, usage)
- **Free:** 3 new recipes per UTC calendar year; cache hits do not consume quota
- **Pro:** unlimited with fair-use (keep ≥40% margin)
- **Admin:** `/dashboard` — users, recipes, jobs, usage, Postgres size

## Stack

- FastAPI + Docker on the VPS (Coolify)
- Self-hosted Postgres (`reciapp-postgres`)
- OpenAI: `gpt-6-luna` for recipe extraction and vision; `gpt-4o-mini-transcribe` for transcription

## Docs for iOS

See **[INTEGRACION_SWIFT.md](INTEGRACION_SWIFT.md)** and the complete [Swift client](docs/swift/ReciAppAPI.swift).

## Local

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

## Deploy

This repo is the API image (`Dockerfile`). VPS host ops (Traefik, Homepage, Fail2ban, compose) live in sibling `~/Desktop/Server`.

Apply PostgreSQL migrations `001_init.sql` through `009_refresh_rotation_replay.sql` in order; see [migrations/README.md](migrations/README.md). Health: `/health`. Ready: `/ready` requires the delivery-idempotency schema from `008` and refresh-replay columns from `009`.

For a bounded recipe verification matrix, set `API_KEY` and `AUTH_JWT_SECRET` and run `scripts/e2e_matrix.sh`.

## Quotas

| Plan | Limit |
|------|-------|
| Free | 3 new recipes / UTC calendar year; cache hits do not consume quota |
| Pro | Cache misses until monthly cost ≥ budget (`profiles.pro_monthly_price_cents × (1 - margin)`). Webhook monthlyizes weekly×52/12 and yearly÷12 into that field. Defaults in `app_settings`. |

## Verification

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```
