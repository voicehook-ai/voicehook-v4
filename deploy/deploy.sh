#!/usr/bin/env bash
# v4 deploy. Single agent service + caddy + livekit (docker run).
# Change-aware: the deployed commit is recorded on the box in
# /var/www/voicehook/.deployed-sha. If only web/** changed since then, only
# web/ is synced (no pip, no agent restart, no caddy reload). Anything else, or
# an unknown/dirty deployed SHA, takes the full path, which ALWAYS restarts the
# agent and reloads caddy. A 2nd run with no change is a no-op ("up to date").
# Before the full path touches anything, a box-local LiveKit ListRooms preflight
# aborts if any room still has a human participant (fails CLOSED on error).
#
#   BOX_HOST=root@<ip>  ./deploy/deploy.sh [--web-only|--full] [--dry-run] [--force-restart]
#   BOX_HOST=local      ./deploy/deploy.sh   # cloud-init first-boot
#
# SSH auth: ssh-agent via SSH_AUTH_SOCK (key from orb, never on disk, see
# docs/DEPLOY.md). SSH_KEY=<file> (-i) is only a fallback when no agent key is
# loaded and the file exists. SSH_KNOWN_HOSTS=<file> overrides known_hosts.
# DEPLOY_SSH=<cmd> replaces the ssh command (tests / stubs).
# CADDY_SITE_MAIN  (default voicehook.ai)        — public host for /, /api/*
# CADDY_SITE_RTC   (default rtc.voicehook.ai)    — public host for LK WSS
set -euo pipefail

MODE=auto DRY=0 FORCE_RESTART=0
for a in "$@"; do
  case "$a" in
    --web-only) MODE=web ;;
    --full) MODE=full ;;
    --dry-run) DRY=1 ;;
    --force-restart) FORCE_RESTART=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

BOX_HOST="${BOX_HOST:-root@voicehook.ai}"
SSH_KEY="${SSH_KEY:-${HOME:-/root}/.ssh/voicehook_v4}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SHA_FILE=/var/www/voicehook/.deployed-sha
EXCL=(--exclude='.env*' --exclude='__pycache__' --exclude='.venv' --exclude='.git' --exclude='*.egg-info')
RS=(rsync -az -i)
[ "${DRY}" -eq 1 ] && RS+=(-n)

case "${BOX_HOST}" in
  local|localhost|root@127.0.0.1|root@localhost) LOCAL=1 ;;
  *) LOCAL=0 ;;
esac
if [ "${LOCAL}" -eq 1 ]; then
  remote() { bash -c "$*"; }
  rs() { "${RS[@]}" "$@"; }
  dst() { echo "$1"; }
else
  if [ -n "${DEPLOY_SSH:-}" ]; then SSH="${DEPLOY_SSH}"
  else
    SSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20"
    [ -n "${SSH_KNOWN_HOSTS:-}" ] && SSH="${SSH} -o UserKnownHostsFile=${SSH_KNOWN_HOSTS}"
    if [ -n "${SSH_AUTH_SOCK:-}" ] && ssh-add -l >/dev/null 2>&1; then
      echo "==> ssh auth: ssh-agent (SSH_AUTH_SOCK)"
    elif [ "${SSH_KEY}" != "/dev/null" ] && [ -f "${SSH_KEY}" ]; then
      echo "==> ssh auth: key file fallback ${SSH_KEY}"; SSH="${SSH} -i ${SSH_KEY}"
    else
      echo "!! no ssh-agent key loaded and no key file at ${SSH_KEY}; see docs/DEPLOY.md" >&2; exit 1
    fi
  fi
  remote() { ${SSH} "${BOX_HOST}" "$@"; }
  rs() { "${RS[@]}" -e "${SSH}" "$@"; }
  dst() { echo "${BOX_HOST}:$1"; }
fi
run() { if [ "${DRY}" -eq 1 ]; then echo "   [dry-run] would run: $*"; else remote "$@"; fi; }

# ---- decide mode: web-only vs full --------------------------------------------
HEAD="$(git -C "${REPO}" rev-parse HEAD 2>/dev/null || echo unknown)"
DIRTY="$(git -C "${REPO}" status --porcelain 2>/dev/null | awk '{print $NF}' || true)"
REC="${HEAD}"; [ -n "${DIRTY}" ] && REC="${HEAD}+dirty"
OLD="$(remote "cat ${SHA_FILE} 2>/dev/null || true" | tr -d '[:space:]')" \
  || { echo "!! cannot read ${SHA_FILE} on ${BOX_HOST}" >&2; exit 1; }
