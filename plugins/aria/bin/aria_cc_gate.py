#!/usr/bin/env python3
"""§CC-CONNECT — hold a risky tool until the user says yes, while they drive this
session from the Aria app or a phone call. A Claude Code PreToolUse and
PermissionRequest hook.

WHEN IT DOES ANYTHING AT ALL. Only while this session is REMOTE-ACTIVE: the
aria-delegate MCP drops `~/.aria/cc_remote/<session id>` each time it pushes their
words in from the app. No marker, or one older than 30 minutes → this prints
nothing and exits at once, without touching the network, and Claude Code behaves
exactly as it always does. A PreToolUse for a tool that is not risky does the
same. So the cost to an ordinary session is one python start on Bash calls.

WHAT IT DOES. Posts the event to the backend, which holds it (≤50s) while the
app shows Approve/Deny and the call reads it back, then prints the backend's
decision in the exact shape Claude Code 2.1.304 accepts (measured 2026-10-21):
a PreToolUse allow/deny works even under --dangerously-skip-permissions.

AT SESSION START it says it is here: a detached process pings gate-seen until
the backend has this session's row (≤2 min). A fresh session has run no tool, so
without this the app can't tell the gate is loaded and won't start a voice call.

UNLESS THEY CHOSE BYPASS. A session in bypassPermissions mode is never held: the
user is pinged only when Claude asks them something or asks permission.
Subagent calls are never held either.

ON A VOICE CALL the main thread only TALKS and hands off (§CALL-TURN,
2026-11-22, "get the fix on"): `aria_say` and its status/stop tools, plus ONE
short hand-off (Agent or SendMessage) per turn, after it has spoken on a voice
turn, plus the call's context note (§CALL-NOTE) for what a short brief can't hold. Everything else — Read, Bash, Monitor, any other MCP tool — is denied
with "put it in the hand-off". A session holds the next thing the user says until its
turn ends, so every second of main-thread work after `aria_say` was a second they
waited (2026-11-22: 4-11 s per turn).

FAILS CLOSED. A risky PreToolUse that cannot reach the backend is DENIED. A
PermissionRequest that cannot reach it prints nothing — the dialog on their screen
still needs a human, which is already safe.

The two patterns below are the backend's (`aria_backend/cc/gate.py`); a test
asserts they are identical. The backend re-checks everything; this copy exists
only so a dead backend cannot turn a `git push` into a yes.

Stdlib only.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aria_credentials as _creds  # noqa: E402

RISKY_BASH = (
    r"\bgit\s+push\b|\bgit\s+reset\s+--hard\b|\brm\s+-[a-zA-Z]*[rf]|\brm\s"
    r"|\bgcloud\b.*\b(deploy|delete|update)\b|\bdeploy_|\bkubectl\s+delete\b"
    r"|\bDROP\s|\bpsql\b.*(?i:prod)|\bprod_sql"
)
# MCP tools only: Claude Code's own SendMessage/SendUserFile talk to this
# session's agents, never the outside world.
RISKY_TOOL = r"^mcp__.*(?i:send|publish|deploy)"

REMOTE_ACTIVE_SECS = 30 * 60
HOLD_SECS = 55
REMOTE_DIR = os.path.join(os.path.expanduser("~"), ".aria", "cc_remote")
#: `~/.aria/cc_call/<session id>`: the aria-delegate MCP refreshes it while the user is
#: on a VOICE call with this session (≤25s apart) and removes it at call end.
CALL_DIR = os.path.join(os.path.expanduser("~"), ".aria", "cc_call")
CALL_STALE_SECS = 120
#: §CALL-TURN — what the main thread may call while the user is on a call. ALLOWED any
#: number of times; a HAND-OFF once per turn; everything else is denied.
#: §CC-HOSTED — the same tools reach a session under TWO prefixes: the local
#: stdio bridge (`aria-delegate`) and the plugin's hosted server (`aria` in plugin
#: `aria` → `mcp__plugin_aria_aria__`). Matching one name only made the gate deny
#: aria_say on a call for every plugin user.
ON_CALL_PREFIXES = ("mcp__aria-delegate__", "mcp__plugin_aria_aria__")
ON_CALL_SAYS = frozenset(p + "aria_say" for p in ON_CALL_PREFIXES)
ON_CALL_SAY = "mcp__aria-delegate__aria_say"
ON_CALL_ALLOWED = frozenset({*ON_CALL_SAYS, *(p + "aria_call_status" for p in ON_CALL_PREFIXES),
                             "TaskStop"})
ON_CALL_HANDOFF = frozenset({"Agent", "SendMessage"})
#: ToolSearch may only load the call's own tools (aria_say is a deferred tool).
ON_CALL_TOOLSEARCH_PREFIX = ON_CALL_PREFIXES
ON_CALL_MAX_BRIEF = 1500
ON_CALL_REASON = ("You're on a live call with the user: this turn only speaks (aria_say) and "
                  "hands off once (Agent or SendMessage). Put this in the hand-off; context "
                  "too long for the brief goes in ~/.aria/cc_call/<session id>.context.md.")
ON_CALL_SAY_FIRST = "Say your reply to them first with aria_say, then hand off."
ON_CALL_ONE_HANDOFF = "One hand-off per turn on a call: put it all in that one."
ON_CALL_SHORT = (f"Keep the hand-off under {ON_CALL_MAX_BRIEF} characters: put the "
                 "context in ~/.aria/cc_call/<session id>.context.md and tell it to read that first.")
#: §CALL-NOTE — the one file the main thread may touch on a call: a running
#: context note a short brief can point at ("read the call context note first").
#: Read/Write/Edit of EXACTLY this path; Claude Code makes Write/Edit read a file
#: before changing it, so a write-only note would be unwritable after its first line.
ON_CALL_NOTE_TOOLS = frozenset({"Read", "Write", "Edit"})


def call_note_path(session_id: str) -> str:
    return os.path.join(CALL_DIR, f"{session_id}.context.md")


def _is_call_note(session_id: str, file_path: str) -> bool:
    if not _valid_session_id(session_id) or not file_path:
        return False
    # The note's own name is NOT resolved: a symlinked note resolves elsewhere
    # and so never equals it — a "note" write can't land anywhere else.
    want = os.path.join(os.path.realpath(CALL_DIR), f"{session_id}.context.md")
    return os.path.realpath(os.path.expanduser(file_path)) == want


#: How much of the transcript's end is read to find this turn's prompt.
TURN_TAIL_BYTES = 256 * 1024
TURN_TAIL_MAX = 8 * 1024 * 1024


_SQL_WRITE = r"(?i)\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|vacuum|reindex)\b"
# reads that still change things: kill sessions, set config, SELECT … INTO, sequences, locks
_SQL_SIDE_EFFECT = (r"(?i)\b(pg_terminate_backend|pg_cancel_backend|set_config|nextval|setval"
                    r"|pg_advisory\w*|lo_\w+|dblink\w*|into|set|lock)\b|\bfor\s+(update|share|no\s+key|key)\b")


def _read_only_prod_sql(command: str) -> bool:
    """`prod_sql.sh "<one SELECT>"` — a read, not a change. Maths on the text, no guess:
    exactly one prod_sql call, its SQL starts with SELECT/WITH/EXPLAIN, one statement,
    no write keyword anywhere, nothing chained after it."""
    m = re.fullmatch(r'\s*(?:\./)?scripts/prod_sql\.sh\s+"([^"]*)"\s*(?:2>&1)?\s*(?:\|\s*(?:tail|head)(?:\s+-n?\s*\d+)?\s*)?',
                     command)
    if not m:
        return False
    sql = m.group(1).strip().rstrip(";")
    # inside double quotes the SHELL expands $(…) / `…` / \ before psql sees it
    if re.search(r"[$`\\]", command):
        return False
    return (bool(re.match(r"(?i)(select|with|explain)\b", sql))
            and ";" not in sql and not re.search(_SQL_WRITE, sql)
            and not re.search(_SQL_SIDE_EFFECT, sql))


def risky(tool_name: str, tool_input: dict) -> bool:
    if tool_name == "Bash":
        command = str(tool_input.get("command") or "")
        if _read_only_prod_sql(command):
            return False
        return bool(re.search(RISKY_BASH, command))
    return bool(re.search(RISKY_TOOL, tool_name))


def _valid_session_id(session_id: str) -> bool:
    return bool(session_id) and "/" not in session_id and not session_id.startswith(".")


def remote_active(session_id: str) -> bool:
    if not _valid_session_id(session_id):
        return False
    try:
        with open(os.path.join(REMOTE_DIR, session_id), encoding="utf-8") as fh:
            at = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return False
    return time.time() - at <= REMOTE_ACTIVE_SECS


def call_active(session_id: str) -> bool:
    if not _valid_session_id(session_id):
        return False
    try:
        with open(os.path.join(CALL_DIR, session_id), encoding="utf-8") as fh:
            at = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return False
    return time.time() - at <= CALL_STALE_SECS


#: §CC-HOSTED-GATE — a session reached through the HOSTED server has no bridge
#: writing cc_remote/cc_call, so the state comes from the backend instead. It is
#: asked only when the transcript holds a channel message from Aria (a session
#: nobody drives from the phone never touches the network) and is cached briefly.
STATE_DIR = os.path.join(os.path.expanduser("~"), ".aria", "cc_state")
STATE_TTL_SECS = 10
STATE_TIMEOUT_SECS = 3
_CHAN_RE = re.compile(r'<channel\b[^>]*\bsource="[^"]*aria[^"]*"[^>]*\bchat_id="([^"]+)"')


def _channel_chat_id(transcript_path: str) -> str | None:
    """The chat_id of the LAST channel message from Aria in the transcript.

    Only a USER line whose text BEGINS with a channel tag counts: the same tag
    quoted in a file Claude read, a tool result or a reply is not a message from
    the phone, and treating it as one would make an offline session fail closed."""
    if not transcript_path:
        return None
    try:
        size = os.path.getsize(transcript_path)
        window = TURN_TAIL_BYTES
        with open(transcript_path, "rb") as f:
            while True:
                start = max(0, size - window)
                f.seek(start)
                chunk = f.read(size - start)
                if start > 0:
                    chunk = chunk[chunk.find(b"\n") + 1:] if b"\n" in chunk else b""
                for raw in reversed(chunk.splitlines()):
                    if b"<channel" not in raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict) or obj.get("type") != "user":
                        continue
                    content = (obj.get("message") or {}).get("content")
                    if isinstance(content, list):
                        content = " ".join(b.get("text", "") for b in content
                                           if isinstance(b, dict) and b.get("type") == "text")
                    if not isinstance(content, str):
                        continue
                    m = _CHAN_RE.match(content.lstrip())
                    if m:
                        return m.group(1)[:80]
                if start == 0 or window >= TURN_TAIL_MAX:
                    return None
                window *= 2
    except OSError:
        return None


def hosted_state(event: dict) -> dict | None:
    """{remote_active, call_live} from the backend, or None when this session was
    never driven from the phone. Unreachable → remote_active True (so a risky
    tool goes on to the hold, which then fails closed) and call_live False."""
    sid = str(event.get("session_id") or "")
    if not _valid_session_id(sid) or not _creds.api_key():
        return None
    chat = _channel_chat_id(str(event.get("transcript_path") or ""))
    if not chat:
        return None
    path = os.path.join(STATE_DIR, f"{sid}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            cached = json.load(fh)
        if (cached.get("chat") == chat
                and time.time() - float(cached.get("at") or 0) <= STATE_TTL_SECS):
            return cached
    except (OSError, ValueError):
        pass
    try:
        req = urllib.request.Request(
            f"{_creds.backend_url()}/v1/delegate/cc/session-state",
            data=json.dumps({"session_id": sid, "chat_id": chat}).encode(),
            headers={"Authorization": f"Bearer {_creds.api_key()}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        out = json.loads(urllib.request.urlopen(req, timeout=STATE_TIMEOUT_SECS).read() or b"{}")
        state = {"remote_active": bool(out.get("remote_active")),
                 "call_live": bool(out.get("call_live")), "chat": chat, "at": time.time()}
    except Exception as e:  # noqa: BLE001
        print(f"aria_cc_gate: state unreachable: {type(e).__name__}"[:200], file=sys.stderr)
        return {"remote_active": True, "call_live": False, "unreachable": True}
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError:
        pass
    return state


GATE_PING_EVERY_SECS = 60


def _turn_prompt(transcript_path: str) -> tuple[str, bool] | None:
    """(uuid, is_voice) of the line that started this turn — the last user line
    with text that Claude Code did not inject — read from the transcript's END.
    None when it can't be found."""
    try:
        size = os.path.getsize(transcript_path)
        window = TURN_TAIL_BYTES
        with open(transcript_path, "rb") as f:
            while True:
                start = max(0, size - window)
                f.seek(start)
                chunk = f.read(size - start)
                if start > 0:
                    chunk = chunk[chunk.find(b"\n") + 1:] if b"\n" in chunk else b""
                for raw in reversed(chunk.splitlines()):
                    try:
                        obj = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict) or obj.get("type") != "user" or obj.get("isMeta"):
                        continue
                    content = (obj.get("message") or {}).get("content")
                    if isinstance(content, list):
                        content = " ".join(b.get("text", "") for b in content
                                           if isinstance(b, dict) and b.get("type") == "text")
                    if not isinstance(content, str) or not content.strip():
                        continue  # a tool result, not a prompt
                    return str(obj.get("uuid") or ""), 'via="voice"' in content
                if start == 0 or window >= TURN_TAIL_MAX:
                    return None
                window *= 2
    except OSError:
        return None


