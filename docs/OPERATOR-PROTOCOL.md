# Operator protocol (voicehook v4)

Reference for agents that join a call as operator. Source of truth is the code on
`main`: `apps/agent/relay.py`, `server.py`, `worker.py`, `live.py`, `gate.py`,
`speaker.py`, `budget.py`, `bridge.py`. Stand 2026-10-01 (up to PR #81, plus HTTPS bridge). How-to for agents:
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
`skills/voicehook-join/SKILL.md` has a wrapper (`$D/vh`) that gives the curl bridge
(Quickstart A) and the CLI (Quickstart B) the same `next`/`say`/`leave` interface. CLI 0.4.0:
upgrade with `uv tool install --force git+https://github.com/voicehook-ai/voicehook-agent`.

## HTTPS bridge (no WebRTC, no install)

For agents that only get HTTPS out, usually through an HTTP CONNECT proxy (`HTTPS_PROXY`,
cloud sandboxes like claude.ai/code). libwebrtc ignores that proxy and the box has no TURN,
so a WebRTC join times out (`wait_pc_connection timed out`). Through the bridge the server
joins the room for the agent as a normal participant, with the same token as the CLI
(`GET /api/token?invite=1&op_invite=<hmac>&name=&model=`: `vh.role=agent`, `vh.name`,
`vh.model`, same active-room check, voice-ai dispatch only for rooms with a payer). The HMAC
`?invite=` from the invite URL is verified (403 if invalid). Without it the join only works
during the transition period (`VH_REQUIRE_OPERATOR_INVITE=0`, logged as legacy join); with
`VH_REQUIRE_OPERATOR_INVITE=1` it is 403. The CLI sends the URL's invite as `op_invite`. Code: `apps/agent/bridge.py`,
`apps/agent/bridge_routes.py`.

The session key from `join` is a bearer secret (stored hashed on the server). It goes ONLY
in the header `Authorization: Bearer <session>`, never in a URL.

| endpoint | body / query | answer |
|---|---|---|
| `POST /api/bridge/join` | `{invite_url` or `room`+`invite?, name, model, identity?, greet?, persona?, force_persona?, idle_timeout?}` (`idle_timeout` in minutes, default 10, 0 = off) | `{session, expires_in, room, identity, idle_timeout_s, peers, notes}` |
| `GET /api/bridge/next?timeout=50` | max 120 s | one object like CLI `next`: `{type: user\|revise\|status_request\|timeout\|ended, text, ...}`; plus `status_stale: true` when the board is older than 5 min and the user spoke since |
| `POST /api/bridge/say` | `{text, mode?}` | `{ok, seq}`; payload on the wire `{text, _seq, _ts, mode?}` like the CLI |
| `POST /api/bridge/leave` | `{say?}` (optional) | `{ok, type: "leaving"}`; `say` is spoken with `mode:"append"` first |
| `GET /api/bridge/status` | | `{connected, pending, idle_s, peers[], sse_clients, ...}` |
| `GET /api/bridge/events` | | Server-Sent Events, see below |
| `POST /api/bridge/send` | `{topic, payload, force?}` | raw publish; topics: `operator.say`, `operator.persona`, `operator.mode`, `operator.interrupt`, `operator.inject`, `operator.backchannel`, `operator.status` (else 400) |

SSE events (`event: <type>` + `data: <json>`; comment `: ping` every 15 s): `hello`
`{room, identity, expires_in, peers}`, `room-state` `{peers}`, `data` `{topic, payload,
sender}` for EVERY data packet (transcript, operator.revise, operator.notice,
agent.heartbeat, ...), `peer-joined` / `peer-left` / `peer-updated`, `speakers`, `track`
`{identity, state: on|off|mute|unmute}`, `reconnecting` / `reconnected`, and `ended`
`{reason}` as the last event. A peer: `{identity, kind (LK int, 4 = agent), kind_label,
name, attributes, audio, speaking, operator}`.

Queue: user turns and `operator.revise` are queued from `join` on (bounded, 200), so turns
spoken while the agent works wait for the next `next`. Guards on the server: idle guard
(no `next`/`say`/`send` for `idle_timeout`; a running `next` counts as alive) announces and
leaves like the CLI; persona guard: `persona` at join and `send` of `operator.persona`/
`operator.mode` are refused (409) while another operator is in the room unless `force`;
SSE guard: once an SSE client was connected, the server leaves 60 s after the last one went
away; hard cap `VH_MAX_CALL_SECONDS` (CallGuard, default 3600); room ended -> `ended`.
Limits: 4 sessions per room, 4 per IP, 100 total, 10 joins per IP per minute, 20 sends per
10 s per session (429). Errors at join: 400 no room, 403 invite invalid, 410 call ended,
429 limits, 502 LiveKit connect failed, 503 server without LiveKit credentials. Nothing of
what is said and no key is logged. `VOICEHOOK_BRIDGE_LIVEKIT_URL` overrides the LiveKit URL
the server itself uses (default `LIVEKIT_URL`).

CLI 0.6.0 (`--transport auto|webrtc|bridge`, default auto) uses the bridge on its own when
`HTTPS_PROXY`/`ALL_PROXY` is set or the WebRTC connect fails (one retry via the bridge,
logged as a `_meta` line). Its output, `say`/`next`/`leave` and FIFO input stay identical.

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
| `operator.status` | operator to agent | `{doing, open[], done[]}` (or `{text}` = doing) | status board, see below; replaces the previous one, never spoken |
| `operator.status_request` | agent to operator | `{text}` | the user asked for your status; answer at once with `operator.status` |
| `operator.notice` | agent to everyone | `{kind, minutes_left, seconds_left, free_s, free_eur, balance_eur, topup_url, text}` | server notice, see below; sent reliable |
| `cost` | agent to everyone | `{eur, mode}` (admin rooms also `usd, basis, prices_as_of`) | running customer price of the call, see below; only sent when the sum changed |
| `transcript` | agent to everyone | `{role, text}` | see transcript roles |
| `transcript.live` | agent to everyone | `{phase:"start", role:"operator", id, text}` / `{phase:"end", role, id, interrupted}` | an `operator.say` output started / finished playing (pipeline mode only); for the browser's live reading. Not an echo: `transcript` alone means "spoken" |
| `agent.heartbeat` | agent to everyone | `{ts, room, probe, healthy}` | every 30 s; no tick for more than 60 s = worker dead |

## operator.status (board)

The voicebot never says "Operator" to the user; it names you by your `vh.name`
(`--name`, letters only, max 24 chars, else "dein Agent"): "Kurzen Moment, ich frag
Claude." Your status board sits at a fixed place in its instructions:
`{doing: "baut gerade den Fix", open: ["Tests"], done: ["Analyse"]}`. Every send
REPLACES the whole board (context never grows). Budget 600 chars in total (each item
max 120, 10 per list): `done` is cut first (oldest), then `open` (last). Empty or
`doing:"fertig"` with no lists clears it. At most one update per 5 s per room is applied,
the last one wins. Nothing is spoken; asked for the status, the voicebot answers from the
board ("Kurz Moment, Claude baut gerade den Fix").

Triggers: push the board on every task change (started, finished, new task). When the
user asks for your status, the voicebot sends `operator.status_request`
(`next` -> `{type:"status_request"}`); answer with a fresh board at once. If it arrives
within 8 s, the voicebot speaks one sentence from `doing`. `next` also carries
`status_stale: true` when your board is older than 5 min and the user spoke since.

Keep the main loop free: answer a `user` turn within ~3 s and hand slow work (shell, web,
edits, builds) to a background agent. CLI 0.7.0 measures the time from a `user` turn
leaving `next` to your next `say`; over 8 s the following `next` carries
`latency_warning: {seconds, hint}`. Nothing is spoken automatically.

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

## operator.notice

Sent by the voicebot to everyone in the room (operator CLI and browser), at most once per
call per `kind`. The CLI prints it as a system line with `"topic": "operator.notice"` and
the `text` field.

`kind:"low_balance"`: the free allowance (1 EUR of usage per UTC day) plus credit will last
about `minutes_left` more minutes at the current usage ((free rest + balance) divided by the
real cost of the last 3 minutes, gross incl. factor and VAT). Fired when that drops to 5
minutes or less.

| field | type | meaning |
|---|---|---|
| `kind` | `"low_balance"` | notice type |
| `minutes_left` | int | rounded up |
| `seconds_left` | int | estimate in seconds |
| `free_s` | int or null | estimated seconds the free rest lasts at the current usage; `0` = free part used up, `null` = room has no free part or usage still unknown |
| `free_eur` | float or null | free allowance left today in EUR; `0` = used up, `null` = room has no free part |
| `balance_eur` | float or null | wallet balance; `null` = room has no wallet |
| `topup_url` | string | `https://voicehook.ai/aufladen` |
| `text` | string | what the voicebot says at the same moment: "Hey, Achtung, das Guthaben ist in wenigen Minuten leer." |

Operator: do not repeat the sentence; mention top-up once in your next `say`. Browser:
show a visible hint with a link to `topup_url`. The call ends when the free allowance and
credit are both used up (free first, then credit), with its own short announcement.

## cost

Running cost of the call for the browser subtitle, sent by the voicebot only when the
sum changed (silence sends nothing). `eur` is the customer price: real provider cost x
factor (normal 3, live 1.5) plus VAT, the same amount that is booked from the free
allowance and the wallet. `mode` is `"pipeline"` or `"live"`. Raw provider cost (`usd`,
`basis` with the measured quantities, `prices_as_of`) is only included in admin/operator
rooms (exempt), never in customer rooms.

## Transcript roles

The worker publishes everything that was actually spoken on `transcript`:

| role | meaning |
|---|---|
| `user` | final STT of the human (after the speech and speaker filters) |
| `operator` | text from `operator.say`, published after it was spoken; on an interruption only the spoken part. Browser: red |
| `agent` | the agent's own answer (auto mode). Browser: blue |

Pipeline mode matches operator lines by speech handle and text prefix; live mode only
by handle, because the realtime model rephrases.

`transcript.live` (separate topic, so the `transcript` echo semantics stay unchanged):
`phase:"start"` is sent when the first audio frame of an `operator.say` output plays,
with the full text and the speech `id`; `phase:"end"` when that output is done, with
`interrupted:true` if it was cut off. The browser shows the text from the start (word
reveal at speaking pace) and replaces it with the spoken part from `transcript`, marking
the unspoken rest. Live mode sends nothing here (the realtime model rephrases).
Operators should keep treating only `transcript` role `operator` as "spoken".

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
