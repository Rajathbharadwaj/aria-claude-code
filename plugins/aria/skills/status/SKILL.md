---
description: Show whether this Claude Code is connected to Aria — the credential, the MCP transport, presence registration, and the hooks — and exactly what is broken if anything is.
disable-model-invocation: true
---

# Aria connection status

Report the real state. Never infer one check from another: the four things
below fail independently, and the entire point of this skill is to name *which*
one broke and the single thing that fixes it.

## Rule zero: NEVER end this turn without printing the report block

Measured on a cold install, 2026-09-23: on an old client `/aria:status`
printed **zero bytes** and exited 0. Every other failure here has one
actionable sentence; that one had nothing at all, and a user staring at
nothing has no idea whether the command ran, the plugin is missing, or their
account is broken.

So: whatever happens — the script errors, `${CLAUDE_PLUGIN_ROOT}` does not
expand, the tools are absent, a call 401s — you still print the block from
**How to report it** with the failing lines marked. A line you could not check
reads `unknown — <why>`. Silence is the one answer that is never correct.

If the Bash call itself fails (no such file, no `python3`), say that as the
`credential` line and keep going; the tool calls below are independent of it.

## 1. The local half — run the script

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/aria_status.py"
```

It prints which credential is in play and where it came from, the endpoints,
whether `python3` and the hook script exist, and a `MISMATCH` line if the key
was minted against a different host than the plugin dials (that failure
surfaces as a 401 and gets blamed on the key).

It also prints a `note` line about its own blind spot: Claude Code does **not**
export `CLAUDE_PLUGIN_OPTION_*` into Bash commands, so a key pasted into
`/plugin` is invisible to this script. That is why the tool calls below are
what settle it, and why you must not report "no key" on the script alone.

## 2. Transport

Is the `aria` MCP server connected? The `aria_*` tools existing at all is the
signal. If they don't, **name the cause in this order and stop** — the
remaining checks cannot run, but you still print the block:

1. **The client is too old.** Read the `claude code` line the script printed.
   Below the floor it names, this plugin does not load *at all* — no tools, no
   hooks, no MCP error, nothing. That is not a broken key and telling someone
   to reconnect will waste their afternoon. Say: *"Claude Code &lt;version&gt; is
   below &lt;floor&gt;; the Aria plugin does not run on it. Upgrade Claude Code."*
2. The plugin is disabled, or was installed this session and not reloaded —
   `/reload-plugins`.
3. It is loaded and the server itself failed to start; `/mcp` shows that.

Never report an absent `aria_*` tool as an auth or account problem.

## 3. Auth

Call `aria_recall` with `k=3` and a trivial query. Any answer, **including an
empty list**, means the key was accepted and the user was derived from it. An
empty list is a healthy answer, not a broken one — say "connected, no matching
memories", never "not working".

401/403 → the key is wrong, revoked, or minted against another backend. Work
through these three in order before blaming the key, because all three present
as the same 401 and the MCP layer's own advice ("clear authentication tokens in
your MCP client") does not apply — there are no OAuth tokens here:

1. The `MISMATCH` line from step 1 — a key minted against one host and offered
   to another.
2. **The client is too old for `headersHelper`.** If step 1 shows the key
   coming from `~/.aria/credentials` and auth still 401s, the MCP connection
   may never have read that file: `headersHelper` is how it gets there, and an
   older Claude Code ignores the field silently. Check `claude --version`
   against the Requirements section of the plugin README. The fix is to put the
   key in `/plugin` → aria instead, which every version supports — or upgrade.
3. The key really is revoked. Ask Aria for a fresh setup link.

## 4. Presence

Call `aria_next_task`. A *different* check from auth:

- `"No tasks queued right now."` or a real task → this session is registered as
  a live instance. Healthy.
- `"This session isn't registered with Aria yet…"` → auth worked, presence did
  not. The transport is up and Aria still cannot target this session. Say
  exactly that. `/reload-plugins` re-registers it.

## 5. The hooks

They run locally and are invisible to the tools, so they are checked
separately — step 1 covers whether they *can* run (script present, `python3`
found). They fail silently by design, so "the tools work" says nothing about
them. If the user wants proof a hook actually reached Aria, that evidence lives
server-side, not here: say it isn't locally observable rather than guessing.

## How to report it

Six lines, no ceremony, and **always all six** — see Rule zero:

```
claude code 2.1.281 — ok
credential  ok — from ~/.aria/credentials (…a1b2)
transport   ok — aria @ mcp.aria.paralleluniverse.ai
auth        ok — key accepted
presence    ok — registered, no tasks queued
hooks       ok — reporter present, python3 found
```

If something is broken, name which of the six and the one thing that fixes it.
A check you could not run is `unknown — <why>`, never omitted and never
guessed. Do not report "connected" unless presence answered.

To turn it all off: `/aria:disconnect`.
