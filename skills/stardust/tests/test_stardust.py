"""Pytest suite for stardust.

Uses the bundled fixture JSON so no live GitHub calls are made.
"""
import json
import os
import sys
from pathlib import Path

import pytest

# Ensure scripts/ is on sys.path
SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import stardust  # noqa: E402

FIXTURE = SKILL_DIR / "tests" / "fixtures" / "stars_sample.json"


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Isolate STARDUST_HOME to a tmp dir and rebind module-level paths."""
    monkeypatch.setenv("STARDUST_HOME", str(tmp_path))
    monkeypatch.setattr(stardust, "DATA_DIR", tmp_path)
    monkeypatch.setattr(stardust, "DB_PATH", tmp_path / "stars.db")
    return tmp_path


@pytest.fixture
def conn(tmp_home):
    c = stardust.db()
    yield c
    c.close()


@pytest.fixture
def synced(conn):
    """Conn with fixture data synced in."""
    stardust.sync(conn, fixture=FIXTURE)
    return conn


# ---------------------------------------------------------------------------
# Schema / init
# ---------------------------------------------------------------------------

def test_db_creates_schema(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "stars" in tables
    assert "meta" in tables

    # FTS5 virtual tables show up in sqlite_master
    fts_tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'stars_fts%'"
    )}
    assert "stars_fts" in fts_tables


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def test_sync_inserts_fixture(conn):
    result = stardust.sync(conn, fixture=FIXTURE)
    assert result["new"] == 6
    assert result["updated"] == 0
    assert result["total"] == 6

    rows = list(conn.execute("SELECT full_name, language, stars_count FROM stars ORDER BY full_name"))
    names = [r["full_name"] for r in rows]
    assert "anthropics/claude-code" in names
    assert "tokio-rs/tokio" in names


def test_sync_is_idempotent(conn):
    stardust.sync(conn, fixture=FIXTURE)
    result = stardust.sync(conn, fixture=FIXTURE)
    assert result["new"] == 0
    assert result["updated"] == 6
    assert result["total"] == 6


def test_sync_sets_last_sync_meta(conn):
    stardust.sync(conn, fixture=FIXTURE)
    last = stardust.get_meta(conn, "last_sync")
    assert last  # non-empty ISO8601


def test_sync_preserves_readme_on_reimport(conn):
    """Fixture reimport shouldn't wipe an already-stored README."""
    stardust.sync(conn, fixture=FIXTURE)
    conn.execute("UPDATE stars SET readme='cached readme body' WHERE full_name='tokio-rs/tokio'")
    conn.commit()
    stardust.sync(conn, fixture=FIXTURE)
    row = conn.execute("SELECT readme FROM stars WHERE full_name='tokio-rs/tokio'").fetchone()
    assert row["readme"] == "cached readme body"


def test_sync_respects_limit(conn):
    result = stardust.sync(conn, fixture=FIXTURE, limit=2)
    assert result["total"] == 2


# ---------------------------------------------------------------------------
# List filters
# ---------------------------------------------------------------------------

def test_list_no_filter_excludes_archived(synced):
    rows = stardust.cmd_list(synced)
    names = [r["full_name"] for r in rows]
    assert "archived-org/old-thing" not in names
    assert len(rows) == 5


def test_list_includes_archived_with_flag(synced):
    rows = stardust.cmd_list(synced, archived=True)
    assert len(rows) == 6


def test_list_filter_by_language(synced):
    rows = stardust.cmd_list(synced, language="rust")
    names = {r["full_name"] for r in rows}
    assert names == {"tokio-rs/tokio", "cloudflare/workers-rs", "pola-rs/polars"}


def test_list_filter_by_topic(synced):
    rows = stardust.cmd_list(synced, topic="llm")
    names = {r["full_name"] for r in rows}
    assert names == {"anthropics/claude-code", "jxnl/instructor"}


def test_list_filter_by_owner(synced):
    rows = stardust.cmd_list(synced, owner="cloudflare")
    assert [r["full_name"] for r in rows] == ["cloudflare/workers-rs"]