def _handoff_brief(tool: str, tool_input: dict) -> str:
    if tool == "Agent":
        return str(tool_input.get("prompt") or "")
    msg = tool_input.get("message")
    return msg if isinstance(msg, str) else json.dumps(msg or "")


def on_call(event: dict) -> dict | None:
    """§CALL-TURN — the main thread's tool on a live call: None = allowed."""
    tool = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    if tool == "ToolSearch":
        q = str(tool_input.get("query") or "")
        names = [n.strip() for n in q[len("select:"):].split(",")] if q.startswith("select:") else []
        if names and all(n.startswith(ON_CALL_TOOLSEARCH_PREFIX) for n in names):
            return None
        return deny(ON_CALL_REASON)
    if tool in ON_CALL_NOTE_TOOLS and _is_call_note(str(event.get("session_id") or ""),
                                                     str(tool_input.get("file_path") or "")):
        return None
    if tool not in ON_CALL_ALLOWED and tool not in ON_CALL_HANDOFF:
        return deny(ON_CALL_REASON)
    if tool in ON_CALL_HANDOFF and len(_handoff_brief(tool, tool_input)) > ON_CALL_MAX_BRIEF:
        return deny(ON_CALL_SHORT)
    if tool not in ON_CALL_HANDOFF and tool not in ON_CALL_SAYS:
        return None
    turn = _turn_prompt(str(event.get("transcript_path") or ""))
    if turn is None or not turn[0]:
        return None  # can't tell the turn: never strand a hand-off or their answer
    turn_id, voice = turn
    path = os.path.join(CALL_DIR, f"{event.get('session_id')}.turn")
    try:
        # A hosted session has no bridge to have made this directory.
        os.makedirs(CALL_DIR, exist_ok=True)
        with open(path, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)  # parallel tool calls run their hooks at once
            fh.seek(0)
            try:
                state = json.loads(fh.read() or "{}")
            except ValueError:
                state = {}
            if state.get("turn") != turn_id:
                state = {"turn": turn_id, "said": False, "handoffs": 0}
            if tool in ON_CALL_SAYS:
                state["said"] = True
                out = None
            elif voice and not state["said"]:
                out = deny(ON_CALL_SAY_FIRST)
            elif state["handoffs"] >= 1:
                out = deny(ON_CALL_ONE_HANDOFF)
            else:
                state["handoffs"] += 1
                out = None
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(state))
            return out
    except OSError:
        return None


