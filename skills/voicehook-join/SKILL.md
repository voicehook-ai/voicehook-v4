---
name: voicehook-join
description: Join an existing voicehook.ai voice-call as the senior brain behind a STRICT-RELAY voicebot. Use when the user shares a voicehook invite URL (https://voicehook.ai/r/<slug>) or says "join voicehook", "übernimm den voice-call", "/voicehook-join". The voicebot is NOT a clone of you and NEVER improvises — it speaks only (a) text you push via operator.say, (b) a curated knowledge persona you inject (NEVER secrets), and (c) server-side primitives (auto-greet, backchannel). Install: voicehook-agent CLI. Protocol: stdin/stdout JSON, no SDK. Works local + production (https://voicehook.ai).
---

# voicehook-join — drive a voicehook v4 call as senior brain

> **Stand 2026-09-30** (voicehook-v4 PR #70: LiveKit 1.8.3, `operator.revise`). Kanonisch: `voicehook-ai/voicehook-v4:skills/voicehook-join/SKILL.md`.

## ⚠️ ZUERST: CLI **und** Skill aktualisieren (Olivers Regel)

**Die CLI ist wichtiger als der Skill-Text.** Belegter Ausfall 2026-09-25: der
Skill wurde brav gegen v4 diffed, die CLI aber nicht angefasst. Sie war 0.2.0
vom 15.09., und Commit `f59a9c1c` vom **17.09.** hatte die Topics von
`senior.*` auf `operator.*` umbenannt. Die CLI publizierte weiter `senior.say`,
der Worker hörte auf `operator.say` — Oliver hörte 20 Minuten lang nur die
Ausweichfloskel "ich gebe das an den Operator weiter", also wörtlich den neuen
Topic-Namen. Nichts warf einen Fehler: kein `[warn]`, kein Stale-Drop, FIFO
sauber geleert. **Ein stiller Topic-Mismatch sieht exakt aus wie ein toter
Worker.**

Deshalb IMMER zuerst die Version vergleichen, erst danach den Skill-Text:

```bash
export PATH="$HOME/.local/bin:$PATH"
voicehook-agent --version                       # installiert
gh api repos/voicehook-ai/voicehook-agent/commits?per_page=5 \
  --jq '.[] | "\(.commit.author.date[0:16])  \(.sha[0:8])  \(.commit.message|split("\n")[0])"'
# Liegt ein Commit NACH dem Installationsdatum? Dann erst hochziehen:
pip3 install --user --break-system-packages --upgrade \
  git+https://github.com/voicehook-ai/voicehook-agent
# Gegenprobe, welches Topic die CLI wirklich sendet:
grep -n 'publish_data(payload' -A1 \
  ~/.local/lib/python3.*/site-packages/voicehook_agent/cli.py
```

Dann erst der Skill-Text:

```bash
gh api repos/voicehook-ai/voicehook-v4/contents/skills/voicehook-join/SKILL.md \
  --jq '.content' | base64 -d > /tmp/SKILL-v4.md
diff <(grep -E '^#{1,3} ' ~/.claude/skills/voicehook-join/SKILL.md) \
     <(grep -E '^#{1,3} ' /tmp/SKILL-v4.md)
```

Kanonisch: Skill-Text in **voicehook-v4** `skills/voicehook-join/`, CLI-Code in **voicehook-agent**
(dessen SKILL.md liegt ab PR #70 auch unter voicehook.ai/agent/SKILL.md; vorher nur SPA-Hülle).

## Design: strict mouthpiece

Die voice-ai generiert **nie** selbst. `RelayAgent.on_user_turn_completed` wirft
serverseitig `StopResponse`. Sie sagt ausschliesslich:

1. Text, den du per `operator.say` schickst (wortwörtliches TTS)
2. Inhalt der zuletzt injizierten `operator.persona` (begrenztes Wissen)
3. serverseitige Primitive: einmaliger Auto-Greet beim ersten Persona-Push,
   periodischer Backchannel bei langen User-Turns

Alles andere ist Halluzination und gehört als v4-Issue gemeldet.

## operator.* Topics

| topic | payload | Wirkung |
|---|---|---|
| `operator.say` | `{text, mode?}` | TTS. Default `revise`: nichts offen → sofort; sonst Stopp + `operator.revise` zurück, neue Aussage wartet (max 8s) auf dein `overwrite`. `mode:"overwrite"` = deine Zusammenfassung, ersetzt alles. `append` = anhängen (ab voicehook-v4 PR #70) |
| `operator.persona` | `{text}` | ersetzt agent.instructions live; erster Push löst Auto-Greet aus |
| `operator.interrupt` | `{}` | alles stoppen; Ungesprochenes kommt als `operator.revise` zurück |
| `operator.revise` | ← `{unspoken[], new, text}` | vom Agent an dich: was NICHT gesprochen wurde + Anweisung |
| `operator.inject` | `{text, role?}` | synthetischer chat-ctx-Eintrag, wird NICHT gesprochen |

## Serverseitig, kein Roundtrip nötig (v4, fixt v3#61)

- **Auto-Greet**: erster `operator.persona` löst einen einmaligen TTS-Gruss aus.
- **Heartbeat**: Topic `agent.heartbeat`, alle 30s,
  `{ts, room, probe:{stt,tts,llm}, healthy}`. NICHT auf `transcript` (v3#62).
  Bleiben die Ticks >60s aus, ist der Worker tot, auch wenn operator.say nicht wirft.
- **Backchannel**: spricht der User ≥6s ohne Antwort, feuert der Server
  "mhm"/"ja"/"ok" mit niedriger Priorität. Nicht selbst pushen.

## CLI-Stand auf DIESER Box (wichtig, weicht vom v4-Text ab)

Installiert via `pip3 install --user --break-system-packages git+https://github.com/voicehook-ai/voicehook-agent`
(kein `uv` vorhanden; `python3 -m venv` scheitert mangels ensurepip; System-pip
ist per PEP 668 gesperrt).

Stand 2026-09-25: **0.3.0**. Kennt weiterhin **kein `--auto` und kein
`--memory-dir`** — der v4-Zehnzeiler funktioniert also nach wie vor nicht.
Vorhandene Flags (aus `join --help`, nicht aus dem Gedächtnis):
`--name --model` (**Pflicht ab 0.4.0**, sonst Exit 2; Web-Chip: Ladekreis, dann "Name · Modell") `--identity --topic --username --prompt --greet/--no-greet --json
--persona --persona-file --keep-alive/--no-keep-alive --notify-url
--wake-only-user --wake-all --suppress-echo --say-ttl --strict-relay
--graph --graph-interval`

**Folge:** FIFO-Halter weiterhin von Hand. Sobald die CLI `--auto` kann, ersetzt
das den ganzen Block.

```bash
export PATH="$HOME/.local/bin:$PATH"
SESS=vh-call; NAME=Claude; MODEL=opus-5.5   # Selbstauskunft: eigener Name + echtes Modell
mkfifo /tmp/$SESS.in 2>/dev/null
( exec -a vh-holder sleep 100000 > /tmp/$SESS.in & )     # hält stdin offen
( setsid nohup voicehook-agent join "$INVITE_URL" --name "$NAME" --model "$MODEL" --json \
      --persona-file /tmp/$SESS.persona \
      < /tmp/$SESS.in > /tmp/$SESS.out 2>&1 & )
sleep 7; tail -n 8 /tmp/$SESS.out        # erwartet: connected + persona auto-pushed
```

Drei Betriebsfallen, alle am 2026-09-25 erlebt:
- **`setsid` benutzen.** Ohne das stirbt der Join mit der Bash-Tool-Shell.
- **Niemals `pkill -f "voicehook-agent join"`.** Das Muster steht auch in der
  eigenen Kommandozeile, die Shell killt sich selbst (Exit 144) und alles
  danach im selben Aufruf läuft nie. Stattdessen gezielt:
  `pgrep -af "voicehook-agent join" | grep -v "bin/bash -c"` und per PID killen.
- **Beim ersten Testlauf `--suppress-echo` WEGLASSEN.** Mit dem Flag fehlt das
  `{"role":"agent"}`-Echo im Stream, und dann ist "gesprochen" nicht von
  "verschluckt" zu unterscheiden. Erst wenn das Echo einmal sichtbar war,
  darf es wieder rein.

Steuern und mitlesen (kein `jq` auf dieser Box, deshalb python3):

```bash
say(){ python3 -c 'import json,sys;print(json.dumps({"topic":"operator.say","text":sys.argv[1]},ensure_ascii=False))' "$1" > /tmp/vh-call.in; }
tail -n 30 /tmp/vh-call.out
```

## Reihenfolge: ALLES vorbereiten, DANN verbinden

Eine dünne Persona beim Join heisst: die voice-ai weiss nichts, überbrückt zu dir,
und der User hört Stille. Also erst Persona rendern, dann connecten.

1. Persona-Text schreiben (`/tmp/$SESS.persona`), Secrets raus
2. FIFO-Halter starten
3. verbinden mit `--persona-file`
4. `_meta room-state` prüfen: ein Peer mit `kind:"agent"` muss da sein,
   sonst spricht dein operator.say ins Leere
5. Monitor scharfstellen (siehe unten)

## Monitor scharfstellen — die Hauptfehlerquelle

Ohne Listener endet dein Chat-Turn nach dem Gruss und niemand liest
`/tmp/$SESS.out` weiter. Jeder User-Turn danach läuft in Stille.

```bash
stat -c %s /tmp/$SESS.out > /tmp/$SESS.offset
# dann Monitor(persistent: true, command:
#   'OFF=$(cat /tmp/vh-call.offset); tail -c +$((OFF+1)) -f /tmp/vh-call.out |
#    grep --line-buffered -E "\"topic\": ?\"transcript\"|agent\\.(error|health)|disconnect|peer-(joined|left)"')
```

Zusätzlich `/loop` als Fallback-Heartbeat, falls der Monitor hängt.

## Sprechmodi

- **Antwort** (`operator.say`): erst wenn der User fertig ist. 1-3 Sätze, Rückfrage 5-10 Wörter.
- **Unterbrechen** (`operator.interrupt` + `operator.say`): Standard ist **nein**.
  Nur bei echter Notwendigkeit und höflich: wichtige Korrektur, die der User
  JETZT hören muss, ein "stopp" von ihm, oder sanftes Zurückholen, wenn er
  abdriftet. Formuliere es zuvorkommend, nie schroff.
- **Backchannel**: macht v4 selbst. Nicht pushen.

## Aussagen-Disziplin (Olli 30.09.: "redet 3 min nach")

- Pro Turn EIN `operator.say`, das zusammenfasst, was JETZT gilt. Keine Satzserien.
- Kommt `(operator.revise …) REVISE: Noch NICHT gesprochen: [1].. [neu]..` auf stdout: sofort alles zu EINER Aussage verschmelzen (nichts Wichtiges weglassen, Falsches/Überholtes streichen) und mit `mode:"overwrite"` senden. Nach 8s spricht der Agent sonst nur [neu].

## Olivers Lehren (gelten weiter, unabhängig von der Version)

- **Im Call bleiben ist Prime Directive.** Nicht nebenher wegarbeiten.
- **`operator.say` kurz halten: unter ~60 Zeichen, ~8s TTS.** Längere Pushes
  verwirft livekit-agents. Mehrteiler: ab Teil 2 `mode:"append"`.
- **Stille ist der Killer.** Ab 8s ansagen (Status-say mit `mode:"append"`), über 15s delegieren.
- **PII und Secrets: niemals in Persona, say oder Graph.** Alles reitet im
  Klartext über den LiveKit-Datenkanal (v3#27). Persönliche Inhalte des Users
  nur so weit hineingeben, wie der Call sie wirklich braucht.
- **Schweres delegieren**, damit der Call schnell bleibt: langes Bash,
  Recherche, Refactors gehen an einen Hintergrund-Agenten, vorher ansagen.
- **Live prüfen statt annehmen.** Ein zweiter `--json`-Listener zeigt, ob die
  voice-ai wirklich gesprochen hat.

## Fehlerbilder

- `command not found` → Installation siehe CLI-Stand oben.
- `0 peers` / kein `kind:"agent"` → voice-ai nicht im Raum, User soll den Tab neu
  laden oder den Call über `?go=1` starten.
- Stille nach dem Gruss → `agent.heartbeat` prüfen; bleiben die Ticks aus, ist
  der Worker abgestürzt.
- voice-ai erfindet etwas → `operator.interrupt`, Persona neu pushen, v4-Issue mit
  Transkript.

## Cleanup

```bash
pkill -f "voicehook-agent join" ; pkill -f vh-holder ; rm -f /tmp/vh-call.in
```

## Quellen

- v4-Repo: https://github.com/voicehook-ai/voicehook-v4
- CLI: https://github.com/voicehook-ai/voicehook-agent
- Plan: https://github.com/voicehook-ai/voicehook-v3/pull/64
