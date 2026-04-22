#!/usr/bin/env python3
"""stardust - Local search and analysis for GitHub stars.

Analogous to `ft` (fieldtheory) for X/Twitter bookmarks. Sync stars via the
`gh` CLI into a local SQLite store, search with FTS5, filter by language/topic/
owner/date/category, LLM-classify, and export to markdown for kb ingest.
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Config + storage
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("STARDUST_HOME") or (Path.home() / ".github-stars"))
DB_PATH = DATA_DIR / "stars.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS stars (
  full_name     TEXT PRIMARY KEY,
  owner         TEXT NOT NULL,
  repo          TEXT NOT NULL,
  description   TEXT,
  language      TEXT,
  topics        TEXT,              -- JSON array
  stars_count   INTEGER DEFAULT 0,
  forks_count   INTEGER DEFAULT 0,
  url           TEXT,
  homepage      TEXT,
  starred_at    TEXT,              -- ISO8601
  pushed_at     TEXT,
  archived      INTEGER DEFAULT 0,
  readme        TEXT,
  category      TEXT,
  domain        TEXT,
  synced_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_language   ON stars(language);
CREATE INDEX IF NOT EXISTS idx_owner      ON stars(owner);
CREATE INDEX IF NOT EXISTS idx_starred_at ON stars(starred_at);
CREATE INDEX IF NOT EXISTS idx_category   ON stars(category);

CREATE VIRTUAL TABLE IF NOT EXISTS stars_fts USING fts5(
  full_name UNINDEXED,
  description,
  topics,
  readme,
  tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""


def db(path: Path | None = None) -> sqlite3.Connection:
    # Resolve at call time so tests can monkeypatch DB_PATH after import.
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


# ---------------------------------------------------------------------------
# gh CLI
# ---------------------------------------------------------------------------

def gh_api(path: str, paginate: bool = False, headers: dict | None = None) -> str:
    """Call `gh api <path>`. Returns stdout. Raises on failure."""
    args = ["gh", "api", path]
    if paginate:
        args.append("--paginate")
    for k, v in (headers or {}).items():
        args.extend(["-H", f"{k}: {v}"])
    out = subprocess.run(args, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {out.stderr.strip()}")
    return out.stdout


def fetch_stars_live(username: str | None = None, limit: int | None = None) -> list[dict]:
    """Fetch starred repos via `gh api`. Returns list of star records with repo details."""
    user = username or "user"
    path = f"{'user/starred' if user == 'user' else f'users/{user}/starred'}?per_page=100"
    headers = {"Accept": "application/vnd.github.star+json"}

    if limit and limit <= 100:
        raw = gh_api(f"{path}&per_page={limit}", paginate=False, headers=headers)
        items = json.loads(raw)
    else:
        raw = gh_api(path, paginate=True, headers=headers)
        # --paginate returns concatenated JSON arrays; gh merges them into a single array
        # but to be safe, handle the case where it's multiple arrays:
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            items = []
            for chunk in re.split(r"\]\s*\[", raw):
                chunk = chunk.strip()
                if not chunk.startswith("["):
                    chunk = "[" + chunk
                if not chunk.endswith("]"):
                    chunk = chunk + "]"
                items.extend(json.loads(chunk))

    if limit:
        items = items[:limit]
    return items


def fetch_readme_live(full_name: str, max_chars: int = 8000) -> str:
    """Fetch README for a repo via `gh api`. Returns trimmed text."""
    try:
        raw = gh_api(f"repos/{full_name}/readme", headers={"Accept": "application/vnd.github.raw"})
        # Strip image tags to save space
        cleaned = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", raw)
        cleaned = re.sub(r"<img[^>]*>", "", cleaned)
        cleaned = cleaned.strip()
        return cleaned[:max_chars]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync(
    conn: sqlite3.Connection,
    fixture: Path | None = None,
    full: bool = False,
    limit: int | None = None,
    no_readme: bool = False,
    username: str | None = None,
    readme_fetcher=None,
) -> dict:
    """Sync stars into the DB. Returns {'new': N, 'updated': N, 'total': N}."""
    if fixture:
        with open(fixture) as f:
            items = json.load(f)
        if limit:
            items = items[:limit]
    else:
        items = fetch_stars_live(username=username, limit=limit)

    readme_fn = readme_fetcher if readme_fetcher is not None else (
        (lambda fn: "") if (no_readme or fixture) else fetch_readme_live
    )

    existing = {r["full_name"]: (r["readme"] or "") for r in conn.execute("SELECT full_name, readme FROM stars")}
    new_count = 0
    updated_count = 0
    now = datetime.now(timezone.utc).isoformat()

    for item in items:
        # gh api returns {"starred_at": "...", "repo": {...}}
        starred_at = item.get("starred_at")
        repo = item.get("repo") or item
        full_name = repo.get("full_name")
        if not full_name:
            continue
        owner = repo.get("owner", {}).get("login") if isinstance(repo.get("owner"), dict) else full_name.split("/")[0]
        name = repo.get("name") or full_name.split("/")[-1]

        is_new = full_name not in existing
        existing_readme = existing.get(full_name, "")

        # Fetch README for new repos or when --full is set. Skip entirely
        # when caller opted out (no_readme or fixture-mode).
        should_fetch_readme = (is_new or full) and not (no_readme or fixture)
        if should_fetch_readme:
            readme_text = readme_fn(full_name)
        else:
            readme_text = existing_readme

        row = (
            full_name,
            owner,
            name,
            repo.get("description") or "",
            repo.get("language") or "",
            json.dumps(repo.get("topics") or []),
            int(repo.get("stargazers_count") or 0),
            int(repo.get("forks_count") or 0),
            repo.get("html_url") or f"https://github.com/{full_name}",
            repo.get("homepage") or "",
            starred_at or "",
            repo.get("pushed_at") or "",
            1 if repo.get("archived") else 0,
            readme_text,
            None,  # category — set by classify
            None,  # domain
            now,
        )

        conn.execute(
            """
            INSERT INTO stars (full_name, owner, repo, description, language, topics,
                               stars_count, forks_count, url, homepage, starred_at,
                               pushed_at, archived, readme, category, domain, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(full_name) DO UPDATE SET
                description=excluded.description,
                language=excluded.language,
                topics=excluded.topics,
                stars_count=excluded.stars_count,
                forks_count=excluded.forks_count,
                url=excluded.url,
                homepage=excluded.homepage,
                pushed_at=excluded.pushed_at,
                archived=excluded.archived,
                readme=CASE WHEN excluded.readme != '' THEN excluded.readme ELSE stars.readme END,
                synced_at=excluded.synced_at
            """,
            row,
        )

        # Sync FTS row
        conn.execute("DELETE FROM stars_fts WHERE full_name = ?", (full_name,))
        conn.execute(
            "INSERT INTO stars_fts (full_name, description, topics, readme) VALUES (?, ?, ?, ?)",
            (full_name, row[3], " ".join(repo.get("topics") or []), readme_text or (existing_readme or "")),
        )

        if is_new:
            new_count += 1
        else:
            updated_count += 1

    set_meta(conn, "last_sync", now)
    conn.commit()

    return {
        "new": new_count,
        "updated": updated_count,
        "total": conn.execute("SELECT COUNT(*) FROM stars").fetchone()[0],
    }


# ---------------------------------------------------------------------------
# List / search / show / stats
# ---------------------------------------------------------------------------

def cmd_list(conn, *, language=None, topic=None, owner=None, since=None,
             category=None, domain=None, archived=False, limit=50):
    where = []
    params: list = []
    if language:
        where.append("LOWER(language) = LOWER(?)")
        params.append(language)
    if topic:
        where.append("topics LIKE ?")
        params.append(f'%"{topic}"%')
    if owner:
        where.append("LOWER(owner) = LOWER(?)")
        params.append(owner)
    if since:
        where.append("starred_at >= ?")
        params.append(since)
    if category:
        where.append("LOWER(category) = LOWER(?)")
        params.append(category)
    if domain:
        where.append("LOWER(domain) = LOWER(?)")
        params.append(domain)
    if not archived:
        where.append("archived = 0")

    sql = "SELECT * FROM stars"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY starred_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


def cmd_search(conn, query: str, limit: int = 25):
    """FTS5 search. `query` supports FTS5 syntax."""
    rows = conn.execute(
        """
        SELECT s.*, bm25(stars_fts) AS rank
        FROM stars_fts
        JOIN stars s ON s.full_name = stars_fts.full_name
        WHERE stars_fts MATCH ?
        ORDER BY rank LIMIT ?
        """,
        (query, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def cmd_show(conn, full_name: str):
    row = conn.execute("SELECT * FROM stars WHERE full_name = ?", (full_name,)).fetchone()
    if not row:
        # fuzzy fallback
        rows = conn.execute(
            "SELECT * FROM stars WHERE full_name LIKE ? ORDER BY stars_count DESC LIMIT 5",
            (f"%{full_name}%",),
        ).fetchall()
        return [dict(r) for r in rows]
    return dict(row)


def cmd_stats(conn):
    total = conn.execute("SELECT COUNT(*) FROM stars").fetchone()[0]

    def top(col, n=10):
        return [
            dict(r) for r in conn.execute(
                f"SELECT {col} AS key, COUNT(*) AS n FROM stars "
                f"WHERE {col} IS NOT NULL AND {col} != '' "
                f"GROUP BY {col} ORDER BY n DESC LIMIT ?", (n,),
            )
        ]

    # topics are JSON-encoded, flatten them
    topic_rows = conn.execute(
        "SELECT topics FROM stars WHERE topics IS NOT NULL AND topics != '[]'"
    ).fetchall()
    topic_counts: dict[str, int] = {}
    for r in topic_rows:
        try:
            for t in json.loads(r["topics"]):
                topic_counts[t] = topic_counts.get(t, 0) + 1
        except Exception:
            continue
    top_topics = sorted(topic_counts.items(), key=lambda kv: -kv[1])[:15]

    # by year
    year_rows = [
        dict(r) for r in conn.execute(
            "SELECT substr(starred_at, 1, 4) AS year, COUNT(*) AS n "
            "FROM stars WHERE starred_at != '' GROUP BY year ORDER BY year DESC"
        )
    ]

    return {
        "total": total,
        "languages": top("language"),
        "categories": top("category"),
        "domains": top("domain"),
        "owners": top("owner", n=15),
        "topics": [{"key": k, "n": v} for k, v in top_topics],
        "by_year": year_rows,
        "last_sync": get_meta(conn, "last_sync", "never"),
    }


# ---------------------------------------------------------------------------
# Classify (LLM)
# ---------------------------------------------------------------------------

DEFAULT_CLASSIFY_PROMPT = """You are a taxonomist for a user's GitHub-starred repositories.

Given a repo's name, description, topics, and README excerpt, assign:
- category: one of {tool, library, framework, ai-tooling, llm-infra, agents, devops,
  research, reference, docs, tutorial, game, web-dev, mobile, design, data, systems,
  security, infra, cli, plugin, template, starter, other}
- domain: one of {ai, web, mobile, systems, data, devops, design, security,
  finance, research, education, entertainment, other}

Respect these user preferences if provided:
{preferences}

Output ONLY a single line of JSON: {"category": "...", "domain": "..."}
"""


def classify_one(full_name: str, description: str, topics: list[str],
                 readme: str, preferences: str = "") -> dict:
    """Run `claude -p` to classify a single repo. Returns {'category': ..., 'domain': ...}."""
    topics_str = ", ".join(topics) if topics else "(none)"
    readme_excerpt = (readme or "")[:1500]
    prompt = DEFAULT_CLASSIFY_PROMPT.format(preferences=preferences or "(none)")
    payload = (
        f"Repo: {full_name}\n"
        f"Description: {description or '(none)'}\n"
        f"Topics: {topics_str}\n"
        f"README excerpt:\n{readme_excerpt}\n"
    )

    try:
        out = subprocess.run(
            ["claude", "-p", prompt, "--model", "claude-haiku-4-5"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if out.returncode != 0:
            return {"category": None, "domain": None, "error": out.stderr.strip()}
        text = out.stdout.strip()
        # Extract first JSON object
        match = re.search(r"\{[^{}]*\}", text)
        if not match:
            return {"category": None, "domain": None, "error": "no JSON in output"}
        return json.loads(match.group(0))
    except Exception as e:
        return {"category": None, "domain": None, "error": str(e)}


def cmd_classify(conn, preferences: str = "", limit: int | None = None,
                 classifier=classify_one) -> dict:
    """Classify unclassified stars. Returns counts."""
    sql = "SELECT full_name, description, topics, readme FROM stars WHERE category IS NULL"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = list(conn.execute(sql))
    classified = 0
    errors = 0
    for row in rows:
        try:
            topics = json.loads(row["topics"] or "[]")
        except Exception:
            topics = []
        result = classifier(row["full_name"], row["description"] or "",
                            topics, row["readme"] or "", preferences=preferences)
        if result.get("category"):
            conn.execute(
                "UPDATE stars SET category=?, domain=? WHERE full_name=?",
                (result.get("category"), result.get("domain"), row["full_name"]),
            )
            classified += 1
        else:
            errors += 1
    conn.commit()
    return {"classified": classified, "errors": errors, "pending": len(rows) - classified - errors}


# ---------------------------------------------------------------------------
# Markdown export
# ---------------------------------------------------------------------------

def md_frontmatter(row: dict) -> str:
    try:
        topics = json.loads(row.get("topics") or "[]")
    except Exception:
        topics = []
    lines = [
        "---",
        "type: star",
        "source: github",
        f"owner: {row.get('owner') or ''}",
        f"repo: {row.get('repo') or ''}",
        f"full_name: {row.get('full_name') or ''}",
        f"url: {row.get('url') or ''}",
        f"starred_at: {row.get('starred_at') or ''}",
        f"language: {row.get('language') or ''}",
        f"topics: {json.dumps(topics)}",
    ]
    if row.get("category"):
        lines.append(f"category: {row['category']}")
    if row.get("domain"):
        lines.append(f"domain: {row['domain']}")
    lines.append(f"stars: {row.get('stars_count') or 0}")
    lines.append(f"ingested_at: {datetime.now(timezone.utc).isoformat()}")
    lines.append("---")
    return "\n".join(lines)


def md_body(row: dict) -> str:
    desc = (row.get("description") or "").strip()
    readme = (row.get("readme") or "").strip()
    parts = [f"# {row.get('full_name')}", ""]
    if desc:
        parts.extend([desc, ""])
    if readme:
        words = readme.split()
        excerpt = " ".join(words[:800])
        parts.extend(["## README excerpt", "", excerpt, ""])
    else:
        parts.extend(["_No README available._", ""])
    return "\n".join(parts)


def cmd_md(conn, out_dir: Path, since: str | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    sql = "SELECT * FROM stars"
    params: list = []
    if since:
        sql += " WHERE starred_at >= ?"
        params.append(since)
    rows = list(conn.execute(sql, params))
    written = 0
    for r in rows:
        row = dict(r)
        date_part = (row.get("starred_at") or "")[:10] or datetime.now(timezone.utc).date().isoformat()
        safe = (row.get("full_name") or "unknown").replace("/", "_")
        filename = f"{date_part}-{safe}.md"
        path = out_dir / filename
        content = md_frontmatter(row) + "\n\n" + md_body(row)
        path.write_text(content)
        written += 1
    return {"written": written, "out_dir": str(out_dir)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def format_row_short(r: dict) -> str:
    full = r.get("full_name") or ""
    lang = r.get("language") or "-"
    stars = r.get("stars_count") or 0
    desc = (r.get("description") or "")[:80]
    cat = r.get("category") or ""
    when = (r.get("starred_at") or "")[:10]
    cat_part = f" [{cat}]" if cat else ""
    return f"{when}  {full:<42}  {lang:<14} ★{stars:<6}{cat_part}  {desc}"


def print_rows(rows: list[dict]) -> None:
    if not rows:
        print("(no results)")
        return
    for r in rows:
        print(format_row_short(r))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="stardust", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Create data dir + db")

    sp = sub.add_parser("sync", help="Sync stars from GitHub")
    sp.add_argument("--fixture", type=Path, help="Path to JSON fixture (for testing)")
    sp.add_argument("--full", action="store_true", help="Re-fetch READMEs for existing stars")
    sp.add_argument("--limit", type=int, help="Cap total stars processed")
    sp.add_argument("--no-readme", action="store_true", help="Skip README fetch")
    sp.add_argument("--username", help="Fetch starred list for another user (default: authed user)")

    sp = sub.add_parser("list", help="List stars with filters")
    sp.add_argument("--language")
    sp.add_argument("--topic")
    sp.add_argument("--owner")
    sp.add_argument("--since", help="YYYY-MM-DD (starred on or after)")
    sp.add_argument("--category")
    sp.add_argument("--domain")
    sp.add_argument("--archived", action="store_true")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--json", action="store_true", help="Emit JSON rows")

    sp = sub.add_parser("search", help="Full-text search over description + topics + readme")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=25)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("show", help="Show one star in detail")
    sp.add_argument("full_name")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("stats", help="Aggregate stats")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("classify", help="LLM-classify unclassified stars")
    sp.add_argument("--preferences", default="", help="Steer the classifier with user preferences")
    sp.add_argument("--limit", type=int, help="Max stars to classify this run")

    sp = sub.add_parser("md", help="Export stars to markdown")
    sp.add_argument("--out", required=True, type=Path, help="Output directory")
    sp.add_argument("--since", help="YYYY-MM-DD (only stars on or after)")

    args = p.parse_args(argv)

    if args.cmd == "init":
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = db()
        conn.close()
        print(f"Initialized at {DATA_DIR}")
        return 0

    conn = db()

    if args.cmd == "sync":
        result = sync(
            conn,
            fixture=args.fixture,
            full=args.full,
            limit=args.limit,
            no_readme=args.no_readme,
            username=args.username,
        )
        print(f"Sync complete: {result['new']} new, {result['updated']} updated, {result['total']} total")
        return 0

    if args.cmd == "list":
        rows = cmd_list(
            conn,
            language=args.language, topic=args.topic, owner=args.owner,
            since=args.since, category=args.category, domain=args.domain,
            archived=args.archived, limit=args.limit,
        )
        if args.json:
            print(json.dumps(rows, indent=2, default=str))
        else:
            print_rows(rows)
        return 0

    if args.cmd == "search":
        rows = cmd_search(conn, args.query, limit=args.limit)
        if args.json:
            print(json.dumps(rows, indent=2, default=str))
        else:
            print_rows(rows)
        return 0

    if args.cmd == "show":
        result = cmd_show(conn, args.full_name)
        if args.json:
            print(json.dumps(result, indent=2, default=str))
            return 0
        if isinstance(result, list):
            print(f"(no exact match; {len(result)} candidates)")
            print_rows(result)
            return 0
        r = result
        try:
            topics = json.loads(r.get("topics") or "[]")
        except Exception:
            topics = []
        print(f"{r['full_name']}  ★{r.get('stars_count') or 0}")
        print(f"  URL:       {r.get('url')}")
        print(f"  Language:  {r.get('language') or '-'}")
        print(f"  Topics:    {', '.join(topics) or '-'}")
        print(f"  Category:  {r.get('category') or '-'}")
        print(f"  Domain:    {r.get('domain') or '-'}")
        print(f"  Starred:   {r.get('starred_at') or '-'}")
        print(f"  Pushed:    {r.get('pushed_at') or '-'}")
        print()
        print(r.get("description") or "(no description)")
        if r.get("readme"):
            print()
            print("--- README (first 60 lines) ---")
            for line in (r["readme"] or "").splitlines()[:60]:
                print(line)
        return 0

    if args.cmd == "stats":
        s = cmd_stats(conn)
        if args.json:
            print(json.dumps(s, indent=2, default=str))
            return 0
        print(f"Total stars: {s['total']}  (last sync: {s['last_sync']})")

        def block(title, rows, key="key"):
            if not rows:
                return
            print(f"\n{title}")
            for r in rows:
                print(f"  {r['n']:>5}  {r[key]}")

        block("Languages", s["languages"])
        block("Categories", s["categories"])
        block("Domains", s["domains"])
        block("Top owners", s["owners"])
        block("Top topics", s["topics"])
        block("By year", s["by_year"], key="year")
        return 0

    if args.cmd == "classify":
        result = cmd_classify(conn, preferences=args.preferences, limit=args.limit)
        print(f"Classified: {result['classified']}, errors: {result['errors']}, pending: {result['pending']}")
        return 0

    if args.cmd == "md":
        result = cmd_md(conn, args.out, since=args.since)
        print(f"Wrote {result['written']} files to {result['out_dir']}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
