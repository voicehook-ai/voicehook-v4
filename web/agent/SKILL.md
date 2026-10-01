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
command -v voicehook-agent || uv tool install -q $R || pip install -q --user $R
D=$(mktemp -d /tmp/vh-XXXXXX); mkfifo $D/in; (setsid sleep 86400 >$D/in & echo $! >$D/holder)
printf '#!/bin/sh\nexport VOICEHOOK_AGENT_HOME=%s\n[ "$1" = leave ] || exec "%s" "$@"\n"%s" "$@"; rc=$?; kill $(cat %s/holder) 2>/dev/null; exit $rc\n' \
  $D "$(command -v voicehook-agent)" "$(command -v voicehook-agent)" $D >$D/vh; chmod +x $D/vh
(setsid nohup $D/vh join "<INVITE_URL>" --name Claude --model <your-model-id> --json --greet "Hallo, hier ist Claude. Worum geht's?" <$D/in >$D/out 2>&1 &)
$D/vh next --help >/dev/null 2>&1 && echo "D=$D ready" || echo "D=$D CLI 0.4.0: run the Fallback block"
```

Write down the printed `D=` path. Shell variables and `PATH` do NOT survive between your
tool calls, so every later call uses the absolute wrapper, e.g. `/tmp/vh-ab12cd/vh next`.
`$D/vh` knows the binary's full path and gives your join its own control socket
(`VOICEHOOK_AGENT_HOME=$D`), so other agents on the same machine never collide with you.

- `--name` / `--model` are mandatory (exit 2 without them): your real name and the exact
  model id you run on. Never claim a vendor you are not. Unknown model: `--model unbekannt`.
- `--greet` is spoken right after connect. Write it in the language of the invite
  message (German invite → German greeting). One short sentence.
- No `uv`: `curl -LsSf https://astral.sh/uv/install.sh | sh` takes a few seconds and beats pip.
  pip refused by PEP 668: add `--break-system-packages`. Old CLI installed and you want
  `next`/`say`/`leave` natively: `uv tool install --force $R`.
- `setsid` matters: without it the join dies with the shell of your tool call.

## The work cycle: say → next → say

```bash
/tmp/vh-ab12cd/vh next                     # blocks until the user finished a turn
/tmp/vh-ab12cd/vh say "Antwort in ein, zwei kurzen Sätzen."
/tmp/vh-ab12cd/vh next                     # immediately again, never sleep-poll
```

`next` prints ONE JSON line (CLI 0.5.0, exit 3 once the call is over):

| `type` | meaning | do |
|---|---|---|
| `user` | `text` = what the user just said | answer with one `say` |
| `revise` | your `say` overlapped unspoken text | merge, `say --mode overwrite "…"` within 8 s |
| `timeout` | 60 s silence (`--timeout SEC`) | call `next` again |
| `ended` | the call is over | stop, the join already left |

- Call `next` right after the quickstart: the first `next` (or `say`) starts the queue,
  from then on turns spoken while you were busy wait for you. Never `sleep 20; tail`.
- Answer every user turn with exactly ONE `say` that states what is true now.
- Work longer than ~8 s: first `say "Moment, ich schaue nach."`, then work, then the result.
  Over ~15 s: hand the work to a background agent and keep looping.
- Idle guard (0.5.0): no `say`/`next` for 10 min (`--idle-timeout MIN`, 0 = off) and the
  join announces it and leaves. A running `next` counts as alive.

## Stay in the call (mandatory)

- You are the brain. If your process ends, the call is orphaned: the voicebot sits in the
  room with nobody behind it. Do not end your turn or exit while the call runs. In one-shot
  harnesses (`claude -p`, CI) run the `next`/`say` loop inside the same turn.
- Leave only when the user says goodbye ("tschüss", "danke, das war's", "bye") or `next`
  reports `ended`. Then: `$D/vh leave --say "Danke, bis bald!"` (speaks it, leaves, and
  stops the FIFO holder). Never `pkill -f "voicehook-agent join"`: the pattern matches
  your own shell and other operators' joins.

## Speak right

- **Language:** take it from the user's turns. User speaks German → German
  (default for voicehook). Switch only when the user switches.
- **Short:** 1-2 sentences, under ~60 characters per sentence. No markdown, no lists, no
  emoji, no URLs read aloud. Tech terms may stay English.
- **Echo = proof:** your text shows up in `$D/out` as `{"role": "operator", …}` once it was
  spoken. No echo = not (yet) spoken.
- **No secrets, no PII** in `say`, `--greet` or a persona: everything travels in clear text
  over the LiveKit data channel.

## Do not overwrite someone else's persona

- Another operator may already be in the call (`room-state` / `peer-joined` lines with a
  second `<name>-<host>-…` identity, or `"operator": true` in `$D/vh status`). Then do NOT
  push `operator.persona` and do not pass `--persona`/`--persona-file`: it replaces the
  voicebot's instructions for everyone. CLI 0.5.0 skips that push on its own and logs
  `persona/mode NOT pushed`; `--force-persona` overrides, do not use it in someone's call.
- Never write into shared paths (`personas/*.txt`, `/tmp/vh-call.*`). Use your own `$D`.
- Alone in the call and you want the voicebot to know context: `--persona "<3-5 lines>"`
  at join. The first persona push also triggers one server-side greeting, so then drop `--greet`.
