#!/usr/bin/env python3
"""`/aria:disconnect` — the off switch. Revokes the key, then removes it.

WHAT CHANGED, AND WHY IT MATTERED
---------------------------------
This used to delete `~/.aria/credentials` and print an apology: the key still
existed server-side, because every `/v1/delegate/keys/*` route required a login
JWT and an `aria_sk_` key deliberately could not revoke keys. So a key that had
already been copied off the machine kept working *after the user believed they
had turned it off*, and the off switch said so out loud because it could not do
better.

An off switch that does not turn the thing off is the defect. The honest
message about it was not a fix. `DELETE /v1/delegate/keys/self` closes it: a
key may revoke ITSELF, authenticated by itself, with no parameter naming which
key — so the only thing a stolen key can do with it is destroy the access it
stole. The rule that route protects ("a key may not grow its own authority") is
untouched; this only ever destroys authority.

ORDER IS LOAD-BEARING
---------------------
Revoke FIRST, delete SECOND. Deleting first and then failing to revoke would
throw away the only credential that can perform the revocation — the user would
be left unable to switch it off from this machine at all. So on a revoke
failure the file STAYS, and the message says the machine is still connected and
how to force it. That is the opposite of the reassuring default, and it is the
right one: the user asked for the thing to be off.

Stdlib only. Never prints a key.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aria_credentials as creds  # noqa: E402

REVOKE_PATH = "/v1/delegate/keys/self"


def revoke(backend: str, key: str, timeout: float = 15.0) -> tuple[bool, str]:
    """Returns (revoked, human sentence). Never raises."""
    req = urllib.request.Request(
        backend.rstrip("/") + REVOKE_PATH,
        headers={"Authorization": f"Bearer {key}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        label = data.get("label") or "this machine"
        return True, f"Revoked the key for {label} on Aria's side — it is dead everywhere now."
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # Already revoked, or never valid. Either way there is nothing
            # live to switch off, which is the state the user wanted.
            return True, "That key was already invalid or revoked on Aria's side."
        if e.code == 404:
            return False, (
                f"{backend} has no self-revoke endpoint — it is running a backend older "
                "than this plugin. Ask Aria to revoke your Claude Code key instead."
            )
        return False, f"Aria's backend refused the revocation (HTTP {e.code})."
    except urllib.error.URLError as e:
        return False, f"Could not reach {backend} to revoke the key ({e.reason})."
    except Exception as e:  # noqa: BLE001
        return False, f"Could not revoke the key: {e}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Disconnect this machine from Aria.")
    ap.add_argument(
        "--local-only",
        action="store_true",
        help="remove the key from this machine WITHOUT revoking it server-side",
    )
    ap.add_argument(
        "--status",
        action="store_true",
        help="report what is set without changing anything",
    )
    ap.add_argument("--backend", default=None)
    args = ap.parse_args(argv)

    path = creds.credentials_path()
    env_key = (os.environ.get("CLAUDE_PLUGIN_OPTION_API_KEY") or "").strip()
    plain_env_key = (os.environ.get("ARIA_API_KEY") or "").strip()
    key = creds.api_key()

    if args.status:
        print(f"credentials file : {'present' if path.exists() else 'absent'} ({path})")
        print(f"plugin config key: {'set' if env_key else 'not set'}")
        print(f"ARIA_API_KEY     : {'set' if plain_env_key else 'not set'}")
        return 0

    if not key:
        print(f"No Aria key on this machine — nothing to disconnect. ({path} is absent.)")
        return 0

    backend = (args.backend or creds.backend_url()).rstrip("/")
    revoked, why = (False, "Skipped revocation (--local-only).")
    if not args.local_only:
        revoked, why = revoke(backend, key)
    print(why)

    if not revoked and not args.local_only:
        # THE FILE STAYS. See the docstring: deleting it here would strand the
        # user without the one credential that can revoke itself.
        print(
            "This machine is STILL CONNECTED and the key still works. The key has been "
            "left in place so you can retry — run /aria:disconnect again when you have "
            "a network."
        )
        print(
            "To unhook this machine anyway and leave the key live, run: "
            "/aria:disconnect --local-only"
        )
        return 1

    try:
        path.unlink()
        print(f"Removed {path}. This machine no longer has an Aria key on disk.")
    except FileNotFoundError:
        print(f"No credentials file at {path} — nothing to remove there.")
    except Exception as e:  # noqa: BLE001
        print(
            f"Could not remove {path}: {e}. Delete it by hand.",
            file=sys.stderr,
        )
        return 1

    # A key in the keychain or the environment SHADOWS the file just deleted,
    # so a user who also pasted one into /plugin is still connected. Saying
    # "disconnected" without checking would be false.
    if env_key:
        print(
            "STILL CONNECTED: a key is set in the plugin's own config. Clear it in "
            "/plugin → aria → Aria API key, then /reload-plugins."
        )
    if plain_env_key:
        print(
            "STILL CONNECTED: ARIA_API_KEY is set in this shell's environment. "
            "Unset it (and remove it from your shell profile) to finish."
        )
    if not env_key and not plain_env_key:
        print("Run /reload-plugins to drop the live connection now.")
        print("To stop the plugin running at all: /plugin disable aria")

    if args.local_only:
        print(
            "The key still EXISTS and still works on Aria's side — --local-only only "
            "removed it from this machine. Ask Aria to revoke your Claude Code key to "
            "kill it for good."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
