# Antenna

**A local-first feed reader for email and MCP.** Antenna pulls RSS / Atom / JSON Feed, stores posts in SQLite with full-text search, emails you what's new, and exposes the same index to AI agents over MCP.

Phase 0 is a single-user local release. No service, no account, no hosted anything — it's a Python package, a SQLite file, and a launchd agent. You own all of it.

---

## Why

Mainstream RSS-to-email services (you know which one) have drifted into loud, out-of-context ads and dated layouts. Meanwhile everyone's workflow now includes at least one AI agent. Antenna is what happens when you rebuild the feed-subscription backbone for a world where your inbox and your agent are both first-class readers:

- **Email output** — clean per-post or digest HTML, delivered via your own SMTP (Gmail App Password works fine).
- **MCP output** — `list_sources`, `search_posts`, `get_post`, and friends, exposed over stdio so Claude Desktop or any MCP client can answer "what's new on Hackaday about Z80?" directly against the same SQLite.

Same subscriptions, same index, two outputs.

---

## What's in Phase 0

- 14 stable CLI subcommands covering the full loop (`init`, `add-feed`, `import-opml`, `fetch`, `sync`, `list-sources`, `recent-posts`, `search`, `doctor`, `render-digest`, `send-email`, `setup-email`, `test-email`, `serve-mcp`).
- 1 experimental local-only subcommand (`fetch-agent-eyes`) behind an explicit `--experimental-agent-eyes` flag on this branch.
- SQLite schema with `sources`, `posts`, `subscriptions`, `deliveries` — plus an FTS5 virtual table so search is fast.
- Fetcher for RSS, Atom, and JSON Feed with ETag / Last-Modified conditional GET.
- Deduplication by stable entry id across polls, so you never get the same post twice.
- Jinja2 HTML email templates (per-post + digest), dark-mode friendly.
- SMTP sender with a `--dry-run` mode that writes HTML to `outbox/` for visual preview.
- Rule engine: include / exclude terms per feed (or `*`), plus an `alert` flag that promotes a match to an immediate email.
- MCP stdio server with six tools, pointed at the same SQLite file.
- OPML import so you can walk your existing Blogtrottr / Feedly / Inoreader subs over in one command.

Phase 0 is explicitly not: hosted, multi-tenant, a Slack/Discord router, or anything involving embeddings. Those are on the roadmap, not in the box.

This branch also contains an experimental, local-only Agent Eyes bridge for
hard sources that are not real feeds. It is hidden behind the
`--experimental-agent-eyes` flag and documented in
`docs/tim-agent-eyes-experiment.md`.

---

## Quick start

### 1. Prereqs

- macOS (these instructions) or Linux. Windows should work but isn't what I'm testing.
- Python 3.12+.
- A Gmail account with 2FA, if you want email output. We'll generate an App Password in a minute.

### 2. Get the code

```bash
git clone https://github.com/toddllm/antenna.git ~/code/antenna
cd ~/code/antenna
```

In these examples, the code checkout lives in `~/code/antenna`. Your config,
database, and logs live separately in `~/antenna/`, outside the repo.

For Tim's experimental hard-source branch, check out the branch before
installing:

```bash
git checkout tim/agent-eyes-experimental-local
```

If you'd rather pin a known snapshot than track `main`, check out `v0.1.1`
after cloning or download the tagged source release from GitHub. This is pure
Python; there is no native build step.

### 3. Install

Pick one.

**Editable install (recommended — gives you an `antenna` command on your PATH):**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

**Or run it without installing** — works fine, you just have to use `python3 -m antenna.cli` instead of `antenna`:

```bash
pip install -r requirements.txt
export PYTHONPATH="$(pwd)"
```

From here on the docs show `antenna …`; substitute `python3 -m antenna.cli …` if you didn't install.

### 3.5. Prove the install on this machine

If you want a confidence check before wiring up your own feeds or SMTP:

```bash
bash scripts/smoke_test.sh
```