CHANGED=""
if [ -n "${OLD}" ] && [ "${OLD}" = "${OLD%%+*}" ] && git -C "${REPO}" cat-file -e "${OLD}^{commit}" 2>/dev/null; then
  CHANGED="$( { git -C "${REPO}" diff --name-only "${OLD}" HEAD; printf '%s\n' "${DIRTY}"; } | sed '/^$/d' | sort -u)"
  NONWEB="$(printf '%s\n' "${CHANGED}" | grep -v '^web/' | sed '/^$/d' || true)"
  if [ "${MODE}" = auto ]; then
    if [ -z "${CHANGED}" ]; then echo "==> up to date (${OLD}); use --full to force"; exit 0; fi
    if [ -z "${NONWEB}" ]; then MODE=web; else MODE=full; fi
  elif [ "${MODE}" = web ] && [ -n "${NONWEB}" ]; then
    echo "!! --web-only but non-web changes since ${OLD} stay UNDEPLOYED:"; printf '%s\n' "${NONWEB}" | sed 's/^/     /'
  fi
else
  [ "${MODE}" = auto ] && MODE=full
fi
echo "==> plan: mode=${MODE}  box=${BOX_HOST}  deployed=${OLD:-unknown}  head=${REC}  dry-run=${DRY}"
[ -n "${CHANGED}" ] && printf '%s\n' "${CHANGED}" | sed 's/^/     changed: /'

finish() {
  if [ "${HEAD}" = unknown ]; then echo "!! no git HEAD, ${SHA_FILE} not written"; else
    run "printf '%s\n' '${REC}' > ${SHA_FILE}"; fi
  echo "==> deploy complete (mode=${MODE}, dry-run=${DRY})"
}

# ---- web-only fast path: no --delete (box-only files in the docroot survive) ---
if [ "${MODE}" = web ]; then
  echo "==> web-only: sync web/ → /var/www/voicehook (no pip, no restart, no caddy reload)"
  run "mkdir -p /var/www/voicehook"
  rs "${EXCL[@]}" "${REPO}/web/" "$(dst /var/www/voicehook)/"
  # Only web/ went out: if the box state before was unknown or non-web changes
  # were skipped, mark the SHA so the next auto run goes full instead of "up to date".
  if [ -z "${OLD}" ] || [ "${OLD}" != "${OLD%%+*}" ] || [ -n "${NONWEB:-}" ]; then REC="${REC%%+*}+webonly"; fi
  finish; exit 0
fi

