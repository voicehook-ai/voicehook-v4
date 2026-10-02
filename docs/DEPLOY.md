# Deploy (voicehook v4)

`deploy/deploy.sh` deploys the current checkout to one box. Prod: `root@voicehook.ai`.

## 1. SSH auth: orb key in ssh-agent, never on disk

The deploy key lives only in the orb keystore (`voicehook_v4_ssh_key`). Load it into a
short-lived ssh-agent, deploy, then kill the agent:

```bash
eval "$(ssh-agent -s)"
orbctl get voicehook_v4_ssh_key | ssh-add -
BOX_HOST=root@voicehook.ai make deploy-dry     # plan + rsync -n, changes nothing
BOX_HOST=root@voicehook.ai make deploy         # real deploy
ssh-agent -k
```

- `deploy.sh` uses the agent via `SSH_AUTH_SOCK` (checked with `ssh-add -l`).
- `SSH_KEY=<file>` (`-i`) is only a fallback, used when no agent key is loaded and
  the file exists. Do not create that file on agent boxes.
- `SSH_KNOWN_HOSTS=<file>` sets a separate known_hosts (host key: `accept-new`).
- If `ssh-add` fails with `error in libcrypto`, the orb entry is truncated. Fix the
  entry in orb (`orbctl write`, full multi-line key via STDIN). Do not work around it.

## 2. Modes

The box records the deployed commit in `/var/www/voicehook/.deployed-sha`. It is
written at the end of every successful deploy (`<sha>`, or `<sha>+dirty` if the
checkout had uncommitted changes).