That creates a throwaway config, fetches a couple of public feeds, renders both
email modes, and exercises the MCP server over real stdio.

### 4. Pick a home for your config

I keep mine at `~/antenna/`. Nothing about that path is required; it's just a convenient place that isn't inside the repo.

```bash
mkdir -p ~/antenna/logs
cp antenna.example.yaml ~/antenna/antenna.yaml
```

Either point every command at it with `-c`, or set it once:

```bash
export ANTENNA_CONFIG="$HOME/antenna/antenna.yaml"
```

(I put that line in my shell rc.)

### 5. Wire up email the fast way

If you want inbox delivery, the easiest path is to let Antenna write the SMTP
section for you and then send a fixed test email before your first real sync.

**Gmail preset:**

1. Open <https://myaccount.google.com/apppasswords>. This requires 2FA on your Google account.
2. Create a new App Password; name it `antenna`. Copy the 16-character string.
3. Run:

```bash
antenna setup-email \
  --provider gmail \
  --gmail-address you@gmail.com \
  --app-password "xxxx xxxx xxxx xxxx" \
  --to-address you+antenna@gmail.com

antenna test-email --dry-run
antenna test-email
```

That rewrites the `smtp:` and `email:` sections in `~/antenna/antenna.yaml`
and saves a timestamped backup beside it before making any change.

**Amazon SES preset:**

```bash
antenna setup-email \
  --provider ses \
  --region us-east-1 \
  --username YOUR_SMTP_USERNAME \
  --password YOUR_SMTP_PASSWORD \
  --from-address feeds@yourdomain.com \
  --to-address you@yourdomain.com

antenna test-email
```

**Generic SMTP:**

```bash
antenna setup-email \
  --provider generic \
  --host smtp.example.com \
  --port 587 \
  --username YOUR_SMTP_USERNAME \
  --password YOUR_SMTP_PASSWORD \
  --from-address you@example.com \
  --to-address you@example.com
```

If you prefer to edit YAML directly, this is the Gmail equivalent:

```yaml
smtp:
  host: smtp.gmail.com
  port: 587
  username: you@gmail.com
  password: "xxxx xxxx xxxx xxxx"    # the App Password
  use_tls: true

email:
  from_address: you@gmail.com
  from_name_template: "{feed_title}"   # Gmail will group threads by sender
  to_address: you+antenna@gmail.com    # plus-tag → easy filter in Gmail
```

If you're not sending email yet, leave the defaults and only run `--dry-run` commands until you're ready.
`antenna.example.yaml` is starter data, not a forever-maintained bundle of canonical feeds, so it's worth swapping in the feeds you actually care about before your first long-running sync.

### 6. Initialize the database and load your feeds

```bash
antenna init
```

This creates `~/antenna/antenna.db` (or wherever `database:` in the config points) and loads every feed from the `feeds:` section of the YAML.

### 7. First fetch

```bash
antenna -v fetch
```

Expected output: one line per feed, `✓ <N> new  <title>`. The `first_run_entries` setting (default 3) caps how many historical posts each feed gets on its very first poll, so you're not flooded. Later polls keep moving forward from there; they do not backfill the older history you intentionally skipped on first run.

If one feed is broken, rate-limited, or temporarily down, Antenna records that source's `last_error`, backs that source off before retrying it again, and still exits successfully by default so later steps like `send-email` can keep working for healthy feeds. Use `antenna fetch --strict` when you want any source error to fail the command, for example in CI or while debugging one specific feed.

If you want the full headless loop in one command, run:

```bash
antenna -v sync
```

That polls feeds and immediately delivers any pending email in one step. For humans and coding agents alike, `sync` is the safest default because it avoids leaving the system half-finished after a manual fetch.

### 8. Browse what landed

```bash
antenna list-sources
antenna recent-posts --limit 20
antenna search "rust" --limit 5
antenna doctor
antenna test-email --dry-run
antenna sync --json
```

