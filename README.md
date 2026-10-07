# Nightly site sweep

The first of three agentic workflows from *Open the Claw, Close the Ticket*
(NAGW 2026). It finds pages on a public website that have quietly stopped being
true, and emails a human about them. It has no write access to anything.

## Why two stages

**`sweep.py` is not an agent.** It is a crawler. It answers "what is stale?"
with HTTP and HTML and nothing else — no API key, no model, no cost. On most
sites this is already 90% of the value, and it is the part your IT director
will approve without a meeting.

**`triage.py` is the judgement.** It takes only the pages the crawler flagged —
typically a few percent of the site — and asks one question: is this page still
doing its job, or does it describe something that has ended?

That split is the whole design. A crawler knows the page has not been edited
since 2019. It cannot know that the city charter is supposed to look like that
and the grant page is not. Run stage one on its own for a week before you
connect stage two; you will learn more from the baseline than from the verdicts.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
# Stage 1 — no API key needed
python3 sweep.py https://www.example.gov --out sweep.json

# Stage 2 — needs a key, read from .env (or the environment)
cp .env.example .env && chmod 600 .env   # then put your key in .env
python3 triage.py sweep.json --out digest.md

# Same thing on Grok
export XAI_API_KEY=xai-...
python3 triage.py sweep.json --provider xai
```

Or watch it run in a browser. Create an account first (passwords are stored only
as scrypt hashes in `auth/users.json`):

```bash
python3 manage.py add-user yourname --admin
```

Sign in with that, or with Google once `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`
are set in `.env` (see `.env.example`). `manage.py` is also the admin override:
`set-password`, `unlock` and `login-link` work without signing in. The full guide
is under **Documents** in the dashboard.

Then:

```bash
python3 dashboard.py        # open http://127.0.0.1:8765
```

The dashboard's address (`/`) is the public product page (`landing.html`); **Sign in**
takes you to the dashboard at `/dashboard`. Fonts and logos are served from `static/`.

The dashboard keeps **projects**: a site plus its settings, with every run saved
under `./projects/<project>/runs/` (result, `sweep.json`, `digest.md`). Switch
projects from the header and reopen any earlier run from the Run menu.

Each run shows:

- **Content age**: how long since each page was edited, which sections hold the
  stale pages, and a trend across runs.
- **Outdated wording**: past dates still written as upcoming ("applications close
  May 15, 2024"), found without a model.
- **Contradictions**: related pages are grouped and compared by the model for
  conflicting fees, dates, phone numbers and rules. Every quote is checked against
  the page and dropped if it isn't there word for word.
- **AI triage**, dead pages (404/403) and broken links, as before. A page the
  model has already judged keeps its verdict while it is unchanged (stored in
  `projects/<project>/analysis.json`), so repeat runs only pay for new or edited
  pages. A page is looked at again if its text changes or one of its dates
  passes. The contradiction check always re-runs across the whole site.

Reports export as CSV, JSON and the Markdown digest.

**Crawl speed** is set per project. Human-paced waits 4–12s between requests with
occasional longer pauses, Polite 1.5–4s, Standard 0.4–0.8s, or set your own
range. Only one request is in flight to the site at a time. If the site answers
429 or 503 the crawler honours Retry-After and halves its speed, then speeds back
up once the site is calm. A robots.txt Crawl-delay is always respected, and the
User-Agent still says who is crawling.

The dashboard binds to localhost only and, like the scripts, changes nothing on
the site.

To share a run without sharing the dashboard, export a read-only snapshot (charts,
report, exports and the Documents guide, with no server and no secrets) and publish
the folder to any static host:

```bash
python3 export_static.py <project-id>      # latest finished run -> ./public
sf publish public                          # e.g. Spacefast
```

Check the wiring without spending anything:

```bash
python3 triage.py sweep.json --dry-run
```

## Schedule it

```bash
SITE=https://www.example.gov TO=webteam@example.gov ./run.sh
```

```cron
0 3 * * *  SITE=https://www.example.gov TO=webteam@example.gov /opt/nightly-sweep/run.sh
```

## Options worth knowing

| Flag | Default | Notes |
| --- | --- | --- |
| `--max-pages` | 1000 | Start low. A 20,000-page county site is an all-night crawl. |
| `--delay` | 0.4 | Seconds between requests. Raise it on a small site. |
| `--stale-months` | 18 | What counts as untouched. |
| `--no-link-check` | off | Skip link checking; much faster for a first look. |
| `--batch-size` | 8 | Pages per model call in stage 2. |
| `--limit` | 120 | Cap on pages sent to the model per run. |

## Before you point it at someone else's site

Crawling a government site you do not operate is a thing to do with permission,
not a thing to do quietly. Put a real contact address in `USER_AGENT` at the top
of `sweep.py` before the first run. If you are demonstrating findings publicly,
get consent or redact the agency — never surprise a web team from a stage.

## What it deliberately will not do

- It will not change, unpublish or delete anything. There is no write path.
- It will not tell you a page is dead because it is old. Age is not evidence.
- It will not propose a deletion. Taking down a public page is a records
  decision, and the digest asks for a review instead.
- It will not claim certainty it does not have. "Unclear" is a valid verdict and
  the prompt encourages it.

## Cost

Stage one is free. Stage two sends roughly 2 KB per flagged page in batches of
eight. A 1,500-page site with 60 flagged pages is well under a dollar a night at
current Sonnet pricing. The expensive resource here is the crawl, not the model.

## The agent version

`agent.py` does the same job as an agent. The pipeline (`sweep.py` → `triage.py`, or the
dashboard) has code decide every step and calls a model at one fixed point. The agent hands
the steps to Claude: it reads the sitemap, decides which pages are worth reading, reads
them, checks their links, judges them, and submits a report.

```bash
.venv/bin/python agent.py https://www.example.gov                      # 40 pages, polite pace
.venv/bin/python agent.py https://www.example.gov --max-pages 80 --effort medium --mail-to web@example.gov
```

- **The spec is the prompt.** The agent's instructions are `nightly-sweep.md`, word for word,
  plus a few paragraphs about this run's budget and tools.
- **The claws are the tool list.** Read: `read_sitemap`, `fetch_page` (this site only, robots.txt
  enforced). Browse: `check_links` (only links found on pages it has read). Write and Shell are
  closed by having no tools at all: there is nothing to misuse and nothing to ask for. Schedule is
  cron.
- **Every action goes through one gate** (`Harness.call_tool`), which enforces the claws and
  writes `audit.jsonl`: a timestamped line for every page read and link checked.
- **Claims are checked by code, not trusted.** A decision or contradiction whose quote isn't on
  the page is discarded before the digest is written. Broken links, expired events and
  staleness are computed by the harness from what the agent did, not reported by the model.
- **Budgets keep it bounded:** pages read, links checked, and turns. Output goes to
  `agent-runs/<site>-<time>/` as `digest.md`, `report.json` and `audit.jsonl`, with a token
  count and cost estimate. A 10-page test site cost about $0.12 with Claude Opus 5.

Nightly, from cron:

```cron
0 3 * * *  cd /opt/nightly-sweep && .venv/bin/python agent.py https://www.example.gov --mail-to webteam@example.gov >> agent-runs/cron.log 2>&1
```

## Each person brings their own API keys

In the dashboard, each signed-in person saves their own AI keys under **My API keys**. Sweeps
use the keys of whoever started them; the server's `.env` keys are not used by the dashboard.
Keys are encrypted with AES-256-GCM (`vault.py`), each sealed to its owner and name, and are
never shown again after saving. The master key is `SECRETS_KEY`, or `auth/secret.key`, created
on first use. To move the AI keys in your `.env` into your account:

```bash
python3 manage.py keys import yourname
```

The command-line scripts and `agent.py` still read `.env`, since they run as you.

## Deploy to Render

`render.yaml` sets up one web service with a 1 GB persistent disk (a paid Starter plan, because
free services have no disk).

1. In Render: **New → Blueprint**, pick this GitHub repository, and fill in the three settings it asks for:
   - `PUBLIC_URL`: the service's address, e.g. `https://nightly-sweep.onrender.com`
   - `ADMIN_USERNAME` and `ADMIN_PASSWORD`: the first admin account (password 12+ characters)
