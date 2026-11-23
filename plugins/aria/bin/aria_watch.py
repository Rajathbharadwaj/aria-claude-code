#!/usr/bin/env python3
"""`/aria:watch` — turn the event stream ON for ONE repo. And `--off`.

WHY THIS IS PER-REPO AND NOT SHIPPED IN THE PLUGIN
--------------------------------------------------
The plugin could ship these hooks in `hooks/hooks.json` and they would apply
everywhere. That is exactly why it doesn't. A hook config is global to a
session, so a plugin that ships this streams **every session the user ever
runs** — their client work, their employer's code, someone else's repo they
cloned to read. The server-side gates (`ARIA_OBJECTIVE_USERS`, and a live
objective) decide what is STORED; they do nothing about what LEAVES the
machine, and the leaving is the part that matters.

So switching it on is a deliberate, per-repo act with a visible artifact: a
block in that repo's `.claude/settings.json`. You can see it, diff it, and
delete it. `--off` removes it.

The block contains **no secret** — the header is the literal string
`Bearer $ARIA_API_KEY`, interpolated at hook time from the environment and only
because the same block lists it in `allowedEnvVars`. So the file is safe to
commit, and committing it is a reasonable thing for a team to do on purpose.

THE CONFIG IS FETCHED, NOT HARD-CODED
-------------------------------------
`aria_backend.delegate.hooks.recommended_hook_config()` is the one definition
of what the ingest accepts, and a config naming an event the server ignores is
the persona/tool-binding drift class wearing a different hat. A copy of that
JSON living here would drift the first time an event is added.

So this asks the backend for it and falls back to `_BUNDLED` only if the
endpoint isn't there yet — and when it falls back it SAYS SO, rather than
letting a stale copy look authoritative.

TWO TRANSPORTS, and the split matters here
------------------------------------------
`Stop` is an `mcp_tool` call, everything else is HTTP. That is the backend's
design, and it has a consequence this script has to handle: the MCP hook rides
the connection that `headersHelper` already authenticates from
`~/.aria/credentials`, so **end-of-turn summaries work with no `ARIA_API_KEY`
in the environment**. Only the HTTP hooks need the env var. So "not connected"
is not one state here, it is two, and the output says which.

The plugin's own global `Stop` hook was unwired when this landed. Keeping both
double-reported every turn — measured: two `claude_tasks` rows for one turn,
`[project] asked: …` from the plugin's hook and `[project] turn ended: …` from
this one.

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aria_credentials as creds  # noqa: E402

INGEST_PATH = "/v1/delegate/hooks"
CONFIG_PATH = "/v1/delegate/hooks/config"
CONSENT_PATH = "/v1/delegate/watch/consent"
MCP_TURN_TOOL = "aria_session_turn"
MCP_SERVER_ID = ""  # set in main() once derived; see is_ours

# Nothing is skipped any more. `Stop` used to be, while the server was still
# deciding its shape; it is now an `mcp_tool` call and the plugin's own global
# Stop hook has been unwired in its favour, so there is no double-report left
# to avoid. Kept as a named, empty knob because the reason it existed will
# recur the next time an event is mid-flight.
_SKIP_EVENTS: tuple[str, ...] = ()

# Fallback ONLY. Mirrors `recommended_hook_config` as of 2026-09-23; used when
# the backend has no config endpoint, and always announced as a fallback.
_BUNDLED_TOOLS = "Bash|Write|Edit|MultiEdit|NotebookEdit"
_BUNDLED_EVENTS = {
    "PreToolUse": _BUNDLED_TOOLS,
    "PostToolUse": _BUNDLED_TOOLS,
    "PostToolUseFailure": _BUNDLED_TOOLS,
    "Notification": "permission_prompt|agent_needs_input",
    "StopFailure": None,
    "SessionEnd": None,
    "UserPromptSubmit": None,
}

# What the matchers let through, spelled out. Printed every time this runs,
# because "visible when Aria is watching" means the user can see WHAT, not just
# THAT. These names come from the matcher strings above; if those change, this
# is generated from them (see `_describe`), never hand-maintained alongside.
_NEVER_STREAMED = (
    "Read, Grep, Glob, Task, WebFetch, WebSearch, TodoWrite, and every MCP tool"
)

# BUMP THIS WHENEVER THE TEXT BELOW CHANGES. It is stored with every consent,
# so that "did they agree to THIS behaviour?" stays answerable instead of
# assumed — an old acceptance must never silently cover new behaviour.
CONSENT_VERSION = "2026-10-01.1"

# EVERY LINE BELOW IS A FIELD NAME OUT OF `recommended_hook_config`, read from
# the source rather than from the design. On `transport=mcp` the `input` blocks
# name these eleven and nothing else:
#
#   hook_event_name  session_id  cwd            (_COMMON, on every event)
#   tool_name  command  file_path  commit_sha  error    (tool events)
#   notification_type  (Notification)   error_type  (StopFailure)
#   summary            (Stop — ${last_assistant_message})
#   prompt             (UserPromptSubmit — ${prompt}; added 2026-10-01.1, and the
#                       backend keeps it only under a consent of that version or
#                       later — `hooks.PROMPT_CONSENT_FROM`)
#
# `tool_input.content` and `tool_response` are NOT among them, which is why
# "no file contents" is stated flatly rather than hedged.
#
# AND `error` IS `${error}` — for a Bash tool that failed, that is the command's
# own error output, prefixed "Exit code N". The previous version of this screen
# promised "command output" never leaves AND admitted a failed command's error
# text does. A tester's own model caught the contradiction and flagged it to
# the user unprompted, which is precisely where a cautious person uninstalls.
# So the promise is narrowed to what is true: the output of a command that
# SUCCEEDS never leaves. A failing one's does.
CONSENT_TEXT = """\
─────────────────────────────────────────────────────────────────────────
  Let Aria watch this repo?

  Connecting Aria to this repo lets her watch what this session does — the
  commands it runs, the tests, the commits — and act on your behalf using
  what she learns. She'll text you when she needs a decision.

  By connecting, you confirm you have the right to share what's in this
  repo.

  WHAT LEAVES THIS MACHINE — these fields, and no others
    · the shell commands you run: the command LINE, never its output
    · the PATHS of files you write or edit
    · commit SHAs, the working directory, and a session id
    · the assistant's own last message, as an end-of-turn summary
    · each prompt you type. The whole prompt travels; Aria keeps only its
      first line (about 100 characters) as this session's current task,
      so she can say what it is working on right now
    · when a command FAILS, the error text Claude Code reports for it.
      That IS that command's own error output, and it is read for the
      exit code. A command that succeeds sends nothing of its output.

  WHAT NEVER LEAVES
    · file contents — not one byte
    · the output of any command that succeeds
    · anything from Read, Grep, Glob, Task, WebFetch, WebSearch, TodoWrite
      or any MCP tool: those are not watched at all

  Aria keeps what she needs to track the work. She doesn't train models on
  it. /aria:unwatch withdraws your consent and stops the stream at once —
  it does NOT delete what was already recorded; ask Aria to delete that.

  Turn it on:  /aria:watch yes        Stop it:  /aria:unwatch
