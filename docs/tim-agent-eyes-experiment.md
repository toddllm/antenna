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

## Fresh install for Tim

```bash
git clone https://github.com/toddllm/antenna.git ~/code/antenna-tim
cd ~/code/antenna-tim
git checkout tim/agent-eyes-experimental-local
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
bash scripts/smoke_test.sh
bash scripts/agent_eyes_smoke_test.sh
```

The second smoke test uses a fake local `agent-eyes` executable. It proves the
Antenna branch wiring without logging into anything, spending LLM tokens, or
calling a third-party site.

## Quickest real ASCO test

Once Todd or Codex gives you a local `agent-eyes` binary path, this one command
writes a separate `~/antenna-tim/antenna.yaml`, verifies the setup, runs ASCO
through real Agent Eyes, and renders a dry-run digest. It does not send email.

```bash
export OPENAI_API_KEY="..."
bash scripts/tim_agent_eyes_quickstart.sh --agent-eyes /path/to/agent-eyes
```

Useful variants:

```bash
# Put the test config/database somewhere else.
bash scripts/tim_agent_eyes_quickstart.sh --agent-eyes /path/to/agent-eyes --home ~/antenna-tim-test

# Try a different hard source after ASCO works.
bash scripts/tim_agent_eyes_quickstart.sh \
  --agent-eyes /path/to/agent-eyes \
  --url "https://example.com/hard-source" \
  --title "Example Hard Source"
```

The command prints the generated digest path at the end. Open that HTML file in
a browser to inspect exactly what would be sent.

## Manual config path

If you prefer to wire the config yourself, copy the example config and point it
at the real Agent Eyes binary:

```bash
mkdir -p ~/antenna-tim/logs
cp antenna.example.yaml ~/antenna-tim/antenna.yaml
export ANTENNA_CONFIG="$HOME/antenna-tim/antenna.yaml"
export OPENAI_API_KEY="..."
```

Run `antenna doctor` after editing the `experimental_agent_eyes` block. On this
branch, doctor checks whether Agent Eyes sources are configured, whether the
binary is executable, whether `OPENAI_API_KEY` is present, and whether any
configured cookie export files exist.

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
antenna doctor
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

## Safety posture

This branch treats Agent Eyes output as untrusted extracted data. It stores the
JSON as a local Antenna post and renders it into email/search/MCP, but Antenna
does not execute instructions found on the page, click buttons, submit forms, or
make account changes. For logged-in sources, export cookies to a local file and
reference that path; do not paste cookie values into `antenna.yaml` or email
them to Todd/Codex.

## Current limitations

- only local Agent Eyes execution is supported
- only structured state snapshots are modeled cleanly right now
- prompt-injection hardening still needs more review before this becomes a
  stable unattended feature; keep it on curated sources for this branch
- Agent Eyes currently requires an OpenAI-compatible API key

## Support notes for Todd and Codex

The bridge code lives in:

- `antenna/agent_eyes_bridge.py`
- `antenna/cli.py`
- `antenna/config.py`

The experimental source results are stored as ordinary rows in `sources` and
`posts`, tagged with `agent-eyes`, so normal Antenna search, digest rendering,
email, and MCP tools continue to work.
