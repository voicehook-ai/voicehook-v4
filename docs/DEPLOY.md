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
