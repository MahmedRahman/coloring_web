#!/usr/bin/env bash
# Bring coloring_web + public tunnel back after reboot / power loss.
set -euo pipefail

DIR="/home/webadmin/coloring_web"
LOG="/home/webadmin/coloring_web-boot.log"
URL_FILE="/home/webadmin/coloring_web.public_url"
LT_PID_FILE="/tmp/coloring_lt.pid"
LT_OUT="/tmp/coloring_lt.out"
FORCE="${FORCE:-0}"

mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
echo "==== $(date -Is) ensure-up start (FORCE=$FORCE) ===="

for i in $(seq 1 60); do
  if docker info >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: docker not ready"
  exit 1
fi

cd "$DIR"
docker compose up -d

sync_app_url() {
  local url="$1"
  echo "$url" > "$URL_FILE"
  local old
  old="$(grep -E '^APP_URL=' .env | head -1 | cut -d= -f2- || true)"
  if [[ "$old" != "$url" ]]; then
    if grep -qE '^APP_URL=' .env; then
      sed -i "s|^APP_URL=.*|APP_URL=$url|" .env
    else
      printf '\nAPP_URL=%s\n' "$url" >> .env
    fi
    echo "APP_URL updated: ${old:-<empty>} -> $url"
    docker compose up -d --force-recreate
  else
    echo "APP_URL unchanged ($url)"
  fi
}

extract_lt_url() {
  grep -oE 'https://[a-z0-9.-]+\.lhr\.life' "$LT_OUT" 2>/dev/null | tail -1 || true
}

lt_alive() {
  [[ -f "$LT_PID_FILE" ]] && kill -0 "$(cat "$LT_PID_FILE")" 2>/dev/null
}

start_localhost_run() {
  # Prefer localhost.run — trycloudflare quick tunnels currently return NXDOMAIN here
  if [[ "$FORCE" != "1" ]] && lt_alive; then
    local existing
    existing="$(extract_lt_url)"
    if [[ -n "$existing" ]] && curl -sf -m 5 "$existing/app" >/dev/null 2>&1; then
      echo "localhost.run already healthy: $existing"
      sync_app_url "$existing"
      return 0
    fi
  fi

  if lt_alive; then
    kill "$(cat "$LT_PID_FILE")" 2>/dev/null || true
  fi
  # also clear any older anonymous ssh tunnel
  pkill -f 'nokey@localhost.run' 2>/dev/null || true
  sleep 1

  : > "$LT_OUT"
  nohup ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes \
    -R 80:127.0.0.1:5001 nokey@localhost.run \
    >"$LT_OUT" 2>&1 &
  echo $! > "$LT_PID_FILE"

  local url=""
  for i in $(seq 1 40); do
    url="$(extract_lt_url)"
    if [[ -n "$url" ]]; then
      break
    fi
    sleep 1
  done

  if [[ -z "$url" ]]; then
    echo "ERROR: localhost.run URL not found"
    cat "$LT_OUT" || true
    return 1
  fi

  echo "public_url=$url"
  sync_app_url "$url"
  return 0
}

# Stop broken quick tunnels so they don't confuse ops
if docker ps -a --format '{{.Names}}' | grep -qx coloring_tunnel; then
  docker update --restart no coloring_tunnel >/dev/null 2>&1 || true
  docker stop coloring_tunnel >/dev/null 2>&1 || true
fi

if ! start_localhost_run; then
  echo "WARN: public tunnel failed; LAN still available on :5001"
fi

for i in $(seq 1 20); do
  code="$(curl -sS -m 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:5001/ || true)"
  if [[ "$code" == "200" ]]; then
    echo "health ok ($code)"
    echo "==== $(date -Is) ensure-up done ===="
    exit 0
  fi
  sleep 2
done

echo "WARN: local health check failed"
echo "==== $(date -Is) ensure-up done with warnings ===="
exit 0
