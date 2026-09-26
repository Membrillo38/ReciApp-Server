# Superwall server checklist (ReciApp)

The iOS and server source are wired. Live check on 2026-09-26: the webhook secret is present (value not read) and PostgreSQL contains 7 Superwall events received in the last 30 days, all processed and none failed (3 Pro-on, 2 Pro-off, 2 other status changes). This proves recent delivery and handling, but not a controlled sandbox purchase/restore followed by `/v1/me` on the same device. The endpoint URL below targets the VPS (not Render).

You still need App Store products plus a server that maps Superwall events to `profiles.is_pro`. The client never trusts StoreKit for Pro. After purchase it polls `GET /v1/me` until `is_pro` flips.

## Documented Superwall values

The values below come from the last documented dashboard inspection and can drift. Recent server events prove the endpoint is receiving events; check the dashboard before changing configuration.

| Item | Value |
| --- | --- |
| Project | ReciApp (`40844`) |
| Application | ReciApp iOS (`54783`), bundle `com.membri.reciapp`, App Store ID `6811032731` |
| Public key (in the app) | `pk_3QyV6dXg2nPMj9gDTpZkF` |
| Entitlement | `pro` |
| Products (today) | `reciapp_an_3trial` (yearly + 3-day trial), `reciapp_wk` (weekly) — prices still placeholder; A/B bands below |
| Campaign | **Pro** |
| Placements | `free_limit_reached`, `settings_upgrade` |
| Webhook | `https://51-255-43-100.sslip.io/v1/webhooks/superwall` |
| Identify | backend user UUID. Attribute `user_id` is the same UUID. That UUID is StoreKit `appAccountToken`. |

Events already selected on the webhook: `initial_purchase`, `renewal`, `cancellation`, `uncancellation`, `expiration`, `billing_issue`, `product_change`, `non_renewing_purchase`, `subscription_paused`.

## Pricing A/B (US list)

Target packaging: **yearly (default CTA) + weekly (anchor)**.

| Period | US A/B band | Notes |
| --- | --- | --- |
| Weekly | **$6.99 – $12.99** | No trial (trial only on yearly) |
| Yearly | **$34.99 – $69.99** | 3-day free trial on yearly arms |

Apple: **one list price per product ID**. Real price A/B = **clone SKUs** (e.g. `reciapp_wk_699`, `reciapp_wk_999`, `reciapp_an_3499_3trial`, `reciapp_an_4999_3trial`) and let Superwall assign arms. Server does not need a deploy when you change which arm wins — webhook writes whatever `price` Apple/Superwall send.

Server fair-use: `subscription_price_cents` stores the **period** list price; `pro_monthly_price_cents` stores **monthlyized** revenue (`weekly × 52/12`, `yearly ÷ 12`). OpenAI budget = that monthly figure × (1 − margin); default margin is **40%**. Cancellation / billing_issue past `expirationAt` revoke Pro.

## 1. App Store Connect (same product IDs as Superwall)

One subscription group **Pro**. Create the SKUs you will A/B (weekly + yearly clones). Example shape:

| Product ID pattern | Duration | Price band (USD) | Intro offer |
| --- | --- | --- | --- |
| `reciapp_an_*_3trial` | 1 year | $34.99 – $69.99 | 3-day free trial |
| `reciapp_wk_*` | 1 week | $6.99 – $12.99 | none |

They must match Superwall product IDs exactly. Paid Applications Agreement + banking + tax must be active or StoreKit returns empty products.

Local Xcode testing uses `ReciApp/Config/ReciApp.storekit` (scheme ReciApp). StoreKit Configuration purchases **do not** fire Superwall webhooks. Sandbox Apple ID on device/TestFlight does.

## App Store Server Notifications

Superwall generated an ASSN v2 URL for this app in the last documented setup. Recent server events prove delivery is not currently empty, but do not prove Apple Server Notifications are configured for both **Production and Sandbox**. Verify those destinations before relying on renewal/refund updates; the end-to-end device check is still pending.

