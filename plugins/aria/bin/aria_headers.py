#!/usr/bin/env python3
"""MCP `headersHelper` — supplies the Authorization header at connection time.

WHAT THIS BUYS, AND WHY THE STATIC HEADER ALONE WAS NOT ENOUGH
--------------------------------------------------------------
`.mcp.json` can carry a static `Authorization: Bearer ${user_config.api_key}`.
That works, but it forces ONE setup path: the user must open `/plugin`, find
this plugin, and paste a 72-character key into a form. Every step of that is a
place a stranger stops.

A `headersHelper` is a command Claude Code runs at connection time whose stdout
is a JSON object of headers (docs/en/mcp). Because it can read a FILE, it lets
`/aria:connect <link>` finish the whole setup in one command — redeem the link,
write `~/.aria/credentials`, reconnect — with nothing pasted into a form.

Both paths stay live and the precedence is in `aria_credentials.py`. Dynamic
headers override static ones of the same name, so:

  key found  → this emits Authorization and it wins.
  no key     → this emits `{}`, no override happens, and the static
               `${user_config.api_key}` header (if the user set one) applies.

THE TWO RULES THIS FILE MUST NOT BREAK
--------------------------------------
1. **stdout is a JSON object and nothing else.** A stray print, a warning, a
   traceback on stdout — any of them make the header unparseable and take the
   MCP connection down. Everything diagnostic goes to stderr.
2. **Never fail closed with a non-zero exit on a missing key.** A user who has
   not connected yet should see "needs authentication" from the server, not a
   helper crash. `{}` is the correct answer to "I have no credential".

Re-run on every connection and on a 401/403 during a tool call, with a 10s
timeout and NO caching by Claude Code — so this must stay fast and do no
network I/O.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    try:
        import aria_credentials as creds

        key = creds.api_key()
    except Exception as exc:  # noqa: BLE001 — see rule 2
        print(f"[aria] could not read credentials: {exc}", file=sys.stderr)
        sys.stdout.write("{}")
        return 0

    if not key:
        # Not an error. The user has not run /aria:connect and has not pasted a
        # key. Say so on stderr (which `claude --debug` shows) and defer.
        print(
            "[aria] no key found — run /aria:connect <setup link>, "
            "or set the Aria API key in /plugin → aria",
            file=sys.stderr,
        )
        sys.stdout.write("{}")
        return 0

    headers = {"Authorization": f"Bearer {key}"}

    # WHERE THIS SESSION IS. Read from a file the SessionStart hook wrote, NOT
    # derived here — measured 2026-09-23, this process runs with `cwd` set to
    # the PLUGIN ROOT and no `CLAUDE_PROJECT_DIR`, so deriving it here would
    # announce the plugin's own checkout as the user's repo. `aria_session_state`
    # has the full measurement; the short version is that a wrong repo_key is
    # worse than none, because Aria would then target work at a session sitting
    # somewhere else.
    #
    # Absent is expected and fine: the hook and this helper start within ~1ms of
    # each other and this one often wins, so a fresh session may have no file
    # yet. Omitting the headers means "keep what I told you before", and Claude
    # Code re-runs this on every reconnect and on a 401/403.
    try:
        import aria_session_state as state

        headers.update(state.workspace_headers(os.environ.get("CLAUDE_CODE_SESSION_ID", "")))
    except Exception as exc:  # noqa: BLE001 — see rule 2: never fail the auth
        print(f"[aria] no workspace headers: {exc}", file=sys.stderr)

    # §CC-SWITCH — this computer's name, so the app can show "<session> ·
    # <machine>" and "switch to the work laptop" has something to match.
    # Latin-1 only (a header is latin-1); anything else is dropped, not sent.
    try:
        import socket

        host = socket.gethostname().strip()[:64]
        if host and host.isascii():
            headers["X-Aria-Host"] = host
    except Exception as exc:  # noqa: BLE001 — see rule 2
        print(f"[aria] no host header: {exc}", file=sys.stderr)

    sys.stdout.write(json.dumps(headers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
