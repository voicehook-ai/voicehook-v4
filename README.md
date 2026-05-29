# voicehook v4

Clean-slate rewrite of voicehook.ai. Only what was used. Plan: [voicehook-v3#64](https://github.com/voicehook-ai/voicehook-v3/pull/64) (`docs/PLAN-v4.md`).

> v4 is being built progressively via [PR sequence](https://github.com/voicehook-ai/voicehook-v4/pulls). When PR-10 (E2E voice test) goes green, `terraform apply` + DNS swap brings it live.

## Architecture (target)

```
Hetzner single box
├── docker   livekit-server                   :7880, :7881, :50000-60000/udp
├── systemd  voicehook-agent  (apps/agent)    :7400  (FastAPI + LiveKit worker)
└── systemd  caddy                            :80, :443
```

One Python service handles: STT (Deepgram Nova-3), TTS (Google Chirp3-HD), LLM (Gemini Flash), `/api/token` mint (HMAC-signed invites), `senior.*` relay, `/healthz`, `/status`.

## Quickstart (dev)

```bash
make dev      # python -m venv .venv; pip install -e ".[dev]"
make test     # pytest + ruff
make deploy   # BOX_HOST=root@<ip> ./deploy/deploy.sh
```

## Repo layout

```
apps/agent/        # the ONE service (≤500 LOC agent.py target)
web/               # voice.html only — single page, served by Caddy
infra/             # caddy (≤30 LOC), livekit, systemd, terraform
deploy/deploy.sh   # ≤80 LOC, idempotent
skills/voicehook-join/SKILL.md  # ≤200 LOC, calls voicehook-agent --auto
.github/workflows/ # ci.yml + deploy.yml
```

## Build order

1. Bootstrap (this PR)
2. HMAC-signed `/api/token` + `/healthz`
3. LiveKit worker skeleton
4. Deepgram STT + Google Chirp TTS
5. Gemini Flash LLM + `senior.*` relay with `StopResponse`
6. `/status` + `agent.health`-topic split (off `transcript`)
7. Server-side auto-greet / heartbeat / backchannel
8. Caddy (≤30 LOC) + cloud-init + Terraform + `deploy.sh` (≤80 LOC)
9. `voicehook-agent` CLI `--auto` mode + skill shrink
10. E2E voice test in CI (real audio → transcript → reply)

## Status

Built progressively. See open PRs and PR-10 CI for cutover-readiness.

## License

MIT. See `LICENSE`.