─────────────────────────────────────────────────────────────────────────"""


def mcp_server_id() -> str:
    """The name a hook must use to reach THIS plugin's MCP server.

    MEASURED, because getting it wrong fails silently and looks like nothing.
    The backend's `recommended_hook_config` defaults to `mcp_server="aria"`,
    which is right for someone who ran the standalone
    `claude mcp add --transport http aria …` one-liner. Inside a plugin the
    same server is registered as `plugin:<plugin>:<server>`, and a Stop hook
    naming the bare `aria` produced **no row and no error** — verified against
    a real session on 2026-09-23: `server: "aria"` → nothing; the same hook
    with `server: "plugin:aria:aria"` → the turn row appeared.

    So it is derived from the two manifests rather than assumed: whatever the
    plugin is renamed to, this follows.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    plugin = "aria"
    server = "aria"
    try:
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            plugin = json.load(fh).get("name") or plugin
    except Exception:  # noqa: BLE001
        pass
    try:
        with open(os.path.join(root, ".mcp.json"), encoding="utf-8") as fh:
            names = list((json.load(fh).get("mcpServers") or {}).keys())
        if names:
            server = names[0]
    except Exception:  # noqa: BLE001
        pass
    return f"plugin:{plugin}:{server}"


def retarget_mcp_hooks(cfg: dict, server_id: str) -> int:
    """Point every `mcp_tool` hook at this plugin's server. Returns how many.

    Applied to a FETCHED config too, not just the bundled one — the endpoint
    will hand back the backend's default `"aria"`, which is exactly the name
    that silently does nothing here.
    """
    n = 0
    for entries in cfg.get("hooks", {}).values():
        for entry in entries:
            for hook in entry.get("hooks", []):
                if isinstance(hook, dict) and hook.get("type") == "mcp_tool":
                    hook["server"] = server_id
                    n += 1
    return n


