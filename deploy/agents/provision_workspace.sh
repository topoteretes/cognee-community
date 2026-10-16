#!/usr/bin/env bash
# Provision one cognee tenant for one Optio workspace: a dedicated cognee user
# plus an API key for its agent pods. Prints JSON on stdout; everything else
# goes to stderr. Run it from Optio when a workspace is created, or by hand.
#
#   provision_workspace.sh <workspace-id> [key-name]
#
# Environment:
#   COGNEE_BASE_URL      cognee API as reachable from here (default https://mem.litetracker.com)
#   COGNEE_WS_DOMAIN     domain of the synthetic user email (default optio.internal)
#   COGNEE_WS_PASSWORD   reuse an existing workspace user (e.g. to mint a new key);
#                        when unset a fresh random password is generated and the
#                        user must not exist yet
#
# The password is only needed to mint further keys later (rotation), so store it
# with the key or discard it and re-register under a new id if a key is lost.
#
# Isolation model: the cognee *user* is the tenant. Datasets it creates are its
# own ArcadeDB databases, nothing is shared unless granted explicitly, and the
# plugin is told COGNEE_SHARED_AGENT_MEMORY=false so it never mints sub-users.
set -euo pipefail

usage() { sed -n '2,20p' "$0" >&2; exit 2; }

[ $# -ge 1 ] || usage
workspace="$1"
key_name="${2:-optio}"
base="${COGNEE_BASE_URL:-https://mem.litetracker.com}"
base="${base%/}"
domain="${COGNEE_WS_DOMAIN:-optio.internal}"

case "$workspace" in
  *[!A-Za-z0-9._-]*|"") echo "workspace id must match [A-Za-z0-9._-]+" >&2; exit 2 ;;
esac

for tool in curl python3 openssl; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 2; }
done

email="ws-${workspace}@${domain}"
if [ -n "${COGNEE_WS_PASSWORD:-}" ]; then
  password="$COGNEE_WS_PASSWORD"
  reuse=1
else
  password="$(openssl rand -hex 24)"
  reuse=0
fi

json_get() { python3 -c 'import json,sys; d=json.load(sys.stdin); v=d.get(sys.argv[1], ""); print(v if v is not None else "")' "$1"; }

# curl wrapper: sets $status (HTTP code) and $body. Not used inside $(...),
# so the assignments survive (a subshell would drop them).
status=""
body=""
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
call() {
  status="$(curl -sS -m 30 -o "$tmp" -w '%{http_code}' "$@")" || { echo "curl failed talking to $base" >&2; exit 1; }
  body="$(cat "$tmp")"
}

# 1. Register the workspace user (skipped when reusing an existing one).
if [ "$reuse" -eq 0 ]; then
  call -X POST "$base/api/v1/auth/register" \
    -H 'Content-Type: application/json' \
    --data-binary "$(python3 -c 'import json,sys; print(json.dumps({"email": sys.argv[1], "password": sys.argv[2]}))' "$email" "$password")"
  case "$status" in
    200|201) ;;
    400) echo "register failed ($status): $body" >&2
         echo "user $email already exists; rerun with COGNEE_WS_PASSWORD to mint a key for it" >&2; exit 1 ;;
    *)   echo "register failed ($status): $body" >&2; exit 1 ;;
  esac
  user_id="$(printf '%s' "$body" | json_get id)"
fi

# 2. Log in (form-encoded, fastapi-users).
call -X POST "$base/api/v1/auth/login" \
  --data-urlencode "username=$email" --data-urlencode "password=$password"
[ "$status" = "200" ] || { echo "login failed ($status): $body" >&2; exit 1; }
token="$(printf '%s' "$body" | json_get access_token)"
[ -n "$token" ] || { echo "login returned no access_token" >&2; exit 1; }

if [ "$reuse" -eq 1 ]; then
  call "$base/api/v1/users/me" -H "Authorization: Bearer $token"
  [ "$status" = "200" ] || { echo "users/me failed ($status): $body" >&2; exit 1; }
  user_id="$(printf '%s' "$body" | json_get id)"
fi

# 3. Mint the API key the pods will use (X-Api-Key header).
call -X POST "$base/api/v1/auth/api-keys" \
  -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
  --data-binary "$(python3 -c 'import json,sys; print(json.dumps({"name": sys.argv[1]}))' "$key_name")"
[ "$status" = "200" ] || [ "$status" = "201" ] || { echo "api key creation failed ($status): $body" >&2; exit 1; }
api_key="$(printf '%s' "$body" | json_get key)"
api_key_id="$(printf '%s' "$body" | json_get id)"
[ -n "$api_key" ] || { echo "api key response had no key" >&2; exit 1; }

# 4. Prove the key works before handing it out.
call "$base/api/v1/users/me" -H "X-Api-Key: $api_key"
[ "$status" = "200" ] || { echo "key verification failed ($status): $body" >&2; exit 1; }

python3 - "$workspace" "$email" "$password" "$user_id" "$api_key_id" "$api_key" "$base" <<'PY'
import json, sys
w, e, p, u, kid, k, b = sys.argv[1:]
print(json.dumps({
    "workspace": w,
    "cognee_base_url": b,
    "email": e,
    "password": p,
    "user_id": u,
    "api_key_id": kid,
    "api_key": k,
    "pod_env": {
        "COGNEE_BASE_URL": b,
        "COGNEE_API_KEY": k,
        "COGNEE_MANAGED_ENDPOINT": "true",
        "COGNEE_SHARED_AGENT_MEMORY": "false",
        "COGNEE_PLUGIN_DATASET": f"ws-{w}",
    },
}, indent=2))
PY
