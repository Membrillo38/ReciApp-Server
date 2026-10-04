#!/usr/bin/env bash
# Bounded live matrix: supplied TikTok sources, warm cache, Spanish handoff,
# and repeated polling. Never prints tokens, transcripts, or provider payloads.
set -euo pipefail

API="${API:?Set API to the intended ReciApp API base URL, including any path prefix}"
API_KEY="${API_KEY:?Set API_KEY}"
AUTH_JWT_SECRET="${AUTH_JWT_SECRET:?Set AUTH_JWT_SECRET}"
REPEATS="${REPEATS:-2}"
# Backend allows 10-minute media processing; keep a small margin.
POLL_ATTEMPTS="${POLL_ATTEMPTS:-330}"
POLL_SECONDS="${POLL_SECONDS:-2}"

URLS=(
  "https://www.tiktok.com/@kyfitjourney/video/7628655438912900374"
  "https://www.tiktok.com/@success.fitness/photo/7567845442407501063"
  "https://www.tiktok.com/@jackearly/video/7644717707174186253"
  "https://www.tiktok.com/@_nada_dl/video/7654273990533434657"
)

json_value() {
  local key="$1"
  python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$key"
}

json_summary() {
  python3 -c '
import json, sys
payload = json.load(sys.stdin)
recipe = payload.get("recipe") or {}
images = recipe.get("carousel_image_urls") or []
print("status=%s cache=%s job=%s progress=%s recipe=%s language=%s carousel=%s" % (
    payload.get("status", ""), payload.get("cache_hit", ""),
    payload.get("job_id", ""), payload.get("progress", ""),
    payload.get("recipe_id") or recipe.get("id") or "",
    recipe.get("language_code", ""), len(images)))
'
}

request_extract() {
  local url="$1"
  local language="$2"
  curl -fsS --retry 3 --retry-delay 1 --retry-all-errors --max-time 120 -X POST "$API/v1/extract" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"url":sys.argv[1],"language":sys.argv[2]}))' "$url" "$language")"
}

poll_job() {
  local job_id="$1"
  local language="$2"
  local attempt response next_id status
  for attempt in $(seq 1 "$POLL_ATTEMPTS"); do
    response=$(curl -fsS --retry 3 --retry-delay 1 --retry-all-errors --max-time 120 \
      "$API/v1/jobs/$job_id?language=$language" \
      -H "Authorization: Bearer $TOKEN")
    next_id=$(printf '%s' "$response" | json_value job_id)
    [ -n "$next_id" ] && job_id="$next_id"
    status=$(printf '%s' "$response" | json_value status)
    if [ "$status" = "completed" ] || [ "$status" = "failed" ]; then
      printf 'poll=%s ' "$attempt"
      printf '%s' "$response" | json_summary
      [ "$status" = "completed" ]
      return
    fi
    sleep "$POLL_SECONDS"
  done
  echo "poll=timeout job=$job_id"
  return 1
}

run_case() {
  local label="$1"
  local url="$2"
  local language="$3"
  local response job_id
  response=$(request_extract "$url" "$language")
  printf '%s language=%s ' "$label" "$language"
  printf '%s' "$response" | json_summary
  job_id=$(printf '%s' "$response" | json_value job_id)
  poll_job "$job_id" "$language"
}

echo "health=$(curl -fsS --retry 3 --retry-delay 1 --retry-all-errors --max-time 20 "$API/health" | json_value status)"

EMAIL="e2e-matrix-$(date +%s)@reciapp.test"
CREATED=$(curl -fsS --retry 3 --retry-delay 1 --retry-all-errors --max-time 30 -X POST "$API/v1/admin/users" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"email":sys.argv[1],"display_name":"E2E Matrix"}))' "$EMAIL")")
TOKEN=$(CREATED="$CREATED" EMAIL="$EMAIL" AUTH_JWT_SECRET="$AUTH_JWT_SECRET" python3 -c '
import json, os, time, jwt
user_id = json.loads(os.environ["CREATED"])["items"][0]["id"]
now = int(time.time())
print(jwt.encode({
    "sub": user_id,
    "email": os.environ["EMAIL"],
    "iss": "reciapp-api",
    "aud": "reciapp-ios",
    "iat": now,
    "exp": now + 3600,
}, os.environ["AUTH_JWT_SECRET"], algorithm="HS256"))
')
[ -n "$TOKEN" ]

for url in "${URLS[@]}"; do
  label="${url##*/}"
  run_case "$label first" "$url" "en-US"
  if [ "$REPEATS" -gt 1 ]; then
    for repeat in $(seq 2 "$REPEATS"); do
      run_case "$label repeat-$repeat" "$url" "en-US"
    done
  fi
  run_case "$label spanish" "$url" "es-ES"
done

echo "matrix=ok"
