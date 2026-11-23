# Aria for Claude Code

Aria is an AI that calls and texts you. This plugin connects her to Claude Code:
Claude Code reads Aria's memory of you before it plans anything, and Aria can
queue work to this session and text you the result.

## Install

**Needs Claude Code 2.1.280 or newer** (`claude --version`) and `python3`.
Nothing else: no `uv`, no packages, no clone.

```
claude plugin marketplace add Rajathbharadwaj/aria-claude-code
claude plugin install aria@aria
```

Then, inside Claude Code:

```
/aria:connect
```

It shows a link and a code. Open the link on your phone or computer, sign in
with your phone number, check the code matches, and tap **approve**. Then:

```
/reload-plugins
/aria:status
```

Already have a setup link Aria texted you? `/aria:connect <link>` still works.
From a terminal instead: `npx aria-connect` does the same sign-in and installs
the plugin.

### Talk to this session from the Aria app

Your messages and voice from the app's **Connect to Claude** screen arrive in
the session through a Claude Code *channel*. Until this plugin is on Anthropic's
approved channel list, start the sessions you want to reach with:

```
claude --dangerously-load-development-channels plugin:aria@aria
```

Claude Code asks you to confirm the flag each time it starts. To skip typing
it, `npx aria-connect` installs a `claude-aria` launcher, or add an alias:

```
alias claude-aria='claude --dangerously-load-development-channels plugin:aria@aria'
```

Your replies reach the phone only through `aria_say`: Claude is told this in
every message from the app.

Without the flag everything else (memory, tasks, `aria_say`, `aria_call_me`)
still works; only app messages can't reach the session.

## The five commands

| | |
|---|---|
| `/aria:connect` | sign in with Aria (or `/aria:connect <link>` for a texted link) |
| `/aria:status` | what is connected and what is not |
| `/aria:watch` | let Aria see what THIS repo's sessions do (off by default) |
| `/aria:unwatch` | stop that |
| `/aria:disconnect` | remove the key; MCP, presence and hooks all go dark |

## Letting Aria see what a repo's sessions do

Connecting lets Aria read your memory and target this session. It does **not**
let her see the work. That is a separate switch, it is per repo, and it is gated
on consent:

```
/aria:watch        # shows the consent screen. Turns nothing on.
/aria:watch yes    # records the consent, checks it will work, then starts
/aria:unwatch      # withdraws the consent and stops
```

On the default `mcp` transport, each field is templated individually, so this is
what crosses the wire and there is nothing else in the payload:

**Sent** — your shell command *lines*, the *paths* of files you write or edit,
commit SHAs, the working directory, a session id, your assistant's last message
as a turn summary, and — for a command that **fails** — the error text Claude
Code reports for it, which is that command's own error output, read for the exit
code.

**Never sent** — file contents, not one byte. The output of any command that
succeeds. Anything from Read, Grep, Glob, Task, WebFetch, WebSearch or TodoWrite,
or any MCP tool: those are not watched at all.

`/aria:watch yes` sends one live test event before it claims anything, and prints
a refusal instead of a tick if Aria's server would discard what this repo sends.

---

Everything below is detail. You do not need it to get running.

## What this plugin does NOT do

It does not let Aria run work on your machine while you're away — the thing
people install this hoping for.

Both halves are passive. The MCP connection only looks for queued work when the
model calls `aria_next_task`, which happens while *you* are driving a session.
The hooks only fire when something runs. Neither polls. Text Aria from the car
and the task sits until it expires.

That needs an always-on executor (`scripts/start_code_worker.sh` in the Aria
repo, not in this plugin, because a plugin cannot run a background process). It
is a background agent with write access to your code, so it is deliberately not
something you get from one line of setup. It registers the canonical git remote
of the directory you point it at and is refused `kind='code'` work unless that
directory is a real git worktree — an executor with no repo runs your task in an
empty folder and reports "nothing was produced".

## Where the key comes from

An `aria_sk_` account key, minted under a real login — never by another key, so a
leaked key cannot mint more. You never handle it:

```
you → Aria:  "connect my claude code"
Aria → you:  https://aria.paralleluniverse.ai/connect/<code>   (single use, 15 min)
you       :  /aria:connect <that link>
```

The link carries no credential. The key is minted **at redemption**, returned
once, and written to `~/.aria/credentials` (0600). The server keeps a sha256 hash.
Redeeming twice returns `410`, atomically (Redis `GETDEL`), so two concurrent
opens cannot both mint.

Lost it? Ask for a new link; the old key works until revoked. The key **is** your
identity to the server: every tool derives `user_id` from the verified token and
none takes a `user_id` argument, so there is no way — even for a confused model —
to ask about someone else.

### If you'd rather paste a key

`/plugin` → **aria** → **Aria API key** works and **takes precedence over the
file**. It is `sensitive`, so it lands in the OS keychain, not in `settings.json`.
This is also the fallback on a client too old for `headersHelper`.

If you reconnect and the old key keeps failing, that stale keychain entry is why —
`/aria:status` prints which source is in play.

## The tools

Six read Aria's memory of you; two are the work channel. No count in prose — this
said "8" long enough to be wrong by three. `_EXPECTED_TOOLS` in
`services/backend/tests/test_mcp_remote.py` is the one place that knows.

