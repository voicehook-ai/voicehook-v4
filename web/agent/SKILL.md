---
name: voicehook
title: voicehook.ai, The Voice Interface for Your AI Agents
description: voicehook gives any AI agent (Claude, Cursor, a custom LLM app) a real-time voice on a live phone-style call. The agent joins a LiveKit room as the senior brain behind a strict-relay voicebot, listens to the human speaker, and speaks through the voicebot. The voicebot never improvises: it speaks only what the senior agent pushes (senior.say), what the senior agent injects as a bounded persona (senior.persona), and verbatim web quotes. Join with the voicehook-agent CLI (uv tool) or any LiveKit client over the senior.* data-channel topics. Production base, https://voicehook.ai.
homepage: https://voicehook.ai
cli: https://github.com/voicehook-ai/voicehook-agent
served_at: https://voicehook.ai/agent/SKILL.md
---

# voicehook, The Voice Interface for Your AI Agents

voicehook puts your AI agent on a live voice call. A human talks into a browser
tab (or phone); your agent listens to the transcript in real time and answers
out loud through a voice frontend, the "voicebot". The voicebot is not a clone
of your agent and it never makes things up. It is a strict relay: your agent is
the brain, the voicebot is the mouth.

This document is for an AI agent (Claude, Cursor, or any LLM tool) that has
found this file and wants to join a voicehook call. It describes the real v4
protocol: the CLI, the join URLs, the HTTP endpoints, and the senior.* control
topics. Param names and topic names below are quoted exactly from the running
code.

## What you get when you join

You connect to a LiveKit room as a hidden senior participant. From there you can:

- Listen to the human's spoken turns and the voicebot's spoken turns (the
  `transcript` data topic).
- Speak through the voicebot by pushing text on `senior.say`. The voicebot
  speaks it verbatim as TTS. It never rewrites your words.
- Inject a bounded knowledge persona on `senior.persona`. This becomes the
  voicebot's entire allowed knowledge and its system instructions.
- Cut off a wrong or stale TTS line on `senior.interrupt`.
- Add a synthetic turn into the voicebot's chat context (not spoken) on
  `senior.inject`.

The human hears a natural, low-latency voice. You stay async behind it: you
curate what the voicebot is allowed to know, and you push fresh answers when
real thinking is needed.

## The strict-relay model (read this first)

The voicebot runs a `RelayAgent`. Its `on_user_turn_completed` handler raises
`StopResponse`, so the underlying LLM never generates a turn on its own. This is
an engine-level guarantee, not a prompt request.

The voicebot may speak ONLY:

1. The exact text delivered on `senior.say` (verbatim TTS).
2. Content inside the persona you injected on `senior.persona`. This persona is
   its complete allowed knowledge, nothing beyond it.
3. Verbatim quotes from a web search (a single self-tool, quote-only with
   attribution).

It invents nothing: no numbers, no status, no plans, no success claims it
cannot ground in the persona or a fresh `senior.say` or a web result. When it
does not know, it bridges briefly or stays silent. The default persona makes
this explicit: "Du sagst NUR was dir explizit via senior.say geschickt wird oder
was in deiner persona steht. Du erfindest NICHTS."

Important consequence: the senior agent is the conversation's intelligence. Push
a rich persona up front so the voicebot can answer common things instantly, and
push fresh `senior.say` lines for anything not already in the persona.

## Security note for what you push

The `senior.persona`, `senior.say`, and `senior.inject` payloads ride the
LiveKit room data-channel in cleartext. Never put secrets, API keys, tokens,
internal identifiers, or user PII into any payload or persona. Scrub before you
send.

## How to join, the voicehook-agent CLI

The supported path is the `voicehook-agent` CLI. It connects to the room as a
named senior participant, auto-pushes your persona on connect, and streams
turns as JSON so your control messages are never spoken by accident.

Install once, or run zero-install with `uvx`:

```bash
# one-time install
uv tool install git+https://github.com/voicehook-ai/voicehook-agent

# or zero-install per call
uvx --from git+https://github.com/voicehook-ai/voicehook-agent \
    voicehook-agent join <url> --name <brand> --json
```

Join a call:

```bash
voicehook-agent join "https://voicehook.ai/r/<slug>" --name claude --json \
  --persona-file /tmp/persona.txt
```

- `--name <brand>` sets the senior participant identity (use your brand, e.g.
  `claude`).
- `--json` is required when driving programmatically. Without it, control
  messages can be spoken literally as TTS.
- `--persona-file <path>` (or inline `--persona`) is pushed on connect as
  `senior.persona`, so the strict-relay rules and your knowledge are live from
  the first moment.

After connect, confirm a peer with `kind:"agent"` (the voice-ai worker) is in
the room before pushing your first `senior.say`. On v4 a voice-ai is dispatched
automatically (see "voice-ai dispatch" below), so the agent should appear within
a few seconds.

## Join URLs and the /r/<slug> shape

- Human or browser join: `https://voicehook.ai/r/<slug>`. The slug is three
  lowercase words plus a 4-character uppercase suffix, for example
  `drift-signal-crisp-PDM5`.
- Senior agent join: pass the same `https://voicehook.ai/r/<slug>` URL to the
  CLI. It extracts the slug and mints a token for you.
- Signed invite link: `https://voicehook.ai/r/<slug>?invite=<code>`, where
  `<code>` is an HMAC invite minted by the server (see "Invite codes" below).
  The invite gate protects existing rooms so a leaked slug alone cannot publish.

Mint a join URL with an invite from the CLI helper (requires `INVITE_SECRET` in
the environment):

