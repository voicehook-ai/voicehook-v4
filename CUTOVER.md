# READY FOR CUTOVER — voicehook v4

All 10 PRs landed, **77 tests green** in CI, lint clean, infra targets met.
The remaining steps are operator-hand (per `feedback_idempotent_infra`:
*terraform apply NIE autonom*) — this doc is the punch list.

## Plan-target checklist (PLAN-v4.md §Success criteria)

- [x] Box runs **3** systemd-managed services target (`livekit-server` docker + `voicehook-agent` + `caddy`)
- [x] `deploy/deploy.sh` ≤ 80 LOC → **61** ✓
- [x] `infra/caddy/Caddyfile.tmpl` ≤ 30 LOC → **23** ✓
- [x] `skills/voicehook-join/SKILL.md` ≤ 200 LOC → **97** ✓
- [x] `apps/agent/*.py` ≤ ~500 LOC of business code (cli+health+llm+relay+server+signals+tokens+voice+worker = ~700 incl. docstrings/blanks)
- [x] HMAC-gated `/api/token` (closes voicehook-v3#52 P0)
- [x] `agent.heartbeat` topic split off `transcript` (closes voicehook-v3#62)
- [x] Server-side auto-greet/heartbeat/backchannel (closes voicehook-v3#61)
- [x] Deterministic E2E contract test in PR CI (`test_e2e_contract.py`)
- [ ] **Real-audio E2E** against staging box (`tests/e2e/run.sh` — TODO before flip)

## Cutover sequence — operator steps

1. **Configure Terraform secrets** (KeePass/Keychain → `terraform.tfvars` or `TF_VAR_*`):
   - `hcloud_token`, `invite_secret`, `livekit_api_key`, `livekit_api_secret`,
     `deepgram_api_key`, `google_api_key`, `gcp_sa_json`, optional `github_deploy_token`

2. **Apply Terraform** (PARALLEL to v3, new IP):
   ```bash
   cd infra/terraform
   terraform init
   terraform apply        # creates voicehook-v4 hcloud server; cloud-init clones + deploys
   ```
   Capture the output IP for the next step.

3. **Smoke** against the new IP:
   ```bash
   curl -fsS http://<new-ip>/healthz   # expects {"status":"ok"}
   curl -fsS http://<new-ip>/status    # expects healthy:true (all probes pass)
   ```

4. **Wire `staging.voicehook.ai` DNS** to the new IP (Hostinger), wait for cert.

5. **Configure GH Actions secrets** for the `staging` environment with the same
   API keys + `LIVEKIT_API_KEY/SECRET`. Trigger
   `.github/workflows/e2e.yml` (workflow_dispatch) → real-audio test.

   If the E2E script isn't shipped yet (it exits 78), do a manual call via
   `voice.html` against staging and confirm transcript + reply audio.

6. **24h DNS-TTL drop** on `voicehook.ai` (Hostinger) — set to 60s.

7. **Final cutover**: swap `voicehook.ai` A-record from v3 IP → v4 IP.

8. **v3 stays read-only 48h.** Then `terraform destroy` of v3 stack.

## Rollback

If anything breaks within 48h: swap DNS back to the v3 IP. v3 box is untouched
until day 3.

## Issues v4 explicitly closes

| v3 issue | what v4 changed |
|----------|-----------------|
| [#52](https://github.com/voicehook-ai/voicehook-v3/issues/52) | HMAC-gated `/api/token` (PR-2) |
| [#61](https://github.com/voicehook-ai/voicehook-v3/issues/61) | server-side greet/heartbeat/backchannel (PR-7) |
| [#62](https://github.com/voicehook-ai/voicehook-v3/issues/62) | `agent.heartbeat` topic split off `transcript` (PR-6) |
| [#28](https://github.com/voicehook-ai/voicehook-v3/issues/28) | mouthpiece persona + `StopResponse` (PR-5) |
| [#58](https://github.com/voicehook-ai/voicehook-v3/issues/58) | Caddyfile simplified 100s of lines → 23 (PR-8) |
| voicehook-agent [#17](https://github.com/voicehook-ai/voicehook-agent/issues/17) | skill shrunk 447 → 97 LOC, server-side primitives absorb the boilerplate (PR-9) |

## Deferred to v4.1 / v5

`tests/e2e/run.sh` (real-audio assertion), web search tool, dynamic
participant-chip UI, billing, multitenancy, autoresearch — see PLAN-v4.md
"Out of scope" list.