def _bundled(backend: str) -> dict:
    url = backend.rstrip("/") + INGEST_PATH
    http = {
        "type": "http",
        "url": url,
        "timeout": 5,
        "headers": {"Authorization": "Bearer $ARIA_API_KEY"},
        "allowedEnvVars": ["ARIA_API_KEY"],
    }
    out: dict = {}
    for event, matcher in _BUNDLED_EVENTS.items():
        entry: dict = {"hooks": [dict(http)]}
        if matcher:
            entry["matcher"] = matcher
        out[event] = [entry]
    # Stop rides the MCP connection instead of HTTP — "here is what I just
    # did", end of turn, through a transport that already knows who the user
    # is. That is also why Stop keeps working when ARIA_API_KEY is unset: the
    # MCP connection authenticates from ~/.aria/credentials via headersHelper,
    # and only the HTTP hooks need the environment variable.
    out["Stop"] = [
        {
            "hooks": [
                {
                    "type": "mcp_tool",
                    "server": "aria",  # retarget_mcp_hooks fixes this
                    "tool": "aria_session_turn",
                    "input": {
                        "summary": "${last_assistant_message}",
                        "session_id": "${session_id}",
                        "cwd": "${cwd}",
                    },
                    "timeout": 20,
                }
            ]
        }
    ]
    return {"hooks": out}


