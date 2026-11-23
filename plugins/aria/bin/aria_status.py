#!/usr/bin/env python3
"""`/aria:status` — the LOCAL half of the connection check.

WHY A SCRIPT AND NOT JUST THE SKILL ASKING THE MODEL TO LOOK
------------------------------------------------------------
Three of the four things that can be broken are invisible to the MCP tools:

  - WHICH credential is in play. A key in the OS keychain silently shadows one
    written by `/aria:connect`, so a user who reconnects after revoking sees
    the OLD key keep failing and concludes the server is down.
  - Whether the hooks can run at all (is `python3` on PATH, do the scripts
    exist).
  - Whether the endpoint the key was minted against matches the one the plugin
    is configured to talk to. Mint against a local backend, point the plugin at
    production, and the failure is a 401 that looks like a bad key.

A tool call cannot see any of that — it runs on Aria's server. So this prints
what only a local process knows, and the skill combines it with the tool calls
that cover auth and presence.

It prints a fixed set of lines so the model reports the real state instead of
summarising an impression of it. Never prints the key.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aria_credentials as creds  # noqa: E402

# Only what the PLUGIN itself wires. `aria_session_reporter.py` is no longer
# among them: its global `Stop` hook was unwired in favour of the per-repo
# `aria_session_turn` MCP hook that `/aria:watch` writes, so reporting it here
# as a live component would describe a hook that does not run.
HOOK_SCRIPTS = ("aria_preflight.py",)


def client_version() -> tuple[tuple[int, ...] | None, str]:
    """`((2,1,281), "how we know")`. Unlike the hook path, this MAY shell out.

    `/aria:status` runs from a Bash tool with no 5s budget, so when the
    environment does not carry the version it is worth one `claude --version`
    to answer the question that decides everything else — a client below
    `MIN_CLIENT` runs NONE of this plugin, and on it "no key" and "no tools"
    are both false alarms.
    """
    got = creds.client_version()
    if got:
        return got, "environment"
    exe = shutil.which("claude")
    if not exe:
        return None, "claude not on PATH"
    try:
        out = subprocess.run(  # noqa: S603
            [exe, "--version"], capture_output=True, text=True, timeout=20
        ).stdout
    except Exception as e:  # noqa: BLE001
        return None, f"could not run claude --version ({e})"
    got = creds.parse_version(out)
    return (got, "claude --version") if got else (None, "claude --version said nothing")


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    key = creds.api_key()

    # FIRST LINE, ALWAYS, and first on purpose. On a client below the floor
    # nothing else in this report means anything: the MCP server is not
    # registered, the hooks do not fire, and the skills do not load — so "no
    # aria_* tools" is the client's age, not a broken key, and reporting it as
    # a key problem sends someone to reconnect an account that was fine.
    ver, how = client_version()
    if ver is None:
        print(f"claude code  : UNKNOWN ({how}) — needs {creds.MIN_CLIENT_STR} or newer")
    elif ver < creds.MIN_CLIENT:
        print(
            "claude code  : "
            + ".".join(str(p) for p in ver)
            + f" — TOO OLD. This plugin needs {creds.MIN_CLIENT_STR} or newer. "
            "Below that it does not load at all: no aria_* tools, no hooks, no "
            "MCP error. Upgrade Claude Code, then re-run /aria:status."
        )
    else:
        print("claude code  : " + ".".join(str(p) for p in ver) + " — ok")

    print(f"key          : {creds.mask(key)}")
    print(f"key source   : {creds.source_of_key()}")
    print(f"mcp endpoint : {creds.mcp_url()}")
    print(f"backend      : {creds.backend_url()}")

    interp = shutil.which("python3") or shutil.which("python")
    print(f"python3      : {interp or 'NOT FOUND ON PATH — hooks cannot run'}")

    for name in HOOK_SCRIPTS:
        p = os.path.join(here, name)
        print(f"hook script  : {'present' if os.path.exists(p) else 'MISSING'} ({name})")

    # A key minted against one host and offered to another is a 401 with no
    # clue attached. Name it here rather than let it surface as "auth failed".
    #
    # The comparison that matters is FILE vs PLUGIN CONFIG, not file vs
    # `creds.mcp_url()` — that resolver reads the file itself, so comparing the
    # two compares a value with itself and the warning could never fire. The
    # endpoint the MCP connection actually dials comes from
    # `${user_config.mcp_url}` in .mcp.json; the file's copy records only what
    # the key was minted against and never reaches the connection.
    #
    # And it only speaks when it can SEE the plugin's setting. Falling back to
    # the manifest default when `CLAUDE_PLUGIN_OPTION_MCP_URL` is absent turns
    # this into a guess, and it fires on anyone who legitimately set a custom
    # endpoint — a false alarm in a status report is worse than no alarm,
    # because it sends them to "fix" the one thing that was already right.
    minted_for = (creds.read_credentials_file().get("ARIA_MCP_URL") or "").strip()
    configured = (os.environ.get("CLAUDE_PLUGIN_OPTION_MCP_URL") or "").strip()
    if minted_for and configured and minted_for.rstrip("/") != configured.rstrip("/"):
        print(
            f"MISMATCH     : this key was minted against {minted_for} but the plugin "
            f"dials {configured} — that is a 401 that looks like a bad key. Set the "
            "Aria MCP endpoint in /plugin → aria to match."
        )

    if not os.environ.get("CLAUDE_PLUGIN_OPTION_API_KEY") and not os.environ.get(
        "CLAUDE_PLUGIN_OPTION_MCP_URL"
    ):
        # Be explicit about the blind spot instead of implying completeness.
        # Claude Code does not export `CLAUDE_PLUGIN_OPTION_*` into Bash tool
        # commands (only into hook and MCP subprocesses), so a key pasted into
        # /plugin is INVISIBLE here. Reporting "key source: credentials file"
        # as if that settled it is how a stale keychain entry gets blamed on
        # the server. The MCP tool calls in the skill are what settle it.
        print(
            "note         : values set in /plugin → aria are not visible to this "
            "check (Claude Code does not export them to Bash). The aria_* tool "
            "calls below are what prove which key the server actually accepted."
        )

    if not key:
        print(
            "NOT CONNECTED: no key anywhere. Text Aria \"connect my claude code\" and "
            "run /aria:connect with the link she sends."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
