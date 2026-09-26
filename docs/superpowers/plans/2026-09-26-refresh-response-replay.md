# Refresh Response Replay Implementation Plan

> **For agentic workers:** Execute this plan inline with a review checkpoint after the server and client halves.

**Goal:** Let ReciApp safely retry one refresh request when the server rotated the refresh token but the HTTP response was lost.

**Architecture:** The client persists a UUID request ID beside its current session before calling `/v1/auth/refresh`. The server stores that ID and the successor token hash on the revoked row for 15 minutes; it derives the successor token deterministically with HMAC over the old token hash and request ID, so a retry returns the same token without storing plaintext. Logout with the pending request ID revokes both the old token and its successor.

**Tech Stack:** FastAPI, PostgreSQL, psycopg, Swift Keychain, `ClientStateHarness`.

**Spec:** `docs/2026-09-26-app-server-integration-audit.md` (refresh/session contract and rollout constraints).

## Global Constraints

- Existing clients that omit `request_id` keep one-time random token rotation.
- Reuse the same request ID only for retrying the same refresh token.
- Keep the replay window at 900 seconds; replay must reject closed accounts, expired windows, and revoked/expired successors.
- Store only hashes and request IDs in PostgreSQL; never store/log raw refresh tokens.
- Migration is expand-only. Rollback means stop sending `request_id` and ignore the added nullable columns.
- Deploy migration and API before the iOS build that persists and sends request IDs.

---

### Task 1: Make server refresh rotation replayable

**Files:**
- Create: `migrations/009_refresh_rotation_replay.sql`
- Modify: `app/models.py`
- Modify: `app/auth_tokens.py`
- Modify: `app/main.py`
- Modify: `app/db.py`
- Modify: `migrations/README.md`
- Test: `tests/test_auth_refresh_rotation.py`
- Test: `tests/test_readiness_schema.py`

**Interfaces:**
- `AuthRefreshRequest.request_id: UUID | None = None`.
- `rotate_refresh_token(raw: str, request_id: UUID | None = None) -> dict | None` returns the same refresh token for same-ID retries within 900 seconds.
- `revoke_refresh_token(raw: str, request_id: UUID | None = None) -> None` revokes the successor when the matching old-token request is pending.

- [x] Add tests proving: same request ID returns the same derived token; a different ID is rejected after rotation; replay is bounded to 900 seconds; closed profiles cannot replay; old clients without request IDs still rotate randomly; logout revokes the replay successor.
- [x] Add nullable `rotation_request_id uuid`, `rotation_retry_until timestamptz`, and `rotated_token_hash text` to `auth_refresh_tokens` with `ADD COLUMN IF NOT EXISTS`.
- [x] Derive the request-bound successor using HMAC-SHA256 with `AUTH_JWT_SECRET` and a fixed domain separator; hash it before inserting into `auth_refresh_tokens`.
- [x] In one SQL statement, lock and revoke the active token, record replay metadata, and insert the successor. If no active token rotated, query the revoked row by old-token hash and matching request ID; only return a replay before its deadline, while the profile is open, and while successor is active.
- [x] Update logout so a matching pending request revokes the stored successor hash too.
- [x] Extend readiness checks to require all three 009 columns, so an API build using them cannot report ready against a pre-009 schema.
- [x] Document the 008 then 009 rollout order and rollback behavior.
- [x] Run the full server suite: 251 passed, 2 skipped.

### Task 2: Persist one request ID across client retries

**Files:**
- Modify: `ReciApp/Services/AuthService.swift`
- Modify: `ReciApp/Services/ClientStatePolicy.swift` only if a pure request-ID policy is needed for harness coverage.
- Modify: `ClientStateHarness/Tests/ClientStateHarnessTests/ClientStatePolicyTests.swift` only if that policy is extracted.

**Interfaces:**
- `Session.refreshRequestID: UUID?` is optional so old Keychain-encoded sessions still decode.
- `RefreshAuthRequest` and logout request send the pending UUID as `request_id` when present.

- [x] Add focused policy coverage for reusing an existing request ID and creating one only when absent.
- [x] Before the network call, save the pending request ID in Keychain; if this save fails, do not call the server.
- [x] Retry/restore uses the same pending UUID with the old refresh token. Successful response replaces the session and clears the pending ID.
- [x] If the user signs out while a refresh result is uncertain, include the pending ID in logout so the server revokes the unknown successor.
- [x] Run `swift test` in `ClientStateHarness`: 47 tests passed.
- [x] Build the iOS Simulator target (unsigned build succeeded). `refreshRequestID` is optional and synthesized `Codable` decodes it with `decodeIfPresent`; no dedicated fixture exists yet.

### Task 3: Verify rollout and compatibility

- [x] Run final server tests (251 passed, 2 skipped), harness tests (47 passed), simulator build, and `git diff --check`.
- [x] Verify migration 009 is additive and leaves existing refresh rows unchanged.
- [x] Verify readiness requires all three 009 columns.
- [x] Keep production rollout blocked until migrations 008 and 009 are applied and the new server build is deployed; neither migration is applied as part of this code change.