# ---- full path: preflight BEFORE anything changes on the box -------------------
preflight() {
  # Box-local twirp ListRooms/ListParticipants; LiveKit keys never leave the box.
  # rc 0 = no human in any room, rc 3 = human present, anything else = error.
  local st rc=0
  st="$(remote "systemctl is-active voicehook-agent 2>/dev/null || true")" || st=""
  if [ -z "${st}" ]; then echo "!! preflight: cannot query agent state (fail closed)" >&2; return 1; fi
  if [ "${st}" != active ]; then echo "==> preflight: agent ${st}, no live session to interrupt"; return 0; fi
  # hard caps: outer 120s on the whole call, 90s on python, 80s internal deadline, 10s per request
  # shellcheck disable=SC2086  # SSH is a command string (word-split on purpose, as in remote())
  if [ "${LOCAL}" -eq 1 ]; then preflight_py | timeout 120 timeout 90 python3 - || rc=$?
  else preflight_py | timeout 120 ${SSH} "${BOX_HOST}" "timeout 90 python3 -" || rc=$?; fi
  case "${rc}" in
    0) return 0 ;;
    3) if [ "${FORCE_RESTART}" -eq 1 ]; then echo "!! humans in call, --force-restart given: continuing"; return 0; fi
       echo "!! preflight: human participant in a live room; abort (use --force-restart to override)" >&2; return 1 ;;
    *) echo "!! preflight failed rc=${rc} (fail closed; --force-restart does not bypass errors)" >&2; return 1 ;;
  esac
}
preflight_py() { cat <<'PY'
import base64, hashlib, hmac, json, sys, time, urllib.request
DEADLINE = time.time() + 80
BASE = "http://127.0.0.1:7880/twirp/livekit.RoomService/"
env = {}
for line in open("/opt/voicehook/.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")
KEY, SECRET = env["LIVEKIT_API_KEY"], env["LIVEKIT_API_SECRET"]
def b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def jwt(video):
    n = int(time.time()); h = b64(b'{"alg":"HS256","typ":"JWT"}')
    p = b64(json.dumps({"iss": KEY, "sub": "deploy-preflight", "nbf": n - 5, "exp": n + 120, "video": video}).encode())
    return f"{h}.{p}." + b64(hmac.new(SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
def call(m, body, video):
    if time.time() > DEADLINE: raise SystemExit("internal deadline hit")
    r = urllib.request.Request(BASE + m, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + jwt(video)})
    with urllib.request.urlopen(r, timeout=10) as f: return json.loads(f.read() or b"{}")
def agentish(p):
    return p.get("kind") in (4, "AGENT") or str(p.get("identity", "")).startswith("voice-ai")
rooms = call("ListRooms", {}, {"roomList": True}).get("rooms") or []
humans = 0
for r in rooms:
    parts = call("ListParticipants", {"room": r["name"]}, {"roomAdmin": True, "room": r["name"]}).get("participants") or []
    h = [p.get("identity", "?") for p in parts if not agentish(p)]
    humans += len(h)
    print(f"   room {r['name']}: {len(parts)} participant(s), human={h or '-'}")
print(f"==> preflight: {len(rooms)} room(s), {humans} human participant(s)")
sys.exit(3 if humans else 0)
PY
}

if [ "${DRY}" -eq 1 ]; then echo "   [dry-run] would run: LiveKit ListRooms preflight (abort on human participant)"
else preflight || exit 1; fi

echo "==> sync code → /opt/voicehook  (apps/ layout preserved so pip install -e . works)"
run "mkdir -p /opt/voicehook/apps /var/www/voicehook"
# agent code is a repo mirror: --delete, but box-only .env*/.venv/caches/egg-info are excluded (= protected)
rs --delete "${EXCL[@]}" "${REPO}/apps/agent/" "$(dst /opt/voicehook/apps/agent)/"
# docroot: no --delete, box-only files (.deployed-sha, verification files) are never removed
rs "${EXCL[@]}" "${REPO}/web/" "$(dst /var/www/voicehook)/"
rs "${REPO}/pyproject.toml" "$(dst /opt/voicehook/pyproject.toml)"
rs "${REPO}/README.md"      "$(dst /opt/voicehook/README.md)"

# USE_SSLIP=true (cloud-init first-boot, DNS-less box): self-derive <ip>.sslip.io from the box's own IPv4 so a fresh box gets HTTPS in one apply, no IP injection.
if [ "${USE_SSLIP:-}" = "true" ] && [ -z "${CADDY_SITE_MAIN:-}" ]; then
  IP="$(curl -s --max-time 5 http://169.254.169.254/hetzner/v1/metadata/public-ipv4 || true)"
  [ -n "${IP}" ] || IP="$(curl -s --max-time 5 https://ifconfig.me || true)"
  DASH="${IP//./-}"
  CADDY_SITE_MAIN="${DASH}.sslip.io"
  CADDY_SITE_RTC="rtc-${DASH}.sslip.io"
fi
DOMAIN="${CADDY_SITE_MAIN:-voicehook.ai}"
RTC="${CADDY_SITE_RTC:-rtc.voicehook.ai}"

echo "==> venv + pip install -e ."
run "set -e
  cd /opt/voicehook
  [ -x .venv/bin/python ] || python3.12 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip wheel
  .venv/bin/pip install --quiet -e ."

echo "==> .env LIVEKIT_URL sync (#68)"
CUR="$(remote "sed -n 's/^LIVEKIT_URL=//p' /opt/voicehook/.env 2>/dev/null || true")"
echo "   LIVEKIT_URL: ${CUR:-<unset>} -> wss://${RTC}"
run "[ -f /opt/voicehook/.env ] && sed -i 's|^LIVEKIT_URL=.*|LIVEKIT_URL=wss://${RTC}|' /opt/voicehook/.env || true"

echo "==> systemd unit + restart"
rs "${REPO}/infra/systemd/voicehook-agent.service" "$(dst /etc/systemd/system/voicehook-agent.service)"
run "systemctl daemon-reload && systemctl enable --now voicehook-agent.service && systemctl restart voicehook-agent.service"

echo "==> Caddyfile (rendered from ${DOMAIN} + ${RTC})"
TMP=$(mktemp)
sed -e "s|__SITE_MAIN__|${DOMAIN}|" -e "s|__SITE_RTC__|${RTC}|" "${REPO}/infra/caddy/Caddyfile.tmpl" > "${TMP}"
rs "${TMP}" "$(dst /etc/caddy/Caddyfile)"
rm -f "${TMP}"
run "chgrp caddy /etc/caddy/Caddyfile 2>/dev/null || true; chmod 0640 /etc/caddy/Caddyfile
  caddy validate --config /etc/caddy/Caddyfile && (systemctl reload caddy || systemctl restart caddy)"

echo "==> livekit-server (docker run, host network)"
rs "${REPO}/infra/livekit/docker-compose.yml" "$(dst /etc/livekit/docker-compose.yml)"
run "docker pull -q livekit/livekit-server:latest >/dev/null
  if ! docker ps --format '{{.Names}}' | grep -q '^livekit-server$'; then
    docker rm -f livekit-server 2>/dev/null || true
    docker run -d --name livekit-server --restart unless-stopped --network host \
      -v /etc/livekit/livekit.yaml:/etc/livekit/livekit.yaml:ro \
      livekit/livekit-server:latest --config /etc/livekit/livekit.yaml
  fi"

finish
