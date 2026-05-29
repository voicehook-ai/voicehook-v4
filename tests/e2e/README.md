# E2E voice — real audio against the staging box

The deterministic mouthpiece-pipeline contract lives in
`apps/agent/tests/test_e2e_contract.py` (runs in every PR CI). This directory
is for the **real-audio** test that requires a deployed v4 box.

## Cutover gate (per PLAN-v4.md §Success criteria)

Before flipping DNS from v3 → v4, this MUST pass against the staging box:

```bash
LIVEKIT_URL=wss://rtc.staging.voicehook.ai \
LIVEKIT_API_KEY=...  LIVEKIT_API_SECRET=... \
DEEPGRAM_API_KEY=... GOOGLE_API_KEY=... \
INVITE_SECRET=...                                   \
  bash tests/e2e/run.sh
```

What `run.sh` does (when implemented end-to-end in v4.1):

1. Mint an invite + LK JWT against the deployed `/api/token`
2. Open a fake LK participant (livekit-api Python SDK)
3. Publish `tests/e2e/sample-de.wav` ("Hallo, kannst du mich hören?")
4. Wait ≤ 8s for a `transcript` event with `role: "user"` and text matching `/hallo|hören/i`
5. Push `senior.say` with "Ja, ich höre dich klar."
6. Wait ≤ 12s for the agent to emit audio (any frames) AND a `transcript` event with `role: "agent"` containing "ja"
7. Assert `agent.heartbeat` topic emitted at least once (NOT `transcript`)
8. Disconnect cleanly

## Why not in PR-CI by default

- Needs live API keys (Deepgram, Google) → ~$0.01 per run × every PR = $$
- Needs a running LK server with the v4 worker registered = box must exist
- Needs ~30s wall-clock per run

PR CI keeps the **contract** test (deterministic, 0 external deps, runs every
push). The real-audio test runs:
- On-demand via `.github/workflows/e2e.yml` (workflow_dispatch)
- Nightly against staging once deployed
- **Mandatorily before cutover**

## TODO before cutover

- [ ] Wire `tests/e2e/run.sh` (livekit-api SDK + WAV publish + transcript assert)
- [ ] Ship `tests/e2e/sample-de.wav` (10kB short clip, "Hallo, kannst du mich hören?")
- [ ] `.github/workflows/e2e.yml` workflow_dispatch with org-level secrets
- [ ] Run against staging box; capture log artifact on failure
