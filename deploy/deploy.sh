#!/usr/bin/env bash
# Idempotent v4 deploy. Single service (voicehook-agent) + caddy + livekit-docker.
# Re-runnable: a second run with no code change restarts nothing.
#
#   BOX_HOST=root@<ip>     ./deploy/deploy.sh   # remote
#   BOX_HOST=local         ./deploy/deploy.sh   # cloud-init first-boot
set -euo pipefail

BOX_HOST="${BOX_HOST:-root@voicehook.ai}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/hetzner_voicehook}"
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

DOMAIN="${CADDY_SITE_MAIN:-voicehook.ai}"
RTC="${CADDY_SITE_RTC:-rtc.voicehook.ai}"

echo "==> sync code → /opt/voicehook"
remote "mkdir -p /opt/voicehook /var/www/voicehook"
rsync_to "${REPO}/apps/agent" "/opt/voicehook/agent"
rsync_to "${REPO}/web" "/var/www/voicehook"
rsync_file "${REPO}/pyproject.toml" "/opt/voicehook/pyproject.toml"

echo "==> venv + pip install"
remote "set -e
  cd /opt/voicehook
  [ -x .venv/bin/python ] || python3.12 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip wheel
  .venv/bin/pip install --quiet -e ."

echo "==> systemd unit"
rsync_file "${REPO}/infra/systemd/voicehook-agent.service" /etc/systemd/system/voicehook-agent.service
remote "systemctl daemon-reload && systemctl enable --now voicehook-agent.service && systemctl restart voicehook-agent.service"

echo "==> Caddyfile (rendered)"
TMP=$(mktemp)
sed -e "s|__SITE_MAIN__|${DOMAIN}|" -e "s|__SITE_RTC__|${RTC}|" "${REPO}/infra/caddy/Caddyfile.tmpl" > "${TMP}"
rsync_file "${TMP}" /etc/caddy/Caddyfile
rm -f "${TMP}"
remote "chgrp caddy /etc/caddy/Caddyfile 2>/dev/null || true; chmod 0640 /etc/caddy/Caddyfile
  caddy validate --config /etc/caddy/Caddyfile && (systemctl reload caddy || systemctl restart caddy)"

echo "==> livekit-server (docker)"
rsync_file "${REPO}/infra/livekit/docker-compose.yml" /etc/livekit/docker-compose.yml
remote "cd /etc/livekit && docker compose up -d"

echo "==> deploy complete"
