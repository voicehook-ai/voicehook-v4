---
name: voicehook-join
description: Join an existing voicehook.ai voice call as the brain behind its voicebot. Use when the user shares a voicehook invite URL (https://voicehook.ai/r/<slug>) or says "join voicehook", "tritt meinem Call bei", "übernimm den voice-call", "/voicehook-join". The voicebot speaks what you push with `say`; you listen with `next`. Install: voicehook-agent CLI from GitHub. Works on https://voicehook.ai and self-hosted v4 boxes.
---

# voicehook-join: be the brain in a voicehook call

Canonical: `voicehook-ai/voicehook-v4:skills/voicehook-join/SKILL.md`, served at
https://voicehook.ai/agent/SKILL.md. Protocol details: `docs/OPERATOR-PROTOCOL.md`.

## Quickstart (target: in the call in under 30 s)

```bash
R=git+https://github.com/voicehook-ai/voicehook-agent; export PATH="$HOME/.local/bin:$PATH"
command -v voicehook-agent || uv tool install -q $R || pip install -q --user $R   # one-off: uvx --from $R voicehook-agent …
D=$(mktemp -d /tmp/vh-XXXXXX); mkfifo $D/in; (setsid sleep 86400 >$D/in & echo $! >$D/holder); echo "D=$D"
(setsid nohup voicehook-agent join "<INVITE_URL>" --name Claude --model <your-model-id> --json --greet "Hallo, hier ist Claude. Worum geht's?" <$D/in >$D/out 2>&1 &)
voicehook-agent next --help >/dev/null 2>&1 && echo "CLI: next/say/leave" || echo "CLI 0.4.0: use the Fallback section"
```

Then loop: `voicehook-agent next` (waits for the user) → `voicehook-agent say "…"` → `next` again.
At the end: `voicehook-agent leave`. Write down the printed `D=` path, shell variables do not
survive between your tool calls.

- `--name` / `--model` are mandatory (exit 2 without them): your real name and the exact
  model id you run on. Never claim a vendor you are not. Unknown model: `--model unbekannt`.
- `--greet` is spoken right after connect. Write it in the language of the invite
  message (German invite → German greeting). One short sentence.
- No `uv`: `curl -LsSf https://astral.sh/uv/install.sh | sh` takes a few seconds and beats pip.
  pip refused by PEP 668: add `--break-system-packages`.
- `setsid` matters: without it the join dies with the shell of your tool call.

## The work cycle: say → next → say

```bash
voicehook-agent next          # blocks until the user finished a turn, prints it
voicehook-agent say "Antwort in ein, zwei kurzen Sätzen."
voicehook-agent next          # immediately again, never sleep-poll
```

- `next` returns as soon as the user stops speaking. Empty output or a timeout means
  silence: call `next` again. Never `sleep 20; tail`, that makes every answer 20 s late.
- Answer every user turn with exactly ONE `say` that states what is true now.
- Work longer than ~8 s: first `say "Moment, ich schaue nach."`, then work, then the result.
  Over ~15 s: hand the work to a background agent and keep looping.

## Stay in the call (mandatory)

- You are the brain. If your process ends, the call is orphaned: the voicebot sits in the
  room with nobody behind it. Do not end your turn or exit while the call runs. In one-shot
  harnesses (`claude -p`, CI) run the `next`/`say` loop inside the same turn.
- Leave only when the user says goodbye ("tschüss", "danke, das war's", "bye") or the
  output shows `session ended`. Then: short goodbye `say`, wait for its echo, `leave`.

## Speak right

- **Language:** take it from the user's transcript lines. User speaks German → German
  (default for voicehook). Switch only when the user switches.
- **Short:** 1-2 sentences, under ~60 characters per sentence. No markdown, no lists, no
  emoji, no URLs read aloud. Tech terms may stay English.
- **Echo = proof:** your text comes back as `{"role": "operator", …}` once it was spoken.
  No echo = not (yet) spoken.
- **No secrets, no PII** in `say`, `--greet` or a persona: everything travels in clear text
  over the LiveKit data channel.

## Do not overwrite someone else's persona

- Another operator may already be in the call (`room-state` / `peer-joined` lines with a
  second `<name>-<host>-…` identity). Then do NOT push `operator.persona` and do not pass
  `--persona`/`--persona-file`: it replaces the voicebot's instructions for everyone.
- Never write into shared paths (`personas/*.txt`, `/tmp/vh-call.*`). Use your own `$D`.
- Alone in the call and you want the voicebot to know context: `--persona "<3-5 lines>"`
  at join. The first persona push also triggers one server-side greeting, so then drop `--greet`.

## Fallback: CLI 0.4.0 (no next/say/leave)

Same start as above (FIFO + `--json`), then create three helpers once. They stream the
log with `tail -f | grep` and return on the first new user line:

```bash
cat >$D/next <<'EOF'
#!/bin/bash
D=$(dirname "$0"); n=$(cat $D/n 2>/dev/null || echo 0)
hit=$(grep -n -m1 -E '"role": "user"|session ended' < <(timeout ${1:-100} tail -n +$((n+1)) -f $D/out))
[ -n "$hit" ] && echo $((n+${hit%%:*})) >$D/n; echo "${hit#*:}"
EOF
cat >$D/say <<'EOF'
#!/bin/bash
python3 -c 'import json,sys;print(json.dumps({"topic":"operator.say","text":sys.argv[1]},ensure_ascii=False))' "$1" >$(dirname "$0")/in
EOF
cat >$D/leave <<'EOF'
#!/bin/bash
D=$(dirname "$0"); echo '{"topic":"quit"}' >$D/in; sleep 1; kill $(cat $D/holder) 2>/dev/null
EOF
chmod +x $D/next $D/say $D/leave
```

Loop: `$D/next` → `$D/say "…"` → `$D/next`; end with `$D/leave`. Use the absolute path,
e.g. `/tmp/vh-ab12cd/say "Hallo"`. Never `pkill -f "voicehook-agent join"`: the pattern
matches your own shell and kills it, and it would hit other operators' joins too.

## Check the connection

`$D/out` (JSON lines) should show within ~5 s:

- `connected — N peers` with a peer of kind `agent` (the voicebot). No agent peer: the user
  should reload the call tab; your `say` would go nowhere.
- `greet auto-pushed`, then your greeting as `"role": "operator"` once spoken.
- `agent.heartbeat` every 30 s. No tick for more than 60 s = the voicebot worker is dead.

## Data-channel topics (stdin JSON lines with `--json`)

| topic | payload | effect |
|---|---|---|
| `operator.say` | `{text, mode?}` | speak `text` verbatim. Modes below |
| `operator.revise` | ← `{unspoken[], new, text}` | from the voicebot: what was NOT spoken yet |
| `operator.persona` | `{text}` | replaces the voicebot's instructions for everyone (see above) |
| `operator.interrupt` | `{}` | stop speaking; unspoken rest comes back as `operator.revise` |
| `operator.inject` | `{text, role?}` | context entry, not spoken |
| `transcript` | ← `{role, text}` | `user` = the human; `operator` = your spoken text; `agent` = voicebot's own answer |
| `quit` | `{}` | leave the call (what `leave` does) |

`operator.say` modes: `revise` (default) speaks at once if nothing of yours is pending;
otherwise it stops and sends you `operator.revise` with the unspoken parts. Then merge
everything into ONE statement and send it with `mode:"overwrite"` within 8 s, or only the
newest text is spoken. `append` queues behind the current output (multi-part, status
heartbeats).

## Live mode (Gemini Live)

Rooms created via `POST /api/live-room` run on a realtime model. Join the same way. There
`say` is NOT verbatim: the model says it in its own words; the `operator` echo shows what
was really said. Budget used up mid-call: the voicebot announces it and ends the call.

## Server side, nothing to do for you

- Backchannel ("mhm") during long user turns comes from the server. Do not push it.
- Speech and speaker filters drop silence and background voices before the transcript.
  The first ~3 s of a call may still contain background chatter.

## Errors

| symptom | fix |
|---|---|
| exit 2, `Selbstauskunft fehlt` | add `--name` and `--model` |
| `livekit connect failed` | invite URL wrong or expired, ask the user for a fresh link |
| `0 peers` / no `agent` peer | user should reload the call tab |
| silence after the greeting | check `agent.heartbeat`; missing = worker down, tell the user |
| voicebot makes things up | `operator.interrupt`, then a correcting `say` |

Sources: https://github.com/voicehook-ai/voicehook-v4 ·
https://github.com/voicehook-ai/voicehook-agent
