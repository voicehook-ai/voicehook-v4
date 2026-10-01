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
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | leer | OAuth-Client "Mit Google fortfahren" auf `/login`; fehlt einer der beiden Werte: Knopf ausgegraut, `/api/auth/google/*` -> 503 |
| `GITHUB_OAUTH_CLIENT_ID` / `GITHUB_OAUTH_CLIENT_SECRET` | leer | dasselbe für "Mit GitHub fortfahren" (`/api/auth/github/*`) |
| `VOICEHOOK_PUBLIC_URL` | `https://voicehook.ai` | Basis für Stripe-Rücksprung, Login-Link und OAuth-Callback (`<URL>/api/auth/<anbieter>/callback`) |
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
(`VH_MAX_CALL_SECONDS=1200` im Live-Dienst) gilt zusätzlich. Der Worker ist in beiden Modi
fail-closed, solange das Gratis-Kontingent des Modus an ist: ein Raum ohne Wallet und ohne
Gratis-Eintrag wird abgelehnt (`free_room_unknown`). Ausnahmen stehen als exempt drin: Admin-Live-Räume
und Normal-Räume, die jemand mit einer gültigen HMAC-Einladung betritt, die der Server nicht selbst
ausgestellt hat (call-starten mintet sie mit `INVITE_SECRET`). `GET /api/token?invite=1`
(Operator-Join, voicehook-agent CLI) gibt weiter ein Token, dispatcht voice-ai aber nur in Räume
mit bekanntem Zahler; ein selbst ausgedachter neuer Slug bekommt keinen Gratis-Agent mehr.
Das Live-Monatsbudget zählt alles, was nicht das Guthaben zahlt: Gratis/Demo-Räume ganz, Wallet-Räume
ihren Gratis-Teil. Beendet wird am Budget nur ein Raum ohne Wallet; ist das Budget schon erschöpft,
zahlt bei Wallet-Räumen das Guthaben von Anfang an (kein Gratis-Teil).

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
Resend-Konto, Domain `voicehook.ai` dort verifizieren (SPF/DKIM), `RESEND_API_KEY` und optional
`MAIL_FROM` in `/opt/voicehook/.env`, Agent neu starten.

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
