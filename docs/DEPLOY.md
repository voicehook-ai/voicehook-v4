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
| `--full` | forced | agent code, pip, `.env` LIVEKIT_URL sync (#68, prints old -> new), systemd restart, Caddyfile + reload, livekit container ensure. |
| `--dry-run` | with any mode | Prints the plan and runs every rsync with `-n` (itemized). Only reads from the box (`.deployed-sha`, current LIVEKIT_URL). No preflight, no writes. |
| `--force-restart` | full only | Restart even if a human is in a room. Does NOT bypass preflight errors. |

Make targets: `deploy`, `deploy-dry`, `deploy-web`, `deploy-full`; extra flags via
`DEPLOY_ARGS=...`.

## 3. Restart preflight (calls cost money, never cut a live call)

Before the full path changes anything on the box:

1. If `voicehook-agent` is not active, there is nothing to interrupt: continue.
2. Otherwise box-local LiveKit twirp `ListRooms` + `ListParticipants` on
   `127.0.0.1:7880`. The JWT is signed on the box from `/opt/voicehook/.env`; keys
   never leave the box.
3. Any participant that is not agent-kind and not `voice-ai*` counts as human
   (the senior CLI counts as human, on purpose). Human present: abort, unless
   `--force-restart`.
4. Any error (ssh, timeout, missing keys, HTTP): abort (fail closed).

Hard caps: `timeout 120` around the ssh call, `timeout 90` on python, an 80s
internal deadline and 10s per request.

## 4. rsync and box-only files

- `web/` -> `/var/www/voicehook`: **no `--delete`**. Box-only files in the docroot
  (`.deployed-sha`, verification files) are never removed. A file deleted from
  `web/` stays on the box until removed by hand.
- `apps/agent/` -> `/opt/voicehook/apps/agent`: `--delete` (repo mirror, avoids stale
  modules), but `.env*`, `.venv`, `__pycache__`, `.git`, `*.egg-info` are excluded and
  therefore protected.
- Single files (pyproject, README, unit, Caddyfile, compose) are plain copies.

Note: Caddy serves the docroot, so `/.deployed-sha` is publicly readable (commit SHA only).

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
| `VOICEHOOK_REQUIRE_CREDITS_NORMAL` | `0` | `1` = `/api/host-call` / `/api/invite-room` ohne Wallet mit Saldo > 0 -> 402; greift nur, wenn das Gratis-Kontingent Normal aus ist (`0`) |
| `VOICEHOOK_REQUIRE_CREDITS_LIVE` | `0` | `1` = `/api/live-room` ohne Wallet mit Saldo > 0 -> 402; greift nur, wenn das Gratis-Kontingent Live aus ist |
| `RESEND_API_KEY` | leer | Resend-API-Key für die Login-Mails; leer = Login aus (`/api/login` -> 503 `login_unavailable`) |
| `MAIL_FROM` | `voicehook <login@voicehook.ai>` | Absender der Login-Mail (Domain muss in Resend verifiziert sein) |
| `VH_LOW_BALANCE_WARN_SECONDS` | `300` | Worker: ab dieser Restzeit (Gratis + Guthaben) einmal pro Call Hinweis + Ansage |
| `VOICEHOOK_PRICE_FACTOR_NORMAL` / `_LIVE` | `3` / `1.5` | Abbuchung = Anbieterkosten x Faktor |
| `VOICEHOOK_VAT_RATE` | `0.19` | MwSt obendrauf |
| `VOICEHOOK_USD_EUR` | `0.8807` | Kurs USD -> EUR (EZB 30.09.2026) |
| `VOICEHOOK_TOPUP_AMOUNTS_EUR` | `10,20,50` | Vorschlagsbeträge |
| `VOICEHOOK_TOPUP_MIN_EUR` / `_MAX_EUR` | `10` / `200` | Spanne des Drehreglers (Minimum nie unter 10) |
| `VOICEHOOK_FREE_MIN_PER_DAY_NORMAL` | `20` | Gratis-Gesprächsminuten pro UTC-Tag für Normal-Räume; `0` = aus |
| `VOICEHOOK_FREE_MIN_PER_DAY_LIVE` | `10` | dasselbe für Live-Räume; `0` = aus |
| `VH_FREE_TICK_SECONDS` | `5` | Takt, in dem der Worker Gratis-Minuten bucht (nur Worker) |
| `VOICEHOOK_APPROX_EUR_PER_HOUR_NORMAL` / `_LIVE` | `1.70` / `8.80` | nur Anzeige: ungefährer Kundenpreis pro Stunde (inkl. Marge und MwSt) am Normal/Live-Schalter, via `/api/billing/config` `approx_eur_per_hour` |

Stripe-Dashboard: Webhook-Endpunkt `https://voicehook.ai/api/stripe/webhook` mit den Events
`checkout.session.completed`, `checkout.session.async_payment_succeeded`, `charge.refunded` und
`charge.dispute.created` anlegen (die beiden letzten ziehen erstattete bzw. zurückgebuchte Beträge
wieder ab, Saldo nie unter 0, Fehlbetrag in `reversals.shortfall_ueur`), dessen
Signing-Secret als `STRIPE_WEBHOOK_SECRET` setzen, danach Agent neu starten (Env wird beim Start gelesen).

### Gratis-Kontingent (ohne Login)

Reihenfolge im Call: erst Gratis-Minuten, dann Guthaben. Hat der Ersteller heute noch Gratis-Minuten
und zusätzlich ein gedecktes Wallet, steht der Raum in beiden Tabellen; der Worker zählt erst die
Gratis-Minuten herunter und bucht danach vom Wallet, ohne den Call zu beenden. Der Call endet erst,
wenn beides leer ist. Reichen Gratis-Rest + Guthaben (Guthaben / Verbrauch der letzten 3 Minuten)
noch höchstens 5 Minuten, schickt der Worker einmal pro Call `operator.notice`
`{kind:"low_balance", minutes_left, ...}` an alle im Raum und sagt "Noch etwa fünf Minuten, lade
Guthaben auf voicehook.ai auf." `GET /api/me` liefert der Oberfläche Gratis-Rest und Guthaben.

Gratis gibt es höchstens `VOICEHOOK_FREE_MIN_PER_DAY_*` Gesprächsminuten pro UTC-Tag und Modus
(Zeit mit mindestens einem Menschen im Raum). Gezählt wird je Merkmal des Raum-Erstellers: anonyme
ID aus dem Header `X-Anon-Id` (8 bis 128 Zeichen `A-Za-z0-9_-`, sonst ignoriert) und Client-IP
(letztes Element von `X-Forwarded-For`, das Caddy selbst setzt; IPv6 je /64-Netz). Erreicht EINES der Merkmale das Limit, antworten
`/api/host-call`, `/api/invite-room` bzw. `/api/live-room` ohne gedecktes Wallet mit 402 `{error: free_limit, topup_url: /aufladen}`; ein
laufender Gratis-Call endet mit der Ansage "Deine Gratisminuten für heute sind um. Lade Guthaben
auf." Gespeichert werden nur SHA-256-Hashes der Merkmale in `/opt/voicehook/state/freetier.sqlite`
(älter als 7 Tage wird gelöscht). Mit gedecktem Wallet endet der Call am Gratis-Limit nicht, das
Wallet zahlt weiter. Die Obergrenze pro Live-Call
(`VH_MAX_CALL_SECONDS=1200` im Live-Dienst) gilt zusätzlich. Der Live-Worker ist fail-closed: ein
Live-Raum ohne Wallet und ohne Gratis-Eintrag wird abgelehnt (Admin-Räume stehen als Ausnahme drin).
Das Live-Monatsbudget gilt nur für Gratis/Demo-Räume, Wallet-Räume sind davon ausgenommen.

### Login per Magic-Link

`POST /api/login {email}` schickt über Resend einen Link `https://voicehook.ai/aufladen#login=<token>`
(einmal, 15 Minuten; Ratenlimit 5 je IP in 10 min, 3 je Adresse in 15 min). Die Seite ruft damit
`GET /api/login/verify?token=...` auf und bekommt ein Wallet-Token für das Konto mit dieser jetzt
bestätigten Adresse. Die Stripe-Mail ist nur eine unbestätigte Kontakt-Mail und verknüpft allein nie.
Wird eine Adresse zum ersten Mal bestätigt, verlieren alle anderen Tokens der so übernommenen Konten
ihre Gültigkeit (wer bei Stripe eine fremde Adresse eintippt, behält keinen Zugriff); weitere
unbestätigte Konten mit derselben Kontakt-Mail werden samt Saldo zusammengeführt. Einrichtung:
Resend-Konto, Domain `voicehook.ai` dort verifizieren (SPF/DKIM), `RESEND_API_KEY` und optional
`MAIL_FROM` in `/opt/voicehook/.env`, Agent neu starten.
