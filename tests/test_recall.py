"""Behavioral tests for recall.py — uses fake_projects fixture."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import recall


# ─── slug encoding ──────────────────────────────────────────────────────────

def test_slug_encoding_replaces_both_slash_and_dot():
    assert recall.cwd_to_project_slug("/Users/hernan.desouza/repo") == \
        "-Users-hernan-desouza-repo"


def test_slug_encoding_idempotent_on_safe_chars():
    assert recall.cwd_to_project_slug("/foo/bar") == "-foo-bar"


# ─── digest / cache ─────────────────────────────────────────────────────────

def test_parse_jsonl_to_digest_captures_metadata(fake_projects):
    jsonl = fake_projects / "test-project-slug" / "sess-a.jsonl"
    d = recall.parse_jsonl_to_digest(jsonl)
    assert d is not None
    assert d.ai_title == "Migrate to Next 16"
    assert d.git_branch == "feat/migrate"
    assert d.cwd == "/repo"
    assert d.session_id == "sess-a"
    assert "/repo/next.config.js" in d.files_touched
    assert any("Exit code 1" in e for e in d.errors)
    assert len(d.turns) >= 4


def test_digest_drops_thinking_blocks(fake_projects):
    jsonl = fake_projects / "test-project-slug" / "sess-a.jsonl"
    payload = jsonl.read_text()
    # Inject a thinking-only assistant block
    extra = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "hidden Next 16 thought", "signature": "x"}],
        },
        "timestamp": "2026-05-25T10:05:00Z",
    }
    jsonl.write_text(payload + json.dumps(extra) + "\n")
    d = recall.parse_jsonl_to_digest(jsonl)
    assert all("hidden Next 16 thought" not in t[2] for t in d.turns)


def test_cache_writes_and_reads_on_warm_run(fake_projects, tmp_path):
    jsonl = fake_projects / "test-project-slug" / "sess-a.jsonl"
    # Cold
    d1, hit1 = recall.load_or_build_digest(jsonl, use_cache=True)
    assert hit1 is False
    assert d1 is not None
    cache_file = recall.cache_path_for(jsonl)
    assert cache_file.exists()
    # Warm
    d2, hit2 = recall.load_or_build_digest(jsonl, use_cache=True)
    assert hit2 is True
    assert d2.session_id == d1.session_id


def test_cache_invalidates_on_mtime_change(fake_projects):
    jsonl = fake_projects / "test-project-slug" / "sess-a.jsonl"
    recall.load_or_build_digest(jsonl, use_cache=True)
    # Modify file
    with jsonl.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "user", "message": {"role": "user", "content": "tail"},
        }) + "\n")
    _, hit = recall.load_or_build_digest(jsonl, use_cache=True)
    assert hit is False


# ─── query: OR / AND / NOT ──────────────────────────────────────────────────

def _query_or(*terms: str) -> recall.Query:
    return recall.Query(should=[re.compile(re.escape(t), re.I) for t in terms])


def _query_and(*terms: str) -> recall.Query:
    return recall.Query(must=[re.compile(re.escape(t), re.I) for t in terms])


def _query_or_not(should: list[str], exclude: list[str]) -> recall.Query:
    return recall.Query(
        should=[re.compile(re.escape(t), re.I) for t in should],
        must_not=[re.compile(re.escape(t), re.I) for t in exclude],
    )


def test_or_default_matches_either_term(fake_projects):
    jsonl_a = fake_projects / "test-project-slug" / "sess-a.jsonl"
    jsonl_b = fake_projects / "test-project-slug" / "sess-b.jsonl"
    da, _ = recall.load_or_build_digest(jsonl_a)
    db, _ = recall.load_or_build_digest(jsonl_b)
    q = _query_or("Next 16", "CSP")
    assert recall.match_digest(da, q, since=None) is not None
    assert recall.match_digest(db, q, since=None) is not None


def test_and_requires_both_terms(fake_projects):
    jsonl_a = fake_projects / "test-project-slug" / "sess-a.jsonl"
    jsonl_c = fake_projects / "test-project-slug" / "sess-c.jsonl"
    da, _ = recall.load_or_build_digest(jsonl_a)
    dc, _ = recall.load_or_build_digest(jsonl_c)
    q = _query_and("Next 16", "CSP")
    assert recall.match_digest(da, q, since=None) is None  # only Next 16, no CSP
    assert recall.match_digest(dc, q, since=None) is not None  # both


def test_not_drops_session_with_excluded_term(fake_projects):
    jsonl_b = fake_projects / "test-project-slug" / "sess-b.jsonl"
    db, _ = recall.load_or_build_digest(jsonl_b)
    q = _query_or_not(["CSP"], ["legacy"])
    assert recall.match_digest(db, q, since=None) is None


def test_not_keeps_session_without_excluded_term(fake_projects):
    jsonl_c = fake_projects / "test-project-slug" / "sess-c.jsonl"
    dc, _ = recall.load_or_build_digest(jsonl_c)
    q = _query_or_not(["CSP"], ["legacy"])
    assert recall.match_digest(dc, q, since=None) is not None


# ─── pending precision ──────────────────────────────────────────────────────

def test_pending_regex_matches_actionable_only():
    actionable = [
        "TODO: validar build",
        "falta hacer el smoke test",
        "queda pendiente el merge",
        "queda por revisar el spec",
        "- [ ] checklist item",
        "next step: deploy",
        "blocked on review",
        "waiting on QA",
        "need to wire the analytics",
    ]
    noisy = [
        "queda así por ahora",
        "lo que queda dicho",
        "Listo, queda bien",
        "falta de tiempo",
        "TODOS sabemos",
    ]
    for line in actionable:
        assert any(p.search(line) for p in recall.PENDING_PATTERNS), \
            f"actionable miss: {line!r}"
    for line in noisy:
        assert not any(p.search(line) for p in recall.PENDING_PATTERNS), \
            f"noisy false positive: {line!r}"


# ─── repo-only filter ───────────────────────────────────────────────────────

def test_repo_only_drops_claude_home_paths(fake_projects, monkeypatch):
    # The fixture sess-a.jsonl has Edit on /repo/next.config.js + Write on
    # /home/user/.claude/skills/foo/SKILL.md
    jsonl = fake_projects / "test-project-slug" / "sess-a.jsonl"
    digest, _ = recall.load_or_build_digest(jsonl)
    files = set(digest.files_touched)
    # Sanity check the fixture is right
    assert "/repo/next.config.js" in files
    assert any("/.claude/skills/" in p for p in files)


# ─── render smoke ───────────────────────────────────────────────────────────

def test_format_markdown_no_matches():
    out = recall.format_markdown([], "nada", verbose=False)
    assert "No matches found" in out


def test_format_markdown_includes_resume_command(fake_projects):
    jsonl = fake_projects / "test-project-slug" / "sess-a.jsonl"
    digest, _ = recall.load_or_build_digest(jsonl)
    match = recall.match_digest(digest, _query_or("Next 16"), since=None)
    assert match is not None
    out = recall.format_markdown([match], "Next 16", verbose=False)
    assert "claude --resume" in out
    assert "sess-a" in out
