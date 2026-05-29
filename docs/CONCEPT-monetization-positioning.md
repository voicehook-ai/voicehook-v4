# voicehook v4: Monetarisierung, Free-Tier, Positionierung (Konzept)

Stand 2026-05-29. Quelle: Live-Audit voicehook.ai + Code-Audit v3/v4 (file:line-belegt), plus Marktrecherche Voice+AI-Agent.

Dieses Dokument ist ein Konzept, keine Implementierung. Es entscheidet WAS gebaut wird und in welcher Reihenfolge, und stützt sich ausschließlich auf den verifizierten Code-Stand.

---

## 1. Realitäts-Snapshot (verifiziert, nicht angenommen)

Drei verbreitete Annahmen stimmen NICHT mit dem Code überein. Wichtig fürs Konzept, damit wir nicht auf Phantome bauen.

| Annahme | Realität (Beleg) |
|---|---|
| "Free-Tier ist IP-basiert" | FALSCH. Free-Tier ist **email-keyed**, 50.000 Credits/Tag (~10 Min), idempotent pro UTC-Tag. Kein IP, kein Fingerprint im Code (`billing/db.py:206-235`, `stripe_routes.py:109-112`). IP+Fingerprint waren in den Drafts *gewünscht*, nie gebaut. |
| "Aufladen hat Browser-Fingerprint" | FALSCH. `aufladen.html` hat KEINE Identity: kein Email-Feld, kein Fingerprint, keine Wallet-ID. Die Seite weiß nicht, welche Wallet sie auflädt (`aufladen.html`, 220 Zeilen, 0 Identity). |
| "Stripe-Integration ist da" | TEILS. Backend-Code existiert und ist sauber (Checkout-Session, signierter Webhook, idempotenter Ledger), war aber **nie live**: `email-validator`-ImportError hat den Router 7 Tage still ungemountet (`docs/PLAN-v4.md:152`). UI-CTA ist ein Mockup-`alert` (`aufladen.html:210-215`). Aus v4 komplett rausgeschnitten. |

Was LIVE und echt ist:
- LiveKit-Token-Mint (`apps/router/router.py:51-79`), aber ungated, `canPublish=true` für jeden, **kein TTL** -> LiveKit-Default ca. 6h.
- v4-Carryover: `apps/agent/tokens.py`, HMAC-signierte Invite-Codes + sauberes TTL (`mint_livekit_token(ttl_seconds=3600)`, Default 1h). Schließt das alte "no-auth canPublish"-Loch, hat aber **kein Balance-Gate und kein Refresh**.

Was als wiederverwendbares Asset existiert (v3, tot aber gut):
- Vollständiger Stripe-Credits-Ledger (`stripe_routes.py`, `billing/db.py`, `billing/pricing.py`).
- Fertige, gebrandete Aufladen-UI (`aufladen.html`, Drag-Dial 5-300 Min, Teal-Theme), nur der finale `fetch('/api/checkout')` ist auskommentiert.
- Gebrandete Statik (`impressum.html`, `datenschutz.html`, `styles.css` 35KB Designsystem, `security.txt`).
- Caddy-Route-Block für `/api/checkout|stripe|balance`.

---

## 2. Tote Links und fehlende Seiten (Audit-Ergebnis)

Live voicehook.ai ist eine SPA mit Catch-all: **jeder** unbekannte Pfad liefert 200 + index.html. Es gibt kein 404. Tote Links sind dadurch für Monitoring unsichtbar.

Footer-Burger-Menü (es gibt keine Top-Nav):

| Label | Pfad | Status |
|---|---|---|
| Start | `/` | Live, echte Landing |
| Aufladen | `/aufladen` | **Tot**: Mockup-Seite ohne Identity, CTA ist `alert`. Einziger Monetarisierungs-Eintritt, führt ins Leere. |
| Setup | `/setup` | Quelle ok (v3), aber auf der gebauten Box SPA-Fallback. |
| Datenschutz | `/datenschutz` | Live |
| Impressum | `/impressum` | Live |
| Security | `/security` | Live (security.txt) |
| GitHub | extern | Live |

