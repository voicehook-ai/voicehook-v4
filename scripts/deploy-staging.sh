#!/usr/bin/env bash
# Deploy voicehook v4 to a fresh hcloud box via Terraform.
# Pulls all secrets from p2ai (one Touch-ID tap, no values echoed),
# runs `terraform init + plan`, asks before `apply`.
set -euo pipefail

cd "$(dirname "$0")/.."
TF_DIR=infra/terraform

[ -d "$TF_DIR" ] || { echo "no $TF_DIR"; exit 1; }
which terraform >/dev/null || { echo "terraform not installed: brew install terraform"; exit 1; }
which p2ai      >/dev/null || { echo "p2ai not installed (passwort2ai skill)"; exit 1; }

# init first (idempotent, no secrets needed)
echo "==> terraform init"
( cd "$TF_DIR" && terraform init -input=false )

echo "==> fetching secrets via p2ai (one Touch-ID tap)"
p2ai run \
  -e TF_VAR_hcloud_token='voicehook HCLOUD token' \
  -e TF_VAR_invite_secret='voicehook_v4_invite_secret' \
  -e TF_VAR_livekit_api_key='LIVEKIT_API_KEY' \
  -e TF_VAR_livekit_api_secret='LIVEKIT_API_SECRET' \
  -e TF_VAR_deepgram_api_key='DEEPGRAM_API_KEY' \
  -e TF_VAR_google_api_key='GOOGLE_API_KEY' \
  -e TF_VAR_gcp_sa_json='voicehook_gcp_sa_tts_b64' \
  -- bash -c '
    set -euo pipefail
    # gcp_sa_json comes b64-encoded in the keepass entry; decode to raw JSON for TF
    export TF_VAR_gcp_sa_json="$(printf "%s" "$TF_VAR_gcp_sa_json" | base64 -d 2>/dev/null || printf "%s" "$TF_VAR_gcp_sa_json")"
    cd '"$TF_DIR"'
    echo "==> terraform plan"
    terraform plan -out=/tmp/v4.tfplan -input=false
    echo
    read -rp "Apply this plan? (y/N) " ans
    if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
      echo "==> terraform apply"
      terraform apply -input=false /tmp/v4.tfplan
      echo
      echo "==> outputs:"
      terraform output
      IP=$(terraform output -raw ipv4 2>/dev/null || echo "")
      [ -n "$IP" ] && echo "==> SSH: ssh -i ~/.ssh/hetzner_voicehook root@$IP"
      [ -n "$IP" ] && echo "==> Smoke: curl http://$IP/healthz   |   curl http://$IP/status"
    else
      echo "skipped apply. plan kept at /tmp/v4.tfplan"
    fi
  '
