#!/usr/bin/env python3
"""One definition of where Aria's key lives and how it is read.

WHY THIS FILE EXISTS
--------------------
Four things in this plugin need the key, and they do NOT run in the same
environment, which is the whole problem:

  - `aria_headers.py`   — the MCP `headersHelper`. Runs as a bare subprocess at
                          connection time. Claude Code **strips credential
                          environment variables** from helpers supplied by a
                          plugin (docs/en/mcp, "Credential Environment
                          Variables": *"Helpers supplied by repositories/plugins
                          don't receive credential variables from your
                          environment… have the script read credentials from a
                          file or credential store"*). So a helper CANNOT rely
                          on an env var. It must read a file.
  - `aria_session_reporter.py` — a Stop hook. Hook processes DO get
                          `CLAUDE_PLUGIN_OPTION_*` exported from `userConfig`.
  - `aria_connect.py`   — writes the key after redeeming a setup link.
  - `aria_disconnect.py`— removes it.

Two different environments that can each see only half the story is exactly how
you ship a plugin where `/aria:status` says connected and the MCP server says
401. So the precedence order lives HERE, once, and all four import it.

PRECEDENCE (first hit wins)
---------------------------
  1. `CLAUDE_PLUGIN_OPTION_API_KEY` — what Claude Code exports from the
     plugin's `userConfig` into hook and MCP subprocesses. The key sits in the
     OS keychain. This is the path for someone who pasted it into `/plugin`.
  2. `ARIA_API_KEY` — a plain env var, for development and for the
     hand-wired (non-plugin) rig in `services/mcp-memory/`.
  3. `~/.aria/credentials` — written by `/aria:connect`, mode 0600. This is the
     ONLY one a `headersHelper` can actually see, so it is the one the
     one-command setup writes.

WHY A FILE AND NOT AN ENV VAR, given the usual rule
---------------------------------------------------
"Never write a secret to a file" means never write it somewhere that gets
committed or shared. `~/.aria/credentials` is in the user's HOME, outside every
git repo, mode 0600, and it is the storage the MCP documentation explicitly
names for this case. An env var would have to be exported before Claude Code
starts — which is a setup step a stranger gets wrong, and which the helper may
not even receive.

Stdlib only. Never raises: a credential read that throws would take the user's
session down with it.
"""

from __future__ import annotations

import os
from pathlib import Path

def credentials_path() -> Path:
    """Resolved on every call, never once at import.

    `Path.home()` at module scope freezes HOME as it was the first time
    anything imported this. That is wrong twice: a test that sets HOME to a
    tmpdir after import reads the real one, and a long-lived process that
    changes HOME keeps the stale path. Both look like "the key is not there".
    """
    return Path(os.path.expanduser("~")) / ".aria" / "credentials"

DEFAULT_MCP_URL = "https://mcp.aria.paralleluniverse.ai/mcp"
DEFAULT_BACKEND_URL = "https://api.aria.paralleluniverse.ai"

# THE CLIENT FLOOR, in ONE place — the README quotes this, `/aria:status`
# prints it and the preflight names it, and three copies of a version number is
# three chances to be wrong.
#
# WHY THERE IS A FLOOR AT ALL, measured 2026-09-23 against 2.0.30 in a
# throwaway HOME: the plugin is not partly broken there, it is entirely inert.
# No `aria_*` tools, no MCP error, the `SessionStart` hook never fired (a probe
# hook wrote no file at all), and `/aria:status` printed ZERO BYTES and exited
# 0. Every other failure in this plugin has one actionable sentence; that one
# had nothing, because nothing of ours ran to say it. So the floor is named
# wherever we still have a voice.
MIN_CLIENT = (2, 1, 280)
MIN_CLIENT_STR = ".".join(str(p) for p in MIN_CLIENT)


def parse_version(raw: str) -> tuple[int, ...] | None:
    """`"2.1.281 (Claude Code)"` → `(2, 1, 281)`. None if it is not a version."""
    import re

    m = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", raw or "")
    return tuple(int(g) for g in m.groups()) if m else None