Fehlt komplett (überall SPA-Fallback, keine echte Seite): `/pricing` `/preise` `/plans`, `/demo`, `/checkout` `/billing` `/payment` `/bezahlen`, `/agb` (rechtlich für B2B oft nötig, existiert in KEINEM Repo). Kein Demo-Video. Kein Use-Case-Abschnitt ("für wen"). Die Aufladen-Fineprint verlinkt AGB+Datenschutz als `href="#"` (Dead-Anchor).

Sofort-Fixes (klein):
- AGB-Seite anlegen (fehlt überall), Aufladen-Fineprint-Anchors auf echte Ziele.
- Server-404 statt Catch-all-200, damit tote Links überhaupt sichtbar werden.

---

## 3. Positionierung (aus Marktrecherche)

Markt-Erkenntnis: Retell AI (YC W24) gewinnt den API-Layer ("Conversational Speech API"). AgentWire besetzt Infra/Orchestrierung. voicehook spielt auf einer anderen Ebene: die direkte Mensch-trifft-Agent Call-Experience (3D-Orb, Presence, Transkript), kein API-Abstraktionslayer.

Positionierung: **"The Voice Interface for Your AI Agents"** -> der Ort, wo Mensch und Agent sich begegnen. NICHT "Voice API for Developers" (verloren gegen Retell).

Drei Go-to-Market-Threads (Priorität nach Markt-Signal):
1. "Pair Programming, aber du sprichst" (Developer, YouTube/Twitter-Demo).
2. "Coaching-Sessions mit deinem Agent" (breiteres Publikum, LinkedIn/Medium). Interview/Coaching-Markt wächst (Aura, Assess-AI).
3. "Agent-to-Agent Calls" (futuristisch, Show-HN, Early Adopter).

Landing braucht (existiert heute nicht): Hero mit klarer Aussage, Use-Case-Sektion ("für wen"), Demo-Video oder eingebetteter Live-Call als Demo, sichtbare Pricing-Seite. Die v3-Kringel/Orb-Optik kommt laut Olli später zurück, daher hier nicht im kritischen Pfad.

---

## 4. Pricing-Modell: EINE Entscheidung treffen

Heute existieren zwei widersprüchliche, nicht verbundene Modelle:
- Frontend: 8 Cent/Minute (`aufladen.html:114`).
- Backend: Credits @ 0,0001 EUR + 30% Marge + 3 Packs (Starter 5 / Standard 20 / Pro 50 EUR), Burn ~0,5 Credits/Sek (`pricing.py:24-40`).

Empfehlung: **Credits als Abrechnungseinheit, transparent als Minuten/Zeit angezeigt.** Begründung aus dem v3-Pitch übernehmen: "Vapi-Logik, aber radikal transparent, jede Komponente cost×marge plus ehrliche connection-sec". Credits erlauben echtes Per-Token/Per-Sekunde-Metering (Cent runden bei LLM-Tokens auf 0), die UI zeigt dem Nutzer aber verständlich "~X Minuten Guthaben".

Konkret zu klären (Gaps, die das Konzept auflösen muss):
- UI-Einheit (Minuten-Anzeige) vs Backend-Einheit (Credits): EIN Mapping festschreiben, nicht zwei Modelle.
- Aufladen-Packs (5/20/50 EUR) vs Drag-Dial-Minuten: entweder Packs ODER frei wählbarer Betrag, nicht beides parallel.
- "Guthaben verfällt nicht, kein Abo" (Aufladen-Versprechen) beibehalten, sauber gegen den täglichen Free-Grant abgrenzen (Free resettet pro Tag, gekauftes Guthaben nie).

---

## 5. Free-Tier neu: IP + Browser-Fingerprint (was geplant war, jetzt bauen)

Heute: email-keyed, einzige Abuse-Abwehr ist "Email nötig", kein Verify, kein IP, kein Fingerprint. Die Drafts flaggen das selbst: "trivially abusable, Email-verify + device fingerprint minimum".

Konzept (gestaffelte Anti-Abuse, anonym nutzbar, das ist der GTM-Hebel "10 Min gratis sofort, ohne Anmeldung"):

