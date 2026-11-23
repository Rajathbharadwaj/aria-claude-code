---
description: Connect this Claude Code to Aria - sign in with Aria (no link needed), or redeem a setup link she texted. Run it as /aria:connect, or /aria:connect <link>.
disable-model-invocation: true
---

# Connect this machine to Aria

`$ARGUMENTS` is the setup link Aria texted them. The whole job is one script
call and one honest verification. Be brief — four lines of output, not an essay.

## If they gave you a link

Run it, from the plugin root:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/aria_connect.py" $ARGUMENTS
```

The script redeems the link, mints the key server-side, and writes
`~/.aria/credentials` with mode 0600. It prints one sentence on success and one
sentence on every failure, each saying what to do next. **Relay that sentence.
Do not reinterpret it** — it already distinguishes an expired link from an
unreachable backend from a wrong backend address, and a paraphrase loses that.

On success, tell them to run `/reload-plugins`. The MCP connection reads the
credential when it connects, so until it reconnects nothing has changed. Then
go to **Verify**.

## If they gave you nothing — sign in with Aria

Two steps. Step 1 returns at once, so they can see the link while step 2 waits:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/aria_connect.py"
```

It prints a link and a code (and opens the browser when it can). **Show them
the link and the code in your reply**, one line each, and say: open it, sign in
with your phone, check the code matches, tap approve. Then run step 2 straight
away, in the same turn, with a 10-minute timeout:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/aria_connect.py" --wait
```

It returns when they approve, writing `~/.aria/credentials` (0600). Relay its
sentence exactly, as above. If it says it is still waiting, run `--wait` once
more; if it says expired, start again from step 1. On success: `/reload-plugins`,
then **Verify**.

## Verify — and do not skip this

Three things fail independently and the whole point is to say *which*:

1. **Transport.** Is the `aria` MCP server connected? If the `aria_*` tools
   don't exist, the plugin isn't loaded or wasn't reloaded. Stop and say that.
2. **Auth.** Call `aria_recall` with `k=3` and a trivial query. Any answer —
   *including an empty list* — means the key was accepted and the user was
   derived from it. A 401/403 means the key is wrong, revoked, or minted
   against a different backend than the plugin dials.
3. **Presence.** Call `aria_next_task`. This is a *different* check from auth
   and it is the one people conflate:
   - `"No tasks queued right now."` or a real task → registered as a live
     instance. Healthy.
   - `"This session isn't registered with Aria yet…"` → auth worked, presence
     did not. Aria still can't target this session. Say exactly that;
     `/reload-plugins` re-registers it.

**Never report "connected" unless (3) answered.** A session that authenticates
but never registers is precisely the failure this three-way split exists to
make visible.

## Then say what became true

Two lines, no ceremony:

- Aria can hand work to this session and text back the result.
- Claude Code can read their Aria memory before it plans anything.

## Notes

- Nothing is installed — no `uv`, no packages, no clone. The tools run on
  Aria's server; only the hooks run locally, on stdlib `python3`.
- A key set in `/plugin` → aria **wins over** the file. If they connected but
  `/aria:status` still shows the old key, that stale keychain entry is why.
- To turn it off: `/aria:disconnect`.
- The key is shown once and stored hashed. Lost it? Ask Aria for a new link;
  the old key keeps working until it is revoked.
- Never echo a key back in full. Last four characters only.
