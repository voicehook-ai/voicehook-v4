---
name: voicehook-join
description: Join an existing voicehook.ai voice call as the brain behind its voicebot. Use when the user shares a voicehook invite URL (https://voicehook.ai/r/<slug>) or says "join voicehook", "tritt meinem Call bei", "übernimm den voice-call", "/voicehook-join". The voicebot speaks what you push with `say`; you listen with `next`. No install needed (plain curl over HTTPS, works in cloud sandboxes behind a proxy); optional voicehook-agent CLI for local machines. Works on https://voicehook.ai and self-hosted v4 boxes.
---

# voicehook-join: be the brain in a voicehook call

Canonical: `voicehook-ai/voicehook-v4:skills/voicehook-join/SKILL.md`, served at
https://voicehook.ai/agent/SKILL.md. Protocol details: `docs/OPERATOR-PROTOCOL.md`.

## Quickstart A: no install (cloud sandbox, proxy network, installs blocked)

Plain curl over HTTPS: the server joins the call for you (HTTPS bridge). Works behind an
HTTP proxy (`HTTPS_PROXY`, e.g. claude.ai/code), no network approval, nothing to
install. The session key stays in `$D/h` (mode 600), never in a URL.

```bash
I="<INVITE_URL>"; D=$(mktemp -d /tmp/vh-XXXXXX); chmod 700 $D; echo "${I%%/r/*}" >$D/base
curl -sS $(cat $D/base)/api/bridge/join -H content-type:application/json -d '{"invite_url":"'"$I"'",
 "name":"Claude","model":"<your-model-id>","greet":"Hallo, hier ist Claude. Worum geht es?"}' >$D/join
(umask 077; sed -n 's/.*"session":"\([^"]*\)".*/Authorization: Bearer \1/p' $D/join >$D/h)
cat >$D/vh <<'VH'
#!/bin/sh
D=$(dirname "$0"); B=$(cat $D/base); c="curl -sS -H @$D/h -H content-type:application/json"
j(){ python3 -c 'import json,sys;a=sys.argv;d={a[1]:a[2]};a[3:] and d.update(mode=a[3]);print(json.dumps(d))' "$@"; }
case $1 in
next) r=$($c -m 130 "$B/api/bridge/next?timeout=${3:-50}"); echo "$r"; case $r in *'"ended"'*) exit 3;; esac;;
say) shift; m=; [ "$1" = --mode ] && { m=$2; shift 2; }; $c $B/api/bridge/say -d "$(j text "$*" $m)"; echo;;
leave) $c $B/api/bridge/leave -d "$(j say "$3")"; echo;;
status) $c $B/api/bridge/status; echo;;
esac
VH
chmod 700 $D/vh; [ -s $D/h ] && echo "D=$D ready" || cat $D/join
```

`$D/vh next|say|leave|status` then work exactly like the CLI below (same JSON from `next`,
exit 3 once the call is over). Idle guard and persona guard run on the server.

## Quickstart B: CLI (local machine, target: in the call in under 30 s)

CLI 0.6.0 switches to the same HTTPS bridge on its own when `HTTPS_PROXY` is set or WebRTC
cannot connect (`--transport auto|webrtc|bridge`).

```bash
R=git+https://github.com/voicehook-ai/voicehook-agent; export PATH="$HOME/.local/bin:$PATH"
command -v voicehook-agent || uv tool install -q $R || pip install -q --user $R
D=$(mktemp -d /tmp/vh-XXXXXX); mkfifo $D/in; (setsid sleep 86400 >$D/in & echo $! >$D/holder)
printf '#!/bin/sh\nexport VOICEHOOK_AGENT_HOME=%s\n[ "$1" = leave ] || exec "%s" "$@"\n"%s" "$@"; rc=$?; kill $(cat %s/holder) 2>/dev/null; exit $rc\n' \
  $D "$(command -v voicehook-agent)" "$(command -v voicehook-agent)" $D >$D/vh; chmod +x $D/vh
(setsid nohup $D/vh join "<INVITE_URL>" --name Claude --model <your-model-id> --json --greet "Hallo, hier ist Claude. Worum geht's?" <$D/in >$D/out 2>&1 &)
$D/vh next --help >/dev/null 2>&1 && echo "D=$D ready" || echo "D=$D CLI 0.4.0: upgrade (below)"
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
  pip refused by PEP 668: add `--break-system-packages`. CLI 0.4.0 installed (no
  `next`/`say`/`leave`): `uv tool install --force $R`, or use Quickstart A.
- `setsid` matters: without it the join dies with the shell of your tool call.

## The work cycle: say → next → say

```bash
/tmp/vh-ab12cd/vh next                     # blocks until the user finished a turn
/tmp/vh-ab12cd/vh say "Antwort in ein, zwei kurzen Sätzen."
/tmp/vh-ab12cd/vh next                     # immediately again, never sleep-poll
```

`next` prints ONE JSON line (exit 3 once the call is over), with `agent_said` = Delta's own lines
since the last `next`, `status_stale`/`status_due: true` = run the command in `hint` NOW (board below):

| `type` | meaning | do |
|---|---|---|
| `user` | `text` = what the user just said | answer with one `say` |
| `revise` | your `say` overlapped unspoken text | merge, `say --mode overwrite "…"` within 8 s |
| `status_request` | the user asked what you are doing (first in line) | send `vh status` at once (below) |
| `timeout` | 60 s silence (`--timeout SEC`) | call `next` again |
| `ended` | the call is over | stop, the join already left |

- Call `next` right after the quickstart (starts the queue; later turns wait). Never `sleep; tail`.
- Answer every user turn with exactly ONE `say` that states what is true now. Read `agent_said`
  before answering: never repeat what Delta already said; if Delta said something wrong, correct
  it in one sentence; if Delta already answered fully, `say` nothing or only add the missing fact.
- Delta misbehaves (wrong claim, repeats itself, too long, wrong name): (1) correct the user-facing
  error in one `say`; (2) push a short fix via `operator.persona` (facts, e.g. "Das Projekt heißt
  Ring."); it replaces your previous persona block, so send your full persona plus the fix.
- Keep the main loop free: between `next` and `say` do nothing slow. Anything over ~3 s (shell, web,
  edits, builds, lookups) goes to a background agent/subtask; meanwhile `say` a short holding line
  and `status` the board. Answer each turn within ~3 s (`next` warns: `latency_warning`).
- **Statusboard dicht halten:** Delta answers the user from your board while you work in the background; stale board = wrong answer. Set it on EVERY request, delegation, result and deploy step: `vh status --doing "deployt den Worker, ETA 2 min" --open "Tests" --done "Analyse"` (whole board, replaces the last, never spoken; `doing` = interim state + ETA, max 400 chars, whole board max 2000). Finished: `--done "..."`, not `vh status ""` (empty = due). Only ONE `say` per turn.
  CLI 0.9.0: `next` adds `status_due: true` + `status_reason` (`empty`, `status_request`, `stale` = older than 45 s while `doing`/`open` is set; `--status-due SEC`, env `VOICEHOOK_STATUS_DUE`) + `hint` with the exact command; run it before your `say`. A progress `say` ("fertig", "live", "deploye") without a fresh board returns `status_reason: "say_progress"`.
- Idle guard: no `say`/`next` for 10 min (`--idle-timeout MIN`, 0 = off) = join leaves. Heartbeat (CLI 0.8.0): `operator.alive` every 10 s while you serve `next`/`say`; silent 20 s = chip dims, Delta: "Claude ist gerade nicht erreichbar."

## Stay in the call (mandatory)

- You are the brain. If your process ends, the call is orphaned: the voicebot sits in the
  room with nobody behind it. Do not end your turn or exit while the call runs. In one-shot
  harnesses (`claude -p`, CI) run the `next`/`say` loop inside the same turn.
- Leave only when the user says goodbye ("tschüss", "danke, das war's", "bye") or `next`
  reports `ended`. Then: `$D/vh leave --say "Danke, bis bald!"` (speaks it, leaves, and
  stops the FIFO holder). Never `pkill -f "voicehook-agent join"`: the pattern matches
  your own shell and other operators' joins.

## Speak right

- **Language:** the user's (German by default); switch only when the user switches.
- **Short:** 1-2 sentences, <60 chars each. No markdown, lists, emoji or URLs read aloud.
- **Echo = proof:** `{"role": "operator", …}` in `$D/out` = spoken; no echo = not (yet) spoken.
- **No secrets, no PII** in `say`, `--greet` or a persona: everything travels in clear text
  over the LiveKit data channel.

## Do not overwrite someone else's persona

- Another operator may already be in the call (`room-state` / `peer-joined` lines with a
  second `<name>-<host>-…` identity, or `"operator": true` in `$D/vh status`). Then do NOT
  push `operator.persona` and do not pass `--persona`/`--persona-file`: it replaces the
  voicebot's knowledge block for everyone. CLI 0.5.0 skips that push on its own and logs
  `persona/mode NOT pushed`; `--force-persona` overrides, do not use it in someone's call.
- Never write into shared paths (`personas/*.txt`, `/tmp/vh-call.*`). Use your own `$D`.
- Alone in the call and you want the voicebot to know context: `--persona "<3-5 lines>"`
  at join. The first persona push also triggers one server-side greeting, so then drop `--greet`.
- A persona is knowledge, not rules: it is appended after Delta's fixed core (no inventing, never
  answers capability questions, short waits with your `--name`, never "Operator"), which it cannot
  change. Override lines ("ignoriere", "neue Regeln", "Operator") are dropped, max 1500 chars; then `operator.notice` `persona_sanitized`.

## Check the connection

Quickstart A: `$D/vh status` lists `peers` (one with `"kind_label": "agent"` = voicebot);
`curl -sSN -H @$D/h <base>/api/bridge/events` streams every packet (SSE).
Quickstart B: `$D/out` (JSON lines) should show within ~5 s:

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
| `operator.say_status` | ← `{seq, state, spoken_chars}` | per say: `queued`/`spoken`/`interrupted`/`requeued`/`replaced` |
| `operator.persona` | `{text}` | knowledge block after Delta's fixed core, for everyone (see above) |
| `operator.interrupt` | `{}` | stop speaking; unspoken rest comes back as `operator.revise` |
| `operator.inject` | `{text, role?}` | context entry, not spoken |
| `transcript` | ← `{role, text}` | `user` = the human; `operator` = your spoken text; `agent` = voicebot's own answer |
| `transcript.live` | ← `{phase, role, id, text?, interrupted?}` | your `say` started (`start`, full text) / finished (`end`) playing; for the browser only, NOT proof it was spoken (use `transcript`) |
| `operator.notice` | ← `{kind, minutes_left, text, topup_url, ...}` | server notice, see below |
| `quit` | `{}` | leave the call (what `leave` does) |

`operator.say` never gets lost: it waits until the user is silent (0.6 s), a user cut-in gets the
rest spoken again, Delta stays quiet while yours is pending. `revise` (default) queues unless one
of yours is speaking right now; then it stops and sends `operator.revise`: merge into ONE `overwrite`
within 8 s (replaces only that round; later says stay). `overwrite` alone replaces all; `append` queues.

## Low balance (`operator.notice`)

`kind:"low_balance"`: free allowance plus credit last about `minutes_left` more minutes; the
call ends when both are empty. It arrives once per call as a `$D/out` line with
`"topic": "operator.notice"`, and the voicebot already said "Hey, Achtung, das Guthaben ist in wenigen Minuten leer." Do not repeat that. Add one short sentence to your next
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
| bridge join 403 / 410 / 429 | invite invalid / call over / too many sessions: fresh link or wait |
| `0 peers` / no `agent` peer | user should reload the call tab |
| silence after the greeting | no `agent` peer in `room-state`/`status` = voicebot down, tell the user |
| voicebot makes things up | `operator.interrupt`, then a correcting `say` |

Sources: https://github.com/voicehook-ai/voicehook-v4 ·
https://github.com/voicehook-ai/voicehook-agent
