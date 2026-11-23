#!/usr/bin/env python3
"""SessionStart hook — say "not connected" in words the user can act on.

THE FAILURE THIS EXISTS TO FIX (measured, 2026-09-23)
-----------------------------------------------------
Install the plugin and don't connect it, and the ONLY thing the user ever sees
is the MCP layer's generic auth error:

    ✘ Failed to connect — Server rejected the configured Authorization header
      (HTTP 401) … "To resolve: clear authentication tokens in your MCP client
      and reconnect. Your client should automatically re-register and obtain
      new tokens."

Three things are wrong with that as the user's whole experience:
  - it is the SAME message for "I have no key" and "my key is wrong", which are
    different problems with different fixes;
  - the advice is inapplicable — there are no OAuth tokens to clear, because
    this server authenticates with a bearer key and OAuth fallback is disabled
    precisely *because* a header is configured;
  - it is buried in `/mcp`, which a new user has no reason to open.

`SessionStart` stdout is added as context Claude can see and act on. So the
plugin gets to say the true thing, in its own words, at the moment the user is
actually looking at the session.

WHAT IT MUST NOT DO
-------------------
Be noisy. A connected session gets NOTHING from this hook — no output, no
context, no tokens spent. It speaks only in the one state that is broken and
silently stays quiet in every other, including states it cannot judge (a key
that exists but is revoked looks fine from here; that is the MCP layer's call
to make, and guessing would produce a false alarm every session).

It also must never block: `SessionStart` exit 2 blocks session startup. This
returns 0 unconditionally.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _record_workspace() -> None:
    """Tell the `headersHelper` where this session is. It cannot find out.

    MEASURED 2026-09-23: a `headersHelper` runs with `cwd` set to the PLUGIN
    ROOT and is given no `CLAUDE_PROJECT_DIR`, while THIS hook gets both the
    project directory and the session id. So the one process that knows writes
    it down for the one that needs it, keyed by session id so it can never be
    read by a different session in a different repo.

    Best effort, always. A session must not fail to start because git was slow.
    """
    try:
        import aria_session_state as state

        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        event = json.loads(raw or "{}")
        event = event if isinstance(event, dict) else {}
        cwd = str(event.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or "")
        session_id = str(event.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "")
        if cwd and session_id:
            state.write(session_id, cwd)
    except Exception:  # noqa: BLE001 — a broken preflight must not nag or block
        pass


def main() -> int:
    # FIRST, and before any early return. Every `return 0` below is a state in
    # which the session is still running and the MCP connection still needs to
    # say where it is — putting this after the "connected, say nothing" branch
    # would record the workspace ONLY for users who have no key.
    _record_workspace()
    try:
        import aria_credentials as creds

        # THE CLIENT FLOOR COMES FIRST, and it speaks even WITH a key. Below it,
        # `headersHelper` is ignored silently, so a key sitting in
        # `~/.aria/credentials` never reaches the MCP connection and the whole
        # thing 401s — which then gets blamed on the key, the account, or the
        # server. A version number is the only thing that distinguishes it, and
        # this hook is the only part of the plugin running early enough to say
        # so. (On a client old enough that plugin hooks do not fire at all, this
        # never runs; see MIN_CLIENT for what that measured like.)
        ver = creds.client_version()
        if ver is not None and ver < creds.MIN_CLIENT:
            sys.stdout.write(
                "The Aria plugin needs Claude Code "
                f"{creds.MIN_CLIENT_STR} or newer and this is "
                + ".".join(str(p) for p in ver)
                + ". On this version it cannot read its own key file, so the "
                "aria MCP server will report a 401 no matter what the user "
                "does. If they mention Aria or ask why it failed to connect, "
                "tell them to upgrade Claude Code, or to paste their aria_sk_ "
                "key into /plugin → aria, which works on any version. Do not "
                "bring this up otherwise."
            )
            return 0

        if creds.api_key():
            return 0  # connected (or at least credentialed) — say nothing
    except Exception:  # noqa: BLE001 — a broken preflight must not nag
        return 0

    sys.stdout.write(
        "The Aria plugin is installed but has no key, so its aria_* tools and "
        "hooks are inactive and the MCP server will report a 401. If the user "
        "mentions Aria, or asks why the aria server failed to connect, tell "
        "them: text Aria \"connect my claude code\", then run /aria:connect "
        "with the link she sends back. Do not bring this up otherwise."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