def _post_gate_seen(session_id: str) -> dict | None:
    """One POST to /cc/gate-seen. The backend's answer, or None if it could not
    be reached. Never raises, never blocks more than ~1.5s."""
    try:
        req = urllib.request.Request(
            f"{_creds.backend_url()}/v1/delegate/cc/gate-seen",
            data=json.dumps({"session_id": session_id}).encode(),
            headers={"Authorization": f"Bearer {_creds.api_key()}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        out = json.loads(urllib.request.urlopen(req, timeout=1.5).read() or b"{}")
        return out if isinstance(out, dict) else {}
    except Exception:  # noqa: BLE001
        return None


def _stamp(session_id: str) -> str:
    return os.path.join(REMOTE_DIR, f".gate-{session_id}")


def _write_stamp(session_id: str) -> None:
    try:
        os.makedirs(REMOTE_DIR, exist_ok=True)
        with open(_stamp(session_id), "w", encoding="utf-8") as fh:
            fh.write(str(int(time.time())))
    except OSError:
        pass


def ping_gate_seen(session_id: str) -> None:
    """Tell the backend this gate is running here — on EVERY call it sees while
    the user drives the session, risky or not, at most once a minute. A session that
    runs tools and never pings has no gate, and the backend stops letting them
    drive it ("no_gate")."""
    try:
        if time.time() - os.path.getmtime(_stamp(session_id)) < GATE_PING_EVERY_SECS:
            return
    except OSError:
        pass
    # Stamped BEFORE the ping, which runs DETACHED: the tool never waits on the
    # network for it (2026-10-22: hooks stalled a session 12–20 s per step).
    # A ping that fails is retried by the next call a minute on.
    _write_stamp(session_id)
    _spawn("--ping", session_id)


#: How long the session-start announcer keeps trying. The backend can only
#: record the ping once the aria-delegate mirror has created this session's row,
#: which races the hook — usually seconds behind it.
ANNOUNCE_FOR_SECS = 120
ANNOUNCE_EVERY_SECS = 3


def announce_until_recorded(session_id: str) -> bool:
    """SessionStart: the plugin — and with it this PreToolUse gate — is loaded
    in this session, so say so before any tool runs (a new session has run
    none, and voice waits on gate_confirmed). Retries until the backend has a
    row to record it on. Stops at once if the backend answers without a
    `recorded` key (the feature is off for this user)."""
    deadline = time.time() + ANNOUNCE_FOR_SECS
    while True:
        out = _post_gate_seen(session_id)
        if out is not None and "recorded" not in out:
            return False
        if out and out.get("recorded"):
            _write_stamp(session_id)
            return True
        if time.time() + ANNOUNCE_EVERY_SECS > deadline:
            return False
        time.sleep(ANNOUNCE_EVERY_SECS)


def spawn_announcer(session_id: str) -> None:
    """Run announce_until_recorded detached: SessionStart has a 5s budget and
    must not hold the session up while the row appears."""
    _spawn("--announce", session_id)


def _spawn(mode: str, session_id: str) -> None:
    """This script again, detached. All three streams go to /dev/null so Claude
    Code isn't left waiting on an inherited stdout."""
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), mode, session_id],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    except Exception:  # noqa: BLE001
        pass


def deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def decide(event: dict) -> dict:
    hook = str(event.get("hook_event_name") or "")
    tool = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    if hook == "SessionStart":
        sid = str(event.get("session_id") or "")
        if _valid_session_id(sid) and _creds.api_key():
            spawn_announcer(sid)
        return {}
    if hook not in ("PreToolUse", "PermissionRequest"):
        return {}
    # On a live call the MAIN thread only talks and hands off once — in every
    # permission mode (§CALL-TURN). A subagent's call (agent_id, measured on
    # 2.1.306) is the delegated work itself, so it runs.
    sid = str(event.get("session_id") or "")
    hosted: dict | None = None
    on_a_call = call_active(sid)
    if not on_a_call and hook == "PreToolUse" and not event.get("agent_id"):
        hosted = hosted_state(event)
        on_a_call = bool(hosted and hosted.get("call_live"))
    if hook == "PreToolUse" and not event.get("agent_id") and on_a_call:
        held = on_call(event)
        if held is not None:
            return held
    if not remote_active(sid):
        if hosted is None:
            hosted = hosted_state(event)
        if not (hosted and hosted.get("remote_active")):
            return {}
    if hook == "PreToolUse":
        ping_gate_seen(str(event.get("session_id") or ""))
    # A subagent's call is work this session delegated, not the user driving: it never
    # goes to their phone (an agent's read-only SQL and its SendMessage both did).
    if event.get("agent_id") or event.get("agent_type"):
        return {}
    # The rule (2026-10-21): a session run with bypass permissions never
    # pings the user for a tool — they're pinged only when Claude asks them something
    # (the mirror) or asks permission (the official relay). Pinged above, still.
    if hook == "PreToolUse" and event.get("permission_mode") == "bypassPermissions":
        return {}
    is_risky = risky(tool, tool_input)
    if hook == "PreToolUse" and not is_risky:
        return {}
    key, backend = _creds.api_key(), _creds.backend_url()
    route = "pretool" if hook == "PreToolUse" else "permission"
    body = {k: event.get(k) for k in ("hook_event_name", "session_id", "cwd", "tool_name",
                                       "tool_input", "permission_mode")}
    try:
        if not key:
            raise RuntimeError("no key")
        req = urllib.request.Request(
            f"{backend}/v1/delegate/cc/{route}",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=HOLD_SECS) as r:
            out = json.loads(r.read() or b"{}")
        return out if isinstance(out, dict) else {}
    except Exception as e:  # noqa: BLE001
        # stderr is shown in Claude Code's hook log; stdout is the decision.
        print(f"aria_cc_gate: {type(e).__name__}: {e}"[:300], file=sys.stderr)
        if hook == "PreToolUse":
            return deny("Couldn't reach Aria to ask the user — not run. Tell them what you wanted to do.")
        return {}


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--announce":
        if _valid_session_id(sys.argv[2]):
            announce_until_recorded(sys.argv[2])
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--ping":
        if _valid_session_id(sys.argv[2]):
            _post_gate_seen(sys.argv[2])
        return
    try:
        event = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        return
    out = decide(event if isinstance(event, dict) else {})
    if out:
        print(json.dumps(out))


if __name__ == "__main__":
    main()
