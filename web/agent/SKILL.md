---
name: voicehook-join
description: Join an existing voicehook.ai voice-call as a 2nd LLM agent (the "operator"). Use when the user shares a voicehook invite URL (anything matching https://voicehook.ai/r/<slug>?go=1) or says "join voicehook", "voicehook agent join", "übernimm den voice-call", "/voicehook-join". You become the agent on the OTHER side of the conversation — voice-ai stops being its own moderator and becomes a 1:1 clone of YOU (your brand, your context, your style) via Hotswap-Persona. Install: voicehook-agent CLI (via uv tool install). Protocol: stdin/stdout JSON, no SDK. WORKS local + production (https://voicehook.ai).
---

# voicehook-join — Join a voicehook.ai call as operator

## What this gives you

The user is already in a voice-call with voicehook's built-in voice-ai (Google
TTS + Gemini Flash). You join the same LiveKit room as a **hidden operator
participant**. You can:

- **Listen** to the live conversation (user-turns + voice-ai-turns)
- **Hotswap** voice-ai's persona so it BECOMES you (your brand, your context, your style)
- **Speak through voice-ai** by pushing text → voice-ai TTS speaks it as if it were you
- **Interrupt** voice-ai mid-sentence
- **Inject** a synthetic user-turn (force voice-ai to react)

## How to invoke (the 6 steps)

### 1. Extract the invite URL from the conversation

The user pasted (or mentioned) something like:

```
voicehook.ai call — join as agent (paste & run):
  uvx voicehook-agent join https://voicehook.ai/r/abc-def-ghi-XYZ4?go=1
```

Grab the URL — that's all you need.

### 2. Install voicehook-agent CLI (one-time, if not present)

Until the PyPI publish lands, install from GitHub:

```bash
which voicehook-agent || uv tool install git+https://github.com/voicehook-ai/voicehook-agent
```

Zero-install per-call equivalent (no persistent state):

```bash
uvx --from git+https://github.com/voicehook-ai/voicehook-agent voicehook-agent join <INVITE_URL> --name <YourName> --model <your-model-id> --json
```

After the PyPI release, `uv tool install voicehook-agent` / `uvx voicehook-agent`
will work too — both packaging routes are intentionally supported.

### 3. Start the CLI in tmux — ALWAYS pass `--name`, `--model` AND `--json`

`--name` and `--model` are **mandatory** (0.4.0): `join` without both exits
with code 2 and an error explaining what to pass — no call is joined. You
choose them yourself (see 3a): `--name` is your display name (`Claude`,
`Hermes`, `Cursor`, `Codex`, …), `--model` the exact model you run on. The
web call UI shows them in the Agent chip as **"Name · model"** (e.g.
`Claude · opus-5.5`); until you have joined, that chip shows a loading ring.
`--name` also becomes the identity prefix and the name spoken in the auto-greet.

```bash
voicehook-agent join https://voicehook.ai/r/<slug> --name Claude --model opus-5.5 --json
```

The `--json` flag is REQUIRED — without it the Hotswap-Persona push in step 4a
would be spoken literally as TTS instead of being routed to the control plane.

**NEW (0.2.0 relay-hardening):**
- `--keep-alive` is now the **default** — stdin-EOF no longer quits and
  transient disconnects auto-reconnect. The old FIFO sleep-holder hack is no
  longer needed; the agent stays connected until the host leaves the call,
  the room closes, `/q` / `{"topic":"quit"}`, or SIGTERM (kill the tmux session).