Search uses SQLite FTS5, so you get phrase match (`"exact phrase"`), boolean (`rust AND lifetime`), and prefix (`anthropi*`) for free.
`antenna doctor` is the fastest support check when a feed goes bad or you need to confirm whether email is backed up, the DB exists, or the outbox path is writable.
`antenna test-email` is the safest SMTP check because it sends a fixed probe before any feed traffic is involved.
`antenna sync --json` is the easiest one-shot entry point for coding agents because it returns one structured report for fetch plus delivery, including a `degraded` status when some feeds fail or are still in backoff while healthy ones continue to run.

### 9. Preview the digest

```bash
antenna render-digest --since 24h
```

Writes an HTML file to `outbox/` with all posts from the last 24 hours grouped by feed. Open it in a browser (or just drag it into a Finder Quick Look) to see exactly what the email will look like.

### 10. Dry-run email

```bash
antenna send-email --mode per_post --dry-run
antenna send-email --mode digest   --dry-run
```

No network, no SMTP — just writes the rendered HTML to `outbox/` and records dry-run deliveries so you don't re-preview the same posts next time.

### 11. Send for real

```bash
antenna send-email --mode digest
```

Or switch `default_mode` in the YAML and just run `antenna send-email`.

---

## Importing from Blogtrottr / Feedly / Inoreader

Most feed services export OPML. Grab the file and:

```bash
antenna import-opml ~/Downloads/feedly.opml
antenna -v fetch
```

That's the whole migration.

---

## Running on a schedule (launchd)

The repo ships a launchd plist template at `scripts/com.antenna.fetch.plist`. It fetches every 15 minutes and sends whatever's undelivered.

**Install:**

