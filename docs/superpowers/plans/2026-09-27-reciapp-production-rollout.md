# ReciApp Production Rollout Implementation Plan

> **For agentic workers:** Execute inline in this session. Preserve production backup and rollback image until acceptance checks pass.

**Goal:** Deploy the reviewed ReciApp server branch, durable worker, database security policies, and remote recipe notifications with verified rollback paths.

**Architecture:** Apply a dedicated idempotent RLS policy migration without replacing production business functions. Rehearse it against a restored, isolated production backup, then deploy one server image to API and worker. Enable APNs only after verifying the Apple key type and app push entitlement.

**Tech Stack:** FastAPI, PostgreSQL 16, Docker, Coolify, APNs, Swift/iOS.

**Spec:** `docs/2026-09-26-app-server-integration-audit.md`

## Global Constraints

- Keep production backup before schema or role changes.
- Never print or commit secret values, APNs private key, database URL, or API tokens.
- Do not replace production functions whose live definitions differ without rehearsing and comparing behavior.
- Keep old API image available until new API and worker pass health and readiness checks.
- Keep APNs disabled unless key type, topic, and app entitlement are confirmed.
- Do not claim physical-device or App Store proof without a signed build and connected iPhone.

---

### Task 1: Rehearse database security migration

**Files:**
- Create: `migrations/011_row_level_security_hardening.sql`
- Modify: `migrations/README.md`
- Test: `tests/test_reliability_contract.py`

**Interfaces:**
- Consumes: helpers, tables, and policy intent from migrations 001, 002, and 010.
- Produces: idempotent RLS policies that restrict extract jobs to owner/shared users and keep anonymized spend events service-only.

- [x] Restore the verified pre-migration database backup into an isolated temporary database on PostgreSQL 16.
- [x] Apply 008–011; change temporary object ownership to a non-superuser test role; verify owner/shared visibility, other-account denial, anonymized-row isolation, service access, and durable job claiming.
- [x] Remove the temporary database and test role after rehearsal.
- [x] Apply 011 in production after the backup; it committed successfully and RLS is enabled on 18 public tables.
- [x] Provision `reciapp_runtime` with no superuser or RLS bypass, grant app access/default privileges, and switch API/worker database URLs to it. The provider protects the bootstrap `reciapp` role from demotion; it is no longer used by runtime services.

### Task 2: Deploy API and durable worker

**Files:**
- Use: `Dockerfile`, `docker-compose.worker.yml`, `app/worker.py`
- Update: production API and worker containers from the same commit.

**Interfaces:**
- Consumes: migration 008–011 schema and the `reciapp` runtime role.
- Produces: `/ready` success only after a fresh worker heartbeat; one lease claimant for each open extraction.

- [x] Build branch image `4e10837` in isolated production staging.
- [x] Start worker from the same image; confirm a fresh `recipe-worker` heartbeat.
- [x] Switch API to that image with `WORKER_ENABLED=true`; verify `/health` and `/ready` return 200.
- [x] Run a production RLS smoke test through the API connection: `current_user=reciapp_runtime`; a random user context sees zero profiles/jobs, and service context sees the worker heartbeat.

### Task 3: Enable APNs and verify iOS delivery

**Files:**
- Verify: `ReciApp.xcodeproj/project.pbxproj` and app entitlements.
- Configure: production secret store only.

**Interfaces:**
- Consumes: valid APNs team/key/topic and app push entitlement.
- Produces: localized APNs delivery and visible recipe-ready notification on a signed iPhone build.

- [ ] Confirm an APNs-valid `.p8` key and topic `com.membri.reciapp`. The authorized candidate key signed locally, but Apple returned `InvalidProviderToken` on a dummy-token probe; the key was removed from the VPS and APNs remains disabled pending a valid key/Team ID confirmation.
- [ ] Enable APNs on API and worker, then register a device and verify delivery.
- [ ] Run signed-device login, import, Pro restore, and notification acceptance checks. Current host has no valid Apple signing identity or connected iPhone, so this remains unverified.