def fetch_config(
    backend: str, *, transport: str, server_id: str, timeout: float = 10.0
) -> tuple[dict, str, str]:
    """Return (config, provenance, transport_actually_used).

    `transport` and `mcp_server` are QUERY PARAMETERS, not something to patch
    into the answer afterwards: the server refuses `transport=mcp` without an
    id rather than emitting a hook that would silently never fire, and that
    refusal is information the user needs.

    Falling back matters more now than it did when both transports were the
    same shape. A bundled fallback can only produce the HTTP form, which needs
    `ARIA_API_KEY` exported — so a silent fallback would turn a zero-step
    install into a one-step one without saying so. The transport actually used
    is returned and printed.
    """
    params = urllib.parse.urlencode({"transport": transport, "mcp_server": server_id})
    try:
        req = urllib.request.Request(f"{backend.rstrip('/')}{CONFIG_PATH}?{params}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        if isinstance(data, dict) and isinstance(data.get("hooks"), dict):
            return data, "the backend (authoritative)", transport
    except urllib.error.HTTPError as e:
        if e.code == 400:
            # The server rejected the combination — most likely transport=mcp
            # with an id it would not accept. Say what it said.
            detail = ""
            try:
                detail = (json.loads(e.read().decode("utf-8")) or {}).get("detail", "")
            except Exception:  # noqa: BLE001
                pass
            return _bundled(backend), f"a bundled copy (backend refused: {detail})", "http"
        if e.code != 404:
            return _bundled(backend), f"a bundled copy (backend returned HTTP {e.code})", "http"
    except Exception:  # noqa: BLE001
        return _bundled(backend), "a bundled copy (could not reach the backend)", "http"
    return _bundled(backend), "a bundled copy (backend has no config endpoint yet)", "http"


# ── never block the session (2026-10-22) ─────────────────────────────────────
# An `mcp_tool` hook makes Claude Code WAIT for Aria's MCP server on every
# prompt and tool call; when that server was saturated (Cloud Run 429 "Rate
# exceeded.") a user's session stalled 12–20 s per step. A `command` hook
# can run in the background (`"async": true`), so every `mcp_tool` hook the
# config names becomes one that runs aria_hook_event.py — same events, same
# matchers, the same trimmed fields, posted to the HTTP ingest.
HOOK_SCRIPT_NAME = "aria_hook_event.py"
#: A stable home for the script: a repo's settings can't name the plugin's own
#: (versioned) directory, and must not carry one user's absolute path.
HOOK_HOME = os.path.join(os.path.expanduser("~"), ".aria", "bin")
HOOK_COMMAND = f'python3 "$HOME/.aria/bin/{HOOK_SCRIPT_NAME}"'


def install_hook_script() -> None:
    """Copy the script and its credentials reader to ~/.aria/bin."""
    here = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(HOOK_HOME, exist_ok=True)
    for name in (HOOK_SCRIPT_NAME, "aria_credentials.py"):
        with open(os.path.join(here, name), "rb") as src, open(os.path.join(HOOK_HOME, name), "wb") as dst:
            dst.write(src.read())


def detach(cfg: dict) -> int:
    """Every `mcp_tool` hook → an async command hook. Returns how many."""
    n = 0
    for entries in (cfg.get("hooks") or {}).values():
        for entry in entries if isinstance(entries, list) else []:
            hooks = entry.get("hooks") if isinstance(entry, dict) else None
            for i, h in enumerate(hooks or []):
                if isinstance(h, dict) and h.get("type") == "mcp_tool":
                    hooks[i] = {"type": "command", "command": HOOK_COMMAND, "async": True, "timeout": 15}
                    n += 1
    return n


def is_ours(entry: dict) -> bool:
    """An entry is ours if every hook in it posts to the ingest path.

    Matching on the URL rather than a marker key keeps this working against a
    block the backend generated, which knows nothing about any marker we might
    have invented — and a marker we add would have to survive the user editing
    the file, which it would not.
    """
    hooks = entry.get("hooks")
    if not isinstance(hooks, list) or not hooks:
        return False

    def ours(h: object) -> bool:
        if not isinstance(h, dict):
            return False
        if str(h.get("url", "")).endswith(INGEST_PATH):
            return True
        if h.get("type") == "command" and HOOK_SCRIPT_NAME in str(h.get("command", "")):
            return True
        # An `mcp_tool` hook carries no url at all. Matching only on url left
        # the Stop hook behind on --off — the switch reported "no longer
        # watching" while every turn kept reporting.
        #
        # And do NOT enumerate tool names. The first version listed only
        # `aria_session_turn`; the backend then added `aria_session_event` for
        # the MCP transport, and overnight this stopped recognising six of the
        # seven hooks — so --off left them behind AND re-running stacked a
        # duplicate of each (measured: two identical PostToolUse entries).
        # Ownership is "an mcp_tool call into OUR server", which stays true
        # however many tools the backend grows.
        if h.get("type") != "mcp_tool":
            return False
        server = str(h.get("server", ""))
        return server == MCP_SERVER_ID or str(h.get("tool", "")).startswith("aria_")

    return all(ours(h) for h in hooks)


def record_consent(backend: str, key: str, repo: str) -> tuple[bool, str, dict]:
    """POST the consent BEFORE anything is written. `(ok, sentence, verdict)`.

    THE THIRD RETURN IS THE ANSWER TO "WILL THIS DO ANYTHING". The backend
    computes it from `hooks.consent_gate` — the same function the ingest
    refuses with — and hands it back on the 201 as `may_stream` /
    `may_stream_reason` / `may_stream_detail`. Reading it off the response we
    already make is better than any probe: it is per-repo, it covers the
    consent row as well as the server flag, and it cannot drift from the rule
    because it IS the rule.

    An OLDER backend (prod, measured 2026-09-23) answers 201 with none of those
    fields. `{}` then, and the caller falls back to the live MCP probe rather
    than assuming either answer.

    Server-side, because a consent we cannot produce later is one we did not
    get — and a file on the consenting machine is not producible: it is
    editable by the person it would be produced against and gone with the
    laptop. If this fails, `/aria:watch` writes NOTHING; turning the stream on
    with no record of permission is the failure this whole screen exists to
    prevent.
    """
    body = json.dumps({"repo": repo, "consent_version": CONSENT_VERSION}).encode()
    req = urllib.request.Request(
        backend.rstrip("/") + CONSENT_PATH,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        verdict = (
            {
                "may_stream": data["may_stream"],
                "reason": data.get("may_stream_reason"),
                "detail": data.get("may_stream_detail"),
            }
            if isinstance(data, dict) and isinstance(data.get("may_stream"), bool)
            else {}
        )
        return (
            True,
            f"Consent {CONSENT_VERSION} recorded (id {data.get('id', '?')}).",
            verdict,
        )
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, (
                "Aria did not recognise this machine's key, so your consent could "
                "not be recorded. Run /aria:connect first."
            ), {}
        if e.code == 404:
            return False, (
                f"{backend} cannot record consent — it is running a backend older "
                "than this plugin. Nothing was turned on."
            ), {}
        if e.code in (405, 501):
            # Same call as `aria_connect.py`: Aria's backend accepts POST here,
            # so a refusal of the METHOD means this is not Aria's backend.
            return False, (
                f"{backend} answered but refused to accept a POST there (HTTP "
                f"{e.code}) — that is not Aria's backend. Something else is on "
                "that address. Fix Aria backend in /plugin → aria (the default "
                "is https://api.aria.paralleluniverse.ai). Nothing was turned on."
            ), {}
        return False, f"Aria's backend refused to record the consent (HTTP {e.code}).", {}
    except urllib.error.URLError as e:
        return False, f"Could not reach {backend} to record your consent ({e.reason}).", {}
    except Exception as e:  # noqa: BLE001
        return False, f"Could not record your consent: {e}", {}


def revoke_consent(backend: str, key: str, repo: str) -> str:
    params = urllib.parse.urlencode({"repo": repo})
    req = urllib.request.Request(
        f"{backend.rstrip('/')}{CONSENT_PATH}?{params}",
        headers={"Authorization": f"Bearer {key}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            n = (json.loads(resp.read().decode("utf-8") or "{}")).get("revoked", 0)
        return f"Withdrew {n} consent record(s) for this repo."
    except Exception as e:  # noqa: BLE001
        # The hooks still come out. Say the consent record is unchanged rather
        # than implying the whole thing was undone.
        return (
            f"Could not withdraw the consent record on Aria's side ({e}). The hooks "
            "are removed, so nothing more is sent; the record still shows granted."
        )


def _refuse_dark(repo: str, why: str, *, recorded: bool) -> None:
    """The one shape of "no" this file has, so neither caller can soften it.

    It goes to STDERR and it says NOT watching first, because the failure being
    fixed is a tick nobody could back: `/aria:watch yes` printed "Aria is now
    watching <repo>" for an account whose every event was discarded server-side
    (measured on a cold install, 2026-09-23 — five tool calls, five 200s,
    nothing stored). The word "watching" may not appear in a success-shaped
    sentence unless something confirmed it.
    """
    tail = (
        "Your consent is recorded, so nothing else needs redoing once it is "
        "switched on."
        if recorded
        else "Nothing was turned on and no consent was recorded."
    )
    print(f"Aria is NOT watching {repo}. {tail}", file=sys.stderr)
    print(why, file=sys.stderr)


def tools_named_by(cfg: dict) -> set[str]:
    return {
        str(h.get("tool"))
        for entries in cfg.get("hooks", {}).values()
        for entry in entries
        for h in entry.get("hooks", [])
        if isinstance(h, dict) and h.get("type") == "mcp_tool" and h.get("tool")
    }


# What `aria_session_event` / `aria_session_turn` answer, and what each means
# for whether turning this on would do anything. The tools return a bare word
# in `ok (<outcome>)` — see `mcp_server/tools.py`; it is deliberately bare
# because Claude Code parses a hook tool's return like command-hook stdout.
#
#   stored             attached to a live objective — kept
#   noted              unattached stream on — kept as a background note
#   dropped-unattached unattached stream ON, this particular event not kept
#   stream-off         THE USER IS NOT ALLOWED TO STREAM. Nothing is ever kept.
_STREAM_ACCEPTED = ("stored", "noted", "dropped-unattached")
_STREAM_OFF = "stream-off"

# The probe event. SessionEnd is chosen ON PURPOSE over every other shape:
# `classify` turns it into a Kept that is NOT `note_worthy` and does NOT carry
# `clears_question`, so on a user whose stream IS on it is counted and
# discarded — it writes no note row, evicts nothing from the 20-row note log,
# and cannot close an open question on a live objective. It still travels the
# whole path, so `stream-off` comes back from the same gate a real event hits.
_PROBE = {
    "hook_event_name": "SessionEnd",
    "reason": "aria-watch preflight (no session ended)",
}


def _last_json(body: str) -> dict | None:
    """Streamable HTTP may answer as SSE; take the last JSON object either way."""
    payload = None
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
    return payload if isinstance(payload, dict) else None


def probe_server(
    mcp_url: str, key: str, timeout: float = 15.0
) -> tuple[set[str] | None, str | None]:
    """ONE MCP session that answers both questions. `(tools, stream_outcome)`.

    `None` for either half means "could not ask" — which is not the same as a
    bad answer and must never be reported as one.

    QUESTION 1 — WHICH TOOLS EXIST. A hook naming a tool the server does not
    expose fires NOTHING: no row, no error, no log line, no notice in the
    session. Measured 2026-09-23 by pointing a PostToolUse hook at
    `aria_tool_that_does_not_exist` — the turn row still arrived, the receipt
    silently did not, and Claude Code said nothing at all. That is how a stale
    server produces a phantom bug: a backend started before
    `aria_session_event` existed served a config naming it, `Stop` (an older
    tool) kept working, and "Stop yes, everything else silent" reads exactly
    like "Claude Code doesn't support mcp_tool on PostToolUse". It is version
    skew with no error surface.

    QUESTION 2 — WOULD ANYTHING BE KEPT. This is the same defect one layer up,
    and it is the one that shipped. The stream is gated server-side on
    `ARIA_HOOKSTREAM_USERS`, which is empty or a short allowlist. A user who is
    not on it gets a consent row, six hooks in `.claude/settings.json`, a
    cheerful "Aria is now watching", and then every event is received and
    discarded — measured on a cold install 2026-09-23: five real tool calls,
    HTTP 200 on each, nothing stored, nothing said. Claiming an outcome you
    cannot observe, inside our own onboarding.

    So we ask, before recording a consent or writing a line: send one event
    that is counted and discarded and read the word that comes back.
    """
    def _post(body: dict, sid: str | None) -> tuple[str | None, str]:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if sid:
            headers["Mcp-Session-Id"] = sid
        req = urllib.request.Request(
            mcp_url, data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.headers.get("Mcp-Session-Id"), r.read().decode("utf-8")

    try:
        sid, _ = _post(
            {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "aria-watch", "version": "1"},
                },
            },
            None,
        )
        _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        _, listed = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid)
    except Exception:  # noqa: BLE001 — not being able to ask is not a failure
        return None, None

    tools: set[str] | None = None
    payload = _last_json(listed)
    if payload is not None:
        found = (payload.get("result") or {}).get("tools")
        if isinstance(found, list):
            tools = {t.get("name") for t in found if isinstance(t, dict) and t.get("name")}

    if tools is not None and "aria_session_event" not in tools:
        # Nothing to ask it with. The tools check below will refuse anyway.
        return tools, None

    try:
        _, called = _post(
            {
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "aria_session_event", "arguments": dict(_PROBE)},
            },
            sid,
        )
    except Exception:  # noqa: BLE001
        return tools, None

    payload = _last_json(called)
    if payload is None:
        return tools, None
    result = payload.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        return tools, None
    # READ `structuredContent`. Joining the `content` blocks turns a structured
    # answer into prose and the word we need stops being parseable — the same
    # mistake this repo has made on MCP probes before.
    said = ""
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("result"), str):
        said = structured["result"]
    else:
        said = " ".join(
            str(c.get("text") or "")
            for c in (result.get("content") or [])
            if isinstance(c, dict)
        )
    # `ok (<outcome>)`
    if "(" in said and ")" in said:
        return tools, said[said.index("(") + 1 : said.rindex(")")].strip()
    return tools, None