| Mode | When | What happens |
|---|---|---|
| auto (default) | always | Diff `.deployed-sha..HEAD` plus uncommitted files. Nothing changed: exit "up to date". Only `web/**`: web-only. Anything else, or SHA missing/dirty/unknown: full. |
| `--web-only` | forced | rsync `web/` only. No pip, no agent restart, no caddy reload, no preflight. Non-web changes are listed as UNDEPLOYED. |
| `--full` | forced | preflight, stage release, venv, `.env` LIVEKIT_URL sync (#68), Caddyfile + reload, livekit container ensure, Blue/Green activation (below). |
| `--dry-run` | with any mode | Prints the plan and runs every rsync with `-n` (itemized). Only reads from the box (`.deployed-sha`, `.color`, unit state, LIVEKIT_URL). No preflight, no writes. |
| `--wait[=MIN]` | full only | Human in a call: nothing is copied, re-check every 30 s for up to MIN minutes (default 30), then abort. |
| `--force-restart` | full only | Deploy even if a human is in a room. Their call keeps running on the old color (drain). Lost: HTTPS-bridge sessions (see 5). Does NOT bypass preflight errors. |

Make targets: `deploy`, `deploy-dry`, `deploy-web`, `deploy-full`; extra flags via
`DEPLOY_ARGS=...`.

## 3. Order of the full path (02.10.2026, no mixed versions)

Incident 02.10.: code was rsynced into the running tree at 08:58, the restart waited
for a live call until 09:06. For 8 minutes every new job process (spawned from disk)
ran new code while the HTTP server ran old code, with wrong free-tier decisions.
Now:

1. **Preflight before anything is copied.** Human in a room: nothing is copied, the box is
   unchanged, abort (or `--wait`). Errors fail closed.
2. **Next color must be idle.** If `voicehook-agent@<next>` is still `deactivating` (the
   previous deploy is draining a call on it), abort before copying.
3. **Stage** `/opt/voicehook/releases/<sha12>-<utc-ts>/` (`apps/agent`, `infra/systemd`,
   `web`, pyproject, README). Venv: `/opt/voicehook/venvs/<sha256(pyproject)[:12]>`, built
   only when the dependency set changes (deps only, no `pip install -e`), linked as
   `<release>/venv`. Running processes are untouched.
4. `.env` LIVEKIT_URL sync, Caddyfile render + validate + reload (Caddy first, so the HTTP
   restart in 5 is already covered by `lb_try_duration`), livekit container ensure (only
   starts it if missing; the new worker must be able to register).
5. **Activate** (`infra/systemd/voicehook-release activate`, one step on the box):
   symlink swap `current -> release` (`mv -T`, atomic), `systemctl start
   voicehook-agent@<new> voicehook-agent-live@<new>` (Type=notify: returns only after the
   new worker is registered with LiveKit; fails -> symlink back, old color keeps serving,
   exit 1), restart `voicehook-http`, sync `web/` into the docroot, write `.color`, then
   `systemctl stop --no-block` the old color = SIGTERM = drain. Keeps the newest 5
   releases and their venvs.

`infra/systemd/voicehook-run` resolves `current` once at process start (cwd, `sys.path[0]`
and venv are real paths). A running worker and every job process it spawns later keep
importing their own release, a symlink swap never changes a running process.

## 4. Services and drain

| Unit | Runs | Stop |
|---|---|---|
| `voicehook-http` | `python -m agent http` (FastAPI :7400 only) | SIGTERM, uvicorn finishes open requests (5 s). Restart ~1-2 s, Caddy retries the upstream for up to 10 s (`lb_try_duration`), clients see no 502 |
| `voicehook-agent@blue\|green` | `python -m agent start` (worker `voice-ai`, no HTTP) | drain up to 60 min (`VH_DRAIN_TIMEOUT=3600` = CallGuard cap), `TimeoutStopSec=3660` |
| `voicehook-agent-live@blue\|green` | same, `voice-ai-live` | drain up to 20 min (`VH_DRAIN_TIMEOUT=1200` = `VH_MAX_CALL_SECONDS`), `TimeoutStopSec=1260` |

Why `start` instead of `dev` (livekit-agents 1.8.3, `cli/cli.py`): on SIGTERM/SIGINT the
worker runs `server.drain()` only `if not devmode`. Drain = worker reports `WS_FULL`
(LiveKit sends no new jobs), waits until all running jobs end or `drain_timeout`, then
exits 0. In `dev` the worker shuts down at once and running calls die. `dev` also meant
`load_threshold=inf` and `num_idle_processes=0`. Production options (`agent/procctl.py`,
env-overridable): `VH_WORKER_LOAD_THRESHOLD=0.7`, `VH_WORKER_IDLE_PROCS=1` (one warm job
process per worker, ~300 MB; during a deploy up to 4 workers run on the cx33 with 8 GB),
`VH_DRAIN_TIMEOUT`, `VH_WORKER_HTTP_PORT=0` (worker health port; the prod default 8081
would collide between colors).

`KillMode=mixed`: systemd sends SIGTERM only to the worker main process (job processes
would otherwise get it too and end the call), SIGKILL to leftovers only after
`TimeoutStopSec`. A second SIGTERM during the drain force-exits (livekit behavior), so do
not `systemctl kill` a draining worker unless you mean it.

Check a drain: `systemctl is-active voicehook-agent@blue` (`deactivating` = draining),
`tail -f /var/log/voicehook-agent-blue.log` ("draining worker"). Abort a drain on purpose:
`systemctl kill -s SIGKILL voicehook-agent@blue`.

**In-memory state (HTTP restart):** HTTPS-bridge sessions (`bridge.py`, `/api/bridge/*`),
their SSE streams and the in-memory rate-limit counters live in the HTTP process and are
lost on every deploy, as before. A bridged agent gets errors on its next poll and must
rejoin; the voice call itself (browser + worker) keeps running on the old color. Everything
the worker needs (wallet, free tier, budget) is in `/opt/voicehook/state/*.sqlite`, shared
by HTTP and both colors.

**Mixed versions that remain (by design):** during a drain the old color serves its
running calls with old code while HTTP and new calls run new code. They share the sqlite
files; a schema change must stay backward compatible for one drain window (max 60 min).

**Deploy duration:** stage + Caddy ~20-40 s (+1-3 min when pyproject changed and a new venv
is built), activation ~5-10 s (new worker registers in ~3 s locally, plus HTTP restart).
The command returns then; the old color drains in the background for as long as its
longest running call (max 60 min Normal, 20 min Live). A further full deploy during that
window is refused (step 2); web-only deploys are not affected.

**First deploy after this change (migration):** legacy `voicehook-agent.service` /
`voicehook-agent-live.service` (dev mode, HTTP + worker in one process, cannot drain) are
stopped and removed after the new color is registered and before `voicehook-http` starts
(port 7400). That cut is only safe without a live call, so the preflight gate applies;
do not use `--force-restart` for this first deploy. `/opt/voicehook/apps` and
`/opt/voicehook/.venv` of the old layout stay on disk unused and can be removed by hand
later.

## 5. Restart preflight (calls cost money)

Before the full path copies anything to the box:

1. If no `voicehook-agent`, `voicehook-agent@blue` or `@green` is active, there is
   nothing to interrupt: continue.
2. Otherwise box-local LiveKit twirp `ListRooms` + `ListParticipants` on
   `127.0.0.1:7880`. The JWT is signed on the box from `/opt/voicehook/.env`; keys
   never leave the box.
3. Any participant that is not agent-kind and not `voice-ai*` counts as human
   (the senior CLI counts as human, on purpose). Human present: nothing copied, abort
   (or `--wait`), unless `--force-restart`.
4. Any error (ssh, timeout, missing keys, HTTP): abort (fail closed).

Hard caps: `timeout 120` around the ssh call, `timeout 90` on python, an 80s
internal deadline and 10s per request.

## 6. rsync and box-only files

- `web/` is staged into the release and copied into `/var/www/voicehook` during activation
  (web-only mode: directly): **no `--delete`**. Box-only files in the docroot
  (`.deployed-sha`, verification files) are never removed. A file deleted from
  `web/` stays on the box until removed by hand.
- `apps/agent/` goes into a fresh release dir; `.env*`, `.venv`, `__pycache__`, `.git`,
  `*.egg-info` are excluded. `/opt/voicehook/.env` and `/opt/voicehook/state/` live outside
  the releases.
- Single files (pyproject, README, units, Caddyfile, compose) are plain copies.

Note: Caddy serves the docroot, so `/.deployed-sha` is publicly readable (commit SHA only).

## 7. Tests

- `apps/agent/tests/test_deploy_bluegreen.py`: deploy.sh against ssh/rsync stubs
  (preflight before any copy, draining color blocks, order stage -> activate) and the box
  script on a temp filesystem (swap, start-before-drain, legacy migration, rollback, prune,
  launcher pins the real release path).
- `tests/e2e/drain_bluegreen.py`: real local `livekit-server --dev`, real workers via
  `voicehook-run`, real jobs. `LIVEKIT_SERVER=<bin> .venv/bin/python tests/e2e/drain_bluegreen.py`
  (PASS) and `... dev` as negative control (FAIL: the call dies).

## Gemini-Live-Testraum (Admin)

Nur für interne Tests. Der Schlüssel `VOICEHOOK_LIVE_KEY` (orb) steht in `/opt/voicehook/.env`
und reist ausschließlich im Header, nie in einer URL:

```bash
curl -s -X POST https://voicehook.ai/api/admin/live-room \
  -H "Authorization: Bearer $VOICEHOOK_LIVE_KEY" -H 'content-type: application/json' -d '{}'
# -> {"room": "...", "url": "https://voicehook.ai/r/<slug>?invite=<hmac>", "expires_in": 3600}
```

Der zurückgegebene Link ist eine normale raumgebundene Einladung (läuft ab). In diesem Raum
arbeitet der Worker `voice-ai-live` (Gemini 3.8 Live, 20-Min-Deckel), kein `voice-ai`.

**Monatsbudget (Sperre):** Der Live-Worker bucht die Kosten jeder Antwort (Token-Zahlen × Preise
aus `apps/agent/live.py`) in `/opt/voicehook/state/live-budget.json` (ein Zähler je UTC-Monat).
Ist `VOICEHOOK_LIVE_BUDGET_USD_MONTH` (Default 10) erreicht, antworten `/api/admin/live-room`
und `/api/live-room` mit 402, `/api/live/status` meldet `available: false`, ein laufender Live-Call wird mit Ansage beendet und neue Live-Jobs starten nicht.
Unlesbares Ledger oder ungültiger Wert = gesperrt (fail-closed). Stand prüfen:
`ssh root@voicehook.ai cat /opt/voicehook/state/live-budget.json`.

## Live-Modus öffentlich (Demo)

Seit 30.09. darf jeder ohne Schlüssel einen Live-Raum starten (später Login + Guthaben).
Schalter `VOICEHOOK_LIVE_PUBLIC` in `/opt/voicehook/.env`: Default an, `0`/`false`/`off`/`no`
schaltet ab (dann 404, Status `available: false`). Der Admin-Endpunkt oben ist davon unberührt.

```bash
curl -s https://voicehook.ai/api/live/status
# -> {"available": true}   (false: Budget weg, Schalter aus oder Live nicht konfiguriert; nie Beträge)

curl -s -X POST https://voicehook.ai/api/live-room \
  -H 'content-type: application/json' -d '{"identity": "host-abc123"}'
# -> {"token": "<LK-JWT>", "url": "wss://rtc.voicehook.ai", "room": "<slug>", "identity": "host-abc123",
#     "invite_url": "https://voicehook.ai/r/<slug>?invite=<hmac>", "expires_in": 3600, "agent": "voice-ai-live"}
```

Antwort = Format von `/api/host-call` plus `invite_url`/`expires_in`/`agent`. Schutz: dasselbe
IP-Ratenlimit wie `/api/host-call` (gemeinsamer Zähler, 5 Starts je 10 Min und IP, sonst 429) und
die Monatsbudget-Sperre (402). "Konfiguriert" heißt: LiveKit-Zugang und `GOOGLE_API_KEY` oder
`GOOGLE_APPLICATION_CREDENTIALS` gesetzt (sonst 503); ob der Dienst `voicehook-agent-live` läuft,
sieht der HTTP-Server nicht (`systemctl is-active voicehook-agent-live`).

## Guthaben aufladen (Stripe, Default aus)

Seite `/aufladen` (Caddy schreibt auf `web/aufladen.html` um). Ledger:
`/opt/voicehook/state/billing.sqlite` (HTTP-Server und Worker teilen die Datei). Ohne Stripe-Keys
meldet `/api/billing/config` `checkout_available: false`, die Seite zeigt "bald verfügbar".

Konto = Wallet (Token im Browser + Wiederherstellungs-Link), nie die E-Mail: ein Checkout ohne
gültiges Wallet-Token legt immer ein neues Konto an. Zahlen tut nur, wer den Raum über
`/api/host-call`, `/api/invite-room` oder `/api/live-room` mit `X-Wallet-Token` anlegt; `/api/token` (Beitritt per
Einladung) bindet nie ein Wallet. Die Bindung Raum -> Wallet gilt nur bis zum Call-Ende (der Worker
schließt sie) und höchstens die Token-TTL; danach antworten Joins in diesen Raum mit 410 und der
Worker lehnt ihn ab. Ein Fehlbetrag aus Erstattung/Rückbuchung wird bei der nächsten Gutschrift auf
dasselbe Konto zuerst verrechnet. Der Wiederherstellungs-Link gilt genau einmal und wird dabei erneuert.

| Env in `/opt/voicehook/.env` | Default | Bedeutung |
|---|---|---|
| `STRIPE_SECRET_KEY` | leer | Stripe-Secret-Key (Checkout-Session anlegen) |
| `STRIPE_WEBHOOK_SECRET` | leer | Signing-Secret des Webhook-Endpunkts |
| `STRIPE_ACCOUNT_TAX_ID` | leer | Eigene USt-ID als Stripe-Tax-ID (`txi_...`, Live: `txi_1UM4IoDRrOhbsRSIbgJoTHim` = DE310620765, Sandbox hat eine eigene). Nur bei Aufladung mit Rechnung (`invoice=true`) geht sie als `invoice_creation[invoice_data][account_tax_ids][]` mit und steht so garantiert auf der Rechnung, unabhängig von der Dashboard-Einstellung ([Stripe-Doku](https://docs.stripe.com/invoicing/taxes/account-tax-ids)); leer = Parameter entfällt (Dashboard-Voreinstellung gilt). Kein Secret |
| `VOICEHOOK_REQUIRE_CREDITS_NORMAL` | `0` | `1` = `/api/host-call` / `/api/invite-room` ohne Wallet mit Saldo > 0 -> 402; greift nur, wenn das Gratis-Kontingent Normal aus ist (`0`) |
| `VOICEHOOK_REQUIRE_CREDITS_LIVE` | `0` | `1` = `/api/live-room` ohne Wallet mit Saldo > 0 -> 402; greift nur, wenn das Gratis-Kontingent Live aus ist |
| `RESEND_SENDING_API_KEY` | leer | Resend-Key mit Recht "Sending access" (nur Senden) für die Login-Mails; leer = Login aus (`/api/login` -> 503 `login_unavailable`). Nie den Full-Access-Key auf den Server |
| `RESEND_API_KEY` | leer | Fallback, wenn `RESEND_SENDING_API_KEY` leer ist (ältere `.env`); ebenfalls nur ein Sending-Key |
| `MAIL_FROM` | `voicehook <login@voicehook.ai>` | Absender der Login-Mail (Domain muss in Resend verifiziert sein) |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | leer | OAuth-Client "Mit Google fortfahren" auf `/login`; fehlt einer der beiden Werte: Knopf ausgegraut, `/api/auth/google/*` -> 503 |
| `GITHUB_OAUTH_CLIENT_ID` / `GITHUB_OAUTH_CLIENT_SECRET` | leer | dasselbe für "Mit GitHub fortfahren" (`/api/auth/github/*`) |
| `VOICEHOOK_PUBLIC_URL` | `https://voicehook.ai` | Basis für Stripe-Rücksprung, Login-Link und OAuth-Callback (`<URL>/api/auth/<anbieter>/callback`) |
| `VH_LOW_BALANCE_WARN_SECONDS` | `300` | Worker: ab dieser Restzeit (Gratis + Guthaben) einmal pro Call Hinweis + Ansage |
| `VOICEHOOK_PRICE_FACTOR_NORMAL` / `_LIVE` | `3` / `1.5` | Abbuchung = Anbieterkosten x Faktor |
| `VOICEHOOK_VAT_RATE` | `0.19` | MwSt obendrauf |
| `VOICEHOOK_USD_EUR` | `0.8807` | Kurs USD -> EUR (EZB 30.09.2026) |
| `VOICEHOOK_TOPUP_AMOUNTS_EUR` | `5,10,20,50` | Vorschlagsbeträge (nur Werte im erlaubten Bereich) |
| `VOICEHOOK_TOPUP_MIN_EUR` / `_MAX_EUR` | `5` / `200` | Spanne des Drehreglers und der Checkout-Prüfung (Minimum nie unter 5; `/api/checkout` < Minimum -> 400) |
| `VH_FREE_EUR_PER_DAY` | `0.30` | Gratis-Verbrauch in Euro (Kundenpreis inkl. Faktor und MwSt, 0,30 € ≈ 10 min Normal oder ≈ 2 min Live) pro UTC-Tag und Identität, Normal und Live gemeinsam; `0` = aus; kaputter Wert (kein Zahlwert, negativ, inf/nan) = 0 € Gratis bei weiter aktiver Prüfung (ohne Wallet 402), nie unbegrenzt |
| `VH_FREE_EXEMPT_ACCOUNTS` | leer | Angemeldete Konten ohne Gratis-Limit (Owner), kommagetrennt `github:<id>` / `google:<sub>` = stabile Anbieter-ID aus `oauth_identities.subject` (siehe unten); gilt nur mit Login (X-Wallet-Token, bestätigte Mail), nie per IP/Browser; leer = keine Ausnahme |
| `VH_FREE_EXEMPT_KEYS` | leer | ALT, nur noch Abwärtskompatibilität (Nachfolger `VH_FREE_EXEMPT_ACCOUNTS`): Merkmale ohne Gratis-Limit, kommagetrennt, nur gehashte Keys im Format `anon:<sha256>` / `ip:<sha256>` (siehe unten); leer = keine Ausnahme, kaputte Einträge werden ignoriert |
| `VH_FREE_POT_EUR_MONTH` | `60` | INTERN, nirgends nach außen nennen: globaler Gratis-Deckel in ECHTEN Kosten (cost_usd × `VOICEHOOK_USD_EUR`) pro UTC-Monat für alle Gratis-Nutzer zusammen, siehe unten; kaputter/negativer Wert = 0 = Gratis gesperrt |
| `VH_FREE_TICK_SECONDS` | `5` | Prüftakt der Restzeit-Warnung im Worker (bucht nichts) |
| `VH_REQUIRE_OPERATOR_INVITE` | `0` | Operator-Join `GET /api/token?invite=1` und `/api/bridge/join` ohne HMAC-Einladung (`op_invite` bzw. `?invite=` in der URL): `0` = Übergangsfrist, erlaubt, aber laut geloggt (`legacy operator join without invite`); `1` = 403. Ungültige Signatur ist immer 403. Umschalten auf `1`, sobald die neue voicehook-agent CLI 1 bis 2 Tage draußen ist und das Log keine Legacy-Joins mehr zeigt |

Entfallen: `VOICEHOOK_FREE_MIN_PER_DAY_NORMAL` / `_LIVE` (Gratis-Minuten) werden ignoriert und
können aus der Env-Datei gelöscht werden. Die alte Sekunden-Tabelle `free_usage` in
`freetier.sqlite` bleibt liegen, wird nicht mehr gelesen und nach 7 Tagen leer geräumt; die neue
Tabelle `free_usage_eur` legt der Dienst beim Start selbst an (idempotent).
| `VOICEHOOK_APPROX_EUR_PER_HOUR_NORMAL` / `_LIVE` | `1.70` / `8.80` | nur Anzeige: ungefährer Kundenpreis pro Stunde (inkl. Marge und MwSt) am Normal/Live-Schalter, via `/api/billing/config` `approx_eur_per_hour` |

Stripe-Dashboard: Webhook-Endpunkt `https://voicehook.ai/api/stripe/webhook` mit den Events
`checkout.session.completed`, `checkout.session.async_payment_succeeded`, `charge.refunded` und
`charge.dispute.created` anlegen (die beiden letzten ziehen erstattete bzw. zurückgebuchte Beträge
wieder ab, Saldo nie unter 0, Fehlbetrag in `reversals.shortfall_ueur`), dessen
Signing-Secret als `STRIPE_WEBHOOK_SECRET` setzen, danach Agent neu starten (Env wird beim Start gelesen).

### Gratis-Kontingent (ohne Login)

Reihenfolge im Call: erst der Gratis-Topf, dann Guthaben. Hat der Ersteller heute noch Gratis-Rest
und zusätzlich ein gedecktes Wallet, steht der Raum in beiden Tabellen; der Worker bucht erst aus
dem Gratis-Topf und danach vom Wallet, ohne den Call zu beenden. Das Kostenereignis, das die Grenze
überschreitet, leert den Topf bis 0, der Überhang geht ans Wallet. Der Call endet erst, wenn beides
leer ist. Reichen Gratis-Rest + Guthaben ((Gratis-Rest + Guthaben) / Verbrauch der letzten 3 Minuten)
noch höchstens 5 Minuten, schickt der Worker einmal pro Call `operator.notice`
`{kind:"low_balance", minutes_left, ...}` an alle im Raum und sagt "Hey, Achtung, das Guthaben ist in wenigen Minuten leer." `GET /api/me` liefert der Oberfläche Gratis-Rest und Guthaben.

Gratis gibt es `VH_FREE_EUR_PER_DAY` (0,30 €) Verbrauch pro UTC-Tag, Normal und Live gemeinsam.
Gebucht wird nur aus echten Kostenereignissen des Workers (Spracherkennung, Sprachmodell,
Sprachausgabe bzw. Live-Turns), als Kundenpreis: Anbieterkosten x Faktor (Normal 3, Live 1,5) plus
MwSt, dieselbe Rechnung wie beim Guthaben. Stille ohne Kosten zählt nichts herunter (früher zählte
die Wanduhr, sobald ein Mensch im Raum war). Gezählt wird je Merkmal des Raum-Erstellers: anonyme
ID aus dem Header `X-Anon-Id` (8 bis 128 Zeichen `A-Za-z0-9_-`, sonst ignoriert) und Client-IP
(letztes Element von `X-Forwarded-For`, das Caddy selbst setzt; IPv6 je /64-Netz). Erreicht EINES der Merkmale das Limit, antworten
`/api/host-call`, `/api/invite-room` bzw. `/api/live-room` ohne gedecktes Wallet mit 402 `{error: free_limit, topup_url: /aufladen, free_eur_per_day}`; ein
laufender Gratis-Call endet mit der Ansage "Dein Gratis-Verbrauch für heute ist um. Lade Guthaben
auf." `GET /api/me` und `GET /api/free/remaining` liefern `free: {eur_left, eur_per_day}` (Euro, 2
Nachkommastellen, abgerundet). Gespeichert werden nur SHA-256-Hashes der Merkmale in `/opt/voicehook/state/freetier.sqlite`
(älter als 7 Tage wird gelöscht). Mit gedecktem Wallet endet der Call am Gratis-Limit nicht, das
Wallet zahlt weiter. Die Obergrenze pro Live-Call
(`VH_MAX_CALL_SECONDS=1200` im Live-Dienst) gilt zusätzlich. Der Worker ist in beiden Modi
fail-closed, solange das Gratis-Kontingent an ist: ein Raum ohne Wallet und ohne
Gratis-Eintrag wird abgelehnt (`free_room_unknown`). Ausnahmen stehen als exempt drin: Admin-Live-Räume
und Normal-Räume, die jemand mit einer gültigen HMAC-Einladung betritt, die der Server nicht selbst
ausgestellt hat (call-starten mintet sie mit `INVITE_SECRET`). `GET /api/token?invite=1`
(Operator-Join, voicehook-agent CLI) verlangt die HMAC-Einladung aus der URL als `op_invite`
(siehe `VH_REQUIRE_OPERATOR_INVITE`), gibt dann ein Token, dispatcht voice-ai aber nur in Räume
mit bekanntem Zahler; ein selbst ausgedachter neuer Slug bekommt keinen Gratis-Agent mehr.
Das Live-Monatsbudget zählt alles, was nicht das Guthaben zahlt: Gratis/Demo-Räume ganz, Wallet-Räume
ihren Gratis-Teil. Beendet wird am Budget nur ein Raum ohne Wallet; ist das Budget schon erschöpft,
zahlt bei Wallet-Räumen das Guthaben von Anfang an (kein Gratis-Teil).

**Globaler Gratis-Deckel (`VH_FREE_POT_EUR_MONTH`, intern).** Fester Marketing-Topf pro
UTC-Monat in echten Kosten (nicht Kundenpreis). Tagesbudget =
(Monatstopf − im Monat vor heute verbraucht) / verbleibende Tage inkl. heute, d. h. Start 60/30 =
2 €/Tag, nicht Genutztes verteilt sich auf die Resttage. Ist heute das Tagesbudget verbraucht, ist
Gratis für ALLE leer bis zum nächsten UTC-Tag: neue Räume 402 `free_limit` (dieselbe Meldung wie
beim persönlichen Limit), `/api/me` zeigt `eur_left: 0`, laufende Gratis-Calls wie beim
persönlichen Limit (Wallet zahlt weiter, sonst Ansage + Ende). Gebucht wird im selben
Kostenereignis wie der persönliche Topf in die Tabelle `free_pot` (`freetier.sqlite`, legt der
Dienst selbst an), Lesefehler = leer (fail-closed). Owner (`VH_FREE_EXEMPT_KEYS`) und Admin-Räume
buchen nicht und werden nicht gesperrt. Die Live-Monatssperre gilt zusätzlich. Stand nur für
den Admin:

```bash
curl -s -H "Authorization: Bearer $VOICEHOOK_LIVE_KEY" https://voicehook.ai/api/admin/free-pot
# {month, day, days_left, month_budget_eur, month_used_eur, budget_today_eur, today_used_eur, left_today_eur}
```

**Gratis-Ausnahme am Konto (`VH_FREE_EXEMPT_ACCOUNTS`, bevorzugt).** Eintrag je Konto
`github:<id>` oder `google:<sub>`: die STABILE Anbieter-ID (GitHub: numerische User-ID, nicht
der umbenennbare Login; Google: OpenID `sub`, nicht die Mail). Ausnahme gilt, wenn der Browser
angemeldet ist (X-Wallet-Token eines Kontos mit bestätigter Mail) und diese Mail die ist, mit
der der gelistete Anbieter-Zugang zuletzt kam. Ohne Login nie, egal welche IP/Anon-ID. Beim
Raumanlegen merkt sich der Server das Konto (`free_rooms.account`), der Worker prüft mit
derselben Funktion (`freetier.free_state`). Wirkung wie unten (kein 402, nichts gebucht,
`exempt: true`). ID read-only auf der Box nachschlagen (gibt nur Anbieter und ID aus):

```sh
sqlite3 -readonly /opt/voicehook/state/billing.sqlite \
  "SELECT provider, subject FROM oauth_identities WHERE email = lower('<mail>')"
# -> VH_FREE_EXEMPT_ACCOUNTS=github:<subject>  (bzw. google:<subject>)
```

Danach wie unten `voicehook-http` und die Worker-Farbe neu starten (Env wird beim Start gelesen).

**Gratis-Ausnahme für den Owner per Hash (`VH_FREE_EXEMPT_KEYS`, alt).** Trifft eines der Merkmale eines
Anfragenden bzw. Raums die Liste, ist Gratis unbegrenzt: kein 402 `free_limit`, kein Call-Ende
wegen `free_limit`, in `free_usage_eur` wird nichts gebucht. `/api/me` und `/api/free/remaining`
liefern dann `free: {eur_left: eur_per_day, eur_per_day, exempt: true}`, die Seite zeigt
"Gratis: unbegrenzt (Test)". Wallet und Live-Monatsbudget (`budget.py`) bleiben unverändert, die
Live-Sperre gilt auch für den Owner. In der Env stehen nur Hashes, keine Klartext-IP. Keys lokal
auf der Box erzeugen (IP und Anon-ID gehen nicht in Logs; Anon-ID = `localStorage` des Browsers,
IPv6 zählt je /64):

```sh
cd /opt/voicehook/current/apps && ../venv/bin/python -m agent.freetier keys --ip <ip> --anon <anon-id>
# Ausgabe: je Merkmal ein Key, zuletzt die fertige Zeile VH_FREE_EXEMPT_KEYS=anon:...,ip:...
```

Zeile in `/opt/voicehook/.env` eintragen, danach `voicehook-http` und die aktive Worker-Farbe (`cat /opt/voicehook/.color`)
neu starten, am einfachsten per `deploy.sh --full` (drainet laufende Calls) (die Env wird beim Start gelesen). Ein Key reicht (z. B. nur die Anon-ID,
wenn die IP wechselt).

### Login per Magic-Link

`POST /api/login {email}` schickt über Resend einen Link `https://voicehook.ai/aufladen#login=<token>`
(einmal, 15 Minuten; Ratenlimit 5 je IP in 10 min (IPv6 je /64), 3 je Adresse und IP in 15 min,
10 je Adresse in 60 min; fehlgeschlagener Mailversand zählt nicht). Die Antwort enthält eine
`login_nonce`, die nur dieser Browser kennt. Die Seite ruft mit dem Link
`GET /api/login/verify?token=...&nonce=...` auf und bekommt ein Wallet-Token für das Konto mit dieser
jetzt bestätigten Adresse. Ohne passende Nonce (Link in einem anderen Browser geöffnet) wird der
Link nicht verbraucht: 409 `confirm_required` mit maskierter Adresse; erst nach "Anmelden als ...?"
und erneutem Aufruf mit `confirm=1` wird eingeloggt, ein Wallet dieses Browsers aber nie verknüpft
(Schutz gegen Rest-Login-CSRF: ein vom Angreifer an seine Adresse angeforderter Link schaltet einen
fremden Browser nicht mehr still in sein Konto). Jeder Login widerruft die älteren Recovery-Codes des
Kontos. Das Wallet des Browsers (`X-Wallet-Token`) wird nur verknüpft, wenn es exakt das
Token ist, mit dem der Link angefordert wurde (Hash in `login_links.requester_hash`, Schutz gegen
Login-CSRF); sonst bleibt es unberührt und die Antwort sagt `wallet_linked: false`. Die Stripe-Mail ist nur eine unbestätigte Kontakt-Mail und verknüpft allein nie.
Wird eine Adresse zum ersten Mal bestätigt, verlieren alle anderen Tokens der so übernommenen Konten
ihre Gültigkeit (wer bei Stripe eine fremde Adresse eintippt, behält keinen Zugriff); weitere
unbestätigte Konten mit derselben Kontakt-Mail werden samt Saldo zusammengeführt. Einrichtung:
Resend-Konto, Domain `voicehook.ai` dort verifizieren (Region eu-west-1; DNS bei Hostinger:
TXT `resend._domainkey` (DKIM), MX `send` -> `10 feedback-smtp.eu-west-1.amazonses.com`, TXT `send`
`v=spf1 include:amazonses.com ~all`, CNAME `rsend` -> `send.forge.rmta.net`), Sending-Key als
`RESEND_SENDING_API_KEY` und optional `MAIL_FROM` in `/opt/voicehook/.env`, Agent neu starten.

**Übergang beim Deploy (24 h):** Backend und Web (`aufladen.html`) gemeinsam ausrollen. Bis alle
Browser die neue Seite haben (Cache, offene Tabs; spätestens nach 24 h), schickt eine alte Seite keine
Nonce und kennt kein 409: ein Login-Link endet dort mit einem Fehler statt einer Rückfrage, der Nutzer
lädt die Seite neu und klickt den Link erneut (der Link bleibt bei 409 gültig, 15 min). Links, die vor
dem Deploy angefordert wurden, haben keine Nonce (Spalte `login_links.nonce_hash` wird beim Start
ergänzt, Altbestand NULL) und gehen nur über die Rückfrage (`confirm=1`). In den ersten 24 h im Log
auf gehäufte `GET /api/login/verify` mit 409 achten; danach ist der Übergang vorbei, es ist nichts
zurückzubauen.

### Login-Seite `/login` (Google, GitHub, E-Mail-Link)

Seite `/login` (Caddy schreibt auf `web/login.html` um). Alle "Anmelden"-Links auf `voice.html` und
`/aufladen` führen auf `/login?next=<relativer Pfad der Ausgangsseite>`; nach dem Login geht es dorthin
zurück. `next` gilt nur als relativer Pfad (beginnt mit `/`, nicht `//` oder `/\`, keine
Steuerzeichen, nicht `/login`), sonst `/aufladen` (Schutz gegen Open-Redirect, Server und Seite prüfen
beide). `GET /api/auth/providers` -> `{google, github, email}` sagt der Seite, welche Wege eingerichtet
sind; der Rest steht ausgegraut mit "bald verfügbar" da.

Ablauf Google/GitHub: `POST /api/auth/<anbieter>/start {next}` (mit `X-Wallet-Token`, falls vorhanden;
Ratenlimit 5 je IP in 10 min, eigener Zähler) -> `{authorize_url, login_nonce}`. Der Server legt einen
einmaligen `state` (10 min, nur als Hash in `oauth_states`) mit PKCE-Verifier (S256), `next`, Nonce- und
Wallet-Hash an. Der Anbieter ruft `GET /api/auth/<anbieter>/callback?code&state` auf: `state` wird
verbraucht, der Code getauscht, dann zählt nur eine bestätigte Adresse (Google: `email_verified=true`
aus userinfo; GitHub: `/user/emails` mit `primary` und `verified`). Daraus entsteht ein normaler
Login-Link (wie aus der Mail), gebunden an die Nonce und das Wallet aus `start`, und der Browser geht
per 302 auf `/login?next=...#login=<token>`. Ab da gilt unverändert der Magic-Link-Vertrag oben
(`/api/login/verify` mit Nonce 200, sonst 409 `confirm_required` mit Rückfrage). Konto-Identität bleibt
die bestätigte E-Mail: gleiche Adresse über Mail, Google oder GitHub = dasselbe Konto. Die Anbieter-ID
(Google `sub`, GitHub `id`) wird nur in `oauth_identities` vermerkt. Fehler enden auf
`/login#error=<denied|state|provider|email_unverified>`.

#### OAuth-Apps anlegen

**Google** (Google Cloud Console, APIs & Services):
1. OAuth-Zustimmungsbildschirm: Typ "Extern", App-Name voicehook, Support-Mail, Domain `voicehook.ai`;
   Scopes `openid` und `email` (keine weiteren). Danach veröffentlichen ("In Produktion").
2. Anmeldedaten -> OAuth-Client-ID -> Anwendungstyp "Webanwendung".
   Autorisierte Weiterleitungs-URI: `https://voicehook.ai/api/auth/google/callback`
   (exakt so, ohne Schrägstrich am Ende; für die Testbox zusätzlich `<VOICEHOOK_PUBLIC_URL>/api/auth/google/callback`).
3. Client-ID -> `GOOGLE_OAUTH_CLIENT_ID`, Clientschlüssel -> `GOOGLE_OAUTH_CLIENT_SECRET`.

**GitHub** (Settings -> Developer settings -> OAuth Apps -> New OAuth App, bei der Organisation):
1. Homepage URL `https://voicehook.ai`,
   Authorization callback URL `https://voicehook.ai/api/auth/github/callback`.
2. Scope fragt der Server selbst an: `user:email` (nur Adressen lesen). PKCE (S256) wird mitgeschickt.
3. Client ID -> `GITHUB_OAUTH_CLIENT_ID`, "Generate a new client secret" -> `GITHUB_OAUTH_CLIENT_SECRET`.

Beide Paare in `/opt/voicehook/.env` (Secrets aus dem orb, nie ins Repo), Agent neu starten (Env wird
beim Start gelesen). Prüfen: `curl -s https://voicehook.ai/api/auth/providers` zeigt `true` für den Anbieter.
Hinweis Logs: der Callback-Query (`code`, `state`) steht wie jede URL im Caddy- und uvicorn-Access-Log;
der Code ist einmalig und ohne PKCE-Verifier wertlos, der Server selbst loggt weder Code noch Token.
