---
description: Let Aria see what this repo's sessions are doing. Shows a consent screen first and turns nothing on until the user accepts. Run it as /aria:watch, then /aria:watch yes.
disable-model-invocation: true
---

# Let Aria watch this repo

This is a deliberate, per-repo act, and it is gated on consent. It is **not**
shipped in the plugin's own hooks, on purpose: a plugin's hooks apply to every
session the user ever runs, so shipping it would stream their client work,
their employer's code, and every repo they ever cloned to read.

## Step 1 — show the screen. Always.

Run it with `$ARGUMENTS` passed straight through:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/aria_watch.py" $ARGUMENTS
```

With no arguments the script prints the consent screen, writes **nothing**, and
exits 2. **Print that screen verbatim.** Do not summarise it, do not shorten it,
and do not answer it on the user's behalf — a consent someone else accepted is
not a consent. Then stop and wait.

If the user runs `/aria:watch` and the screen appears, your entire job that turn
is to show it and ask whether they want to go ahead.

## Step 2 — only if they say yes

They re-run `/aria:watch yes`. `$ARGUMENTS` carries the word and the script
takes it from there, in this order, and nothing is written until all of it
passes:

1. **A live test event** goes to Aria's server. If it comes back `stream-off`
   the account is not allowed to stream at all, and the script stops *before*
   recording anything — no hooks, no consent row.
2. **The consent is recorded server-side** (repo, version, timestamp, user).
3. **The backend's own verdict** on that repo comes back with it. If it says
   the events will not be kept, the script stops there too.
4. Only then are the hooks written.

If any of that refuses, **do not soften it**. The script prints `Aria is NOT
watching …` and a sentence saying which gate said no. Relay that sentence and
stop; retrying changes nothing, and the thing to do is usually to ask Aria to
switch watching on for the account.

## Step 3 — relay what it printed

- **Whether she is actually watching.** The script only prints "Aria is now
  watching" when something confirmed it, and names what confirmed it. If it
  instead says the stream is **UNVERIFIED**, say exactly that — do **not**
  round it up to "watching". A green tick nobody could back is the bug this
  whole flow was rewritten to stop telling.
- **Which repo** is watched and **which file** was written. The point is that
  it is visible and diffable.
- **Where the config came from.** It fetches the canonical config from the
  backend so it cannot drift from what the ingest accepts. If it says *a
  bundled copy*, say that — the block may be behind the server.
- **Which events**, and over which transport.
- **The transport's privacy answer**, which differs and is not a detail:
  - `mcp` (the default): every event rides the already-authenticated MCP
    connection. Each field is templated individually, so **no file contents
    leave the machine at all**, and neither does the output of a command that
    **succeeds** — only paths, command lines, SHAs, and, for a command that
    FAILS, the error text Claude Code reports for it. Say that last part; the
    screen does, and a summary that drops it is the contradiction this text
    used to carry. Nothing to export.
  - `http`: needs `ARIA_API_KEY` in the environment, and does not watch file
    edits at all. If the script prints `PARTLY LIVE ONLY`, do **not** say Aria
    is watching; give them the export line it printed.

Hooks load at session start, so it takes effect after `/reload-plugins` or a
restart. Say so.

## If they ask what the receipts prove

They defeat forgetting, not lying: a hook fires whether or not the model
decides to report, so a busy session cannot silently skip it. But the event
carries `commit_sha` as an argument, and on the HTTP transport a session with a
shell can read its own key — so a receipt is *what the session said it did*,
not an independently verified fact. Say that if it comes up; do not describe
the stream as proof.

## Do not

- Do not offer to widen the matchers. Read, Grep, Glob, Task, WebFetch,
  WebSearch, TodoWrite and every MCP tool are absent by design; that is the
  privacy control, not an optimisation.
- Do not pass `--accept` yourself. It exists for the user's "yes".

## Turning it off

`/aria:unwatch` — removes the hooks and withdraws the consent record.