def transport_of(cfg: dict) -> str:
    """What the config ACTUALLY uses — read from the hooks, never from what we
    asked for.

    Caught by this function's absence: asking the backend for
    `transport=mcp` against a server too old to honour it returns the HTTP
    config with a 200, and the script cheerfully printed "every event rides the
    MCP connection" over a block where six of seven events were `type: http`.
    That is the claim-an-outcome-you-cannot-observe failure in miniature, and
    the user would then skip exporting ARIA_API_KEY and stream nothing.

    "mcp" only when every event is an `mcp_tool`; `Stop` alone being one is the
    HTTP config, not the MCP one.
    """
    kinds = {
        h.get("type")
        for entries in cfg.get("hooks", {}).values()
        for entry in entries
        for h in entry.get("hooks", [])
        if isinstance(h, dict)
    }
    if not kinds:
        return "none"
    return "mcp" if kinds == {"mcp_tool"} else "http"


def _describe(cfg: dict) -> list[str]:
    lines = []
    for event, entries in sorted(cfg.get("hooks", {}).items()):
        for entry in entries:
            matcher = entry.get("matcher") or "(every occurrence)"
            kinds = {h.get("type") for h in entry.get("hooks", []) if isinstance(h, dict)}
            how = "via MCP" if kinds == {"mcp_tool"} else "via HTTP"
            lines.append(f"  {event:22s} {matcher:38s} {how}")
    return lines


