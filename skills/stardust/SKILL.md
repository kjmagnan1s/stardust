---
name: stardust
description: Search and analyze the user's GitHub stars locally. Trigger whenever the user mentions their starred repos, asks "what have I starred about X", wants to find a repo they starred, asks about GitHub stars stats, or is starting work where a starred repo might be relevant context. Also use for categorizing or exporting stars to markdown for knowledge-base ingestion.
---

# Stardust: Local Search for GitHub Stars

Search, filter, categorize, and export the user's GitHub-starred repositories. Turns `gh api users/<you>/starred` into a local searchable store with FTS5, filters, LLM classification, and markdown export.

## When to trigger

- User mentions their GitHub stars, starred repos, saved repositories
- User asks "what have I starred about X" or "find the repo I starred for Y"
- User asks for stats or patterns across their starred list
- User is starting a task where a previously-starred repo could help ("I'm building a rate limiter" then check if they've starred one)
- User wants to categorize, tag, or export stars
- User asks to sync stars into their knowledge base

## Setup (first use only)

Before running any `stardust` command, check whether the CLI is on PATH:

```bash
which stardust
```

If it's missing (common right after `/plugin install`), run the bundled self-installer. It symlinks the script into `~/.local/bin/` (no sudo) and warns if that dir isn't on PATH yet:

```bash
python3 "$(dirname "$0")/stardust.py" install    # from the skill's scripts/ dir
# OR, absolute path. Claude can resolve the plugin location:
python3 <plugin-path>/skills/stardust/scripts/stardust.py install
```

Then the first real setup:

```bash
stardust init      # create ~/.github-stars/ + sqlite db
stardust sync      # pull all stars via `gh api` (takes ~30s per 1000 stars)
```

`stardust sync` uses the authenticated `gh` CLI. No extra tokens needed.

If the user sees a PATH warning after `install`, tell them the one-line export they need to add to `~/.zshrc` or `~/.bashrc`. The `install` command prints it explicitly.

## Workflow

1. Look at what the user is working on (conversation, open files, branch)
2. Pick the narrowest command that answers the question
3. Start with a filtered `list` or `search`, widen only if empty
4. Summarize the results, don't dump raw rows

## Commands

```bash
stardust install [--target DIR]               # Symlink CLI onto PATH (first-run helper)
stardust init                                 # Create ~/.github-stars/ + sqlite db
stardust sync [--full]                        # Incremental sync; --full re-fetches READMEs
stardust list [--language X] [--topic Y]      # Filter by language, topic, owner, date, category
         [--owner Z] [--since DATE] [--limit N]
         [--category C]
stardust search <query> [--limit N]           # FTS5 search on description + readme + topics
stardust show <owner/repo>                    # Full detail for one star
stardust stats                                # Counts by language, topic, category, year
stardust classify [--preferences "..."]       # LLM categorize each star (uses `claude -p`)
stardust md --out DIR [--since DATE]          # Export to markdown (for kb ingest)
```

### Filters combine

```bash
stardust list --language rust --topic cli --limit 20
stardust list --since 2026-01-01 --category ai-tooling
stardust search "rate limit" --limit 10
```

### Search syntax (FTS5)

- `stardust search "exact phrase"` for literal match
- `stardust search "rate AND limit"` boolean AND
- `stardust search "rust OR zig"` boolean OR
- `stardust search "llm NOT openai"` exclusion

### When to scan instead of search

`stardust search` is good for precise keyword matches but under-recalls on broad
topical questions ("all AI-related repos"). For topical triage, scan a filtered
slice in JSON and score in-memory:

```bash
stardust list --since 2026-01-01 --limit 500 --json
```

Then filter by keyword density in description + topics + name. This catches
repos where the topic is implicit (e.g. an "agent harness" repo that doesn't
literally say "agent" in the description).

## Classification (preference-driven)

`stardust classify` uses an LLM to assign each star a `category` (tool, library, research, reference, inspiration, infra, ai-tooling, etc.) and `domain` (ai, web-dev, systems, devops, design, etc.). It's idempotent: re-running only reclassifies stars without categories.

Optional `--preferences` steers the classifier:

```bash
stardust classify --preferences "I'm an AI engineer building SwiftUI + Cloudflare Workers apps.
Prefer categories that match my stack: ai-agents, swiftui, cloudflare, llm-infra, rust-systems.
Exclude game-dev and robotics, I don't use them."
```

## Markdown export

`stardust md --out ./github-stars/` writes one file per star with frontmatter:

```yaml
---
type: star
source: github
owner: anthropics
repo: claude-code
full_name: anthropics/claude-code
url: https://github.com/anthropics/claude-code
starred_at: 2026-04-18T14:22:03Z
language: TypeScript
topics: [llm, agents, cli]
category: ai-tooling
domain: ai
stars: 12400
---
```

Body is repo description + first ~800 words of README.

Use `--since DATE` in crons to only write newly-starred repos. Idempotent: re-running overwrites by `full_name`.

## Cron / daily ingest pattern

Works well as a daily job feeding a markdown knowledge base (Obsidian vault, `raw/` source folder, etc.):

```bash
#!/usr/bin/env bash
stardust sync
stardust md --since "$(date -u -v-2d +%Y-%m-%d)" --out "$KB_RAW/github-stars/"
stardust classify --limit 20 || true
```

## Guidelines

- Never dump raw tabular output. Summarize. Group by language or category. Surface the 3-5 most relevant stars to the user's current task.
- When the user asks "what have I starred about X", run both `search X` and `list --topic X` (different signals) and merge.
- For new-repo recommendations, cross-reference: "You starred `foo/bar` in January, same space as what you're looking at now."
- If `stardust sync` hasn't run in >24h, suggest running it before answering.
- `show <owner/repo>` works on exact `owner/repo`. If the user gives a fuzzy name like "the claude code repo", try `search claude-code` first.

## Data location

- DB: `~/.github-stars/stars.db`
- Override: `STARDUST_HOME=/alt/path stardust sync`

Safe to delete `~/.github-stars/` and re-run `stardust init && stardust sync`. Nothing in it is original source: GitHub is the source of truth.
