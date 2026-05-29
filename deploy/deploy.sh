#!/usr/bin/env bash
# Idempotent v4 deploy. Single agent service + caddy + livekit (docker run).
# Re-runnable: a 2nd run with no code change = no restart.
#
#   BOX_HOST=root@<ip>     ./deploy/deploy.sh   # remote (SSH)
#   BOX_HOST=local         ./deploy/deploy.sh   # cloud-init first-boot
#
# CADDY_SITE_MAIN  (default voicehook.ai)        — public host for /, /api/*
# CADDY_SITE_RTC   (default rtc.voicehook.ai)    — public host for LK WSS
set -euo pipefail

BOX_HOST="${BOX_HOST:-root@voicehook.ai}"
SSH_KEY="${SSH_KEY:-${HOME:-/root}/.ssh/voicehook_v4}"
SSH="ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20"
[ "${SSH_KEY}" != "/dev/null" ] && SSH="ssh -i ${SSH_KEY} -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

case "${BOX_HOST}" in
  local|localhost|root@127.0.0.1|root@localhost) LOCAL=1 ;;
  *) LOCAL=0 ;;
esac
if [ "${LOCAL}" -eq 1 ]; then
  remote() { bash -c "$*"; }
  rsync_to() { rsync -az --delete --exclude='.env*' --exclude='__pycache__' --exclude='.venv' --exclude='.git' "$1/" "$2/"; }
  rsync_file() { rsync -az "$1" "$2"; }
else
  remote() { ${SSH} "${BOX_HOST}" "$@"; }
  rsync_to() { rsync -az --delete --exclude='.env*' --exclude='__pycache__' --exclude='.venv' --exclude='.git' -e "${SSH}" "$1/" "${BOX_HOST}:$2/"; }
  rsync_file() { rsync -az -e "${SSH}" "$1" "${BOX_HOST}:$2"; }
fi

# USE_SSLIP=true (cloud-init first-boot, DNS-less box): self-derive <ip>.sslip.io from the box's own IPv4 so a fresh box gets HTTPS in one apply, no IP injection.
if [ "${USE_SSLIP:-}" = "true" ] && [ -z "${CADDY_SITE_MAIN:-}" ]; then
  IP="$(curl -s --max-time 5 http://169.254.169.254/hetzner/v1/metadata/public-ipv4 || true)"
  [ -n "${IP}" ] || IP="$(curl -s --max-time 5 https://ifconfig.me || true)"
  DASH="${IP//./-}"
  CADDY_SITE_MAIN="${DASH}.sslip.io"
  CADDY_SITE_RTC="rtc-${DASH}.sslip.io"
  [ -f /opt/voicehook/.env ] && sed -i "s|^LIVEKIT_URL=.*|LIVEKIT_URL=wss://${CADDY_SITE_RTC}|" /opt/voicehook/.env
fi
DOMAIN="${CADDY_SITE_MAIN:-voicehook.ai}"
RTC="${CADDY_SITE_RTC:-rtc.voicehook.ai}"

echo "==> sync code → /opt/voicehook  (apps/ layout preserved so pip install -e . works)"
remote "mkdir -p /opt/voicehook/apps /var/www/voicehook"
rsync_to "${REPO}/apps/agent" "/opt/voicehook/apps/agent"
rsync_to "${REPO}/web"        "/var/www/voicehook"
rsync_file "${REPO}/pyproject.toml" "/opt/voicehook/pyproject.toml"
rsync_file "${REPO}/README.md"      "/opt/voicehook/README.md"

echo "==> venv + pip install -e ."
remote "set -e
  cd /opt/voicehook
  [ -x .venv/bin/python ] || python3.12 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip wheel
  .venv/bin/pip install --quiet -e ."

echo "==> systemd unit + start"
rsync_file "${REPO}/infra/systemd/voicehook-agent.service" /etc/systemd/system/voicehook-agent.service
remote "systemctl daemon-reload && systemctl enable --now voicehook-agent.service && systemctl restart voicehook-agent.service"

echo "==> Caddyfile (rendered from ${DOMAIN} + ${RTC})"
TMP=$(mktemp)
sed -e "s|__SITE_MAIN__|${DOMAIN}|" -e "s|__SITE_RTC__|${RTC}|" "${REPO}/infra/caddy/Caddyfile.tmpl" > "${TMP}"
rsync_file "${TMP}" /etc/caddy/Caddyfile
rm -f "${TMP}"
remote "chgrp caddy /etc/caddy/Caddyfile 2>/dev/null || true; chmod 0640 /etc/caddy/Caddyfile
  caddy validate --config /etc/caddy/Caddyfile && (systemctl reload caddy || systemctl restart caddy)"

echo "==> livekit-server (docker run, host network)"
rsync_file "${REPO}/infra/livekit/docker-compose.yml" /etc/livekit/docker-compose.yml
remote "docker pull -q livekit/livekit-server:latest >/dev/null
  if ! docker ps --format '{{.Names}}' | grep -q '^livekit-server$'; then
    docker rm -f livekit-server 2>/dev/null || true
    docker run -d --name livekit-server --restart unless-stopped --network host \
      -v /etc/livekit/livekit.yaml:/etc/livekit/livekit.yaml:ro \
      livekit/livekit-server:latest --config /etc/livekit/livekit.yaml
  fi"

echo "==> deploy complete"
