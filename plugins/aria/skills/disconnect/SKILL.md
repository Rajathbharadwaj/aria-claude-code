---
description: Stop Aria seeing this machine — remove the local key so the MCP connection, presence and hooks all go dark. Run it as /aria:disconnect.
disable-model-invocation: true
---

# Disconnect this machine from Aria

One command. Run it:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/aria_disconnect.py"
```

Then tell them to run `/reload-plugins` to drop the live connection now.

## Relay the output exactly

The script prints up to three things, and each is load-bearing:

- **What it removed.** `~/.aria/credentials` is gone, so the MCP server stops
  authenticating, presence drops, and the hooks find no key and post nothing.
- **`STILL CONNECTED:` lines.** A key in `/plugin` → aria, or an `ARIA_API_KEY`
  in their shell, **shadows the file that was just deleted** — so this machine
  is still fully connected. If the script prints one of these, do not say
  "disconnected". Say which one is still set and where to clear it.
- **That the key was revoked**, and for which machine (by its label).

## What it does, in order — and why the order matters

1. **Revokes the key on Aria's side**, via `DELETE /v1/delegate/keys/self`. A
   key may revoke itself: it is authenticated by itself and the request names
   no key, so the worst a stolen key can do with that route is destroy the
   access it stole.
2. **Then** removes `~/.aria/credentials`.

If the revocation FAILS, the file is deliberately **left in place** and the
script exits non-zero. Deleting it first would throw away the only credential
that can perform the revocation, leaving them unable to switch it off from this
machine at all. Relay that: the machine is still connected, retry when they
have a network, or `--local-only` to unhook this machine and leave the key
live.

Say which happened. "Disconnected" after a failed revoke is the thing this
whole flow exists to stop being true.

## Degrees of off

- `/aria:disconnect` — no credential, so no data flows. The plugin still loads.
- `/plugin disable aria` — the plugin stops running at all: no hooks, no MCP
  server, no skills.
- `/plugin uninstall aria@aria` — gone from this machine.

Offer the second one if they want the code to stop running, not just the data
to stop flowing.
