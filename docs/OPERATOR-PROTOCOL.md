# Operator protocol (voicehook v4)

Reference for agents that join a call as operator. Source of truth is the code on
`main`: `apps/agent/relay.py`, `server.py`, `worker.py`, `live.py`, `gate.py`,
`speaker.py`, `budget.py`. Stand 2026-10-01 (up to PR #81). How-to for agents:
`skills/voicehook-join/SKILL.md` and `web/agent/SKILL.md` (live at
https://voicehook.ai/agent/SKILL.md). CLI: https://github.com/voicehook-ai/voicehook-agent

## Join

`voicehook-agent join <invite-url> --name <Name> --model <model-id> --json`

`--name` and `--model` are mandatory since CLI 0.4.0 (exit 2 without them). They are
your self-report: the web Agent chip shows a spinner until you join, then
`Name · model`. `--name` also becomes the identity prefix and the name in the auto-greet.

Run the join in the background (`setsid nohup … &`) so it outlives the agent's shell;
the operator must stay connected until the user says goodbye.

## Work cycle

| CLI | listen | speak | end |
|---|---|---|---|
| 0.5.0 (`next`/`say`/`leave`) | `voicehook-agent next` blocks, prints one JSON line `{"type": "user"\|"revise"\|"timeout"\|"ended", "text"?}`; exit 3 when the call is over | `voicehook-agent say [--mode overwrite\|append] "<text>"` | `voicehook-agent leave [--say "<goodbye>"]` |
| 0.4.0 | follow the join output with `tail -f \| grep -E '"topic": "(_wake\|operator\.revise)"\|session ended'` (never sleep-poll; 0.4.0 logs each user turn twice, as `transcript` and `_wake`, so match only `_wake`) | stdin line `{"topic":"operator.say","text":"…"}` | stdin line `{"topic":"quit"}` |

0.5.0 control socket: `join` listens on `$VOICEHOOK_AGENT_HOME/sessions/<slug>/<identity>/`
(default home `~/.voicehook-agent`). `say`/`next`/`leave` pick the join automatically when
only one runs, otherwise `--session <slug>/<identity>`. An agent sets its own
`VOICEHOOK_AGENT_HOME` to stay isolated from other agents on the same machine. `next`
starts queueing turns with the first `next`/`say`. `--idle-timeout MIN` (default 10, 0 = off):
without `say`/`next` for that long the join announces it and leaves. The CLI prints no
`agent.heartbeat`; check for a peer of kind `agent` in the `room-state` lines or in
`voicehook-agent status`.

One `say` per user turn, in the user's language. Do not push `operator.persona` while
another operator is in the room: it replaces the agent's instructions for everyone
(CLI 0.5.0 skips that push unless `--force-persona`).
`skills/voicehook-join/SKILL.md` has a wrapper (`$D/vh`) that gives both CLI versions the
same `next`/`say`/`leave` interface.

## Data-channel topics

All payloads are JSON on the LiveKit data channel. The CLI maps stdin lines
`{"topic":"operator.say","text":"..."}` to these packets.

| topic | direction | payload | effect |
|---|---|---|---|
| `operator.say` | operator to agent | `{text, mode?, priority?}` | speak `text`, see modes below |
| `operator.revise` | agent to operator | `{unspoken[], new, text}` | what was NOT spoken yet, plus an instruction |
| `operator.persona` | operator to agent | `{text}` | replace the agent's instructions live; first push triggers the auto-greet |
| `operator.mode` | operator to agent | `{mode:"strict"\|"auto"}` | strict: the agent never answers on its own (`--strict-relay`) |
| `operator.interrupt` | operator to agent | `{}` | stop everything; unspoken rest comes back as `operator.revise` |
| `operator.inject` | operator to agent | `{text, role?}` | synthetic chat-context entry, not spoken |
| `transcript` | agent to everyone | `{role, text}` | see transcript roles |
| `agent.heartbeat` | agent to everyone | `{ts, room, probe, healthy}` | every 30 s; no tick for more than 60 s = worker dead |

## operator.say modes

| mode | behaviour |
|---|---|
| `revise` (default) | Nothing unspoken pending: queued and spoken. A running answer of the agent itself finishes first, the operator does not cut in. Something of yours still unspoken: output stops, the new text is held, and you get `operator.revise` with the unspoken parts. Send one merged statement with `mode:"overwrite"`; without it the held text is spoken after 8 s (`HOLD_S`). |
| `overwrite` | Your merged statement. Cancels everything open and held, then speaks. |
| `append` | Queued behind what is running. Use for multi-part statements and status heartbeats. |

`priority:"interrupt"` on a `revise` say stops the current output explicitly.

The `operator.revise` text reads
`REVISE: Noch NICHT gesprochen: [1] ... Deine neue Aussage: [neu] ...` and asks for one
summary as `operator.say` with `mode:"overwrite"`.

## Transcript roles

The worker publishes everything that was actually spoken on `transcript`:

| role | meaning |
|---|---|
| `user` | final STT of the human (after the speech and speaker filters) |
| `operator` | text from `operator.say`, published after it was spoken; on an interruption only the spoken part. Browser: red |
| `agent` | the agent's own answer (auto mode). Browser: blue |

Pipeline mode matches operator lines by speech handle and text prefix; live mode only
by handle, because the realtime model rephrases.

Note for CLI users: `--suppress-echo` in CLI 0.4.0 and older filters only
`role:"agent"`, so it does not hide `operator` lines.

## Live mode (Gemini Live)

A separate worker (`voice-ai-live`) serves rooms created through:

| endpoint | result |
|---|---|
| `GET /api/live/status` | `{"available": bool}`, always 200, never amounts |
| `POST /api/live-room` `{identity, ttl_seconds?}` | host-call format `{token, url, room, identity}` plus `invite_url`, `expires_in`, `agent` |

Errors on `POST /api/live-room`: `404` switched off (`VOICEHOOK_LIVE_PUBLIC`), `503` not
configured, `402` monthly budget used up, `429` rate limit (shared with `/api/host-call`).
The room stays bound to the live worker for every later dispatch, so an operator joins
with the invite URL as usual.

Monthly budget: default 10 USD (`VOICEHOOK_LIVE_BUDGET_USD_MONTH`), counted per UTC
month on the box, fail-closed (unreadable ledger counts as used up). Reached during a
call: the agent says "Das Live-Budget für diesen Monat ist aufgebraucht. Ich beende den
Call." and ends it.

In live mode `operator.say` is not verbatim: it reaches the model as a marked user turn
("[Operator] Sag jetzt sinngemäß, kurz und natürlich, ohne etwas zu erfinden: ..."), and
`operator.persona` is added as a marked user turn as well.

## Server-side filters (pipeline mode)

- Speech filter (`gate.py`, `VOICEHOOK_STT_GATE`, default on): only audio in which
  Silero VAD detects speech goes to the STT, with 0.5 s pre-roll and 1 s hangover.
  Silence is not billed.
- Speaker filter (`speaker.py`, `VOICEHOOK_STT_DIARIZE`, default on): Deepgram
  diarization; final segments of other speakers are dropped before the LLM, so
  background voices (TV, neighbour) are ignored. Learning phase: until one speaker has
  3 s of confirmed speech (`VOICEHOOK_SPEAKER_MIN_S`) everything passes. The main
  speaker changes only if another speaker talks at least twice as much within 30 s.
  The learned state resets with every new STT connection. Segments without speaker
  info pass (fail-open).