```bash
python -m agent.cli invite <slug> --ttl 3600 --base https://voicehook.ai
# prints: https://voicehook.ai/r/<slug>?invite=<code>
```

## voice-ai dispatch (why your senior.say is heard)

A voice-ai worker must be in the room or your `senior.say` speaks into the void.
On v4 this is automatic:

- A senior peer fetches `GET /api/token?room=<slug>&identity=<id>&invite=1`. The
  `invite=1` flag mints a plain join token (no `roomConfig.agents` claim) and, on
  the server, fires an idempotent `CreateDispatch` for the `voice-ai` agent. So
  any senior join guarantees a voice-ai shows up, even if the room was created
  agentless.
- A normal token mint (`POST /api/token` with a real invite, or
  `POST /api/host-call`) carries a `roomConfig.agents` claim for `voice-ai`, so
  LiveKit dispatches on the first participant join, and the server also fires an
  explicit, presence-idempotent dispatch as a belt-and-suspenders fallback.

Dispatch is presence-idempotent: the server lists existing dispatches under a
per-room lock and skips creating a second one, so concurrent senior and host
mints cannot spawn two voice-ai workers.

## The senior.* data-channel topics

These are the control topics the relay handles. Each payload is a JSON object
published on the named LiveKit data topic.

| Topic | Payload | Effect |
|---|---|---|
| `senior.say` | `{"text": "...", "priority": "interrupt"}` | The voicebot speaks `text` verbatim as TTS. `priority:"interrupt"` drops the current floor first. Empty `text` is ignored. Each say is also mirrored to the `transcript` topic as `{"role":"agent","text":...}` for the browser UI. |
| `senior.persona` | `{"text": "..."}` | Replaces the voicebot's instructions with `text` (live-injected bounded knowledge). Empty `text` is ignored. |
| `senior.interrupt` | `{}` | Drops the current TTS line. |
| `senior.inject` | `{"role": "user", "text": "..."}` | Adds a synthetic turn into the voicebot's chat context. Not spoken. `role` defaults to `user`. |

Inbound, you receive the `transcript` topic, `{"role": "user"|"agent", "text":
"..."}`, so you can read what the human said and what the voicebot actually
spoke, and ground your next `senior.say` in what was really heard.

Any LiveKit client works as a sender, since these are plain data-channel
messages on topics. The CLI is the supported listener (it streams `transcript`
and room state as JSON).

## Endpoints

Base URL in production is `https://voicehook.ai`. The LiveKit media URL the
token points at is `wss://rtc.voicehook.ai`.

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/token` | `POST` | Mint a LiveKit JWT. Body: `room`, `identity`, `invite` (HMAC code, required), `ttl_seconds` (60..86400, default 3600). Returns `token`, `url`, `room`, `identity`. Auto-dispatches `voice-ai`. |
| `/api/token` | `GET` | CLI-compatible mint. Query: `room`, `identity`, `invite`, `ttl_seconds`. `invite=1` gives a senior peer a plain join token and fires an idempotent `voice-ai` dispatch. Any other `invite` value must be a valid HMAC code. |
| `/api/host-call` | `POST` | Start a fresh call with no invite. Body: `identity`, `ttl_seconds`. The server generates the room slug itself (so no existing room can be hijacked), mints a token, and dispatches `voice-ai`. Per-IP rate limited (stopgap). |
| `/status` | `GET` | Functional health. Returns `service`, `version`, a `probe` object for `stt`/`tts`/`llm`, and a top-level `healthy` boolean. Always HTTP 200; read `healthy`. |
| `/healthz` | `GET` | Liveness probe. Returns `{"status": "ok"}`. |

`TokenResponse` shape, returned by both token endpoints and `/api/host-call`:

```json
{
  "token": "<livekit-jwt>",
  "url": "wss://rtc.voicehook.ai",
  "room": "<slug>",
  "identity": "<id>"
}
```

The minted LiveKit JWT grants `roomJoin`, `canPublish`, `canSubscribe`, and
`canPublishData` for the room. A senior token (the `invite=1` path) omits the
`roomConfig.agents` claim so the join itself does not dispatch; the server's
explicit dispatch covers that case.

## Invite codes

An invite is an HMAC-signed code of the shape
`<b64url(room)>.<b64url(exp)>.<b64url(hmac_sig)>`, where the signature is
HMAC-SHA256 over `"<room>|<exp>"` using the server's `INVITE_SECRET`. The server
verifies it in constant time before minting a publish token, so a leaked slug
alone cannot let a stranger publish into an existing room. Default TTL is one
hour.

## Quick start for an AI agent

1. Get a call URL. Either the human shares `https://voicehook.ai/r/<slug>`, or
   you start a fresh one via `POST /api/host-call`, or you mint an invite link
   with `python -m agent.cli invite <slug>`.
2. Prepare a bounded persona file. Include the strict-relay rules plus the real
   knowledge the voicebot may speak. No secrets.
3. Join: `voicehook-agent join "<url>" --name <brand> --json --persona-file <path>`.
4. Confirm a `kind:"agent"` peer is present (voice-ai is auto-dispatched).
5. Listen to the `transcript` topic, and push `senior.say` for any answer not
   already covered by the persona. Use `senior.interrupt` to drop a wrong line
   and `senior.persona` to refresh the voicebot's knowledge as the conversation
   moves.

## More

- CLI and protocol source: https://github.com/voicehook-ai/voicehook-agent
- Consumer-side join skill (senior-brain operating guide): the `voicehook-join`
  skill, which adds the listening-loop, persona-rendering, and latency patterns
  on top of these primitives.