def client_version() -> tuple[int, ...] | None:
    """The running Claude Code version, from the ENVIRONMENT only. Never shells.

    `CLAUDE_CODE_EXECPATH` is measured, not assumed: on 2.1.281 a plugin hook
    subprocess received
    `/home/<user>/.local/share/claude/versions/2.1.281`. This never spawns a
    process, because its callers include a `SessionStart` hook on a 5s budget
    and a node startup can eat most of that.
    """
    for name in ("CLAUDE_CODE_VERSION", "CLAUDE_CODE_EXECPATH"):
        got = parse_version(os.environ.get(name) or "")
        if got:
            return got
    return None


def parse_credentials(raw: str) -> dict:
    """`~/.aria/credentials` is KEY=VALUE lines — that is what is on disk, and
    what `scripts/start_code_worker.sh` reads.

    Parsing it as JSON raised on line one and fell into an `except`, so every
    value in it was silently ignored. JSON is still accepted for a file that
    was written that way.
    """
    s = raw.strip()
    if s.startswith("{"):
        try:
            import json

            data = json.loads(s)
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    out: dict = {}
    for line in s.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        # `export ` is not decoration. The file is written to be shell-source-able
        # (`scripts/start_code_worker.sh` sources it), so half the lines in a
        # hand-written one carry the prefix. Not stripping it silently drops
        # every value on such a line — the key resolves, the backend URL does
        # not, and the failure surfaces as a request to the wrong host.
        out[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    return out


def read_credentials_file() -> dict:
    try:
        return parse_credentials(credentials_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing/unreadable is the normal case
        return {}


def cred(name: str, default: str = "") -> str:
    """THE one definition of the precedence order. `name` is the `ARIA_*` form.

    `ARIA_API_KEY` maps to `CLAUDE_PLUGIN_OPTION_API_KEY`, because Claude Code
    exports each userConfig option as `CLAUDE_PLUGIN_OPTION_<KEY uppercased>`
    and the option key is `api_key`.
    """
    env_suffix = name.removeprefix("ARIA_")
    for env_name in (f"CLAUDE_PLUGIN_OPTION_{env_suffix}", name):
        val = (os.environ.get(env_name) or "").strip()
        if val:
            return val
    stored = read_credentials_file()
    for key in (name, *_FILE_ALIASES.get(name, ())):
        val = (stored.get(key) or "").strip()
        if val:
            return val
    return default


# A file written by hand rather than by /aria:connect may use the short names.
_FILE_ALIASES = {
    "ARIA_API_KEY": ("api_key", "key"),
    "ARIA_BACKEND_URL": ("backend_url",),
    "ARIA_MCP_URL": ("mcp_url",),
}


def api_key() -> str:
    return cred("ARIA_API_KEY")


def backend_url() -> str:
    return cred("ARIA_BACKEND_URL", DEFAULT_BACKEND_URL).rstrip("/")


def mcp_url() -> str:
    return cred("ARIA_MCP_URL", DEFAULT_MCP_URL)


def source_of_key() -> str:
    """Which of the three sources supplied the key — for `/aria:status`.

    Status output that says "connected" without saying WHERE the credential
    came from is the reason a stale keychain entry can shadow a fresh
    `/aria:connect` and look like a server problem.
    """
    if (os.environ.get("CLAUDE_PLUGIN_OPTION_API_KEY") or "").strip():
        return "plugin config (/plugin → aria)"
    if (os.environ.get("ARIA_API_KEY") or "").strip():
        return "ARIA_API_KEY environment variable"
    if api_key():
        return f"{credentials_path()}"
    return "nowhere — no key is configured"


def mask(key: str) -> str:
    """Never print a key. Last 4 only, and only when there is enough to mask."""
    key = (key or "").strip()
    if not key:
        return "(none)"
    return f"…{key[-4:]}" if len(key) > 8 else "(set)"