1. **Anonyme Identity zuerst, nicht Email.** Beim ersten Laden eine stabile anonyme Wallet-ID erzeugen und in `localStorage` persistieren (Muster existiert schon: `voice.html:2756-2767` macht das für die LK-Presence-ID, bisher unverbunden mit Billing). Diese ID bindet die Wallet, ohne Login-Friktion.
2. **Browser-Fingerprint** (canvas/audio/UA-Entropy, z.B. FingerprintJS-Open-Source oder eigenes leichtes Schema) als zweites Signal gegen localStorage-Reset.
3. **IP** (aus Caddy-Header `X-Forwarded-For`) als drittes Signal. Free-Grant gilt pro (Fingerprint ODER IP) pro UTC-Tag, nicht pro frischem Page-Load.
4. **Email optional** für mehr Free oder zum Mitnehmen des Guthabens über Geräte.

Free-Betrag: ~10 Min/Tag (entspricht den 50.000 Credits aus dem Bestandscode), als Zeit angezeigt. Burst-Schutz: ein laufender Call pro Identity.

Wichtig: Diese drei Signale lösen GENAU das Loch, das die Drafts offen ließen. Anonyme Wallet + Fingerprint + IP statt nackter Email-String.

---

## 6. JWT-Refresh und Mid-Call-Cutoff (Kern der Abrechnung)

Heute live: Token ohne TTL (~6h Default), kein Refresh, kein Cutoff. Ein leeres Guthaben blockt nichts (und der einzige Balance-Gate-Endpoint ist tot). Das ist das eigentliche Billing-Loch.

Konzept (entspricht den v3-Drafts Layer A + B, jetzt umsetzen):

**Layer A: TTL = wie lange Guthaben reicht.**
`exp = now + min(maxSession, balance_credits / burn_rate_per_sec)`, gefloort z.B. 60s. Der JWT kann das Geld nicht überleben, selbst wenn der Metering-Loop stirbt. Client refresht rollend alle 60-120s VOR `exp`, jeder Refresh prüft die Balance neu. Balance <= 0 -> kein Refresh -> Verbindung endet am aktuellen `exp`. Worst-Case-Überzug = ein TTL-Fenster.

Konkret zu bauen:
- `/api/token/refresh` (fehlt komplett), re-mint mit frischem TTL aus aktueller Balance.
- Client-Refresh-Loop in voice.html (heute fetcht es den Token genau einmal, `voice.html:2785`).
- Das balance-gated `POST /api/token` mit 402 (`stripe_routes.py:397-438`) tatsächlich an Caddy routen UND der Client muss 402 behandeln (heute 0 Handling): bei 402 -> Aufladen-Flow öffnen.

**Layer B: Server-autoritativer Cutoff.**
Metering-Loop zählt echten Verbrauch (STT/LLM/TTS/connection-sec). Bei 0: finaler TTS via `senior.say` ("Guthaben leer, lade auf um weiterzureden"), dann `RoomService.RemoveParticipant`/`DeleteRoom`. Heute nicht gebaut (explizit Phase 2 im Bestandscode).

---

## 7. Stripe scharf schalten (vorhandenen Code zum Leben bringen)

Der Backend-Flow ist fertig, nur tot. Aufgaben:
1. **Router hart mounten.** Den stillen `try/except` in `agent.py:671-676` entfernen bzw. fail-loud machen, `email-validator` als Dependency pinnen (war der 7-Tage-Killer), CI-Test der den Mount verifiziert (sonst "im Source gemountet" != "in Prod gemountet").
2. **UI entstubben.** `aufladen.html:210-215` Mockup-`alert` durch echtes `fetch('/api/checkout')` ersetzen (Zeile ist schon als Kommentar da), Redirect auf Stripe-Checkout-URL.
3. **Identity an Aufladen binden.** Die anonyme Wallet-ID (Abschnitt 5) an Checkout + `/api/balance` durchreichen, damit die Seite weiß, welche Wallet sie auflädt.
4. **Einheiten reconcilen** (Abschnitt 4): Packs vs Minuten auflösen.
5. **Env-Vars** (nur Namen): `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_PACK_*`, `STRIPE_SUCCESS_URL`, `STRIPE_CANCEL_URL`, `FREE_DAILY_CREDITS`, `MIN_CREDITS_TO_JOIN`, `MAX_SESSION_SEC`, `BILLING_DB_PATH`. Secrets über p2ai/cloud-init, nie hardcoden.

End-to-End-Flow (Soll): Aufladen-UI -> `POST /api/checkout {walletId, betrag}` -> Stripe Hosted Checkout -> Webhook `checkout.session.completed` (signiert, idempotent auf session.id) -> Ledger-Gutschrift -> `/api/balance` zeigt Stand -> Token-Mint erlaubt sobald `balance >= MIN_CREDITS_TO_JOIN`.

