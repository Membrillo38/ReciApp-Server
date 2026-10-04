#!/usr/bin/env bash
# E2E smoke: admin user → JWT → extract → poll job
set -euo pipefail

API="${API:?Set API to the intended ReciApp API base URL, including any path prefix}"
URL="${1:-https://vm.tiktok.com/ZGdQJr1J4/}"
LANGUAGE="${LANGUAGE:-en-US}"
API_KEY="${API_KEY:?Set API_KEY}"
AUTH_JWT_SECRET="${AUTH_JWT_SECRET:?Set AUTH_JWT_SECRET}"

EMAIL="e2e-$(date +%s)@reciapp.test"
PASS="$(openssl rand -base64 18)"

echo "== health =="
curl -fsS --retry 3 --retry-delay 1 --retry-all-errors --max-time 20 "$API/health"
echo

echo "== create user =="
CREATED=$(curl -fsS --retry 3 --retry-delay 1 --retry-all-errors --max-time 30 -X POST "$API/v1/admin/users" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d "{\"email\":\"$EMAIL\",\"display_name\":\"E2E\"}")
echo "$CREATED" | python3 -m json.tool

echo "== mint jwt =="
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
echo "token len ${#TOKEN}"

echo "== me =="
curl -fsS --retry 3 --retry-delay 1 --retry-all-errors --max-time 30 "$API/v1/me" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

echo "== extract $URL =="
JOB_JSON=$(curl -fsS --retry 3 --retry-delay 1 --retry-all-errors --max-time 120 -X POST "$API/v1/extract" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"url\":\"$URL\",\"language\":\"$LANGUAGE\"}")
echo "$JOB_JSON" | python3 -m json.tool
JOB_ID=$(echo "$JOB_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['job_id'])")

echo "== poll job $JOB_ID =="
for i in $(seq 1 330); do
  R=$(curl -fsS --retry 3 --retry-delay 1 --retry-all-errors --max-time 120 "$API/v1/jobs/$JOB_ID?language=$LANGUAGE" -H "Authorization: Bearer $TOKEN")
  # A completed base extraction may hand off to a shared translation job.
  NEXT_JOB_ID=$(echo "$R" | python3 -c "import json,sys; print(json.load(sys.stdin).get('job_id',''))")
  if [ -n "$NEXT_JOB_ID" ]; then
    JOB_ID="$NEXT_JOB_ID"
  fi
  STATUS=$(echo "$R" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")
  echo "poll $i: $STATUS"
  if [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ]; then
    echo "$R" | python3 -m json.tool
    [ "$STATUS" = "completed" ] || exit 1
    break
  fi
  sleep 2
done

echo "== my recipes =="
curl -fsS --retry 3 --retry-delay 1 --retry-all-errors --max-time 30 "$API/v1/me/recipes" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

echo "OK"
