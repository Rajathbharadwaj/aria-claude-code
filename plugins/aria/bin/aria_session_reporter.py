#!/usr/bin/env python3
"""Aria session reporter — a Claude Code **Stop hook**.

WHY THIS IS NOT AN MCP TOOL
---------------------------
The other half of this plugin is a *remote* MCP server
(`services/backend/aria_backend/mcp_server/`). Everything that could move there
did. This could not, for a structural reason: **an MCP tool is called BY the
session and cannot read the session.** A tool receives only the arguments the
model chose to pass; it has no handle on the transcript, the cwd, or what the
user actually asked for. A Stop hook is the inverse — Claude Code hands it the
session JSON (including `transcript_path`) on stdin every time a turn ends.

So the split is: MCP tools = what Claude Code can *ask Aria for*; this hook =
what Aria can *see about the session*. Without it Aria knows a session exists
(the held MCP connection registers presence) but not what it is working on.

It POSTs a one-line summary to `POST /v1/delegate/log`
(`services/backend/aria_backend/delegate/api.py:713`), stored as a `claude_tasks`
row with status='note' — never claimed by the worker, surfaced to Aria by the
`recent_claude_work` tool. The backend keeps only the latest 20 notes per user.

CREDENTIALS, in precedence order:
  1. `CLAUDE_PLUGIN_OPTION_API_KEY` / `CLAUDE_PLUGIN_OPTION_BACKEND_URL` —
     exported into every plugin hook process from the plugin's `userConfig`
     (`.claude-plugin/plugin.json`). This is the normal path: the key lives in
     the OS keychain, never in a config file, never on a command line. Note the
     hook is deliberately declared in *exec* form (`command` + `args`) — a
     shell-form plugin hook that references `${user_config.*}` is rejected
     outright, and reading the exported env var avoids the question entirely.
  2. `ARIA_API_KEY` / `ARIA_BACKEND_URL` from the environment — for development
     and for the hand-wired (non-plugin) setup.
  3. `~/.aria/credentials` — the pre-plugin file. Still honored so an existing
     rig keeps working; nothing writes it any more.

The hook talks to the REST backend (`api.aria...`), NOT the MCP host
(`mcp.aria...`). They are separate Cloud Run services; see docs/mcp-server.md.

Stdlib only (urllib, no deps) so it runs fast and never blocks the session.
Best-effort: any failure is swallowed — a reporting hiccup must never break CC.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aria_credentials as _creds  # noqa: E402

# ONE definition of the precedence order, shared with the MCP `headersHelper`
# (aria_headers.py) and `/aria:status` (aria_status.py). It used to be a
# private copy here, and a private copy is how `/aria:status` ends up reporting
# a key that this hook cannot actually see — the two would disagree about which
# source wins and nothing would notice.
# Re-exported so this file still answers `_cred(name, default)` — the shape
# `tests/test_delegate_mcp_client.py` checks across all four credential
# readers. It is the SHARED implementation, not a private copy: a copy here is
# exactly how /aria:status ends up reporting a key this hook cannot see.
_cred = _creds.cred

BACKEND = _creds.backend_url()
KEY = _creds.api_key()


def _flatten(content: object) -> str:
    """Pull plain text out of a message's content (str or block-list). Skips
    tool_use / tool_result blocks — we only want the human-readable prose."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        return " ".join(parts).strip()
    return ""


def typed_by_a_human(obj: dict) -> bool:
    """Was this transcript line typed by the person? Decided by STRUCTURE.

    Claude Code stamps what it injected: `isMeta` (stop-hook feedback, scheduled
    and looped prompts, skill bodies), `isCompactSummary`, and on newer builds
    `origin.kind` — "human" for the user, "task-notification" / "peer" for a
    background task finishing or another session's message. Founder, 2026-10-16:
    this hook took the LAST user line as "asked:", which in an agent team is a
    `<teammate-message>` — and Aria told the user what they "asked" from it.
    Older lines with no `origin` fall through as human; the backend's
    `scrub_note` drops those by their envelope tag.
    """
    if obj.get("isMeta") or obj.get("isCompactSummary") or obj.get("isVisibleInTranscriptOnly"):
        return False
    origin = obj.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return False
    return True


def main() -> None:
    if not KEY:
        return
    try:
        data = json.load(sys.stdin)
    except Exception:
        return

    tpath = data.get("transcript_path")
    session_id = data.get("session_id")
    cwd = data.get("cwd") or ""
    folder = os.path.basename(cwd.rstrip("/")) or "session"
    if not tpath or not os.path.exists(tpath):
        return

    last_user, last_asst = "", ""
    try:
        with open(tpath, encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                msg = obj.get("message") or obj
                role = msg.get("role") or obj.get("type")
                txt = _flatten(msg.get("content"))
                if not txt:
                    continue
                if role == "user":
                    # The turn's prompt, or nothing: never fall back to an
                    # older one of theirs under a turn someone else started.
                    last_user = txt if typed_by_a_human(obj) else ""
                elif role == "assistant":
                    last_asst = txt
    except Exception:
        return

    if not (last_user or last_asst):
        return

    summary = f"[{folder}] "
    if last_user:
        summary += f"asked: {last_user[:200]}"
    if last_asst:
        summary += (" — " if last_user else "") + f"did: {last_asst[:240]}"

    body = json.dumps(
        {"summary": summary[:900], "session_id": session_id}
    ).encode()
    req = urllib.request.Request(
        f"{BACKEND}/v1/delegate/log",
        data=body,
        headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass


if __name__ == "__main__":
    main()