---

## 8. Roadmap (Phasen, v4-first, v3-Assets wiederverwenden)

Billing wurde aus v4 bewusst gecuttet. Diese Phasen holen es sauber zurück.

**Phase 0 (klein, sofort): tote Links zu.** AGB-Seite, Fineprint-Anchors, Server-404 statt Catch-all-200. Statik aus v3 portieren (impressum/datenschutz/styles).

**Phase 1: Free-Tier-Identity + Gate.** Anonyme Wallet-ID (localStorage) + Fingerprint + IP, Free-Grant pro Identity/Tag, balance-gated Token-Mint an Caddy routen, Client 402-Handling. Liefert: "10 Min gratis ohne Login, dann blockiert".

**Phase 2: Refresh + Cutoff.** `/api/token/refresh`, rollender Client-Refresh, Layer-B-Metering + RemoveParticipant. Liefert: Geld kann nicht überzogen werden.

**Phase 3: Stripe live.** Router fail-loud mounten + CI-Guard, Aufladen-UI entstubben, Wallet-ID-Bindung, Einheiten reconcilen. Liefert: echtes Aufladen.

**Phase 4: SSOT-Frontend-Port (statt Landing neu bauen).** Die reiche v3-`voice.html` (198KB, Orb + Footer-Menü + Wizard, das was heute auf voicehook.ai ausgeliefert wird) ins v4-Deployment ziehen und dort als kanonisches Frontend iterieren. Begründung: Single Source of Truth statt Divergenz zwischen "hübschem v3-Frontend" und "sauberem v4-Backend". Die v3-Orb-Optik kommt damit automatisch zurück.

Machbarkeit (verifiziert): Der Endpoint-Contract passt bereits. v3-`voice.html` ruft `GET /api/token?room=` und liest `{url, token}` (`voice.html:2779-2798`); v4-Server liefert exakt diese Form (`server.py:42-43,143`), `url` aus `LIVEKIT_URL`-env (folgt sslip/prod automatisch). `deploy.sh` rsynct `web/` ohnehin. File-Move ist trivial.

Der eine echte Gap: v4 gated JEDEN Token-Mint hinter HMAC-Invite (`verify_invite`, #52-Fix). v3-`voice.html` hat einen Host-Pfad OHNE Invite ("Start Voice Call" mintet freie Räume), der unter v4 bricht. Das ist aber keine Extra-Arbeit, sondern GENAU das Free-Tier-Gate aus Abschnitt 5/6: statt "no-auth canPublish" -> "anonyme Wallet + Free-Tier-Gate". Invite-Gate und Billing-Gate sind dieselbe Entscheidung. Der Invite-Deeplink-Pfad (`/r/<slug>?invite=`) läuft sofort (so lief der Test-Call).

Aufwand: File-Move + Deploy ~1h; Invite-Deeplink-E2E sofort; Host-Mint-Pfad unter v4-Gate 0,5-1 Tag (= die Free-Tier-Entscheidung, nicht extra). Netto ~1-2 Tage, dominiert von der Gate-Entscheidung, nicht vom Move.

Reihenfolge-Logik: Gate (1) und Cutoff (2) MÜSSEN vor Stripe-live (3), sonst verkaufst du Guthaben, das nichts blockt. Der SSOT-Port (4) kann FRÜH passieren (sofort als Spike, der Invite-Pfad läuft), und der Host-Mint-Teil verschmilzt dann mit Phase 1. Empfehlung: Phase 4-Spike (Port + Deploy + Invite-E2E) vorziehen, weil billig und es macht v4 sofort zum echten SSOT.

---

## 9. Offene Entscheidungen für dich

1. Pricing-Einheit: Credits-mit-Minuten-Anzeige (Empfehlung) oder simpel 8 Cent/Min?
2. Aufladen: feste Packs (5/20/50) oder frei wählbarer Betrag (Drag-Dial)?
3. Free-Tier ohne Login (anonym, Empfehlung für GTM) oder Email-Pflicht ab Sekunde eins?
4. v4 als Ziel-Repo fürs Billing-Comeback bestätigen (Code aus v3 portieren), oder anders?
