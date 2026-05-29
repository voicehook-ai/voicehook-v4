# voicehook-join — drive a voicehook v4 call as senior brain

> v4 is the **strict-mouthpiece** path. The agent never auto-generates. You
> push `senior.say` for everything substantive. Greet + 30s heartbeat +
> backchannel are server-side (v4 fixes voicehook-v3#61) — you don't push them.

## 10-line quickstart

```bash
# 1) install once
uv tool install git+https://github.com/voicehook-ai/voicehook-agent
# 2) mint an invite for a room
INVITE_URL=$(python -m agent.cli invite drift-signal-crisp-AB12 --ttl 3600)
# 3) join as senior in the same room
voicehook-agent join "$INVITE_URL" --name claude --json --auto
# 4) push a reply when needed
echo '{"topic":"senior.say","text":"Hallo Olli, was brauchst du?"}' >> /tmp/vh.in
# 5) read new turns
tail -f /tmp/vh.out | jq -c 'select(.topic=="transcript")'
```

That's it. v4 does the rest: greet on first persona-push, 30s heartbeat on
`agent.heartbeat`, "mhm" backchannel when user speaks >6s without reply.

## What the voice-ai may say

ONLY one of:
1. text you push via `senior.say` (verbatim TTS)
2. content of the latest `senior.persona` you injected (bounded knowledge)
3. server-side primitives: the one-time greet on first persona, periodic
   backchannel during long user-turns, web-search verbatim quote (deferred)

NEVER invents. `RelayAgent.on_user_turn_completed` raises `StopResponse`
inside the agent so the LLM cannot produce a turn on its own.

## senior.* topics

| topic              | payload                                  | behavior |
|--------------------|------------------------------------------|----------|
| `senior.say`       | `{text, priority?}`                      | verbatim TTS; `priority:"interrupt"` drops the current floor first |
| `senior.persona`   | `{text}`                                 | replaces agent.instructions live; first push also triggers server-side auto-greet |
| `senior.interrupt` | `{}`                                     | drop current say |
| `senior.inject`    | `{text, role?}`                          | synthetic chat-ctx entry (NOT spoken); senior reads it back via transcript |

## Server-side primitives (no roundtrip needed)

- **Auto-greet**: first `senior.persona` triggers a one-shot TTS greet.
- **Heartbeat**: `agent.heartbeat` topic emits `{ts, room, probe:{stt,tts,llm}, healthy}` every 30s. NOT on `transcript` (v3#62 fix).
- **Backchannel**: when the user has been speaking ≥6s and the agent has not
  spoken, the server fires "mhm"/"ja"/"ok" (cycled) at low priority. You do
  not need to manage this.

## Pre-flight

```bash
which voicehook-agent || uv tool install git+https://github.com/voicehook-ai/voicehook-agent
test -n "$INVITE_SECRET" || { echo "set INVITE_SECRET"; exit 1; }
```

## Conversation loop

Listen for new turns on the transcript topic; push `senior.say` for real
answers; otherwise do nothing. Silence is correct when nothing was asked.

```bash
tail -f /tmp/vh.out | grep --line-buffered '"topic":"transcript"' | \
  jq -c 'select(.role=="user")'                     # see only user-turns

echo '{"topic":"senior.say","text":"Verstanden, ich check kurz."}' >> /tmp/vh.in
# (do work — read files, run greps, etc.)
echo '{"topic":"senior.say","text":"Fix gefunden, deploye jetzt."}' >> /tmp/vh.in
```

## What changed vs v3 skill (447 LOC → this)

- **Greet/heartbeat/backchannel**: GONE — server-side now (v3#61)
- **FIFO + holder + nohup boilerplate**: GONE — `--auto` flag bundles it (v3 voicehook-agent#17)
- **Dual Monitor (transcript + box log)**: GONE — single events file, server emits dedicated `agent.heartbeat` topic
- **PRE-FLIGHT 8-box checklist**: shrunk to 2 lines (CLI + secret)
- **Persona render heredoc**: GONE — pass `--memory-dir` to CLI; it renders + scrubs secrets
- **JSONL knowledge-graph + rolling-window**: GONE from skill (still implementable on top, but not required for a working call)

## Cleanup

`--auto` mode registers a SIGTERM trap that disconnects + removes FIFOs.
On Ctrl-C you exit cleanly.

## Failure modes

- voice-ai not in room → check `_meta room-state` in /tmp/vh.out for `kind:"agent"` peer
- silence after greet → check `agent.heartbeat` is firing; if stops >60s the worker crashed
- agent inventing content → server-side StopResponse should prevent; if it slips, file v4 issue with transcript

## More

Plan: <https://github.com/voicehook-ai/voicehook-v3/pull/64>
v4 repo: <https://github.com/voicehook-ai/voicehook-v4>