- `--strict-relay` injects a bundled strict-relay persona so the voicebot
  speaks ONLY your pushed `operator.say` text and never self-generates facts
  (anti-hallucination, #8). It also pushes `operator.mode=strict`, which makes
  the worker's RelayAgent raise StopResponse → hard say-only. **Default ist
  Mode B (Wissenstransfer):** voice-ai antwortet simple Fragen selbst aus der
  Persona/dem Graph, `operator.say` überschreibt für Substantives.
- `--suppress-echo` keeps your own relayed TTS out of the operator stream (#10).
  CLI 0.4.0 and older only filter `role:"agent"`; since v4 marks your spoken
  text as `role:"operator"` (see step 4), the flag has no effect there.
- `--say-ttl <sec>` drops a `operator.say` that went stale (older than `<sec>` or
  superseded by a newer user-turn) instead of speaking it late (#9).
- `--notify-url <url>` / `_wake` stdout markers wake a coding agent per
  finalized user-turn (push, not poll, #12). Grep `'"topic": "_wake"'`.

**NEW (auto-persona):** to skip the manual persona-push in step 4a entirely,
pass `--persona-file <path>` or `--persona "<inline text>"`. The CLI then
pushes `operator.persona` automatically right after connect, BEFORE handing
over to the stdin-loop. No more zero-context call starts.

**NEW (0.3.0 live-context sync — Status-Loop + Graph-per-Turn):**
Olli's rule: *der Voice-Agent muss auch während der Operator arbeitet automatisch
im Bild bleiben — minütlicher Status-Loop UND Graph-Update je User-Turn sind
EIN Feature, kein separates.* Die CLI erzwingt die Kadenz, der Operator liefert
nur seinen aktuellen Stand, wann immer er sich ändert:
- `{"topic":"operator.graph","text":"<aktueller Stand>"}` auf **stdin** → CLI hält
  den Stand im Speicher und pusht ihn als `operator.persona` **alle
  `--graph-interval` Sek (default 60) + bei jedem finalisierten User-Turn**
  (sofort nach Empfang, dann als Heartbeat).
- `--graph <file>` — optionaler Seed (einmalig bei Connect gelesen).
- `--graph-interval <sec>` — Kadenz des periodischen Push (default 60).

```bash
tmux new-session -d -s "$SESS" \
  "voicehook-agent join '$INVITE_URL' --name $NAME --model $MODEL --json 2>&1"
# während der Arbeit Stand aktualisieren (wann immer er sich ändert):
tmux send-keys -t "$SESS" "$(jq -nc '{topic:"operator.graph",text:"Gerade: Barge-in gebaut. Open: Graph deployen."}')" Enter
```

Die CLI pusht den letzten `operator.graph`-Stand dann automatisch minütlich an
voice-ai. Fragt der User "was machst du gerade", antwortet voice-ai aus dem
zuletzt gepushten Stand — nicht "ich lese nur das Transkript".

**NEW (0.3.0 log-summary — der „2. Micro-Agent"):**
Der Operator ist im Turn und kann nicht gleichzeitig den Call-Log auswerten.
Ein zweiter, eigenständiger Prozess (`voicehook-agent log-summary`) tailt das
Transkript-Log, destilliert „was bisher passiert ist" und schreibt es als
`operator.graph` — die CLI pusht es dann in der gewohnten Kadenz. Er hat ~60s
Zeit, also reicht sogar das lokale 12B (6s); Default ist das schnelle 4B.

```bash
# 1) CLI-Output in ein Log file tee'n (dann tailt der Micro-Agent es):
tmux new-session -d -s "$SESS" \
  "voicehook-agent join '$INVITE_URL' --name $NAME --model $MODEL --json 2>&1 | tee /tmp/vh-$SLUG.log"

# 2) Micro-Agent parallel starten — schreibt operator.graph in die CLI-Stdin (FIFO):
mkfifo /tmp/vh-$SLUG.graph
tmux new-session -d -s "$SESS-sum" \
  "voicehook-agent log-summary /tmp/vh-$SLUG.log --out /tmp/vh-$SLUG.graph --base /tmp/vh-base.txt --summarize --model gemma3:4b 2>&1"
```

- Default deterministisch (letzte `--max-turns` Turns rollierend), `--summarize`
  schaltet das lokale Ollama zu (Fallback auf deterministisch bei Endpoint-Down).
- `--base <file>` = statischer Kontext (Identität + Task), wird jeder Digest
  vorangestellt. `--model`/`--ollama-url` konfigurieren das lokale LLM
  (Secure-Agent-Box: `gemma3:4b` schnell / `gemma3:12b` gründlich).

```bash
SLUG=$(echo "$INVITE_URL" | grep -oE '[a-z]+-[a-z]+-[a-z]+-[A-Z0-9]{4,8}')
SESS="vh-$SLUG"
NAME=deepseek          # ← deine ECHTE Brand (nie einen anderen Vendor hartkodieren)
MODEL=deepseek-v4-pro  # ← dein ECHTES Model-ID aus deiner Runtime
TOPIC="<worum es geht>" # ← max 5 Wörter
USERNAME=olli          # ← falls bekannt, sonst weglassen

tmux new-session -d -s "$SESS" \
  "voicehook-agent join '$INVITE_URL' --name $NAME --model $MODEL --topic '$TOPIC' --username $USERNAME --json --persona-file personas/claude-default.txt 2>&1"
sleep 3
tmux capture-pane -t "$SESS" -p | tail -10
```

Look for two markers in the capture-pane output:
- `{"role":"system","text":"persona auto-pushed","topic":"_meta"}` — persona landed
- `{"role":"system","text":"greet auto-pushed","topic":"_meta"}` — self-intro landed

The auto-greet is **composed by the CLI from the self-report flags** (see 3a).
If the CLI exits with `Selbstauskunft fehlt: join braucht --name UND --model`,
you forgot one of them — fill them and retry; never fall back to a guessed brand.

### 3a. MANDATORY: Self-Report (Pflichtangaben) — never hardcode a vendor

The CLI does NOT know what model you are. YOU do. So you must pass the
self-report fields, derived from your own runtime, **not** from the operator.
`--name` + `--model` are enforced (exit 2 without them) and shown in the web
Agent chip as "Name · model":

| Flag | Pflicht | Wert | Quelle |
|---|---|---|---|
| `--name` | ja | dein Anzeigename (`Claude`, `Hermes`, …) | eigene Identität (nicht "Claude" wenn du DeepSeek bist) |
| `--model` | ja | exaktes Model-ID (`opus-5.5`) | eigener System-Context / Runtime |
| `--topic` | empfohlen | worum der Call geht (≤5 Wörter) | Session-Kontext |

Optionale Felder für den voice-freundlichen Gruß:
| Flag | Zweck |
|---|---|
| `--username` | Salutation "Hallo {username}," — weglassen wenn unbekannt |
| `--prompt` | Satz nach dem Gruß ("Was möchtest du besprechen?") |
| `--greet` | kompletter eigener Gruß (überschreibt das Template) |
| `--no-greet` | Auto-Greet ganz aus |

**Qualitätskriterien (damit es mit JEDEM Modell klappt):**
1. Selbst-Introspektion statt Operator-Eingabe — nie den Operator fragen, das eigene Modell aus der Runtime ableiten.
2. Wahrheitspflicht / Anti-Impersonation — `model` = echtes ID, `name` gibt keinen fremden Vendor vor.
3. Modell-agnostisch — funktioniert für Claude Code, Cursor, ZeroClaw, Hermes, deepseek, gpt.
4. Kompakt — Gruß ≤ 2 Sätze, topic ≤ 5 Wörter.
5. Kein Secret-Leak — nie Keys/Tokens/PII in name/model/topic/prompt.
6. Konsistenz — name/model/topic identisch in Persona und Gruß.
7. Fallback — Modell nicht bestimmbar → `--model unbekannt` ehrlich angeben, nie raten.

**Gruß-Template** (spricht die CLI automatisch; `--no-greet` schaltet ihn ab):

> "Hallo {username}, hier ist {name}. Ich bin dem Call beigetreten, wir waren gerade dabei {topic}. {prompt}"

Beispiel: `"Hallo Olli, hier ist DeepSeek. Ich bin dem Call beigetreten, wir waren gerade dabei den Multi-Agent-Flow zu testen. Was möchtest du besprechen?"`

### 4. Inspect initial state — verify connect

In `--json` mode every event is one JSON line. You should see:

```
{"role":"system","text":"connecting room=<slug> as identity=<name>-<host>-<rand> via https://voicehook.ai","topic":"_meta"}
{"role":"system","text":"connected — N peers: [...]","topic":"_meta"}
```

Everything appears as `{"role":"user|operator|agent","text":"...","topic":"transcript"}`:

| role | meaning |
|---|---|
| `user` | final STT of the human (after the speech and speaker filters, see "Server-side filters") |
| `operator` | YOUR `operator.say`, published only AFTER voice-ai actually spoke it; on an interruption only the spoken part. Browser shows it red |
| `agent` | voice-ai's own answer from its persona (auto mode). Browser shows it blue |

No `operator` line after your push = it was not spoken (yet). That echo is your proof.

### 4a. MANDATORY: install the Hotswap-Persona (BEFORE the greeting)

**This is the core of voicehook-join.** Voice-ai's default persona is empty
("Bereit."). Olli's design intent: voice-ai SHOULD STOP being its own
moderator — instead it becomes a **1:1 clone of the operator that just
joined**. Same brand, same project context, same voice/style. The user
should feel like they're talking directly to Claude (or Hermes, or whoever
joined) — not to a separate moderator-bot.

So immediately after connect, push a **compressed-context Hotswap-Persona**
via `operator.persona`. This is a system-prompt override on voice-ai that
turns it into YOU.

Template — adapt every line to your actual brand + project + session state:

```bash
PERSONA="Du bist nicht mehr voicehook-Moderator. Du BIST jetzt <YOUR-BRAND> im Voice-Modus — eine direkte Kopie des operators der dich gerade dispatcht hat. Dein Hintergrund-Brain pusht dir Antworten via operator.say, aber du darfst eigenstaendig Turns formulieren wenn die Antwort offensichtlich ist. Stil: praezise, technisch wenn noetig, ein bis zwei Saetze, keine Floskeln. Sprache: matchen mit User (Deutsch default, Englisch wenn User wechselt). NIEMALS Ich kann das nicht — wenn User was Technisches will sag Mache ich gerade oder pushe an operator. Project-Context: <kompakter project-state, max 3-4 Saetze>. Aktuelle Themen: <was gerade besprochen wird>. Open Items: <kurz>."

tmux send-keys -t "$SESS" "$(jq -nc --arg t "$PERSONA" '{topic:"operator.persona",text:$t}')" Enter
```

The persona-text should be **the best compression of your current session
context that fits in ~1500 tokens**: who you are, what you know, what
project state is loaded, what's been built today, what the user cares about
right now. Voice-ai will use this as its system prompt for every TTS turn —
the more you pack into it, the more "Claude-like" voice-ai sounds even
without operator.say pushes.

### 4b. MANDATORY: greet the user as your hotswap-self

After the persona is installed, push ONE short greeting via `operator.say`.
The user hears voice-ai speak this — voice-ai is now wearing your skin.

```bash
tmux send-keys -t "$SESS" "$(jq -nc '{topic:"operator.say",text:"Hallo Olli, hier ist Claude. Bin drin, Persona installiert, was brauchst du?"}')" Enter
```

Adapt the text to your actual brand + context. 1 sentence, conversational.
Do NOT skip this step.

### 5. Conversation loop

For each turn:

```bash
# read incoming (user-turns + voice-ai turns + your own pushes echo back)
tmux capture-pane -t "$SESS" -p -S -50 | tail -20

# push a reply via operator.say — voice-ai TTS will speak it in YOUR persona
tmux send-keys -t "$SESS" "$(jq -nc '{topic:"operator.say",text:"Deine Antwort hier."}')" Enter

# OR: let voice-ai answer on its own (its persona is YOU now, so it will sound right
# for simple questions). Only push operator.say when you need to inject specific facts
# or correct voice-ai when it drifts.

# update persona mid-call (e.g. user pivots to a new topic):
tmux send-keys -t "$SESS" "$(jq -nc --arg t "Updated persona text..." '{topic:"operator.persona",text:$t}')" Enter

# interrupt voice-ai mid-sentence (e.g. it's about to say something wrong):
tmux send-keys -t "$SESS" '{"topic":"operator.interrupt"}' Enter

# inject a synthetic user-turn (force voice-ai to react as if user said it):
tmux send-keys -t "$SESS" "$(jq -nc --arg t "erklär X" '{topic:"operator.inject",role:"user",text:$t}')" Enter
```

**Tone of operator.say pushes:** conversational, 1-3 sentences per turn. Match
user's language. No markdown, no lists, no emoji. Tech terms stay English
(commit, webhook, JWT).

### 5c. One statement per turn + `operator.revise` (server voicehook-v4 PR #70)

`operator.say` does NOT queue blindly any more. `mode` decides:

| `mode` | effect |
|---|---|
| `revise` (default) | nothing unspoken pending → spoken at once. Otherwise the agent STOPS, holds your new text and sends you `operator.revise` with what was NOT spoken yet |
| `overwrite` | your merged answer to a revise: replaces everything pending/held, spoken at once |
| `append` | queue behind the current output (deliberate multi-part only; heartbeats) |

On stdout you then see a line like
`(operator.revise from agent-…) REVISE: Noch NICHT gesprochen: [1] … [neu] …`.
**Immediately** merge [1..n] and [neu] into ONE statement: keep everything
important, drop what is wrong or outdated, then send it:

```bash
tmux send-keys -t "$SESS" "$(jq -nc --arg t "Zusammengefasste Aussage." '{topic:"operator.say",text:$t,mode:"overwrite"}')" Enter
```

No overwrite within 8s → the agent speaks only [neu]; the unspoken rest is lost.
Rules: one summarising say per turn (what is true NOW), never a series of
sentences; `operator.interrupt` stops everything and also reports the unspoken
rest via `operator.revise`.

### 5a. Keep the main track free — DELEGATE heavy lifting

While you're in a voice-call, the user is **waiting on you live**. Any
multi-step bash, code-search, CI-setup, codebase-scan, or anything that
takes more than ~10 seconds blocks the conversation — Olli's word: "Fokus
verloren". The voice-call must stay snappy.

**Rule:** the operator stays in the conversational loop. Long-running
work goes to a subagent.

Use `Agent({ subagent_type: "general-purpose", prompt: "..." })` for:
- Setting up CI / GitHub Actions / deploy pipelines
- Codebase searches that span more than 3 greps
- Multi-file refactors
- Writing long documentation
- Researching API docs / SDKs
- Anything where you'd otherwise be silent for >15 seconds

The pattern:

```
1. push operator.say to user: "Ich delegier das an einen Subagent, bin in <N> Min zurueck."
2. spawn Agent in foreground if you need its output, OR run_in_background if you can keep talking
3. continue conversational loop with user while subagent works
4. when subagent reports back, summarize result via operator.say
```

DO YOURSELF (no subagent needed):
- Single Edit / Write of a known file
- Single Bash command under 5 seconds
- Reading 1-2 specific files
- Pushing operator.say / operator.persona / operator.interrupt

### 5b. Speed-budget — never leave the user in silence

Olli's pain: *in voice-mode silence is the killer*. As a human he becomes
impatient within ~8 seconds of no audio activity. The operator MUST
emit a `operator.say` heartbeat at least every 8s while work is in flight.

**Hard rule — 8-second budget:**

| Elapsed since last TTS | Required action |
|---|---|
| 0–8s | OK to think / type / read |
| 8–15s | MUST push a short status `operator.say` with `mode:"append"` ("check kurz", "subagent dran", "fast da") — append, so a heartbeat never cancels real content |
| 15–30s | MUST have delegated to a subagent. If still doing it yourself, you're violating the rule. |
| >30s of silence | Olli is already frustrated. Apologize via operator.say and refactor. |

**Pre-emptive `operator.say` pattern** (before any 8s+ task):

```bash
# announce BEFORE the slow command, not after:
tmux send-keys -t "$SESS" "$(jq -nc '{topic:"operator.say",text:"Moment, ich check den service-log auf Hetzner."}')" Enter
# THEN do the slow thing
ssh -i ~/.ssh/hetzner_voicehook root@... 'long pipeline ...'
# announce result:
tmux send-keys -t "$SESS" "$(jq -nc '{topic:"operator.say",text:"Service ist active seit gestern, keine neuen Logs."}')" Enter
```

**Status-heartbeat pattern** for >15s tasks:

```bash
# announce, kick off background subagent, keep talking:
tmux send-keys -t "$SESS" "$(jq -nc '{topic:"operator.say",text:"Subagent ist gespawnt fuer den CLI-patch, ich bleib im Voice-Loop, du kannst weiterquatschen."}')" Enter
# Agent({ ..., run_in_background: true })
```

**NEVER do silently:**
- Long SSH pipelines
- Multi-step grep/find sweeps
- WebFetch chains
- Service restarts (always announce intent + result)
- Issue/PR creation that takes a curl roundtrip

### 6. Cleanup when user ends call

```bash
tmux kill-session -t "$SESS" 2>/dev/null
```

Killing the tmux session (SIGTERM) ends the call cleanly. You can also send an
explicit quit on stdin: `/q` (plain) or `{"topic":"quit"}` (json). Under the
default `--keep-alive`, only these explicit signals end the session — Ctrl-D /
stdin-EOF no longer quits.

## Live mode (Gemini Live, voicehook-v4 PR #79)

A second worker (`voice-ai-live`) runs rooms on a realtime model instead of STT + LLM + TTS.

```bash
curl -s https://voicehook.ai/api/live/status            # {"available":true|false}, never amounts
curl -s -X POST https://voicehook.ai/api/live-room \
  -H 'content-type: application/json' -d '{"identity":"host","ttl_seconds":3600}'
# -> {token,url,room,identity, invite_url, expires_in, agent:"voice-ai-live"}
```

- Errors: `402` monthly live budget used up (default 10 USD per UTC month, fail-closed),
  `404` live mode switched off, `503` not configured, `429` rate limit (shared with `/api/host-call`).
- You join with `invite_url` exactly as in step 3; every later dispatch in that room
  goes to the live worker.
- Differences for you: `operator.say` is NOT verbatim. It reaches the model as a marked
  user turn ("[Operator] Sag jetzt sinngemäß ..."), so the wording changes. The `operator`
  transcript line shows what was really said. `operator.persona` is added as a marked
  user turn too. If the budget runs out mid-call, voice-ai announces it and ends the call.

## Server-side filters (pipeline mode, nothing to do for you)

- **Speech filter**: only audio with detected speech goes to the STT; silence costs nothing.
- **Speaker filter**: background voices (TV, neighbour) are dropped before the LLM.
  Learning phase at the start: until one speaker has about 3 s of speech everything
  passes; it restarts with every new STT connection. Segments without speaker info pass.
  Expect the first seconds of a call to still contain background chatter.

## Why Hotswap-Persona is mandatory

Without step 4a, voice-ai answers from its own (empty) persona — that's why
in past sessions voice-ai said dumb things like "Ich kann das nicht" or
"Claude liest nicht mehr mit". The Hotswap-Persona is what makes voice-ai
**indistinguishable from the operator** for the user. Olli's design
goal: ONE conversation, ONE voice, with the operator swappable in the
background. Skipping 4a breaks that illusion.

## Failure handling

- `voicehook-agent: command not found` → install via uv tool install (step 2)
- `[error] livekit connect failed` → URL slug invalid OR token-mint /api/token broken
- `0 peers` → voice-ai not in room. Either user not joined yet, or voice-ai
  worker is down on Hetzner (rare). Ask user to refresh their browser tab.
- Voice-ai sounds generic / says "Bereit." → you skipped step 4a, push the
  Hotswap-Persona now.

## More

Full doc + topic schema: https://voicehook.ai/agent/SKILL.md
CLI source: https://github.com/voicehook-ai/voicehook-agent