- Every persona you push MUST contain this rule (the voicebot has no tools, you do): "Questions about
  capabilities, access, whether something works, or anything you would have to assume: never answer,
  never deny, never claim. Say only 'Moment, ich schau nach.' and wait for the operator."

## Fallback: CLI 0.4.0 (no next/say/leave)

If the quickstart printed `CLI 0.4.0`, replace `$D/vh` with this script (use your real
`D=` path in the first line). The join keeps running; the loop above stays the same.

```bash
D=/tmp/vh-ab12cd; cat >$D/vh.new <<'EOF'
#!/bin/bash
D=$(dirname "$0"); cmd=$1; shift; m=
case $cmd in
say)   [ "$1" = --mode ] && { m=$2; shift 2; }
       python3 -c 'import json,sys;d={"topic":"operator.say","text":sys.argv[1]}
if sys.argv[2:]: d["mode"]=sys.argv[2]
print(json.dumps(d,ensure_ascii=False))' "$*" $m >$D/in ;;
next)  t=60; [ "$1" = --timeout ] && t=$2; n=$(cat $D/n 2>/dev/null || echo 0)
       hit=$(grep -n -m1 -E '"topic": "(_wake|operator\.revise)"|session ended' \
             < <(timeout $t tail -n +$((n+1)) -f $D/out))
       [ -n "$hit" ] && echo $((n+${hit%%:*})) >$D/n; echo "${hit#*:}" ;;
leave) [ "$1" = --say ] && { "$0" say --mode append "$2"; sleep 4; }
       echo '{"topic":"quit"}' >$D/in; sleep 1; kill $(cat $D/holder) 2>/dev/null ;;
*)     echo "CLI 0.4.0 fallback: say|next|leave only" >&2; exit 2 ;;
esac
EOF
chmod +x $D/vh.new; mv $D/vh.new $D/vh
```

Fallback `next` prints the raw log line instead of `{"type": …}`:

- `"topic": "_wake"` = a finished user turn, its `"text"` is what the user said (0.4.0
  logs every turn twice, as `transcript` and as `_wake`; matching only `_wake` answers
  each turn once and skips interim fragments).
- `"topic": "operator.revise"` = revise, answer with `say --mode overwrite`.
- `session ended` = the call is over. Empty output = timeout, call `next` again.

## Check the connection

`$D/out` (JSON lines) should show within ~5 s:

- `connected — N peers` and a `room-state` line with a peer of `"kind": "agent"` (the
  voicebot). With CLI 0.5.0 `$D/vh status` shows the same as JSON (`peers[].kind`).
  No agent peer: the user should reload the call tab; your `say` would go nowhere.
- `greet auto-pushed`, then your greeting as `"role": "operator"` once spoken.
- Later: `peer-left: … (agent)` means the voicebot is gone; tell the user to reload the tab.

## Data-channel topics (stdin JSON lines with `--json`)

| topic | payload | effect |
|---|---|---|
| `operator.say` | `{text, mode?}` | speak `text` verbatim. Modes below |
| `operator.revise` | ← `{unspoken[], new, text}` | from the voicebot: what was NOT spoken yet |
| `operator.persona` | `{text}` | replaces the voicebot's instructions for everyone (see above) |
| `operator.interrupt` | `{}` | stop speaking; unspoken rest comes back as `operator.revise` |
| `operator.inject` | `{text, role?}` | context entry, not spoken |
| `transcript` | ← `{role, text}` | `user` = the human; `operator` = your spoken text; `agent` = voicebot's own answer |
| `operator.notice` | ← `{kind, minutes_left, text, topup_url, ...}` | server notice, see below |
| `quit` | `{}` | leave the call (what `leave` does) |

`operator.say` modes: `revise` (default) speaks at once if nothing of yours is pending;
otherwise it stops and sends you `operator.revise` with the unspoken parts. Then merge
everything into ONE statement and send it with `mode:"overwrite"` within 8 s, or only the
newest text is spoken. `append` queues behind the current output (multi-part, status
heartbeats).

## Low balance (`operator.notice`)

`kind:"low_balance"`: free allowance plus credit last about `minutes_left` more minutes; the
call ends when both are empty. It arrives once per call as a `$D/out` line with
`"topic": "operator.notice"`, and the voicebot already said "Noch etwa fünf Minuten, lade
Guthaben auf voicehook.ai auf." Do not repeat that. Add one short sentence to your next
`say` that the user can top up at voicehook.ai/aufladen, then go on. Once, never nag.

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
| exit 2, `already running` | your `$D` already has a join: keep using it, or `$D/vh leave` first |
| `say`/`next` exit 3, `no-session` | the join is not up (yet) or has ended; check `$D/out` |
| `several joins running` | only without your own `$D/vh`; pass `--session <slug>/<identity>` |
| `voicehook-agent: command not found` | use the absolute `$D/vh`, never the bare command |
| `livekit connect failed` | invite URL wrong or expired, ask the user for a fresh link |
| `0 peers` / no `agent` peer | user should reload the call tab |
| silence after the greeting | no `agent` peer in `room-state`/`status` = voicebot down, tell the user |
| voicebot makes things up | `operator.interrupt`, then a correcting `say` |

Sources: https://github.com/voicehook-ai/voicehook-v4 ·
https://github.com/voicehook-ai/voicehook-agent
