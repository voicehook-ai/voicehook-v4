"""voicehook v4 CLI helpers.

Today: just invite-code minting + URL formatting (the bare minimum so the
skill can hand a user a working /r/<slug>?invite=… URL).

    python -m agent.cli invite <room> [--ttl 3600] [--base https://voicehook.ai]

Returns one line: the join URL. Stdout is the URL only; logs go to stderr.
"""

from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import urlencode

from .slug import is_valid_slug
from .tokens import mint_invite


def cmd_invite(args: argparse.Namespace) -> int:
    secret = os.environ.get("INVITE_SECRET")
    if not secret:
        print("INVITE_SECRET env var not set", file=sys.stderr)
        return 2
    # Root cause of the 2026-05-29 "broken call": invites were minted for slugs
    # the client auto-join silently rejects (e.g. `nvu-GKJM`). Refuse here so a
    # bad slug fails loud at mint time instead of as a dead deeplink.
    if not is_valid_slug(args.room):
        print(
            f"invalid room slug {args.room!r}: need 3 lowercase words + a 4-8 char "
            "suffix (e.g. drift-signal-crisp-PDM5) — the client would reject it",
            file=sys.stderr,
        )
        return 2
    code = mint_invite(args.room, ttl_seconds=args.ttl, secret=secret)
    qs = urlencode({"invite": code})
    print(f"{args.base.rstrip('/')}/r/{args.room}?{qs}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vh", description="voicehook v4 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    inv = sub.add_parser("invite", help="mint a signed invite + print join URL")
    inv.add_argument("room", help="room slug, e.g. drift-signal-crisp-PDM5")
    inv.add_argument("--ttl", type=int, default=3600, help="seconds (default 3600)")
    inv.add_argument("--base", default="https://voicehook.ai", help="frontend base URL")
    inv.set_defaults(func=cmd_invite)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
