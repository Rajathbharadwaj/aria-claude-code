#!/usr/bin/env python3
"""Where THIS session's repo is — written by the hook, read by the helper.

THE MEASUREMENT THAT FORCED THIS SHAPE (2026-09-23, Claude Code 2.1.281, probe
plugin, throwaway HOME). The obvious implementation is to derive the repo inside
`aria_headers.py` from `CLAUDE_PROJECT_DIR`, falling back to `os.getcwd()`.
Both are wrong there, and measured:

    headersHelper:  cwd = /…/<PLUGIN ROOT>        CLAUDE_PROJECT_DIR = None
    SessionStart:   cwd = /…/<the user's repo>    CLAUDE_PROJECT_DIR = the repo

The helper runs IN THE PLUGIN DIRECTORY and is not told the project at all. So
a repo identity derived there reports the PLUGIN's own checkout — for anyone who
installed from a clone of the Aria repo, that is literally `…/aria`. That is
worse than sending nothing: `goal_tools._can_run_code` accepts a `repo_key`, so
Aria would target work at a session sitting somewhere else entirely.

The SessionStart hook DOES know. So the hook writes and the helper reads.

WHY IT IS KEYED BY SESSION ID, and this is the whole safety property. A single
"last known cwd" file is a stale-value machine: open a second Claude Code in
another repo and whichever wrote last decides what BOTH of them claim. Keyed by
`CLAUDE_CODE_SESSION_ID` (present in the helper's environment — measured), a
file belonging to another session is never read. The read is therefore
CORRECT-OR-ABSENT, never wrong.

AND IT IS A RACE, measured over five cold starts: the helper ran FIRST in three
of them, the hook first in two, gap 1-2ms. So on a fresh session the helper will
often find nothing. That is fine and it is why the contract is "omit the header
rather than send an empty one" — absent means *keep what you told me before*,
and Claude Code re-runs the helper on every reconnect and on a 401/403 during a
tool call. The install flow ends in `/reload-plugins`, which is exactly such a
reconnect, so the identity lands on the connection that first has a working key.
Claiming it lands on connection one would be claiming an outcome nobody
observed.

Stdlib only. Never raises: neither caller may be taken down by this.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

# Caps match `mcp_server/presence.py::_workspace_of`, so a value is never
# truncated differently at the two ends of the wire.
CAP_CWD = 512
CAP_REPO_KEY = 400
CAP_REPO_ROOT = 1024

# Files older than this are pruned on write. A session that ended left one
# behind and nothing else ever deletes it.
_MAX_AGE_S = 2 * 24 * 3600


def sessions_dir() -> Path:
    """Resolved on every call, never at import — see `aria_credentials`."""
    return Path(os.path.expanduser("~")) / ".aria" / "sessions"


def repo_identity(cwd: str) -> tuple[str | None, str | None]:
    """`(repo_key, repo_root)` for `cwd`, or `(None, None)` if not a worktree.

    Copied from `services/mcp-memory/aria_delegate_mcp.py::_repo_identity` and
    kept identical on purpose: the worker and the plugin must produce the SAME
    key for the same checkout, or Aria sees two machines where there is one.

    The canonical REMOTE, never the folder name — `os.path.basename` is what
    turned four processes in ~/aria-delegate{,-2,-3,-4} into four look-alike
    machines.
    """
    def _git(*a: str) -> str | None:
        try:
            r = subprocess.run(  # noqa: S603
                ["git", "-C", cwd, *a], capture_output=True, text=True, timeout=3
            )
            return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
        except Exception:  # noqa: BLE001 — a missing git is just "no identity"
            return None

    root = _git("rev-parse", "--show-toplevel")
    if not root:
        return None, None
    url = _git("remote", "get-url", "origin")
    if not url:
        # A real worktree with no remote is still code-capable.
        return f"local:{root.lower()}", root
    u = re.sub(r"^git@([^:]+):", r"\1/", url.strip())
    u = re.sub(r"^[a-z+]+://", "", u)
    u = re.sub(r"\.git$", "", u)
    return u.lower().rstrip("/"), root


def _prune(d: Path) -> None:
    cutoff = time.time() - _MAX_AGE_S
    try:
        for f in d.glob("*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass


def write(session_id: str, cwd: str) -> str | None:
    """Record this session's workspace. Returns the path, or None if it did not.

    Called from `SessionStart`, which has a 5s budget — `repo_identity` shells
    out to git twice with a 3s cap each, so a pathological repo costs the hook
    its budget and nothing else: the hook returns 0 regardless and the helper
    simply finds no file.
    """
    session_id = (session_id or "").strip()
    cwd = (cwd or "").strip()
    # A session id is a uuid; anything else is refused rather than used to build
    # a path. A `..` here would write outside the directory.
    if not cwd or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id):
        return None
    repo_key, repo_root = repo_identity(cwd)
    try:
        d = sessions_dir()
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        _prune(d)
        p = d / f"{session_id}.json"
        p.write_text(
            json.dumps({"cwd": cwd, "repo_key": repo_key, "repo_root": repo_root}),
            encoding="utf-8",
        )
        return str(p)
    except Exception:  # noqa: BLE001
        return None


def read(session_id: str) -> dict:
    """This session's workspace, or `{}`. NEVER another session's."""
    session_id = (session_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id):
        return {}
    try:
        data = json.loads((sessions_dir() / f"{session_id}.json").read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — absent is the normal case, not an error
        return {}


def workspace_headers(session_id: str) -> dict:
    """The three `X-Aria-*` headers, or `{}`.

    Percent-encoded because a header is latin-1 and a path is not. The backend
    `unquote`s all three (`mcp_server/presence.py::_hdr`), and `unquote` is a
    no-op on the ASCII values that are the normal case.

    A field that is missing is OMITTED, never sent empty: the backend writes
    `workspace.X or row.X`, so an empty header and an absent one both mean
    "keep what I told you before" — and sending a blank one for a value we
    simply could not compute would be indistinguishable from a real answer.

    An OVERLONG value is omitted too, not truncated. The server caps at the
    same numbers, and a truncated path is not a shorter path — it is a
    DIFFERENT path, which is the wrong-repo failure this whole module exists
    to avoid. Nothing beats something wrong here.
    """
    state = read(session_id)
    out: dict[str, str] = {}
    for header, field, cap in (
        ("X-Aria-Cwd", "cwd", CAP_CWD),
        ("X-Aria-Repo-Key", "repo_key", CAP_REPO_KEY),
        ("X-Aria-Repo-Root", "repo_root", CAP_REPO_ROOT),
    ):
        value = (state.get(field) or "").strip()
        if not value or len(value) > cap:
            continue
        out[header] = quote(value, safe="")
    return out
