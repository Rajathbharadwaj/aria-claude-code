#!/usr/bin/env python3
"""§HOOKSTREAM — one hook event to Aria, from a command hook that never blocks.

WHY THIS EXISTS (2026-10-22). `/aria:watch` used to install `mcp_tool` hooks:
Claude Code calls `aria_session_event` on Aria's MCP server and WAITS for the
answer — on every prompt and every tool call. When that server was saturated
(Cloud Run answering `429 Rate exceeded.`), a user's session stalled
12–20 s per prompt and per tool ("UserPromptSubmit hook … timed out after
20s"). An `mcp_tool` hook cannot run in the background; a `command` hook can
(`"async": true`), so the config now runs this instead and the session never
waits on Aria at all.

What it sends is EXACTLY what the `mcp_tool` input carried (the shape
`rebuild_event` rebuilds server-side): the flat fields, the command line or
file path, and a commit sha — never `tool_response` output and never a Write's
file body. It POSTs that to the HTTP ingest (`/v1/delegate/hooks`, which
answers in ~0.1 s and fails open) with the key from `~/.aria/credentials`, so
nothing has to be put in the environment.

Never raises, never prints to stdout (a command hook's stdout can be read as
a decision). Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

#: The fields the old `mcp_tool` input interpolated, and nothing else.
FLAT = ("hook_event_name", "session_id", "cwd", "tool_name", "error", "notification_type",
        "error_type", "reason", "last_assistant_message", "prompt")


def trimmed(event: dict) -> dict:
    """The event as the MCP transport carried it — rebuild_event's shape."""
    out = {k: event[k] for k in FLAT if isinstance(event.get(k), str) and event[k]}
    ti = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    tool_input = {k: ti[k] for k in ("command", "file_path") if isinstance(ti.get(k), str) and ti[k]}
    if tool_input:
        out["tool_input"] = tool_input
    tr = event.get("tool_response") if isinstance(event.get("tool_response"), dict) else {}
    sha = (((tr.get("gitOperation") or {}).get("commit") or {}).get("sha")
           if isinstance(tr.get("gitOperation"), dict) else None)
    if isinstance(sha, str) and sha:
        out["tool_response"] = {"gitOperation": {"commit": {"sha": sha}}}
    return out


def main() -> None:
    try:
        event = json.load(sys.stdin)
        import aria_credentials as creds

        key = creds.api_key()
        if not isinstance(event, dict) or not key:
            return
        req = urllib.request.Request(
            creds.backend_url().rstrip("/") + "/v1/delegate/hooks",
            data=json.dumps(trimmed(event)).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=8).read()
    except Exception as e:  # noqa: BLE001 — telemetry: the work matters, the watching does not
        print(f"aria_hook_event: {type(e).__name__}"[:200], file=sys.stderr)


if __name__ == "__main__":
    main()
