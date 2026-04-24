# Tim experiment: local Agent Eyes source support

**Branch:** `tim/agent-eyes-experimental-local`
**Status:** experimental, local-only, not part of the stable Antenna release

This branch adds a narrow bridge between Antenna and a local `agent-eyes`
binary. It is meant for sources like ASCO that do not behave like normal feeds.

The stable Antenna flow still comes first:

- RSS/Atom/JSON feeds stay on `antenna fetch`
- email still uses the same SMTP setup
- search and MCP still read the same SQLite database

The experiment adds one extra step: ask a locally installed Agent Eyes binary to
read a hard page, store the structured result as an Antenna post, and let the
normal Antenna digest/email path handle it from there.

## What this is for

Good candidates:

- conference landing pages
- abstract portals
- pages that return `403` to plain HTTP fetches but load in a browser
- sources where the useful signal is "what changed on this page?" instead of
  "what are the newest feed entries?"

Tim's initial example:

- `https://www.asco.org/annual-meeting`

## What this is not

- not a hosted browser service
- not automatic account login
- not a CAPTCHA bypass product
- not a replacement for RSS when RSS exists
- not part of Antenna's stable public release yet

## Requirements

- Antenna installed from this branch
- a local `agent-eyes` binary on your machine
- `OPENAI_API_KEY` available in the shell that runs Antenna

Agent Eyes is not bundled with Antenna on this branch. During the beta, Todd or
Codex will provide the Agent Eyes install path separately.

## Example config

Add this block to `antenna.yaml`:

```yaml
experimental_agent_eyes:
  binary: agent-eyes
  model: gpt-4o-mini
  timeout_seconds: 90
  sources:
    - url: https://www.asco.org/annual-meeting
      title: "ASCO Annual Meeting"
      tags: [oncology, conference]
      mode: state
      schema: [title, dates, location, important_links, page_type]
      stable_fields: [title, dates, location, page_type]
      instruction: >
        Extract the conference title, dates, location, important links, and
        whether this is a conference landing page or a rolling news feed.
      settle_ms: 5000
      stealth: false
```

If a source needs your own lawful logged-in access later, use a local cookie
export file:

```yaml
      cookies_file: ~/.config/antenna/cookies/asco.txt
```

Do not paste cookie values directly into `antenna.yaml`.

## Preview the hard source

```bash
export OPENAI_API_KEY="..."
antenna fetch-agent-eyes --experimental-agent-eyes
antenna recent-posts --limit 5
```

Expected behavior:

- Antenna creates or updates a source for ASCO
- Agent Eyes runs locally
- the structured result is inserted as one Antenna post
- if the core page state has not changed on the next run, no new post is
  created

`stable_fields` controls what counts as the same state. In the ASCO example,
minor differences in extracted link anchors should not create a new post, but a
new date, location, title, or page type should.

## Email it through Antenna

For a safe dry run:

```bash
antenna sync --experimental-agent-eyes --mode digest --dry-run
antenna render-digest --since 24h
```

For live email, only after `antenna test-email` passes:

```bash
antenna sync --experimental-agent-eyes --mode digest
```

## Why the explicit flag exists

The flag is intentional. Browser-backed extraction is slower, cost-bearing, and
still experimental. It should never surprise a user during a normal Antenna
`sync`.

## Current limitations

- only local Agent Eyes execution is supported
- only structured state snapshots are modeled cleanly right now
- prompt-injection hardening still needs more work before this becomes a
  stable unattended feature
- Agent Eyes currently requires an OpenAI-compatible API key

## Support notes for Todd and Codex

The bridge code lives in:

- `antenna/agent_eyes_bridge.py`
- `antenna/cli.py`
- `antenna/config.py`

The experimental source results are stored as ordinary rows in `sources` and
`posts`, tagged with `agent-eyes`, so normal Antenna search, digest rendering,
email, and MCP tools continue to work.
