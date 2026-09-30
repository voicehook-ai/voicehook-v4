#!/usr/bin/env bash
# lk_rooms.sh — read-only LiveKit room audit on the voicehook prod box.
# Lists all rooms (name, age, participants, identities/kinds) via box-local
# twirp RoomService. Keys never leave the box (read from /opt/voicehook/.env
# by python3 on the box). SSH key only via orb -> ssh-agent (never on disk).
#
# Usage: lk_rooms.sh [--delete-stale] [--stale-min N] [--room NAME [--delete-room]]
#   --delete-stale  DeleteRoom for rooms with no human participant (only
#                   agent-kind / voice-ai*, or empty) and older than N min.
#   --stale-min N   staleness threshold in minutes (default 10)
#   --room NAME     only report NAME; prints a machine line
#                   "ROOMSTATE name=<n> present=0|1 participants=<n> humans=<n>"
#   --delete-room   with --room: DeleteRoom NAME if it has no human participant
#                   (used by tests/e2e/real_call.py for its OWN test room).
# Exit codes: 0 ok, 3 SSH/key setup failed (nothing was checked).
set -euo pipefail

HOST="${VH_HOST:-root@voicehook.ai}"
KH="/tmp/claude-1000/-home-oliver/8a041989-a8c3-41cb-b574-6278de6d844a/scratchpad/known_hosts_voicehook"
DELETE=0
STALE_MIN=10
ROOM=""
DELETE_ROOM=0
while [ $# -gt 0 ]; do
  case "$1" in
    --delete-stale) DELETE=1 ;;
    --stale-min) STALE_MIN="$2"; shift ;;
    --room) ROOM="$2"; shift ;;
    --delete-room) DELETE_ROOM=1 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
  shift
done

eval "$(ssh-agent -s)" >/dev/null
trap 'ssh-agent -k >/dev/null 2>&1 || true' EXIT
if ! orbctl get voicehook_v4_ssh_key | ssh-add - >/dev/null 2>&1; then
  echo "SSHKEY_FAIL: orb entry voicehook_v4_ssh_key not loadable by ssh-add" >&2
  exit 3
fi
case "$ROOM" in *[!A-Za-z0-9_-]*) echo "bad room name" >&2; exit 2 ;; esac

timeout 120 ssh -o BatchMode=yes -o ConnectTimeout=15 \
  -o UserKnownHostsFile="$KH" -o StrictHostKeyChecking=accept-new \
  "$HOST" "DELETE=$DELETE STALE_MIN=$STALE_MIN ROOM=$ROOM DELETE_ROOM=$DELETE_ROOM timeout 90 python3 -" <<'PY'
import base64, hashlib, hmac, json, os, time, urllib.request

DEADLINE = time.time() + 80  # internal hard deadline
BASE = "http://127.0.0.1:7880/twirp/livekit.RoomService/"
DELETE = os.environ.get("DELETE") == "1"
STALE_S = int(os.environ.get("STALE_MIN", "10")) * 60
KIND = {0: "STANDARD", 1: "INGRESS", 2: "EGRESS", 3: "SIP", 4: "AGENT"}

env = {}
for line in open("/opt/voicehook/.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
KEY, SECRET = env["LIVEKIT_API_KEY"], env["LIVEKIT_API_SECRET"]

def b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def jwt(video):
    now = int(time.time())
    h = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = b64(json.dumps({"iss": KEY, "sub": "audit", "nbf": now - 5, "exp": now + 300, "video": video}).encode())
    s = b64(hmac.new(SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{s}"

def call(method, body, video):
    if time.time() > DEADLINE:
        raise SystemExit("internal deadline hit")
    req = urllib.request.Request(BASE + method, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + jwt(video)})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")

def g(d, *names, default=None):
    for n in names:
        if n in d: return d[n]
    return default

def kind(p):
    k = g(p, "kind", default=0)
    return k if isinstance(k, str) else KIND.get(k, str(k))

def is_agentish(p):
    return kind(p) == "AGENT" or str(g(p, "identity", default="")).startswith("voice-ai")

def snapshot(tag):
    rooms = g(call("ListRooms", {}, {"roomList": True}), "rooms", default=[]) or []
    now = time.time()
    out = []
    print(f"== {tag}: {len(rooms)} room(s) @ {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(now))}")
    for r in rooms:
        name = r["name"]
        ct = int(g(r, "creation_time", "creationTime", default=0) or 0)
        age = now - ct if ct else -1
        parts = g(call("ListParticipants", {"room": name}, {"roomAdmin": True, "room": name}),
                  "participants", default=[]) or []
        desc = []
        for p in parts:
            jt = int(g(p, "joined_at", "joinedAt", default=0) or 0)
            desc.append(f"{g(p,'identity',default='?')}[{kind(p)}, in {int((now-jt)/60) if jt else '?'}m]")
        human = [p for p in parts if not is_agentish(p)]
        stale = (not human) and age > STALE_S
        print(f"  {name:40s} age={int(age/60) if age>=0 else '?'}m  n={g(r,'num_participants','numParticipants',default=0)}"
              f"  parts={', '.join(desc) or '-'}  empty_timeout={g(r,'empty_timeout','emptyTimeout',default='?')}"
              f"  departure_timeout={g(r,'departure_timeout','departureTimeout',default='?')}  STALE={stale}")
        out.append((name, stale))
    return out

ROOM = os.environ.get("ROOM", "")
if ROOM:
    def one():
        rooms = g(call("ListRooms", {"names": [ROOM]}, {"roomList": True}), "rooms", default=[]) or []
        rooms = [r for r in rooms if r.get("name") == ROOM]
        if not rooms:
            print(f"ROOMSTATE name={ROOM} present=0 participants=0 humans=0")
            return 0, []
        parts = g(call("ListParticipants", {"room": ROOM}, {"roomAdmin": True, "room": ROOM}),
                  "participants", default=[]) or []
        human = [p for p in parts if not is_agentish(p)]
        print(f"  parts={', '.join(g(p,'identity',default='?')+'['+kind(p)+']' for p in parts) or '-'}")
        print(f"ROOMSTATE name={ROOM} present=1 participants={len(parts)} humans={len(human)}")
        return len(parts), human
    n, human = one()
    if os.environ.get("DELETE_ROOM") == "1" and n >= 0:
        if human:
            print(f"  NOT deleting {ROOM}: human participant(s) present")
        else:
            try:
                call("DeleteRoom", {"room": ROOM}, {"roomCreate": True})
                print(f"  DELETED {ROOM}")
            except Exception as e:
                print(f"  DeleteRoom {ROOM}: {type(e).__name__}")
            time.sleep(2)
            one()
    raise SystemExit(0)

before = snapshot("BEFORE")
if DELETE:
    for name, stale in before:
        if stale:
            call("DeleteRoom", {"room": name}, {"roomCreate": True})
            print(f"  DELETED {name}")
    time.sleep(2)
    snapshot("AFTER")
PY
