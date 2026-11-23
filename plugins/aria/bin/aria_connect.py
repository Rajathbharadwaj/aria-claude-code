#!/usr/bin/env python3
"""`/aria:connect` — redeem a setup link and store the key. One command, no form.

THE FLOW THIS COMPLETES
-----------------------
    1. the user texts Aria "connect my claude code"
    2. she calls `connect_claude_code` (backend: orchestrator/tools/connect_tools.py),
       which issues a single-use token and texts back
       https://aria.paralleluniverse.ai/connect/<token>
    3. **this script** redeems it:  POST /v1/connect/<token>/claim
    4. the backend mints the `aria_sk_` key AT CLAIM TIME and returns it once
    5. we write it to ~/.aria/credentials (0600) and tell them to reload

Step 3 previously had to happen in a BROWSER, on a page whose only output was a
`claude mcp add …` line to copy into a terminal. That set up the bare MCP
server and left the plugin — hooks, skills, status — unconfigured, so the
plugin still needed a key pasted into `/plugin` by hand. This closes that: the
same link, redeemed from inside the session that is going to use it.

WHY THE KEY IS PARSED OUT OF `command`
--------------------------------------
`/v1/connect/{token}/claim` returns `{"command": "claude mcp add … Bearer
aria_sk_…", "endpoint": …}`. It returns the key only inside that string. We
prefer an explicit `key` field when the backend sends one and fall back to a
regex over `command`, so this keeps working whichever shape it gets.

SINGLE USE IS NOT A BUG TO WORK AROUND
--------------------------------------
A token is consumed atomically (Redis `GETDEL`). If this script runs twice, the
second gets 410 — the same answer a stale link gets, deliberately, so a probe
cannot tell "used" from "never existed". Never retry a 410; ask for a new link.

Stdlib only, so `/aria:connect` works on a machine with nothing installed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aria_credentials as creds  # noqa: E402

# 43 chars of `secrets.token_urlsafe(32)`. Matched loosely (32+) so a change to
# the token length on the server does not silently stop the client recognising
# a link — a too-short guess is rejected by the server, which is the right
# place for that decision.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,}")
_KEY_RE = re.compile(r"aria_sk_[0-9a-fA-F]{16,}")


def extract_token(raw: str) -> str | None:
    """Accept a bare token, a full link, or the whole text message pasted.

    People paste what they were sent, including the sentence around it. Taking
    the LAST path segment of anything that looks like a connect URL, and
    otherwise the longest token-shaped run, covers every form we have seen
    without asking the user to edit their own paste.
    """
    raw = (raw or "").strip().strip("<>\"'")
    if not raw:
        return None
    for chunk in raw.split():
        chunk = chunk.strip().rstrip(".,;)")
        if "/connect/" in chunk:
            tail = chunk.rsplit("/connect/", 1)[1].split("?")[0].split("#")[0]
            if _TOKEN_RE.fullmatch(tail):
                return tail
    candidates = [m.group(0) for m in _TOKEN_RE.finditer(raw) if "/" not in m.group(0)]
    return max(candidates, key=len) if candidates else None


def claim(backend: str, token: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(
        f"{backend}/v1/connect/{token}/claim",
        data=b"",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def write_credentials(key: str, endpoint: str, backend: str) -> str:
    """Write 0600, creating ~/.aria with 0700, and never widen an existing file.

    The file is created with restrictive permissions BEFORE the secret is
    written to it — `open()` then `chmod()` leaves a window in which the key is
    world-readable on a default umask.
    """
    path = creds.credentials_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing = creds.read_credentials_file()
    existing["ARIA_API_KEY"] = key
    if endpoint:
        existing["ARIA_MCP_URL"] = endpoint
    if backend:
        existing["ARIA_BACKEND_URL"] = backend
    body = "".join(f"{k}={v}\n" for k, v in existing.items())

    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, body.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)  # an existing file keeps its old mode without this
    return str(path)


def fail(msg: str) -> int:
    """Every failure exits 1 with ONE sentence that says what to do next.

    A stranger who hits an error here has no model of tokens, Redis TTLs or
    which host mints what. "HTTP 410" is not an instruction.
    """
    print(f"Not connected. {msg}", file=sys.stderr)
    return 1


# ── §DEVICE — sign in with no link at all ────────────────────────────────────
# `/aria:connect` with no argument. Two calls, because a Bash tool call shows
# its output only when it ends: `--start` prints the link and returns at once
# (so the person can open it), `--wait` then polls until they approve.
# The secret device_code stays on THIS machine in ~/.aria/pending-signin (0600);
# only the short user_code and the link are shown.

def _pending_path():
    return creds.credentials_path().parent / "pending-signin"


def _post_json(url: str, body: dict, timeout: float = 15.0) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {}


def _write_private(path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def _can_open_browser() -> bool:
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


def device_start(backend: str, open_browser: bool = True) -> int:
    import socket

    status, data = (0, {})
    try:
        status, data = _post_json(f"{backend}/v1/connect/device",
                                  {"client": "claude-code", "host": socket.gethostname()})
    except urllib.error.URLError as e:
        return fail(f"Could not reach {backend} ({e.reason}). Check your network.")
    if status == 404:
        return fail(f"{backend} has no sign-in endpoint yet — text Aria \"connect my "
                    "claude code\" and run /aria:connect with the link she sends.")
    if status != 200 or not data.get("device_code"):
        return fail(f"Aria's backend could not start a sign-in (HTTP {status}). Try again in a moment.")
    _write_private(_pending_path(), json.dumps({
        "device_code": data["device_code"], "backend": backend,
        "interval": int(data.get("interval") or 3),
    }))
    url = data.get("verification_uri_complete") or data.get("verification_uri")
    opened = False
    if open_browser and _can_open_browser():
        try:
            import webbrowser

            opened = bool(webbrowser.open(url))
        except Exception:  # noqa: BLE001
            opened = False
    print(f"Open this link on your phone or this computer and sign in: {url}")
    print(f"Check it shows the code {data.get('user_code')}. It lasts 15 minutes.")
    if opened:
        print("(Opened it in your browser.)")
    return 0


def device_wait(max_secs: float = 540.0, sleep=None, clock=None) -> int:
    import time

    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    try:
        pending = json.loads(_pending_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return fail("No sign-in in progress. Run /aria:connect to start one.")
    backend = pending["backend"]
    interval = max(1, int(pending.get("interval") or 3))
    deadline = clock() + max_secs
    while True:
        try:
            status, data = _post_json(f"{backend}/v1/connect/device/token",
                                      {"device_code": pending["device_code"]})
        except urllib.error.URLError:
            status, data = 0, {}
        if status == 200 and (data.get("key") or "").startswith("aria_sk_"):
            _pending_path().unlink(missing_ok=True)
            path = write_credentials(data["key"], (data.get("endpoint") or "").strip(), backend)
            print(f"Connected. Key {creds.mask(data['key'])} stored in {path} (readable only by you).")
            print("Now run /reload-plugins so the connection picks it up, then /aria:status.")
            return 0
        if status == 410:
            _pending_path().unlink(missing_ok=True)
            return fail("That sign-in expired or was already used. Run /aria:connect again.")
        # 428 pending, 503 slow down, or a network blip: keep waiting.
        if clock() >= deadline:
            return fail("Still waiting for you to approve. Approve it on the page, then "
                        "run the wait step again — the sign-in stays open for 15 minutes.")
        sleep(interval)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Redeem an Aria setup link.")
    ap.add_argument("link", nargs="*", help="the setup link Aria texted you")
    ap.add_argument("--backend", default=None, help="override the backend base URL")
    ap.add_argument("--wait", action="store_true",
                    help="finish a sign-in started with no link (polls until approved)")
    ap.add_argument("--no-browser", action="store_true", help="don't try to open a browser")
    args = ap.parse_args(argv)

    if args.wait:
        return device_wait()

    raw = " ".join(args.link).strip()
    if not raw:
        # §DEVICE — no link: sign in with Aria instead of asking for one.
        return device_start((args.backend or creds.backend_url()).rstrip("/"),
                            open_browser=not args.no_browser)

    token = extract_token(raw)
    if not token:
        return fail(
            "That does not look like a setup link — it should end in /connect/<code>. "
            "Paste the whole link Aria texted you."
        )

    backend = (args.backend or creds.backend_url()).rstrip("/")

    try:
        data = claim(backend, token)
    except urllib.error.HTTPError as e:
        if e.code == 410:
            return fail(
                "That setup link has already been used or has expired (they last 15 "
                "minutes). Text Aria again and she will send a fresh one."
            )
        if e.code == 503:
            return fail(
                "Aria's setup service is not reachable right now. Wait a moment and "
                "run /aria:connect with the same link — it has not been used up."
            )
        if e.code == 404:
            return fail(
                f"{backend} has no connect endpoint — that is probably the wrong "
                "backend address. Check Aria backend in /plugin → aria."
            )
        if e.code in (405, 501):
            # NOT a backend problem, and saying so sent a cold install chasing
            # Aria's status page instead of its own setting. A 405 is a server
            # that HAS that path and refuses POST on it — which Aria's backend
            # never does. Something else is listening on this address.
            return fail(
                f"{backend} answered, but it is not Aria's backend — it refused to "
                f"accept a POST on the connect path (HTTP {e.code}). Aria's backend "
                "always accepts one there, so something else is on that address: a "
                "proxy, a static site, or a typo. Fix Aria backend in /plugin → aria "
                "(the default is https://api.aria.paralleluniverse.ai). Your link is "
                "still unused."
            )
        return fail(
            f"Aria's backend refused the link (HTTP {e.code}). Text her for a new one; "
            "if it keeps failing, the backend is having a problem, not your machine."
        )
    except urllib.error.URLError as e:
        return fail(
            f"Could not reach {backend} ({e.reason}). Check your network, or the "
            "Aria backend setting in /plugin → aria. Your link is still unused."
        )
    except Exception as e:  # noqa: BLE001
        return fail(f"Something went wrong redeeming the link: {e}")

    key = (data.get("key") or "").strip()
    if not key:
        m = _KEY_RE.search(data.get("command") or "")
        key = m.group(0) if m else ""
    if not key:
        # The link WAS consumed — say so, or they will retry into a 410 and
        # think this script is broken rather than that the key is lost.
        return fail(
            "Aria's backend accepted the link but sent no key back, and the link is "
            "now used up. Text her for a fresh one."
        )

    endpoint = (data.get("endpoint") or "").strip()
    path = write_credentials(key, endpoint, backend)

    print(f"Connected. Key {creds.mask(key)} stored in {path} (readable only by you).")
    if endpoint:
        print(f"MCP endpoint: {endpoint}")
    print("Now run /reload-plugins so the connection picks it up, then /aria:status.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