| Tool | What it does |
|---|---|
| `aria_recall(query, k=8)` | Semantic search across your memory. The one to reach for: ground a plan in what you actually think, prefer, and have already tried. |
| `aria_entities(limit=30)` | The people, projects and places Aria knows about, ranked by how often they come up. |
| `aria_entity(name, limit=20)` | Everything Aria knows about one of them. |
| `aria_journal(limit=10)` | Recent dated digests of your conversations with Aria. |
| `aria_todos(limit=20)` | Your open commitments and their deadlines. |
| `aria_vitals()` | Aria's read on your current state, themes and patterns. Cached ~10 min. |
| `aria_next_task()` | Claim the next task Aria queued **for this session**. |
| `aria_submit_result(task_id, summary, …)` | Hand the result back. `artifact_b64` uploads one file (1 MB cap). |

`aria_session_turn`, `aria_session_event` and `aria_progress` also exist; Claude
Code calls the first two from the `/aria:watch` hooks, so `claude mcp list` shows
more tools than an agent can reach. Results cap at 24 KB. Everything except
`aria_submit_result` is read-only.

## Why a hook, and not just a tool

An MCP tool is called **by** the session and cannot read it — no transcript, no
cwd, no sense of what you asked for. And a tool the model *chooses* to call is one
a model busy finishing does not call: a real session worked an objective to
completion and reported nothing.

A hook is the inverse. Claude Code fires it whether or not the model cooperates.

**What the receipts prove:** they defeat *forgetting*, not lying. A hook fires
whether or not the model decides to report. But `commit_sha` is an argument a
model could pass, and on the HTTP transport a session with a shell can read its
own key — so a receipt is *what the session said it did*, corroborated by
something having run. Not an independently verified fact.

`--transport http` still exists, needs `ARIA_API_KEY` exported, does not watch
file edits, and does send a command's output (read for an exit code, not stored).
`/aria:watch` says so and refuses to claim the stream is on without the key.

`/aria:unwatch` withdraws the consent and removes the hooks. It does **not delete
what was already recorded** — ask Aria for that. Hooks load at session start, so
both take effect after `/reload-plugins`.

## Turning it off

```
/aria:unwatch         # stop streaming what THIS repo's sessions do
/aria:disconnect      # remove the local key: MCP, presence and hooks go dark
/plugin disable aria  # stop the plugin running at all
```

`/aria:disconnect` also **revokes the key on Aria's side**
(`DELETE /v1/delegate/keys/self`), so a copy taken off this machine stops working.
Revocation happens *before* the file is deleted — if it fails, the key is left in
place so you can retry, because deleting it first throws away the only credential
that can revoke itself. `--local-only` skips the revocation deliberately. It also
tells you if a key in `/plugin` or `$ARIA_API_KEY` is still shadowing the file,
because that would leave you connected while believing you weren't.

A key may revoke only *itself*: the route is authenticated by the key and names no
key, so a stolen key can do nothing with it but destroy the access it stole.

## Presence

The server registers your session off the MCP connection itself, not a poll. The
transport's `Mcp-Session-Id` (from the `initialize` **response** header, under the
authenticated principal) becomes a `delegate_instances` row. The held GET stream
ending marks it stale; a 90 s heartbeat TTL is the backstop.

Honest limit: process death is detected sub-second because the socket closes. A
**closed laptop or dropped WiFi sends nothing** — that connection goes half-open
and detection falls back to TCP keepalive, bounded by the 90 s TTL.

Full mechanics: [`docs/mcp-server.md`](../../docs/mcp-server.md).

## Without the plugin

The MCP half is a plain remote server, so any client speaking Streamable HTTP with
custom headers can use it:

```bash
claude mcp add --transport http aria https://mcp.aria.paralleluniverse.ai/mcp \
  --header "Authorization: Bearer aria_sk_..."
```

You get every tool and presence. You give up the hooks and the `/aria:*` commands.

## What's in this directory

```
.claude-plugin/plugin.json    manifest + userConfig (api_key / mcp_url / backend_url)
.mcp.json                     the remote server: type=http, url, Bearer, headersHelper
hooks/hooks.json              SessionStart only — the stream is per-repo, see /aria:watch
bin/aria_credentials.py       ONE definition of where the key lives, and the client floor
bin/aria_headers.py           the MCP headersHelper — emits the Authorization header
bin/aria_connect.py           /aria:connect — redeems the setup link, writes the key
bin/aria_disconnect.py        /aria:disconnect — removes it, and says what it cannot revoke
bin/aria_status.py            the local half of /aria:status
bin/aria_preflight.py         SessionStart — says "not connected" in actionable words
bin/aria_session_reporter.py  UNWIRED — the old global Stop hook, kept for the non-plugin rig
bin/aria_watch.py             /aria:watch and /aria:unwatch — the per-repo event stream
skills/*/SKILL.md             the five commands
```

The plugin's own global `Stop` hook was **unwired**: it streamed every session you
ever ran the moment you installed, and once the server's `aria_session_turn` hook
landed it produced two rows for one turn. Turn summaries now come from
`/aria:watch`, per repo.

`mcp_url` and `backend_url` are separate on purpose: the MCP server is its own
single-instance Cloud Run service (sessions live in a per-process dict, so it
cannot scale horizontally); the REST backend is the autoscaled `aria-backend`.

## Development

```bash
claude --plugin-dir .                        # load locally without installing
claude plugin validate . --strict            # manifest check, warnings fatal
```

Point at a local backend via `/plugin` → aria, or `ARIA_BACKEND_URL` for the hook.

Server side: `services/backend/aria_backend/mcp_server/` — `app.py`, `auth.py`,
`tools.py`, `presence.py`, `delegate_bridge.py`. Tests:
`services/backend/tests/test_mcp_remote.py`, `test_watch_consent.py`,
`test_watch_plugin_truth.py`.
