#!/usr/bin/env python3
"""§CC-WAIT-CALL — tell Aria a turn ended, so a session left waiting on the user
rings them. A Claude Code Stop + UserPromptSubmit hook, run `async` so the
session never waits on it.

Founder 2026-11-23: "how do we make it a thing instead of you just telling me".
Claude promising to call when it's done or blocked is a promise the model can
forget. This hook can't forget: every main-thread turn's last message goes to
the backend, the MODEL there judges whether the session now waits on the user,
and the scheduler rings them after 10 minutes of silence — through the same
`aria_call_me` gates and the user's own call setting.

Stop: post the turn's last assistant text — only when the session is the
user's own (a human typed a prompt in it, or the Aria app sent one). A
teammate session started by another agent never posts: its "I'm done" is to
the lead, not to the user. A marker `~/.aria/cc_wait/<session id>` is written
BEFORE the post and removed when the backend says nothing is waiting, so a
lost answer errs toward clearing on the next prompt.

UserPromptSubmit: the user answered. Only when a marker exists (so an ordinary
prompt touches no network), post `event=prompt` and remove it.

Never raises, never prints to stdout. Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aria_credentials as _creds  # noqa: E402

WAIT_DIR = os.path.join(os.path.expanduser("~"), ".aria", "cc_wait")
TIMEOUT_SECS = 15
MAX_TEXT = 8000
TAIL_BYTES = 256 * 1024
TAIL_MAX = 8 * 1024 * 1024
#: Envelope tags Claude Code wraps around words that did NOT come from the user.
_NOT_TYPED = ("<teammate-message", "<task-notification", "<cross-session-message")
#: A message the Aria app sent this session (the channel tag). Self-contained on
#: purpose: this script must run beside ANY version of the other plugin scripts
#: (a hand-wired setup can carry an older aria_cc_gate.py).
_CHAN_RE = re.compile(r'<channel\b[^>]*\bsource="[^"]*aria[^"]*"[^>]*\bchat_id="([^"]+)"')


def _valid_session_id(session_id: str) -> bool:
    return bool(session_id) and "/" not in session_id and not session_id.startswith(".")


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _from_the_user(obj: dict, text: str) -> bool:
    """A prompt the user gave: typed by them, or sent from the Aria app (a
    channel message). Decided by structure — the transcript's own stamps and
    the envelope tag — never by what the words say."""
    stripped = text.lstrip()
    if _CHAN_RE.match(stripped):
        return True
    if obj.get("isMeta") or obj.get("isCompactSummary") or obj.get("isVisibleInTranscriptOnly"):
        return False
    origin = obj.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return False
    return not stripped.startswith(_NOT_TYPED)


def scan(transcript_path: str) -> tuple[bool, str, str | None]:
    """(the user ever prompted this session, the last assistant text, the chat_id
    of the user prompt found when that came from the app), read from the
    transcript's END outward."""
    user_seen, last, chat = False, "", None
    try:
        size = os.path.getsize(transcript_path)
        window = TAIL_BYTES
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
                    if not isinstance(obj, dict):
                        continue
                    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                    text = _text_of(msg.get("content")).strip()
                    if not text:
                        continue
                    if obj.get("type") == "assistant" and not last:
                        last = text
                    elif (obj.get("type") == "user" and not user_seen
                          and _from_the_user(obj, text)):
                        user_seen = True
                        m = _CHAN_RE.match(text.lstrip())
                        chat = m.group(1)[:80] if m else None
                    if user_seen and last:
                        return True, last, chat
                if start == 0 or window >= TAIL_MAX:
                    return user_seen, last, chat
                window *= 2
    except OSError:
        return False, "", None


def _marker(session_id: str) -> str:
    return os.path.join(WAIT_DIR, session_id)


def _post(body: dict) -> dict:
    req = urllib.request.Request(
        f"{_creds.backend_url().rstrip('/')}/v1/delegate/cc/turn-end",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_creds.api_key()}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    out = json.loads(urllib.request.urlopen(req, timeout=TIMEOUT_SECS).read() or b"{}")
    return out if isinstance(out, dict) else {}


def on_stop(event: dict) -> None:
    sid = str(event.get("session_id") or "")
    tpath = str(event.get("transcript_path") or "")
    if event.get("stop_hook_active") or not tpath:
        return
    user_seen, last, chat = scan(tpath)
    if not user_seen:
        return  # a session the user never prompted: an agent's, not theirs
    text = event.get("last_assistant_message")
    text = text if isinstance(text, str) and text.strip() else last
    if not text.strip():
        return
    os.makedirs(WAIT_DIR, exist_ok=True)
    with open(_marker(sid), "w", encoding="utf-8") as fh:
        fh.write("1")
    out = _post({"event": "stop", "session_id": sid,
                 "chat_id": chat, "text": text[-MAX_TEXT:]})
    if not out.get("waiting"):
        _drop_marker(sid)


def _drop_marker(sid: str) -> None:
    try:
        os.remove(_marker(sid))
    except OSError:
        pass


def on_prompt(event: dict) -> None:
    sid = str(event.get("session_id") or "")
    if not os.path.exists(_marker(sid)):
        return
    tpath = str(event.get("transcript_path") or "")
    _post({"event": "prompt", "session_id": sid,
           "chat_id": scan(tpath)[2] if tpath else None})
    _drop_marker(sid)


def main() -> None:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict) or not _creds.api_key():
            return
        if not _valid_session_id(str(event.get("session_id") or "")):
            return
        name = event.get("hook_event_name")
        if name == "Stop":
            on_stop(event)
        elif name == "UserPromptSubmit":
            on_prompt(event)
    except Exception as e:  # noqa: BLE001 — the work matters, the watching does not
        print(f"aria_turn_end: {type(e).__name__}"[:200], file=sys.stderr)


if __name__ == "__main__":
    main()