def test_list_filter_by_since(synced):
    rows = stardust.cmd_list(synced, since="2026-01-01")
    names = {r["full_name"] for r in rows}
    assert names == {"anthropics/claude-code", "tokio-rs/tokio", "jxnl/instructor"}


def test_list_filter_combines(synced):
    rows = stardust.cmd_list(synced, language="rust", topic="async")
    assert [r["full_name"] for r in rows] == ["tokio-rs/tokio"]


def test_list_order_desc_by_starred_at(synced):
    rows = stardust.cmd_list(synced)
    dates = [r["starred_at"] for r in rows]
    assert dates == sorted(dates, reverse=True)


# ---------------------------------------------------------------------------
# Search (FTS5)
# ---------------------------------------------------------------------------

def test_search_description(synced):
    rows = stardust.cmd_search(synced, "rust")
    names = {r["full_name"] for r in rows}
    # Matches description/topics containing "rust"
    assert "tokio-rs/tokio" in names
    assert "cloudflare/workers-rs" in names
    assert "pola-rs/polars" in names


def test_search_topic_match(synced):
    rows = stardust.cmd_search(synced, "dataframe")
    assert [r["full_name"] for r in rows] == ["pola-rs/polars"]


def test_search_phrase(synced):
    rows = stardust.cmd_search(synced, '"structured outputs"')
    assert [r["full_name"] for r in rows] == ["jxnl/instructor"]


def test_search_boolean_or(synced):
    rows = stardust.cmd_search(synced, "cloudflare OR dataframe")
    names = {r["full_name"] for r in rows}
    assert names == {"cloudflare/workers-rs", "pola-rs/polars"}


def test_search_empty_results(synced):
    rows = stardust.cmd_search(synced, "zzznonexistent")
    assert rows == []


# ---------------------------------------------------------------------------
# Show
# ---------------------------------------------------------------------------

def test_show_exact(synced):
    result = stardust.cmd_show(synced, "anthropics/claude-code")
    assert isinstance(result, dict)
    assert result["full_name"] == "anthropics/claude-code"
    assert result["language"] == "TypeScript"


def test_show_fuzzy_returns_candidates(synced):
    result = stardust.cmd_show(synced, "workers")
    assert isinstance(result, list)
    names = [r["full_name"] for r in result]
    assert "cloudflare/workers-rs" in names


def test_show_missing_returns_empty_list(synced):
    result = stardust.cmd_show(synced, "nope/does-not-exist")
    assert result == []


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def test_stats_totals(synced):
    s = stardust.cmd_stats(synced)
    assert s["total"] == 6
    langs = {row["key"]: row["n"] for row in s["languages"]}
    assert langs.get("Rust") == 3
    assert langs.get("Python") == 2
    assert langs.get("TypeScript") == 1


def test_stats_top_topics(synced):
    s = stardust.cmd_stats(synced)
    topic_keys = {row["key"] for row in s["topics"]}
    assert "rust" in topic_keys
    assert "llm" in topic_keys


def test_stats_by_year(synced):
    s = stardust.cmd_stats(synced)
    years = {row["year"]: row["n"] for row in s["by_year"]}
    assert years.get("2026") == 3
    assert years.get("2025") == 2
    assert years.get("2024") == 1


# ---------------------------------------------------------------------------
# Markdown export
# ---------------------------------------------------------------------------

def test_md_export_writes_files(synced, tmp_path):
    out = tmp_path / "md-out"
    result = stardust.cmd_md(synced, out)
    assert result["written"] == 6
    files = sorted(out.glob("*.md"))
    assert len(files) == 6


def test_md_filename_includes_date_and_slug(synced, tmp_path):
    out = tmp_path / "md-out"
    stardust.cmd_md(synced, out)
    # e.g. "2026-04-18-anthropics_claude-code.md"
    assert (out / "2026-04-18-anthropics_claude-code.md").exists()