2. Deploy, open the address, and sign in. Then delete `ADMIN_PASSWORD` from the settings. It is only
   used when there are no accounts at all.
3. Each person adds their own AI keys under **My API keys**. They're encrypted with the
   `SECRETS_KEY` that Render generates for the service; keep it, or saved keys can't be read.
   (A Secret File named `.env` is only needed for server settings such as Google sign-in.)

Projects, results and accounts are kept on the disk (`DATA_DIR=/var/data`), cookies are marked secure,
and each user's API keys are stored encrypted on the disk. Run one instance only: a sweep's live progress is
held in memory. For Google sign-in, register `PUBLIC_URL/auth/google/callback` with Google.
On your Mac none of these settings are set, so everything works as before.


## Querying results from an agent (MCP)

`mcp_server.py` is a read-only [MCP](https://modelcontextprotocol.io) connector onto
everything under `./projects`: every saved project, every run, its findings, its
contradictions, and the digest. An MCP-aware agent can ask it questions like "what's
new since last week's Suffolk sweep" without touching the crawler, a live site, or any
API key. It has no tool that writes, deletes, or starts anything — Read is its only
open claw, same as the rest of this project.

It only runs over stdio, spawned by a client already on this machine, so there's no
network listener and no separate login: your Mac's file permissions are the whole
access control.

**Claude Code** (this registers it for every project, not just this one):

```bash
claude mcp add -s user nightly-sweep -- /absolute/path/to/Sweeper/.venv/bin/python /absolute/path/to/Sweeper/mcp_server.py
```

Then ask a fresh session something like "What did the last Suffolk VA sweep find?" or
"Compare the two most recent Martin County runs."

**Claude Desktop:** add a custom connector in Settings pointing at the same command and
args as above (the exact menu differs by app version; look for *Settings → Connectors*
or *Developer → Edit Config*). Older versions use a JSON file instead:

```json
{ "mcpServers": { "nightly-sweep": {
    "command": "/absolute/path/to/Sweeper/.venv/bin/python",
    "args": ["/absolute/path/to/Sweeper/mcp_server.py"] } } }
```

Six tools: `list_projects`, `list_runs`, `get_run_report`, `get_findings` (filtered by
category), `get_digest` (the Markdown report), and `compare_runs` (what's new or fixed
between two runs of the same project).
