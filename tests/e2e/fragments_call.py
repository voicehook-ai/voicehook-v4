"""Fragment-Call: Olivers echten Call gegen einen lokalen LiveKit-Server nachstellen (nie Prod).

Echt sind: livekit-server --dev, der Normal-Worker aus `--repo` (python -m agent start, echte
AgentSession), Deepgram STT, Gemini (Thinking laut llm.py) und Google TTS. Nachgestellt sind
nur die beiden anderen Teilnehmer:

- "Nutzer" (oliver-e2e): publiziert deutsche Satzstücke von 1 bis 3 s mit Pausen von 0,5 bis
  3 s (Seed fest), etwa 3 Minuten. Fragen-Turns warten auf eine Antwort (höchstens 12 s),
  sonst redet er weiter. Die Stücke werden einmal mit Google TTS erzeugt und gecacht.
- "Operator" (claude-e2e, vh.role=agent, vh.name=Claude, vh.user=Oliver): sendet wie die CLI
  operator.say {text, _seq, _ts} (ohne mode = revise), operator.status (Board),
  operator.alive alle 10 s, beantwortet operator.status_request sofort mit dem Board und
  operator.revise nach 2 s mit einer zusammengefassten say (mode overwrite), wie es der Skill
  verlangt. Die says kommen absichtlich, während der Nutzer spricht.

Gemessen (Tabelle + JSON in --out):
1. pro say: say_status-Kette (ab #141) und, für beide Stände gleich, welcher Anteil der Wörter
   tatsächlich als `transcript` role operator gesprochen wurde ("verloren" = nie ganz gesprochen).
2. Antwortzeit nach Ende eines Nutzer-Turns (Audio des Workers, Energie-Erkennung): Median, p90,
   längste Stille, in der der Nutzer auf eine Antwort wartet.
3./4. Transkript von Delta (role agent) mit Markierungen: Fortschritt nicht im Board,
   doppelter Name, gestapelte Wartesätze, Ich-Form als Claude. Prüfung von Hand bleibt nötig.

Credentials nur aus der Env (nie ausgegeben, nie auf Platte): DEEPGRAM_API_KEY, GOOGLE_API_KEY,
VH_E2E_GCP_SA_B64 (GCP-Service-Account-JSON, base64). Der Worker bekommt die TTS-Credential
über ein memfd (RAM, keine Datei auf der Platte): GOOGLE_APPLICATION_CREDENTIALS zeigt auf
/proc/<pid>/fd/<n> dieses Prozesses. Kosten: Deepgram als echte Abrechnung aus der
Requests-API des Projekts; Google als echte Mengen (TTS-Zeichen, die gesprochen wurden).

  p2ai run -e DEEPGRAM_API_KEY=DEEPGRAM_API_KEY -e GOOGLE_API_KEY=GOOGLE_API_KEY \\
           -e VH_E2E_GCP_SA_B64=voicehook_gcp_sa_tts_b64 -- \\
    env LIVEKIT_SERVER=/pfad/livekit-server .venv/bin/python tests/e2e/fragments_call.py \\
      --repo . --label neu --out /tmp/fragcall-neu
  # Positivkontrolle: --repo <worktree auf 7aed542> --label alt

Szenario `--scenario kontext` (integ/r8, Oliver 02.10.): reicher Kontext für Delta. Der
Operator schickt ein Board OHNE doing, mit faq, und alle 5 s den Aktivitäts-Feed
(operator.activity {lines, ts}, wie der CLI-Hook). Der Nutzer stellt Fragen, die nur aus faq,
Feed, Board oder Claudes eigenen Sätzen beantwortbar sind. Gemessen je Frage-Turn: antwortet
Delta inhaltlich oder nur mit einem Wartesatz ("Moment"), dazu die Antwortzeit aus der
`[timing]`-Zeile des Workers (play = VAD-Ende bis Wiedergabe, ab #148; ältere Worker: nur
Energie-Messung). Alt gegen neu: derselbe Lauf mit --repo <origin/main>.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import difflib
import json
import os
import random
import re
import signal
import statistics
import subprocess
import sys
import time
import wave
from pathlib import Path

import httpx
from livekit import api, rtc

PORT = 7991
URL, KEY, SECRET = f"ws://127.0.0.1:{PORT}", "devkey", "secret"
SR = 48000
FRAME = 480  # 10 ms
SEED = 7
CALL_CAP_S = 300  # harte Obergrenze für den ganzen Call (Kosten)
USER_VOICE = "de-DE-Chirp3-HD-Fenrir"

# Turns des Nutzers: (wartet auf Antwort?, Satzstücke). Wie Oliver: Stücke, kurze Pausen.
TURNS: list[tuple[bool, list[str]]] = [
    (True, ["Hallo Delta.", "Hörst du mich?"]),
    (True, ["Sag mal,", "wie weit ist Claude", "mit dem Deploy?"]),
    (False, ["Okay.", "Und sag ihm bitte,", "er soll danach", "die Tests laufen lassen."]),
    (True, ["Ach so,", "und was ist eigentlich", "mit dem Login-Bug?"]),
    (False, ["Hm.", "Moment,", "ich überleg kurz."]),
    (True, ["Ja genau,", "die Sache mit der Queue,", "ist die schon drin?"]),
    (False, ["Gut.", "Das klingt gut."]),
    (True, ["Was ist denn", "noch offen?"]),
    (False, ["Okay,", "und wie lange", "dauert das noch?"]),
    (True, ["Bist du noch da?"]),
    (True, ["Was hat Claude", "denn schon fertig?"]),
    (False, ["Alles klar,", "danke dir.", "Dann warte ich", "auf die Tests."]),
    (True, ["Ist der Pull Request", "schon offen?"]),
    (True, ["Okay.", "Bis gleich."]),
]

BOARD_1 = {"doing": "baut den Fix für die Say-Queue, ETA 5 Minuten",
           "open": ["Tests laufen lassen", "Pull Request aufmachen", "Login-Bug"],
           "done": ["Analyse des Calls"]}
BOARD_2 = {"doing": "lässt die Tests laufen, ETA 2 Minuten",
           "open": ["Pull Request aufmachen", "Login-Bug"],
           "done": ["Analyse des Calls", "Fix für die Say-Queue gebaut"]}

# says des Operators: (Turn, Stück, Verzögerung s, Text, Board vorher). Kommen mitten im Satz.
SAYS: list[tuple[int, int, float, str, dict | None]] = [
    (2, 0, 0.4, "Der Deploy ist noch nicht gelaufen, Claude baut gerade den Fix für die Say-Queue, "
                "in etwa fünf Minuten ist er so weit.", None),
    (3, 1, 0.4, "Alles klar, die Tests startet Claude direkt nach dem Fix und meldet dann das Ergebnis.",
     None),
    (4, 0, 0.3, "Der Login-Bug steht noch auf der Liste, den nimmt Claude sich nach der Queue vor.", None),
    (6, 0, 0.3, "Die Queue ist gebaut, die Tests laufen gerade, das dauert noch etwa zwei Minuten.",
     BOARD_2),
    (8, 1, 0.4, "Offen sind noch der Pull Request und der Login-Bug, sonst nichts.", None),
    (9, 0, 0.3, "Rund zwei Minuten noch, dann gibt Claude Bescheid.", None),
    (9, 0, 2.3, "Und den Login-Bug macht Claude gleich danach.", None),  # zweite say: revise-Pfad
    (11, 2, 0.4, "Danke dir, Claude meldet sich, sobald die Tests grün sind.", None),
]

# ----- Szenario kontext: faq + Aktivitäts-Feed, Board ohne doing ----------------------
TURNS_K: list[tuple[bool, list[str]]] = [
    (True, ["Hallo Delta.", "Hörst du mich?"]),
    (True, ["Was macht Claude", "eigentlich gerade?"]),
    (False, ["Okay.", "Gut."]),
    (True, ["Und wann ist das", "live?"]),
    (True, ["Woran hat er", "zuletzt gearbeitet?"]),
    (False, ["Hm.", "Moment,", "ich überleg kurz."]),
    (True, ["Was ist denn", "noch offen?"]),
    (True, ["Geht der Login", "schon wieder?"]),
    (True, ["Was hat Claude", "denn schon fertig?"]),
    (True, ["Wie lange dauert", "das noch?"]),
    (True, ["Was hat er", "vorhin über die Queue gesagt?"]),
    (True, ["Kostet das", "eigentlich extra?"]),
    (True, ["Hat er die Tests", "schon laufen lassen?"]),
    (True, ["Okay.", "Bis gleich."]),
]
BOARD_K = {"doing": "", "open": ["Pull Request aufmachen", "Login-Bug"],
           "done": ["Analyse des Calls", "Fix für die Say-Queue gebaut"],
           "faq": [{"q": "Wann ist das live?", "a": "Nach Olivers Review, heute Abend."},
                   {"q": "Geht der Login wieder?", "a": "Noch nicht, der Login-Bug ist als Nächstes dran."},
                   {"q": "Wie lange dauert das noch?", "a": "Etwa zehn Minuten bis zum Pull Request."},
                   {"q": "Kostet das extra?", "a": "Nein, das gehört zum normalen Paket."}]}
SAYS_K: list[tuple[int, int, float, str, dict | None]] = [
    (2, 0, 0.3, "Kurz von Claude: die Queue spricht jetzt nichts mehr doppelt, der Fehler war ein "
                "zu frühes Nachsprechen nach einer Unterbrechung.", None),
    (6, 0, 0.3, "Claude meldet: die Tests der Say-Queue sind grün, als Nächstes kommt der Pull Request.",
     None),
]
# (Sekunden ab Szenario-Start, Zeile) wie der Hook: "HH:MM:SS Tool: Beschreibung"
ACTIVITY_K: list[tuple[float, str]] = [
    (0, "Read: Say-Queue im Relay gelesen"),
    (4, "Edit: Nachsprechen nach Abbruch in Live entfernt"),
    (12, "Bash: Tests der Say-Queue laufen lassen"),
    (30, "Bash: Ruff über das Repo laufen lassen"),
    (55, "Edit: Doku zum Aktivitäts-Feed ergänzt"),
    (80, "Bash: Tests der Say-Queue laufen lassen"),
    (110, "Bash: Pull Request vorbereiten"),
]
SCENARIOS = {"standard": (TURNS, SAYS, BOARD_1, [], [BOARD_1, BOARD_2]),
             "kontext": (TURNS_K, SAYS_K, BOARD_K, ACTIVITY_K, [BOARD_K])}
ACTIVITY_EVERY_S = 5.0  # wie die CLI: höchstens alle 5 s, nur bei Änderung

PROGRESS_WORDS = re.compile(r"\b(fertig|deployt|deployed|live|erledigt|gemerged|grün|bestanden|"
                            r"abgeschlossen|offen|aufgemacht|eröffnet)\b", re.I)
WAIT_LINE = re.compile(r"\b(moment|sekunde|ich frag)\b", re.I)
ICH_CLAUDE = re.compile(r"\bich (baue|deploye|teste|lasse|mache|merge|schreibe|fixe|habe .* gebaut)\b",
                        re.I)


def now() -> float:
    return time.monotonic()


# ----- Satzstücke --------------------------------------------------------------------


def frag_texts() -> list[str]:
    return [f for turns in (TURNS, TURNS_K) for _w, frs in turns for f in frs]


def frag_path(cache: Path, text: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]
    return cache / f"{slug}.wav"


def ensure_fragments(cache: Path) -> int:
    """Fehlende Stücke einmal mit Google TTS erzeugen. Gibt synthetisierte Zeichen zurück."""
    cache.mkdir(parents=True, exist_ok=True)
    todo = [t for t in dict.fromkeys(frag_texts()) if not frag_path(cache, t).exists()]
    if not todo:
        return 0
    from google.cloud import texttospeech as tts
    from google.oauth2 import service_account

    info = json.loads(base64.b64decode(os.environ["VH_E2E_GCP_SA_B64"]))
    creds = service_account.Credentials.from_service_account_info(info)
    client = tts.TextToSpeechClient(credentials=creds)
    chars = 0
    for text in todo:
        r = client.synthesize_speech(
            input=tts.SynthesisInput(text=text),
            voice=tts.VoiceSelectionParams(language_code="de-DE", name=USER_VOICE),
            audio_config=tts.AudioConfig(audio_encoding=tts.AudioEncoding.LINEAR16,
                                         sample_rate_hertz=SR))
        pcm = r.audio_content[44:] if r.audio_content[:4] == b"RIFF" else r.audio_content
        with wave.open(str(frag_path(cache, text)), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm)
        chars += len(text)
    return chars


def load_pcm(p: Path) -> bytes:
    with wave.open(str(p), "rb") as w:
        assert w.getframerate() == SR and w.getnchannels() == 1
        pcm = w.readframes(w.getnframes())
    # Stille vorne/hinten kappen (TTS hängt ~100 ms an), damit Stückdauer = Sprache
    import array

    a = array.array("h", pcm)
    idx = [i for i in range(0, len(a), 480) if max(abs(x) for x in a[i:i + 480]) > 500]
    if idx:
        a = a[max(0, idx[0] - 480):min(len(a), idx[-1] + 960)]
    return a.tobytes()


# ----- Prozesse ------------------------------------------------------------------------


def memfd_credentials() -> tuple[int, str]:
    """GCP-SA-JSON in ein memfd (nur RAM), Pfad für GOOGLE_APPLICATION_CREDENTIALS."""
    fd = os.memfd_create("vh-e2e-gcp", 0)
    os.write(fd, base64.b64decode(os.environ["VH_E2E_GCP_SA_B64"]))
    return fd, f"/proc/{os.getpid()}/fd/{fd}"


def start_worker(repo: Path, out: Path, cred_path: str, live: bool = False) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items() if k != "VH_E2E_GCP_SA_B64"}
    env.update({
        "PYTHONPATH": str(repo / "apps"), "LIVEKIT_URL": URL, "LIVEKIT_API_KEY": KEY,
        "LIVEKIT_API_SECRET": SECRET, "GOOGLE_APPLICATION_CREDENTIALS": cred_path,
        "VOICEHOOK_HTTP_DISABLED": "1", "VOICEHOOK_STATE_DIR": str(out / "state"),
        "VH_FREE_EUR_PER_DAY": "0", "VH_MAX_CALL_SECONDS": str(CALL_CAP_S),
        "VH_IDLE_NO_HUMAN_SECONDS": "20", "VH_WORKER_LOAD_THRESHOLD": "0.99",
        "VH_WORKER_IDLE_PROCS": "1", "VH_DRAIN_TIMEOUT": "5", "VOICEHOOK_AGENT_NAME": "voice-ai",
        # --live: Gemini-Live-Worker (worker.is_live), Nutzer-Audio geht direkt an Gemini
        "VOICEHOOK_PIPELINE": "live" if live else "pipeline",
    })
    (out / "state").mkdir(parents=True, exist_ok=True)
    log = open(out / "worker.log", "w")  # noqa: SIM115
    return subprocess.Popen([sys.executable, "-m", "agent", "start"], cwd=repo / "apps", env=env,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def wait_log(path: Path, needle: str, timeout: float) -> bool:
    end = now() + timeout
    while now() < end:
        if path.exists() and needle in path.read_text(errors="replace"):
            return True
        time.sleep(0.3)
    return False


def kill_group(p: subprocess.Popen | None, sig: int = signal.SIGTERM, wait: float = 15) -> None:
    if p is None or p.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(p.pid, sig)
    try:
        p.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(p.pid, signal.SIGKILL)


def token(identity: str, room: str, attrs: dict | None = None) -> str:
    t = (api.AccessToken(KEY, SECRET).with_identity(identity).with_name(identity)
         .with_grants(api.VideoGrants(room_join=True, room=room)))
    if attrs:
        t = t.with_attributes(attrs)
    return t.to_jwt()


# ----- Deepgram: echte Abrechnung ------------------------------------------------------


async def deepgram_usd(since_iso: str) -> dict:
    key = os.environ["DEEPGRAM_API_KEY"]
    h = {"Authorization": f"Token {key}"}
    async with httpx.AsyncClient(timeout=20) as c:
        pid = (await c.get("https://api.deepgram.com/v1/projects", headers=h)).json()["projects"][0]["project_id"]
        r = await c.get(f"https://api.deepgram.com/v1/projects/{pid}/requests",
                        params={"start": since_iso, "limit": 100}, headers=h)
        reqs = r.json().get("requests", []) if r.status_code == 200 else []
        bal = (await c.get(f"https://api.deepgram.com/v1/projects/{pid}/balances", headers=h)).json()
    usd = 0.0
    dur = 0.0
    for q in reqs:
        d = ((q.get("response") or {}).get("details") or {})
        usd += float(d.get("usd") or 0)
        dur += float(d.get("duration") or 0)
    return {"requests": len(reqs), "usd": round(usd, 5), "audio_s": round(dur, 1),
            "balance_usd": [b.get("amount") for b in bal.get("balances", [])]}


# ----- Call ----------------------------------------------------------------------------


class Call:
    def __init__(self, cache: Path, scenario: str = "standard") -> None:
        self.turns, self.says_plan, self.board, self.activity_plan, self.boards = SCENARIOS[scenario]
        self.scenario_name = scenario
        self.scenario_t0: float | None = None
        self.t0 = now()
        self.ev: list[dict] = []
        self.cache = cache
        self.queue: asyncio.Queue = asyncio.Queue()
        self.frags: list[dict] = []
        self.agent_seg: list[list[float]] = []  # [start, end] Audio des Workers
        self.says: list[dict] = []
        self.seq = 0
        self.ops: dict[str, dict] = {}  # transcript.live id -> {start, end, text}
        self.stop = False

    def t(self) -> float:
        return round(now() - self.t0, 3)

    def log(self, kind: str, **kw) -> None:  # noqa: ANN003
        self.ev.append({"t": self.t(), "kind": kind, **kw})

    # Audio raus: Stücke aus der Queue oder Stille, im 10-ms-Takt
    async def pump(self, src: rtc.AudioSource) -> None:
        silence = bytes(FRAME * 2)
        cur: dict | None = None
        buf, pos = b"", 0
        nxt = now()
        while not self.stop:
            if cur is None and not self.queue.empty():
                cur = self.queue.get_nowait()
                buf, pos = cur["pcm"], 0
                cur["start"] = self.t()
            if cur is not None:
                chunk = buf[pos:pos + FRAME * 2]
                pos += FRAME * 2
                if len(chunk) < FRAME * 2:
                    chunk += bytes(FRAME * 2 - len(chunk))
                if pos >= len(buf):
                    cur["end"] = self.t() + 0.01
                    cur["done"].set()
                    cur = None
            else:
                chunk = silence
            await src.capture_frame(rtc.AudioFrame(chunk, SR, 1, FRAME))
            nxt += 0.01
            d = nxt - now()
            if d > 0:
                await asyncio.sleep(d)
            else:
                nxt = now()

    async def speak(self, ti: int, fi: int, text: str) -> dict:
        f = {"turn": ti, "frag": fi, "text": text, "pcm": load_pcm(frag_path(self.cache, text)),
             "done": asyncio.Event(), "started": asyncio.Event()}
        self.queue.put_nowait(f)
        while "start" not in f:
            await asyncio.sleep(0.005)
        self.log("user_frag_start", turn=ti, frag=fi, text=text)
        for s in [s for s in self.says_plan if s[0] == ti and s[1] == fi]:
            asyncio.create_task(self.say_later(s[2], s[3], s[4]))
        await f["done"].wait()
        self.log("user_frag_end", turn=ti, frag=fi)
        self.frags.append({k: f[k] for k in ("turn", "frag", "text", "start", "end")})
        return f

    # Agent-Audio: Energie-Segmente
    async def listen(self, track: rtc.Track) -> None:
        import array

        stream = rtc.AudioStream(track, sample_rate=SR, num_channels=1)
        seg: list[float] | None = None
        last_voice = 0.0
        async for e in stream:
            a = array.array("h", bytes(e.frame.data))
            rms = (sum(x * x for x in a) / max(1, len(a))) ** 0.5
            t = self.t()
            if rms > 150:
                last_voice = t
                if seg is None:
                    seg = [t, t]
                    self.agent_seg.append(seg)
                seg[1] = t
            elif seg is not None and t - last_voice > 0.4:
                seg = None

    def agent_speaking(self, within: float = 0.5) -> bool:
        return bool(self.agent_seg) and self.t() - self.agent_seg[-1][1] < within

    # Operator
    async def send(self, room: rtc.Room, topic: str, payload: dict) -> None:
        await room.local_participant.publish_data(json.dumps(payload, ensure_ascii=False).encode(),
                                                  reliable=True, topic=topic)

    async def say(self, text: str, mode: str | None = None, merged: list | None = None) -> None:
        self.seq += 1
        env = {"text": text, "_seq": self.seq, "_ts": time.time()}
        if mode:
            env["mode"] = mode
        self.says.append({"seq": self.seq, "text": text, "sent": self.t(), "mode": mode or "revise",
                          "merged": merged or [], "states": []})
        self.log("say_sent", seq=self.seq, text=text, mode=mode or "revise")
        await self.send(self.op, "operator.say", env)

    async def say_later(self, delay: float, text: str, board: dict | None) -> None:
        await asyncio.sleep(delay)
        if board is not None:
            self.board = board
            await self.send(self.op, "operator.status", board)
            self.log("status_sent", board=board)
        await self.say(text)

    async def activity(self) -> None:
        """Aktivitäts-Feed wie der CLI-Hook: letzte 15 Zeilen, nur bei Änderung, alle 5 s."""
        last: list[str] | None = None
        while not self.stop:
            if self.scenario_t0 is not None and self.activity_plan:
                el = now() - self.scenario_t0
                wall = time.time()
                lines = [time.strftime("%H:%M:%S", time.gmtime(wall - (el - off))) + " " + line
                         for off, line in self.activity_plan if off <= el][-15:]
                if lines and [x[9:] for x in lines] != [x[9:] for x in (last or [])]:
                    await self.send(self.op, "operator.activity", {"lines": lines, "ts": wall})
                    self.log("activity_sent", lines=len(lines), last=lines[-1][9:])
                    last = lines
            await asyncio.sleep(ACTIVITY_EVERY_S)

    async def alive(self) -> None:
        while not self.stop:
            await self.send(self.op, "operator.alive", {"alive": True, "ts": time.time(), "idle_s": 0})
            await asyncio.sleep(10)

    def on_data(self, pkt: rtc.DataPacket) -> None:
        try:
            d = json.loads(pkt.data.decode())
        except ValueError:
            return
        topic = pkt.topic
        if topic == "operator.say_status":
            for s in self.says:
                if s["seq"] == d.get("seq"):
                    s["states"].append([self.t(), d.get("state"), d.get("spoken_chars")])
            self.log("say_status", **d)
        elif topic == "operator.revise":
            self.log("revise", unspoken=d.get("unspoken"), new=d.get("new"))
            asyncio.create_task(self.answer_revise(d))
        elif topic == "operator.status_request":
            self.log("status_request", text=d.get("text"))
            asyncio.create_task(self.send(self.op, "operator.status", self.board))
        elif topic == "transcript":
            self.log("transcript", role=d.get("role"), text=d.get("text"))
        elif topic == "transcript.live":
            o = self.ops.setdefault(d.get("id"), {})
            o["start" if d.get("phase") == "start" else "end"] = self.t()
            if d.get("text"):
                o["text"] = d["text"]
            if d.get("phase") == "end":
                o["interrupted"] = d.get("interrupted")
            self.log("transcript_live", **d)
        elif topic in ("operator.notice", "call_end"):
            self.log(topic, **d)

    async def answer_revise(self, d: dict) -> None:
        """Wie der Skill: binnen 8 s EINE zusammengefasste say mit mode overwrite."""
        await asyncio.sleep(2.0)
        parts = [*(d.get("unspoken") or []), d.get("new") or ""]
        merged = " ".join(p.strip() for p in parts if p and p.strip())
        held = [s["seq"] for s in self.says if s["text"] in parts or any(s["text"].endswith(p) for p in parts if p)]
        if merged:
            await self.say(merged, mode="overwrite", merged=held)

    async def scenario(self) -> None:
        rnd = random.Random(SEED)
        self.scenario_t0 = now()
        for ti, (wait, frs) in enumerate(self.turns):
            for fi, text in enumerate(frs):
                await self.speak(ti, fi, text)
                if fi < len(frs) - 1:
                    await asyncio.sleep(rnd.uniform(0.5, 1.4))
            self.log("turn_end", turn=ti, wait=wait)
            if wait:  # auf Antwort warten: Agent fängt an und hört auf, höchstens 12 s
                end = now() + 12
                started = False
                while now() < end:
                    if self.agent_speaking(0.3):
                        started = True
                    elif started and not self.agent_speaking(1.2):
                        break
                    await asyncio.sleep(0.05)
            await asyncio.sleep(rnd.uniform(0.5, 3.0))
            if self.t() > CALL_CAP_S - 60:
                break


async def run(args: argparse.Namespace) -> dict:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache = Path(args.frag_dir)
    gen_chars = ensure_fragments(cache)
    fd, cred_path = memfd_credentials()
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5))
    lk = worker = None
    call = Call(cache, args.scenario)
    res: dict = {"label": args.label, "repo": str(args.repo), "fragments_tts_chars": gen_chars,
                 "scenario": args.scenario, "live": args.live}
    try:
        res["sha"] = subprocess.run(["git", "-C", str(args.repo), "rev-parse", "--short", "HEAD"],
                                    capture_output=True, text=True).stdout.strip()
        lk = subprocess.Popen([os.environ.get("LIVEKIT_SERVER", "livekit-server"), "--dev", "--bind",
                               "127.0.0.1", "--port", str(PORT)], stdout=open(out / "lk.log", "w"),  # noqa: SIM115
                              stderr=subprocess.STDOUT, start_new_session=True)
        worker = start_worker(Path(args.repo).resolve(), out, cred_path, live=args.live)
        assert wait_log(out / "worker.log", "registered worker", 60), "worker nicht registriert"
        room_name = f"frag-{int(time.time())}"
        user, op = rtc.Room(), rtc.Room()
        call.op = op

        @user.on("track_subscribed")
        def _sub(track, _pub, part) -> None:  # noqa: ANN001
            if track.kind == rtc.TrackKind.KIND_AUDIO and part.identity != "claude-e2e":
                asyncio.create_task(call.listen(track))

        op.on("data_received", call.on_data)
        async with api.LiveKitAPI(URL.replace("ws", "http"), KEY, SECRET) as lkapi:
            await lkapi.room.create_room(api.CreateRoomRequest(name=room_name, empty_timeout=60))
            await user.connect(URL, token("oliver-e2e", room_name))
            src = rtc.AudioSource(SR, 1, queue_size_ms=50)
            track = rtc.LocalAudioTrack.create_audio_track("mic", src)
            await user.local_participant.publish_track(
                track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
            pump = asyncio.create_task(call.pump(src))
            await lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(agent_name="voice-ai", room=room_name))
            end = now() + 30
            while now() < end and not any(p.identity.startswith("agent-") for p in user.remote_participants.values()):
                await asyncio.sleep(0.2)
            await asyncio.sleep(2)
            await op.connect(URL, token("claude-e2e", room_name,
                                        {"vh.role": "agent", "vh.name": "Claude", "vh.model": "e2e",
                                         "vh.user": "Oliver"}))
            alive = asyncio.create_task(call.alive())
            feed = asyncio.create_task(call.activity())
            await asyncio.sleep(1)
            await call.send(op, "operator.status", call.board)
            call.log("status_sent", board=call.board)
            await asyncio.sleep(3)
            call.log("scenario_start")
            await asyncio.wait_for(call.scenario(), timeout=CALL_CAP_S - 30)
            call.log("scenario_end")
            # Nachlauf: Queue darf sich leeren (höchstens 25 s)
            end = now() + 25
            while now() < end:
                await asyncio.sleep(0.5)
                if not call.agent_speaking(3.0) and now() > end - 20:
                    open_ = [s for s in call.says if not s["states"] or s["states"][-1][1] in ("queued", "requeued", "interrupted")]
                    if not open_ or all(not s["states"] for s in call.says):
                        break
            call.log("end")
            call.stop = True
            alive.cancel()
            feed.cancel()
            await asyncio.sleep(0.3)
            pump.cancel()
            with contextlib.suppress(Exception):
                await lkapi.room.delete_room(api.DeleteRoomRequest(room=room_name))
        with contextlib.suppress(Exception):
            await op.disconnect()
            await user.disconnect()
    finally:
        call.stop = True
        kill_group(worker)
        kill_group(lk, wait=5)
        os.close(fd)
    await asyncio.sleep(20)  # Deepgram bucht mit Verzug
    res["deepgram"] = await deepgram_usd(since)
    res.update(analyse(call, out))
    res.update(timing_lines(out / "worker.log"))
    (out / "events.json").write_text(json.dumps({"events": call.ev, "says": call.says,
                                                 "frags": call.frags, "agent_seg": call.agent_seg,
                                                 "ops": call.ops}, ensure_ascii=False, indent=1))
    (out / "result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    return res


# ----- Auswertung ----------------------------------------------------------------------


def words(s: str) -> list[str]:
    return re.findall(r"[\wäöüß]+", (s or "").lower())


def coverage(say: str, spoken: list[str]) -> float:
    """Anteil der Wörter der say, die in gesprochenen Operator-Zeilen vorkommen (Läufe >= 2)."""
    sw = words(say)
    if not sw:
        return 0.0
    hit = [False] * len(sw)
    for line in spoken:
        lw = words(line)
        for b in difflib.SequenceMatcher(None, sw, lw, autojunk=False).get_matching_blocks():
            if b.size >= 2 or (b.size == 1 and len(sw) == 1):
                for i in range(b.a, b.a + b.size):
                    hit[i] = True
    return sum(hit) / len(sw)


def pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, round(q * (len(xs) - 1))))
    return round(xs[k], 2)


def analyse(call: Call, out: Path) -> dict:
    ev = call.ev
    op_lines = [e["text"] for e in ev if e["kind"] == "transcript" and e["role"] == "operator"]
    agent_lines = [(e["t"], e["text"]) for e in ev if e["kind"] == "transcript" and e["role"] == "agent"]
    says = []
    for s in call.says:
        cov = coverage(s["text"], op_lines)
        merged_into = [m for m in call.says if s["seq"] in m["merged"]]
        cov_m = max([cov, *(coverage(s["text"], op_lines) for _m in merged_into)])
        chain = [st[1] for st in s["states"]]
        final = chain[-1] if chain else None
        delivered = cov >= 0.9 or (final == "replaced" and any(coverage(m["text"], op_lines) >= 0.9
                                                               for m in merged_into))
        # Wiederholung: mehr als eine Operator-Zeile deckt >= 60 % der Wörter dieser say ab
        # (requeue spricht die ganze Aussage nochmal). Abbruch: Kette enthält interrupted.
        repeats = sum(1 for line in op_lines if coverage(s["text"], [line]) >= 0.6)
        says.append({"seq": s["seq"], "sent_t": s["sent"], "mode": s["mode"], "text": s["text"],
                     "repeated": repeats >= 2, "interrupts": chain.count("interrupted"),
                     "chain": chain, "final": final, "spoken_words_pct": round(100 * cov),
                     "merged_into": [m["seq"] for m in merged_into], "delivered": delivered,
                     "cov_incl_merge": round(100 * cov_m)})
    # Antwortzeiten: Turn-Ende -> erstes Agent-Audio (irgendwer) und Herkunft
    turn_end = {e["turn"]: e["t"] for e in ev if e["kind"] == "turn_end"}
    frag_start = sorted(f["start"] for f in call.frags)
    op_iv = [(o.get("start"), o.get("end") or o.get("start")) for o in call.ops.values() if o.get("start")]
    rows = []
    for ti, te in sorted(turn_end.items()):
        nxt_user = next((s for s in frag_start if s > te), None)
        seg = next((g for g in call.agent_seg if g[0] >= te - 0.2), None)
        first = seg[0] if seg and (nxt_user is None or seg[0] < nxt_user + 0.05) else None
        who = None
        if first is not None:
            who = "operator" if any(a - 0.6 <= first <= (b or a) + 0.3 for a, b in op_iv) else "delta"
        # say offen zum Turn-Ende: Stand laut say_status bis te (ohne say_status, alter Stand:
        # nicht bestimmbar -> False, Delta-Latenz zählt dann alle Turns mit Delta-Antwort)
        def _open(s: dict, te: float = te) -> bool:
            st = [x for x in s["states"] if x[0] <= te]
            return bool(st) and st[-1][1] in ("queued", "requeued")
        pending = any(s["sent"] < te and _open(s) for s in call.says)
        silence = (first if first is not None else (nxt_user or call.ev[-1]["t"])) - te
        rows.append({"turn": ti, "wait": call.turns[ti][0], "end_t": te, "first_audio_t": first,
                     "latency_s": None if first is None else round(first - te, 2), "who": who,
                     "say_pending": pending, "silence_s": round(silence, 2)})
    delta_lat = [r["latency_s"] for r in rows if r["who"] == "delta" and not r["say_pending"]]
    wait_sil = [r["silence_s"] for r in rows if r["wait"]]
    # Transkript-Markierungen
    flags = []
    board_txt = " ".join(x for b in call.boards for x in [b["doing"], *b["open"], *b["done"]]).lower()
    for t, line in agent_lines:
        f = []
        if len(re.findall(r"\bclaude\b", line, re.I)) >= 2:
            f.append("Name doppelt")
        if len(WAIT_LINE.findall(line)) >= 2:
            f.append("Wartesätze gestapelt?")
        if ICH_CLAUDE.search(line):
            f.append("Ich-Form als Claude?")
        for w in PROGRESS_WORDS.findall(line):
            f.append(f"Fortschritt '{w}' prüfen")
        flags.append({"t": t, "text": line, "flags": f})
    answers = classify_answers(ev, call.turns)
    n_q = len(answers)

    return {
        "answers": answers,
        "answers_inhaltlich": sum(a["kind"] == "inhaltlich" for a in answers),
        "answers_moment": sum(a["kind"] == "moment" for a in answers),
        "answers_keine": sum(a["kind"] == "keine" for a in answers),
        "answers_operator": sum(a["kind"] == "operator" for a in answers),
        "answers_n": n_q,
        "says": says,
        "says_total": len(says),
        "says_repeated": [s["seq"] for s in says if s["repeated"]],
        "says_interrupted": [s["seq"] for s in says if s["interrupts"]],
        "interrupts_total": sum(s["interrupts"] for s in says),
        "says_delivered": sum(s["delivered"] for s in says),
        "says_lost": [s["seq"] for s in says if not s["delivered"]],
        "turns": rows,
        "delta_latency_median_s": round(statistics.median(delta_lat), 2) if delta_lat else None,
        "delta_latency_p90_s": pct(delta_lat, 0.9),
        "delta_latency_n": len(delta_lat),
        "longest_wait_silence_s": max(wait_sil) if wait_sil else None,
        "agent_lines": flags,
        "operator_lines": op_lines,
        "user_lines": [e["text"] for e in ev if e["kind"] == "transcript" and e["role"] == "user"],
        "tts_chars_spoken": sum(len(x) for x in op_lines) + sum(len(x) for _t, x in agent_lines),
        "board_text": board_txt,
    }


def classify_answers(ev: list[dict], turns: list) -> list[dict]:
    """Je Frage-Turn (ohne Begrüßung und Verabschiedung): Delta-Zeilen (role agent) zwischen
    Ende dieses Turns und Ende des nächsten. Nur Wartesätze (<= 10 Wörter, WAIT_LINE) =
    "moment"; keine Delta-Zeile, aber Operator spricht = "operator" (Delta schweigt, solange
    eine say offen ist); gar nichts = "keine"; sonst "inhaltlich"."""
    turn_end = {e["turn"]: e["t"] for e in ev if e["kind"] == "turn_end"}
    ends = [turn_end.get(i) for i in range(len(turns))]
    out = []
    for ti, te in sorted(turn_end.items()):
        if not turns[ti][0] or ti in (0, len(turns) - 1):
            continue
        nxt = next((ends[j] for j in range(ti + 1, len(ends)) if ends[j] is not None), ev[-1]["t"])
        win = [e for e in ev if e["kind"] == "transcript" and te < e["t"] <= nxt]
        lines = [e["text"] for e in win if e["role"] == "agent"]
        ops = [e["text"] for e in win if e["role"] == "operator"]
        if not lines:
            kind = "operator" if ops else "keine"
        elif all(WAIT_LINE.search(x) and len(words(x)) <= 10 for x in lines):
            kind = "moment"
        else:
            kind = "inhaltlich"
        out.append({"turn": ti, "frage": " ".join(turns[ti][1]), "kind": kind, "delta": lines,
                    "operator": ops})
    return out


def timing_lines(log: Path) -> dict:
    """`[timing]`-Zeilen des Workers (#148): play/ttft in ms ab VAD-Ende, Median + p90."""
    if not log.exists():
        return {}
    vals: dict[str, list[float]] = {"play": [], "ttft": [], "eot": []}
    seen: set[str] = set()
    n = 0
    for line in log.read_text(errors="replace").splitlines():
        # der Worker loggt jede Zeile zweimal (Text + JSON): je Turn-ID nur einmal zählen
        m = re.search(r"\[timing\] turn=(\S+)", line)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        n += 1
        for k in vals:
            m = re.search(rf"\b{k}=(\d+)", line)
            if m:
                vals[k].append(float(m.group(1)))
    text = log.read_text(errors="replace")
    applied = sum(1 for x in text.splitlines() if "[operator.activity]" in x and not x.startswith("{"))
    out: dict = {"timing_turns": n, "activity_applied": applied}
    for k, xs in vals.items():
        out[f"timing_{k}_n"] = len(xs)
        out[f"timing_{k}_median_ms"] = round(statistics.median(xs)) if xs else None
        out[f"timing_{k}_p90_ms"] = pct(xs, 0.9)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--label", default="neu")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="standard")
    ap.add_argument("--live", action="store_true", help="Gemini-Live-Worker statt Pipeline")
    ap.add_argument("--out", default="/tmp/fragcall")
    ap.add_argument("--frag-dir", default=str(Path.home() / ".cache" / "voicehook-e2e" / "fragments-v1"))
    args = ap.parse_args()
    for k in ("DEEPGRAM_API_KEY", "GOOGLE_API_KEY", "VH_E2E_GCP_SA_B64"):
        if not os.environ.get(k):
            print(f"fehlt: {k}", file=sys.stderr)
            return 2
    res = asyncio.run(run(args))
    print(json.dumps({k: res.get(k) for k in (
        "label", "scenario", "live", "sha", "says_total", "says_delivered", "says_lost",
        "answers_n", "answers_inhaltlich", "answers_moment", "answers_operator", "answers_keine",
        "delta_latency_median_s", "delta_latency_p90_s", "timing_play_median_ms", "timing_play_p90_ms",
        "timing_play_n", "says_repeated", "says_interrupted", "interrupts_total",
        "activity_applied", "longest_wait_silence_s", "deepgram")}, ensure_ascii=False))
    for a in res["answers"]:
        print(f"frage {a['turn']:>2} {a['kind']:<10} {a['frage'][:40]:<40} | {' / '.join(a['delta'])[:120]}")
    for s in res["says"]:
        print(f"say {s['seq']:>2} {s['mode']:<9} {'->'.join(s['chain']) or '-':<60} "
              f"gesprochen {s['spoken_words_pct']:>3}% {'OK' if s['delivered'] else 'VERLOREN'}")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    # os._exit: livekit-ffi bricht beim normalen Interpreter-Ende sonst mit Rust-Panic ab
    os._exit(main())