Optional: App-Specific Shared Secret in Superwall application settings. StoreKit 2 + ASSN v2 is enough for new subs; the shared secret is only for old receipt-verify fallback.

## 2. Webhook: set `is_pro` from Superwall

Endpoint: `POST /v1/webhooks/superwall` on the VPS (`https://51-255-43-100.sslip.io`).

Verify the Svix signature before touching the database. Superwall uses standard Svix headers (`svix-id`, `svix-timestamp`, `svix-signature`). Copy the signing secret from Superwall → Integrations → Webhooks. Store it as `SUPERWALL_WEBHOOK_SECRET` on the VPS (Coolify env). Never put it in the iOS app.

Payload shape:

```json
{
  "object": "event",
  "type": "initial_purchase",
  "projectId": 40844,
  "applicationId": 54783,
  "timestamp": 1754067715103,
  "data": {
    "originalAppUserId": "<backend user UUID>",
    "originalTransactionId": "700002050981465",
    "productId": "reciapp_an_3trial",
    "environment": "SANDBOX",
    "periodType": "TRIAL",
    "expirationAt": 1756659704000,
    "userAttributes": {
      "user_id": "<backend user UUID>"
    }
  }
}
```

Resolve the user in this order:

1. `data.userAttributes.user_id` if it is a UUID (set by the SDK).
2. `data.originalAppUserId` if it is a UUID (not `$SuperwallAlias:…`).
3. Otherwise log and skip. Do not guess.

`originalAppUserId` is reliable only if `identify(userId:)` ran **before** the purchase. The app does that on Apple sign-in.

Grant / revoke:

| Event `type` | `is_pro` |
| --- | --- |
| `initial_purchase` | `true` (includes trial, `periodType` = `TRIAL`) |
| `renewal` | `true` |
| `uncancellation` | `true` |
| `product_change` | `true` if still in the Pro group |
| `expiration` | `false` |
| `cancellation` | keep `true` until `expirationAt` |
| `billing_issue` | keep `true` during Apple grace; `false` if expired |

Idempotency: key on `data.id` or `(originalTransactionId, type, timestamp)`. Retries happen.

`GET /v1/me` must return the new `is_pro` immediately after the row update. The app waits 0s / 2s / 5s / 10s because the webhook can arrive after StoreKit’s success callback.

Also persist `original_transaction_id` if you want restore/support lookups. Do not trust a client `is_pro` flag.

## 3. Optional: App Store Server Notifications V2

If Superwall is already subscribed to ASSN v2 for this app, you do **not** also need a duplicate ASC webhook on the same events. Superwall normalizes them into the payload above. Keep one source of truth: this Superwall webhook.

If you already ingest ASC notifications on the server, stop writing `is_pro` from that path once Superwall is live, or you will fight the two pipelines.

## 4. VPS env

```
SUPERWALL_WEBHOOK_SECRET=whsec_...
SUPERWALL_APPLICATION_ID=54783
PUBLIC_API_BASE_URL=https://51-255-43-100.sslip.io
```

Confirm the public URL is still `https://51-255-43-100.sslip.io`. If it changes, update the Superwall webhook URL (do not create a second endpoint). Point Superwall only at the VPS Coolify host — never at any old hosted URL.

## 5. Smoke test

1. Create the two IAPs in App Store Connect with the **final** US prices (or run the ReciApp scheme with `ReciApp.storekit` for UI-only).
2. In Superwall Editor, create the paywall. Attach **primary** = yearly arm, **secondary** = weekly arm. Feature gating: Non Gated. Swap product sets per A/B experiment.
3. On campaign **Pro**, replace Example Paywall with yours (audience “Users without Pro”).
4. Sandbox Apple ID: Settings → Upgrade to Pro, and import until quota to hit `free_limit_reached`.
5. Watch `POST /v1/webhooks/superwall` on the VPS, then `GET /v1/me` → `is_pro: true`.
6. Restore purchases on a second install with the same sandbox Apple ID.

Local StoreKit file: paywall UI and purchase sheet only. No webhook, so `is_pro` stays false unless you grant it by hand.