def load_settings(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        print(
            f"Could not read {path} ({e}). Fix or move that file first — refusing "
            "to overwrite settings I cannot read.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


def save_settings(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def turn_on(settings: dict, cfg: dict) -> int:
    """Merge our entries in, leaving every other hook in the file alone."""
    hooks = settings.setdefault("hooks", {})
    added = 0
    for event, entries in cfg.get("hooks", {}).items():
        if event in _SKIP_EVENTS:
            continue
        existing = hooks.setdefault(event, [])
        # Drop any previous copy of ours first, so re-running is idempotent
        # rather than stacking a second identical POST onto every tool call.
        existing[:] = [e for e in existing if not is_ours(e)]
        existing.extend(entries)
        added += len(entries)
    return added


def turn_off(settings: dict) -> int:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    removed = 0
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        keep = [e for e in entries if not is_ours(e)]
        removed += len(entries) - len(keep)
        if keep:
            hooks[event] = keep
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    return removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stream this repo's session events to Aria.")
    ap.add_argument("--off", action="store_true", help="remove the block instead")
    ap.add_argument("--repo", default=os.getcwd(), help="repo root (default: cwd)")
    ap.add_argument("--backend", default=None)
    ap.add_argument("--mcp-url", default=None,
                    help="override the MCP endpoint the preflight probes")
    ap.add_argument("--mcp-server", default=None,
                    help="override the MCP server id the hooks call")
    ap.add_argument(
        "--transport", choices=("mcp", "http"), default="mcp",
        help="mcp (default): every event rides the authenticated MCP connection "
             "— nothing to export, and no file contents or command output leave "
             "the machine. http: the original ingest; needs ARIA_API_KEY.",
    )
    ap.add_argument(
        "--accept", action="store_true",
        help="record consent and turn the stream on. Without it, the consent "
             "screen is printed and NOTHING is written.",
    )
    # The skill forwards $ARGUMENTS verbatim, so "/aria:watch yes" arrives as a
    # positional. Accepting it here means the skill never has to translate a
    # user's word into a flag — a translation that, done wrong, turns the
    # consent gate into a formality.
    ap.add_argument("word", nargs="?", default="", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if (args.word or "").strip().lower() in ("yes", "y", "accept", "ok"):
        args.accept = True

    repo = os.path.abspath(args.repo)
    path = os.path.join(repo, ".claude", "settings.json")
    settings = load_settings(path)

    if args.off:
        n = turn_off(settings)
        # Withdraw the consent record even when no hooks were found: the two
        # can be out of step (someone hand-edited settings.json), and the
        # record is the thing that says "she may", so it is the thing to clear.
        key = creds.api_key()
        if key:
            print(revoke_consent(args.backend or creds.backend_url(), key, repo))
        if not n:
            print(f"Aria was not watching {repo} — no hooks to remove.")
            return 0
        save_settings(path, settings)
        print(f"Aria is no longer watching {repo}.")
        print(f"Removed {n} hook entr{'y' if n == 1 else 'ies'} from {path}.")
        print("Run /reload-plugins (or restart) to stop the hooks in THIS session.")
        return 0

    backend = (args.backend or creds.backend_url()).rstrip("/")
    # THE GATE. Nothing is fetched, written or recorded until the person has
    # seen the screen and said yes. Printing it and turning the stream on in
    # the same breath would make it a notice, not a consent.
    if not args.accept:
        print(CONSENT_TEXT)
        # The screen describes the DEFAULT transport, because that is what
        # `/aria:watch yes` installs. `--transport http` is a different bargain
        # and the screen would over-promise on two of its own lines, so it is
        # corrected here rather than left to be discovered.
        if args.transport == "http":
            print()
            print(
                "  YOU ASKED FOR --transport http, WHICH CHANGES TWO OF THOSE LINES:\n"
                "    · it POSTs the whole event, so a command's output DOES cross\n"
                "      the wire (read for an exit code, not stored)\n"
                "    · it does not watch file edits at all, so no file paths leave\n"
                "  File contents still never leave: it does not subscribe to the\n"
                "  edit tools for exactly that reason."
            )
        print()
        print(
            "Nothing has been turned on. Re-run as `/aria:watch yes` to accept and "
            "start, or just don't."
        )
        return 2

    key = creds.api_key()
    if not key:
        print(
            "No Aria key on this machine, so your consent cannot be recorded and "
            "nothing was turned on. Run /aria:connect first.",
            file=sys.stderr,
        )
        return 1

    global MCP_SERVER_ID
    server_id = args.mcp_server or mcp_server_id()
    MCP_SERVER_ID = server_id
    cfg, provenance, _asked = fetch_config(
        backend, transport=args.transport, server_id=server_id
    )
    # Belt and braces: the server is told the id, but a bundled fallback and any
    # older server still emit the bare default, which fires nothing.
    retarget_mcp_hooks(cfg, server_id)
    # READ IT BACK. What we asked for is not what we got if the backend is
    # older than the transport we requested.
    transport = transport_of(cfg)

    # PREFLIGHT, and it runs BEFORE the consent is recorded and before one byte
    # is written — so a "no" here leaves the machine exactly as it was.
    # Fetching a config is not a write; the rule that matters is that nothing
    # is turned ON without a recorded consent, and that still holds.
    #
    # Two questions, one MCP session. See `probe_server`.
    wanted = tools_named_by(cfg)
    have, stream = probe_server(args.mcp_url or creds.mcp_url(), key)

    # (1) every mcp_tool the config names must exist on the server we will point
    # it at. A hook naming a missing tool is SILENT.
    if wanted and have is not None:
        missing = sorted(wanted - have)
        if missing:
            print(
                "Refusing to turn this on: Aria's MCP server does not have "
                + ", ".join(missing)
                + ". Hooks naming a missing tool fail SILENTLY — no row, no "
                "error — so this would look like it worked and quietly record "
                "nothing. The server is older than the config it served; try "
                "again once it is updated, or use --transport http.",
                file=sys.stderr,
            )
            return 1

    # (2) THE EARLY NO. A live test event that comes back `stream-off` says the
    # account cannot stream at all, and that answer cannot change by recording
    # a consent — so refuse here and leave the machine untouched, with not even
    # a consent row to explain.
    if stream == _STREAM_OFF:
        _refuse_dark(
            repo,
            "Aria's server accepted this machine's key, but watching sessions "
            "is not switched on for this account yet: a live test event came "
            f"back `{_STREAM_OFF}`, which means every event this repo sent "
            "would be received and thrown away. Ask Aria to enable it for you, "
            "then run /aria:watch yes again.",
            recorded=False,
        )
        return 1

    ok, why, verdict = record_consent(args.backend or creds.backend_url(), key, repo)
    print(why)
    if not ok:
        print("Nothing was turned on.", file=sys.stderr)
        return 1

    # (3) THE AUTHORITATIVE NO, from the backend's own gate. It needs the
    # consent row to exist, so it can only be asked once that is recorded —
    # which is why this sits after the POST and the probe above sits before it.
    # `may_stream` is computed by `hooks.consent_gate`, the same function the
    # ingest refuses with, so it covers the consent row AND the server flag.
    if verdict.get("may_stream") is False:
        _refuse_dark(
            repo,
            verdict.get("detail")
            or f"Aria's server will not keep this repo's events ({verdict.get('reason')}).",
            recorded=True,
        )
        return 1

    # WHAT WE ACTUALLY KNOW, named rather than assumed. The backend's verdict
    # wins; the live probe is the fallback for a backend that predates it
    # (measured 2026-09-23: prod answers 201 with no `may_stream` at all).
    if verdict.get("may_stream") is True:
        verified, how = True, "Aria's server confirmed it will keep this repo's events"
    elif stream in _STREAM_ACCEPTED:
        verified, how = True, f"a live test event was accepted ({stream})"
    else:
        verified = False
        how = (
            "could not reach Aria's MCP server to test it"
            if stream is None and have is None
            else f"the test event answered {stream!r}, which is not a word this knows"
        )

    install_hook_script()
    detach(cfg)
    n = turn_on(settings, cfg)
    save_settings(path, settings)

    if verified:
        print(f"Aria is now watching {repo} — {how}.")
    else:
        print(
            f"Hooks installed for {repo}, but do NOT say Aria is watching: "
            f"whether her server keeps these events is UNVERIFIED — {how}. If "
            "nothing shows up, that is the first thing to check."
        )
    print(f"Wrote {n} hook entries to {path} (config from {provenance}).")
    if _asked == "mcp" and transport != "mcp":
        print(
            f"NOTE: asked for the MCP transport, but {backend} returned the HTTP one "
            "— it is running a backend older than that option. Falling back, which "
            "means ARIA_API_KEY below is required."
        )
    if transport == "mcp":
        print(f"Every event goes to {backend}{INGEST_PATH} from a background hook "
              f"(~/.aria/bin/{HOOK_SCRIPT_NAME}): the session never waits on Aria.")
        # An earlier revision of this file warned that receipts never arrived
        # on this transport, and that warning was WRONG. The root cause is
        # worth keeping, because it will recur:
        #
        # The backend process serving that A/B was started BEFORE
        # `aria_session_event` was added to the MCP server. The config it
        # served named that tool; the server did not have it. A hook naming a
        # tool the server lacks fires NOTHING — no row, no error, no log line
        # (measured directly by pointing PostToolUse at
        # `aria_tool_that_does_not_exist`). `Stop` used an OLDER tool that did
        # exist, so it kept working — and "Stop yes, everything else silent"
        # reads exactly like "Claude Code doesn't support mcp_tool mid-turn".
        # It is version skew with no error surface.
        #
        # Re-measured after restarting: test receipt ("ran `python3 -m pytest
        # test_x.py -q` — exit 0") and commit receipt ("committed 047875b")
        # both land, with `ARIA_API_KEY` unset. Duplicated hook entries and a
        # missing warm-up call were ruled out separately; neither suppresses.
        # `tools_on_server` now refuses the install that would have produced
        # the phantom, so the next person gets a sentence instead of a week.
    else:
        print(f"HTTP events go to {backend}{INGEST_PATH}; Stop calls {server_id}.")
    for line in _describe({"hooks": {k: v for k, v in cfg["hooks"].items()
                                     if k not in _SKIP_EVENTS}}):
        print(line)
    print(f"NEVER leaves this machine: {_NEVER_STREAMED}.")

    if transport == "mcp":
        # The reason to prefer it, and it is not just convenience: the MCP
        # payload carries the flat arguments only — no `tool_response` blob and
        # no `tool_input.content` — so a Write's file body and a command's
        # stdout never leave at all, rather than leaving and being dropped
        # server-side.
        print(f"Nothing else to set up: the hook reads the key from {creds.credentials_path()}.")
    else:
        print("The file holds no secret — the header is the literal $ARIA_API_KEY.")
        if not (os.environ.get("ARIA_API_KEY") or "").strip():
            print()
            print(
                "PARTLY LIVE ONLY: the HTTP hooks read the key from ARIA_API_KEY in "
                "your environment and it is not set in this shell, so commits, test "
                "runs and permission prompts will be rejected. A `type: http` hook "
                "cannot read a file, so the key in ~/.aria/credentials does not "
                "reach it."
            )
            print(
                "End-of-turn summaries WILL still arrive — Stop rides the MCP "
                "connection, which reads that file already."
            )
            print("Add this to your shell profile, then restart Claude Code:")
            print(
                "  export ARIA_API_KEY=$(sed -n 's/^ARIA_API_KEY=//p' "
                f"{creds.credentials_path()})"
            )
            print("Or re-run /aria:watch without --transport http to avoid it entirely.")
        else:
            print("ARIA_API_KEY is set, so the HTTP hooks can authenticate too.")
    print("Turn it off with /aria:unwatch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