def test_md_content_has_frontmatter(synced, tmp_path):
    out = tmp_path / "md-out"
    stardust.cmd_md(synced, out)
    path = out / "2026-04-18-anthropics_claude-code.md"
    content = path.read_text()
    assert content.startswith("---\n")
    assert "type: star" in content
    assert "source: github" in content
    assert "full_name: anthropics/claude-code" in content
    assert "language: TypeScript" in content
    assert '"llm"' in content or "llm" in content


def test_md_since_filter(synced, tmp_path):
    out = tmp_path / "md-out"
    result = stardust.cmd_md(synced, out, since="2026-01-01")
    assert result["written"] == 3


# ---------------------------------------------------------------------------
# Classify (mocked — no live `claude -p`)
# ---------------------------------------------------------------------------

def test_classify_applies_results(synced):
    def fake_classifier(full_name, description, topics, readme, preferences=""):
        if "rust" in (description or "").lower() or "rust" in topics:
            return {"category": "systems", "domain": "systems"}
        return {"category": "ai-tooling", "domain": "ai"}

    result = stardust.cmd_classify(synced, classifier=fake_classifier)
    assert result["classified"] == 6
    assert result["errors"] == 0

    rust_repos = synced.execute(
        "SELECT full_name, category FROM stars WHERE language='Rust'"
    ).fetchall()
    assert all(r["category"] == "systems" for r in rust_repos)


def test_classify_only_hits_unclassified(synced):
    # Pre-classify one
    synced.execute("UPDATE stars SET category='tool' WHERE full_name='tokio-rs/tokio'")
    synced.commit()

    def fake_classifier(*a, **kw):
        return {"category": "library", "domain": "systems"}

    result = stardust.cmd_classify(synced, classifier=fake_classifier)
    assert result["classified"] == 5  # 6 total - 1 pre-classified

    tokio = synced.execute("SELECT category FROM stars WHERE full_name='tokio-rs/tokio'").fetchone()
    assert tokio["category"] == "tool"  # untouched


# ---------------------------------------------------------------------------
# CLI entry (argparse + end-to-end)
# ---------------------------------------------------------------------------

def test_cli_sync_then_list(tmp_home, capsys):
    # sync
    assert stardust.main(["sync", "--fixture", str(FIXTURE)]) == 0
    out = capsys.readouterr().out
    assert "Sync complete" in out
    assert "6 new" in out

    # list --language rust
    assert stardust.main(["list", "--language", "rust"]) == 0
    out = capsys.readouterr().out
    assert "tokio-rs/tokio" in out
    assert "cloudflare/workers-rs" in out
    assert "pola-rs/polars" in out


def test_cli_search(tmp_home, capsys):
    stardust.main(["sync", "--fixture", str(FIXTURE)])
    capsys.readouterr()
    assert stardust.main(["search", "dataframe"]) == 0
    out = capsys.readouterr().out
    assert "pola-rs/polars" in out


def test_cli_list_json(tmp_home, capsys):
    stardust.main(["sync", "--fixture", str(FIXTURE)])
    capsys.readouterr()
    assert stardust.main(["list", "--language", "rust", "--json"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert len(data) == 3
    assert all(r["language"] == "Rust" for r in data)


def test_cli_stats(tmp_home, capsys):
    stardust.main(["sync", "--fixture", str(FIXTURE)])
    capsys.readouterr()
    assert stardust.main(["stats"]) == 0
    out = capsys.readouterr().out
    assert "Total stars: 6" in out
    assert "Languages" in out


def test_cli_md_export(tmp_home, tmp_path, capsys):
    stardust.main(["sync", "--fixture", str(FIXTURE)])
    capsys.readouterr()
    out_dir = tmp_path / "export"
    assert stardust.main(["md", "--out", str(out_dir), "--since", "2026-01-01"]) == 0
    out = capsys.readouterr().out
    assert "Wrote 3 files" in out
    assert len(list(out_dir.glob("*.md"))) == 3