1. Copy the template and edit the `{{PLACEHOLDERS}}`:

   ```bash
   cp scripts/com.antenna.fetch.plist ~/Library/LaunchAgents/com.antenna.fetch.plist
   # open ~/Library/LaunchAgents/com.antenna.fetch.plist in your editor
   ```

   Replace:
   - `{{PYTHON}}` → `/Users/YOURNAME/code/antenna/.venv/bin/python` (or `/opt/homebrew/bin/python3`).
   - `{{ANTENNA_HOME}}` → `/Users/YOURNAME/antenna`.
   - `{{CONFIG_PATH}}` → `/Users/YOURNAME/antenna/antenna.yaml`.
   - `{{REPO_PATH}}` → `/Users/YOURNAME/code/antenna` (or delete the `PYTHONPATH` key entirely if you used `pip install -e .`).

   (launchd won't expand `~`. Use absolute paths everywhere.)

2. Load it:

   ```bash
   launchctl load ~/Library/LaunchAgents/com.antenna.fetch.plist
   launchctl start com.antenna.fetch   # optional: trigger one run now
   ```

3. Tail the logs:

   ```bash
   tail -F ~/antenna/logs/fetch.out ~/antenna/logs/fetch.err
   ```

4. Uninstall:

   ```bash
   launchctl unload ~/Library/LaunchAgents/com.antenna.fetch.plist
   rm ~/Library/LaunchAgents/com.antenna.fetch.plist
   ```

### cron alternative

If you'd rather use cron — run `crontab -e` and add:

```cron
*/15 * * * * /Users/YOURNAME/code/antenna/.venv/bin/python -m antenna.cli -c /Users/YOURNAME/antenna/antenna.yaml sync >> /Users/YOURNAME/antenna/logs/cron.log 2>&1
```

On macOS, `cron` needs Full Disk Access granted to `/usr/sbin/cron` in System Settings → Privacy & Security → Full Disk Access, otherwise it can silently fail to reach your home dir. launchd is easier.

---

## Hooking up to Claude Desktop (MCP)

Antenna exposes six MCP tools over stdio: `list_sources`, `subscribe`, `unsubscribe`, `recent_posts`, `search_posts`, `get_post`.

1. Find your Claude Desktop MCP config:

   ```
   ~/Library/Application Support/Claude/claude_desktop_config.json
   ```

2. Add an entry under `mcpServers`:

   ```json
   {
     "mcpServers": {
       "antenna": {
         "command": "/Users/YOURNAME/code/antenna/.venv/bin/python",
         "args": ["-m", "antenna.cli", "serve-mcp"],
         "env": {
           "ANTENNA_CONFIG": "/Users/YOURNAME/antenna/antenna.yaml"
         }
       }
     }
   }
   ```

   If you don't use a venv, `command` can be `python3` and you'll need `"PYTHONPATH": "/path/to/antenna/repo"` in `env`.

3. Restart Claude Desktop. Look for Antenna under the connected tools hammer icon.

Example prompt after it's wired up:

> Using the antenna tools, search for posts about "tokenizer" from the last 7 days and summarize the three most interesting ones.

For feed-health aware agents, `list_sources` includes each source's `last_error`, `consecutive_failures`, `next_poll_after`, and a `poll_status` field (`healthy`, `error`, or `backoff`), so an agent can tell the difference between a stale feed and one that's intentionally cooling off after a failure.

If you build your own Python MCP client, prefer `result.structuredContent` when it is present. Antenna's MCP server can also return one `TextContent` block per item, so naïvely joining all text blocks and calling `json.loads(...)` can fail with `JSONDecodeError: Extra data`. `scripts/smoke_test.sh` includes the canonical `parse_list()` helper.

---

## Configuration reference

See `antenna.example.yaml` — it's commented inline. The fields that matter:

- `database` / `outbox` — paths, relative to the config file (or absolute).
- `smtp.*` — SMTP host, port, username, password, TLS. Username/password can be left blank for a trusted relay that allows unauthenticated local delivery.
- `email.from_address` / `email.to_address` / `email.from_name_template` — `{feed_title}` in the name template makes Gmail thread by feed.
- `default_mode` — `per_post` or `digest`, used when `send-email` is called with no `--mode`.
- `first_run_entries` — cap on historical entries pulled on a feed's first poll.
- `poll_delay_seconds` — polite pause between feeds during `fetch`.
- `feeds:` — list of `{url, title?, tags?}`.
- `rules:` — list of `{match, include?, exclude?, alert?}`. `match` is a feed-URL glob or `*`. `include` / `exclude` terms can be plain substrings or `/regex/`. `exclude` wins. `alert: true` sends a matching post as its own email even in digest mode.
- `experimental_agent_eyes:` — branch-only local Agent Eyes sources for pages
  that are not real feeds. This is ignored unless you explicitly pass
  `--experimental-agent-eyes`; see `docs/tim-agent-eyes-experiment.md`.

---

## Command cheat sheet

```
antenna init                       # create DB, load feeds from config
antenna add-feed URL [--title T --tags a,b]
antenna import-opml PATH.opml
antenna fetch [--source-id N] [-v]
antenna sync [--source-id N] [--dry-run] [--json]

antenna list-sources [--json]
antenna recent-posts [--source-id N] [--since 24h] [--limit 20] [--json]
antenna search QUERY [--source-id N,M] [--since 7d] [--limit 20] [--json]
antenna doctor [--recent-hours 24] [--json]

antenna render-digest [--since 24h]                      # preview digest to outbox/
antenna send-email --mode per_post|digest [--dry-run] [--since 24h] [--source-id N]
antenna setup-email --provider gmail|ses|generic [...]
antenna test-email [--dry-run] [--to you@example.com]
antenna fetch-agent-eyes --experimental-agent-eyes [--json]
scripts/tim_agent_eyes_quickstart.sh --agent-eyes /path/to/agent-eyes

antenna serve-mcp                             # stdio MCP server
```

`--since` accepts `24h`, `7d`, `30m`, or an ISO 8601 timestamp.

---

## Smoke test

Before trusting the scheduler, run:

```bash
bash scripts/smoke_test.sh
```

It builds a throwaway config in `/tmp`, fetches two public feeds, exercises every CLI path, renders a digest, and drives the MCP server over real stdio with the `mcp` client library. If every stage prints a green check, Antenna is healthy on this machine.

The script prefers `./.venv/bin/python` automatically when that venv exists. If your Antenna environment lives somewhere else, override it explicitly:

```bash
PYTHON=/absolute/path/to/venv/bin/python bash scripts/smoke_test.sh
```

Pass `KEEP=1` to keep the scratch dir for inspection.

If it exits early with a missing-dependencies message, install the project into the venv you want to test:

```bash
.venv/bin/pip install -e .
```

## Headless live feed matrix

For a richer real-world parser check before handing Antenna to early adopters, run:

```bash
bash scripts/live_feed_matrix.sh
```

That script stays fully headless and exercises verified public feeds across RSS, Atom, Substack RSS, and JSON Feed using `sync --dry-run`.

---

## Troubleshooting

- **`antenna: command not found`** — either activate the venv (`source .venv/bin/activate`) or use `python3 -m antenna.cli`.
- **`Config file not found`** — set `ANTENNA_CONFIG` or pass `-c /path/to/antenna.yaml`.
- **SMTP auth fails on Gmail** — you need an App Password, not your normal password, and 2FA must be on.
- **A feed shows `ERROR:` in `list-sources`** — that feed's publisher is either down, 403'ing our User-Agent, or returning malformed XML. Antenna records the error per-source, backs that source off, and keeps polling/sending for the rest; use `fetch --strict` if you want that warning to become a hard failure.
- **You ran `fetch` manually and expected email right away** — use `antenna sync` for one-step fetch plus delivery, or run `antenna send-email` after the fetch.
- **`sync --json` returns `degraded`** — at least one feed failed during fetch or is still in backoff, but healthy feeds still finished and delivery still ran. Check `warnings`, `fetch.per_feed`, or `antenna doctor` to see which source needs attention.
- **You need a quick support snapshot** — run `antenna doctor`. It prints config/db/outbox health, failing feeds, recent delivery counts, and the next operator actions to take.
- **Search returns nothing for obvious terms** — did `fetch` actually bring any posts in? Run `antenna recent-posts --limit 5` first.
- **launchd agent doesn't run** — check `~/antenna/logs/fetch.err`. Most failures are a bad path in the plist.
- **MCP client can't see the server** — confirm `python3 -m antenna.cli serve-mcp` exits cleanly with Ctrl-C and that `ANTENNA_CONFIG` is correct.

---

## Layout

```
antenna/
├── pyproject.toml
├── requirements.txt
├── antenna.example.yaml
├── README.md
├── scripts/
│   ├── smoke_test.sh
│   └── com.antenna.fetch.plist
└── antenna/
    ├── __init__.py
    ├── __main__.py
    ├── cli.py              # 14 subcommands
    ├── config.py           # YAML config dataclasses
    ├── db.py               # SQLite schema + helpers, FTS5 triggers
    ├── fetcher.py          # RSS / Atom / JSON Feed + ETag conditional GET
    ├── mcp_server.py       # FastMCP stdio server, 6 tools
    ├── opml.py             # OPML import
    ├── renderer.py         # Jinja2 rendering for per-post + digest
    ├── rules.py            # include / exclude / alert
    ├── sender.py           # SMTP + dry-run outbox
    └── templates/
        ├── base.html
        ├── post.html
        └── digest.html
```

---

## Roadmap

- **Phase 0.5** — webhook output adapter; per-feed rule UI instead of YAML; alert throttling.
- **Phase 1** — hosted multi-user at antennafeed.com; OAuth; shared OPML import.
- **Phase 2** — broader adapters (Slack, Discord, Matrix, HTTP push to Zapier); optional semantic search.

Phase 0 is today, and it's the thing that replaces your current RSS-to-email service end-to-end.

---

## License

MIT &mdash; see [LICENSE](LICENSE). Copyright © 2026 Todd Deshane.
