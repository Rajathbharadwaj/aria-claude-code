---
description: Stop Aria seeing what this repo's sessions do — removes the event-stream hooks from this repo's .claude/settings.json. Run it as /aria:unwatch.
disable-model-invocation: true
---

# Stop Aria watching this repo

One command:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/aria_watch.py" --off
```

Add `--repo <path>` to target a different directory than the cwd.

It does two things, and both matter:

1. **Withdraws the consent record** on Aria's side for this repo. That is the
   thing that says "she may", so it is the thing to clear — and it is cleared
   even if no hooks were found, because the two can drift apart if someone
   hand-edited `settings.json`.
2. **Removes the hook entries**, identified as mcp_tool calls into Aria's
   server or posts to her ingest. Every other hook in that file — the user's
   own, another tool's — is left exactly as it was.

If the consent withdrawal fails but the hooks come out, it says so plainly:
nothing more is sent, but the record still reads as granted. Relay that
distinction rather than rounding it to "done".

**It does not delete what was already recorded.** Say that if they are
disconnecting for privacy rather than convenience — they need to ask Aria to
delete it. The consent screen says the same, and the two must not disagree.

## Relay this bit

Hooks are loaded at session start, so the ones already live in **this** session
keep firing until `/reload-plugins` or a restart. The script says so; repeat it,
because "I turned it off" followed by more events arriving is how someone stops
trusting the switch.

## What this does not touch

The MCP connection and the memory tools. `/aria:unwatch` stops the *stream of
what this repo's sessions do*; it does not disconnect the machine.

- Stop the stream for one repo: `/aria:unwatch`
- Disconnect the machine entirely: `/aria:disconnect`
- Stop the plugin running at all: `/plugin disable aria`

Offer the right one for what they actually asked.
