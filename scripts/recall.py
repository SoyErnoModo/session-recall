#!/usr/bin/env python3
"""
session-recall · mine ~/.claude/projects/<encoded>/*.jsonl by topic/keyword
and compress matching sessions into a paste-ready context bundle.

Usage:
  recall.py <topic>                    # current project (cwd-encoded)
  recall.py <topic> --all              # all projects
  recall.py <topic> --project=<slug>   # specific project slug
  recall.py <topic> --limit=N          # top-N sessions (default 5)
  recall.py <topic> --since=N          # last N days (default 60)
  recall.py <topic> --format=json      # machine-readable
  recall.py <topic> --verbose          # full turns, no truncation

Compression rules:
  - Drop: tool_result blocks (unless is_error), thinking blocks, system-reminders,
    isMeta/isCompactSummary turns, code fences >50 lines (truncate to head/tail),
    duplicate content (content-hash dedup).
  - Keep: user text turns with keyword, assistant decisions near keyword,
    files touched (Edit/Write input.file_path), pending markers (TODO/PENDING/
    "queda"/"falta"/checkboxes `[ ]`), first+last meaningful turn per session.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECTS_ROOT = Path.home() / ".claude" / "projects"
CACHE_ROOT = Path.home() / ".cache" / "session-recall"
CACHE_VERSION = 2  # bump when serialized shape changes

# Markers that flag "pending work" in free-text.
#
# v1 → v2 (2026-05-29) tightened anchors to drop `queda`/`falta` mid-sentence
# false positives ("Listo, queda así", "lo que queda dicho").
# v2 → v3 (2026-05-30) broadened the action-verb whitelist after audit found
# real-world phrasings being dropped (`falta probar/arreglar/documentar`,
# `queda chequear/revisar`). Checkbox restored to v1's bare `[ ]` form so
# non-dash bullets (`* [ ]`, `1. [ ]`, indented `[ ]`) also match.
#
# Design: anchor word + ≤3 words of slack + action verb. The verb list is
# DRY-shared between `queda` and `falta` via `_PENDING_VERBS`.
_PENDING_VERBS = (
    "hacer|implementar|wirear|validar|revisar|testear|merge|mergear|deployar|"
    "pushear|escribir|crear|completar|terminar|cerrar|chequear|investigar|"
    "probar|arreglar|documentar|corregir|agregar|poner|subir|migrar|publicar|"
    "actualizar|configurar|ajustar|conectar|definir|aprobar"
)
# Conjunctions (por, pendiente) are NOT verbs — they would let "queda así
# por ahora" trip as a false positive. `queda por <verb>` still matches
# because the regex allows 0-3 slack words before the action verb.

PENDING_PATTERNS = [
    re.compile(r"\bTODO\b[: ]", re.I),
    re.compile(r"\bPENDING\b[: ]", re.I),
    re.compile(r"\bFIXME\b[: ]", re.I),
    re.compile(r"\bTBD\b\)?", re.I),
    re.compile(r"\bsin terminar\b", re.I),
    re.compile(r"\bquedó? pendiente\b", re.I),
    re.compile(rf"\bqueda\s+(?:\w+\s+){{0,3}}(?:{_PENDING_VERBS})\b", re.I),
    re.compile(rf"\bfalta(?:n|ría|rían)?\s+(?:que\s+)?(?:\w+\s+){{0,3}}(?:{_PENDING_VERBS})\b", re.I),
    re.compile(r"\bnos\s+(?:queda|falta)\b", re.I),
    re.compile(r"\bnext\s+step\b", re.I),
    re.compile(r"\bblocked\s+on\b", re.I),
    re.compile(r"\bwaiting\s+(?:on|for)\b", re.I),
    re.compile(r"\bneed(?:s|ed)?\s+to\b", re.I),
    # Checkbox: bare `[ ]` anywhere — covers `- [ ]`, `* [ ]`, `1. [ ]`, indented.
    # False-positive rate empirically low (real transcripts almost never
    # render the literal two-char sequence outside checklists).
    re.compile(r"\[\s\]"),
]

# Decision/action verbs (es/en) that mark commitments by the assistant.
DECISION_PATTERNS = [
    re.compile(r"\b(decidí|decidimos|voy a|vamos a|propongo|propuesta)\b", re.I),
    re.compile(r"\b(approach|plan|decision|decided|going to|will)\b", re.I),
    re.compile(r"\b(creé|construí|escribí|wireé|implementé)\b", re.I),
    re.compile(r"\b(created|built|wrote|wired|implemented|fixed)\b", re.I),
]

FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def cwd_to_project_slug(cwd: str) -> str:
    # Claude Code encodes both `/` and `.` to `-` (e.g. `hernan.desouza` → `hernan-desouza`).
    return cwd.replace("/", "-").replace(".", "-")


def resolve_project_dirs(args: argparse.Namespace) -> list[Path]:
    if args.all:
        return sorted([d for d in PROJECTS_ROOT.iterdir() if d.is_dir()])
    if args.project:
        return [PROJECTS_ROOT / args.project]
    slug = cwd_to_project_slug(os.getcwd())
    candidate = PROJECTS_ROOT / slug
    if candidate.exists():
        return [candidate]
    return [PROJECTS_ROOT / cwd_to_project_slug(str(Path.cwd()))]


def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def block_text(block: Any) -> str:
    if isinstance(block, dict):
        if block.get("type") == "text":
            return block.get("text") or ""
        if block.get("type") == "tool_result":
            c = block.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return "\n".join(block_text(b) for b in c)
        if block.get("type") == "tool_use":
            inp = block.get("input", {})
            if isinstance(inp, dict):
                cmd = inp.get("command") or inp.get("file_path") or ""
                return str(cmd)
    return ""


def event_text(ev: dict) -> str:
    """Plain text content of a user/assistant event, ignoring noise."""
    if ev.get("isMeta") or ev.get("isCompactSummary"):
        return ""
    msg = ev.get("message")
    if not isinstance(msg, dict):
        return ""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if not isinstance(c, list):
        return ""
    parts: list[str] = []
    for b in c:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "thinking":
            continue
        if bt == "tool_result":
            if b.get("is_error"):
                parts.append(f"[tool error] {block_text(b)[:300]}")
            continue
        if bt == "tool_use":
            continue
        if bt == "text":
            parts.append(b.get("text") or "")
    return "\n".join(p for p in parts if p)


def truncate_code_blocks(text: str, max_lines: int = 50) -> str:
    """Collapse code fences longer than max_lines to head + tail + omission marker."""

    def collapse(match: re.Match) -> str:
        fence = match.group(0)
        lines = fence.splitlines()
        if len(lines) <= max_lines + 2:
            return fence
        head = lines[: max_lines // 2]
        tail = lines[-(max_lines // 2):]
        omitted = len(lines) - len(head) - len(tail)
        return "\n".join(head + [f"... [{omitted} lines omitted] ..."] + tail)

    return re.sub(r"```[\s\S]*?```", collapse, text)


def hash_content(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:12]


@dataclass
class SessionMatch:
    path: Path
    session_id: str
    ai_title: str = ""
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    hits: int = 0
    matched_user_turns: list[tuple[datetime | None, str]] = field(default_factory=list)
    matched_assistant_turns: list[tuple[datetime | None, str]] = field(default_factory=list)
    files_touched: set[str] = field(default_factory=set)
    decisions: list[str] = field(default_factory=list)
    pendings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    first_user_msg: str = ""
    last_user_msg: str = ""
    last_assistant_msg: str = ""
    git_branch: str = ""
    cwd: str = ""

    @property
    def score(self) -> float:
        recency_bonus = 0.0
        if self.last_ts:
            age_days = (datetime.now(timezone.utc) - self.last_ts).days
            recency_bonus = max(0.0, 30 - age_days) / 30.0  # 0..1
        return self.hits + recency_bonus * 5


@dataclass
class Digest:
    """Parsed, query-independent extract of a single jsonl session.

    Persisted to ~/.cache/session-recall/ keyed by jsonl path + mtime.
    Query phase runs against the digest, not the raw transcript.
    """
    file_path: str = ""
    mtime: float = 0.0
    size: int = 0
    session_id: str = ""
    ai_title: str = ""
    first_ts: str = ""  # ISO
    last_ts: str = ""
    git_branch: str = ""
    cwd: str = ""
    files_touched: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # turns: list of (iso_ts_or_empty, role, text)
    turns: list[tuple[str, str, str]] = field(default_factory=list)


def cache_path_for(jsonl_path: Path) -> Path:
    """Stable cache location for a given jsonl path."""
    digest_key = hashlib.sha1(str(jsonl_path).encode("utf-8")).hexdigest()[:16]
    return CACHE_ROOT / f"{digest_key}.json"


def digest_to_dict(d: Digest) -> dict:
    return {
        "_v": CACHE_VERSION,
        "file_path": d.file_path,
        "mtime": d.mtime,
        "size": d.size,
        "session_id": d.session_id,
        "ai_title": d.ai_title,
        "first_ts": d.first_ts,
        "last_ts": d.last_ts,
        "git_branch": d.git_branch,
        "cwd": d.cwd,
        "files_touched": d.files_touched,
        "errors": d.errors,
        "turns": d.turns,
    }


def dict_to_digest(d: dict) -> Digest:
    return Digest(
        file_path=d.get("file_path", ""),
        mtime=d.get("mtime", 0.0),
        size=int(d.get("size", 0)),
        session_id=d.get("session_id", ""),
        ai_title=d.get("ai_title", ""),
        first_ts=d.get("first_ts", ""),
        last_ts=d.get("last_ts", ""),
        git_branch=d.get("git_branch", ""),
        cwd=d.get("cwd", ""),
        files_touched=list(d.get("files_touched", [])),
        errors=list(d.get("errors", [])),
        turns=[tuple(t) for t in d.get("turns", [])],
    )


def parse_jsonl_to_digest(path: Path) -> Digest | None:
    """Read a jsonl end-to-end and build a query-independent digest."""
    st = path.stat()
    d = Digest(
        file_path=str(path),
        mtime=st.st_mtime,
        size=st.st_size,
        session_id=path.stem,
    )
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue

                t = ev.get("type")

                if t == "ai-title":
                    d.ai_title = ev.get("aiTitle") or d.ai_title
                    continue

                if t not in {"user", "assistant"}:
                    continue

                ts_obj = parse_ts(ev.get("timestamp"))
                ts_iso = ts_obj.isoformat() if ts_obj else ""
                if ts_obj:
                    if not d.first_ts:
                        d.first_ts = ts_iso
                    d.last_ts = ts_iso

                if not d.git_branch:
                    d.git_branch = ev.get("gitBranch") or ""
                if not d.cwd:
                    d.cwd = ev.get("cwd") or ""

                msg = ev.get("message")
                if isinstance(msg, dict):
                    c = msg.get("content")
                    if isinstance(c, list):
                        for b in c:
                            if isinstance(b, dict) and b.get("type") == "tool_use":
                                name = b.get("name") or ""
                                if name in FILE_TOOLS:
                                    inp = b.get("input", {}) or {}
                                    fp = inp.get("file_path") or inp.get("notebook_path")
                                    if fp and str(fp) not in d.files_touched:
                                        d.files_touched.append(str(fp))
                            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                                err_text = block_text(b)[:200]
                                if err_text and err_text not in d.errors:
                                    d.errors.append(err_text)

                text = event_text(ev)
                if not text:
                    continue

                role = msg.get("role") if isinstance(msg, dict) else ""
                d.turns.append((ts_iso, role or "", text))
    except OSError:
        return None
    return d


def load_or_build_digest(jsonl: Path, use_cache: bool = True) -> tuple[Digest | None, bool]:
    """Return (digest, was_cache_hit).

    Cache files and the cache dir are created with mode 0600 / 0700 because
    digests contain raw transcript content (env-var dumps, tokens, urls).
    """
    cache_file = cache_path_for(jsonl)
    if use_cache and cache_file.exists():
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            st = jsonl.stat()
            if payload.get("_v") != CACHE_VERSION:
                # Version mismatch — drop stale entry so it does not linger.
                try:
                    cache_file.unlink()
                except OSError:
                    pass
            elif (
                abs(payload.get("mtime", 0) - st.st_mtime) < 1e-3
                and int(payload.get("size", -1)) == st.st_size
            ):
                return dict_to_digest(payload), True
        except (json.JSONDecodeError, OSError):
            pass

    digest = parse_jsonl_to_digest(jsonl)
    if digest is None:
        return None, False
    if use_cache:
        try:
            CACHE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Per-PID tmp filename to avoid concurrent-write interleaving.
            tmp = cache_file.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(digest_to_dict(digest), ensure_ascii=False), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            tmp.replace(cache_file)
        except OSError:
            pass
    return digest, False


@dataclass
class Query:
    """Boolean keyword query: must (AND), should (OR), must_not (NOT)."""
    must: list[re.Pattern] = field(default_factory=list)
    should: list[re.Pattern] = field(default_factory=list)
    must_not: list[re.Pattern] = field(default_factory=list)

    def highlight_patterns(self) -> list[re.Pattern]:
        return self.must + self.should

    def turn_matches(self, text: str) -> list[re.Pattern]:
        """Return positive patterns (must|should) that fired in this turn."""
        return [p for p in self.highlight_patterns() if p.search(text)]

    def display(self) -> str:
        parts: list[str] = []
        if self.must:
            parts.append("AND(" + ", ".join(p.pattern for p in self.must) + ")")
        if self.should:
            parts.append("OR(" + ", ".join(p.pattern for p in self.should) + ")")
        if self.must_not:
            parts.append("NOT(" + ", ".join(p.pattern for p in self.must_not) + ")")
        return " ".join(parts)


def match_digest(digest: Digest, query: Query, since: datetime | None) -> SessionMatch | None:
    """Apply a query over a pre-parsed digest. Cheap — no jsonl IO."""
    path = Path(digest.file_path)
    match = SessionMatch(path=path, session_id=digest.session_id)
    match.ai_title = digest.ai_title
    match.git_branch = digest.git_branch
    match.cwd = digest.cwd
    match.files_touched = set(digest.files_touched)
    match.errors = list(digest.errors)
    match.first_ts = parse_ts(digest.first_ts) if digest.first_ts else None
    match.last_ts = parse_ts(digest.last_ts) if digest.last_ts else None

    seen_hashes: set[str] = set()
    must_hits: set[int] = set()
    should_seen = False
    must_not_tripped = False
    for ts_iso, role, text in digest.turns:
        ts = parse_ts(ts_iso) if ts_iso else None
        if since and ts and ts < since:
            continue

        if role == "user":
            if not match.first_user_msg:
                match.first_user_msg = text[:500]
            match.last_user_msg = text[:500]
        elif role == "assistant":
            match.last_assistant_msg = text[:500]

        if any(p.search(text) for p in query.must_not):
            must_not_tripped = True
            break

        for idx, p in enumerate(query.must):
            if p.search(text):
                must_hits.add(idx)

        hit_patterns = query.turn_matches(text)
        if not hit_patterns:
            continue
        if query.should and any(p.search(text) for p in query.should):
            should_seen = True

        h = hash_content(text)
        if h in seen_hashes:
            continue
        seen_hashes.add(h)

        match.hits += 1
        snippet = text
        for p in hit_patterns:
            snippet = p.sub(lambda m: f"**{m.group(0)}**", snippet)
        snippet = snippet.strip()[:600]

        if role == "user":
            match.matched_user_turns.append((ts, snippet))
        else:
            match.matched_assistant_turns.append((ts, snippet))

        for line_text in text.splitlines():
            if any(p.search(line_text) for p in DECISION_PATTERNS) and role == "assistant":
                if line_text.strip() not in match.decisions:
                    match.decisions.append(line_text.strip()[:300])
            if any(p.search(line_text) for p in PENDING_PATTERNS):
                if line_text.strip() not in match.pendings:
                    match.pendings.append(line_text.strip()[:300])

    if must_not_tripped:
        return None
    if query.must and len(must_hits) < len(query.must):
        return None
    if query.should and not should_seen:
        return None
    if match.hits == 0:
        return None
    return match


def format_markdown(matches: list[SessionMatch], topic: str, verbose: bool) -> str:
    if not matches:
        return f"# session-recall · `{topic}`\n\n_No matches found in the scanned transcripts._\n"

    out: list[str] = []
    out.append(f"# session-recall · `{topic}`")
    out.append("")
    out.append(f"_{len(matches)} session(s) matched · scored by hits + recency_")
    out.append("")

    out.append("## TL;DR")
    for i, m in enumerate(matches, 1):
        title = m.ai_title or "(untitled)"
        when = m.last_ts.strftime("%Y-%m-%d") if m.last_ts else "?"
        out.append(f"{i}. **{title}** — {when} · hits {m.hits} · branch `{m.git_branch or '?'}`")
    out.append("")

    for i, m in enumerate(matches, 1):
        out.append(f"## {i}. {m.ai_title or m.session_id[:8]}")
        if m.last_ts:
            span = ""
            if m.first_ts and m.first_ts.date() != m.last_ts.date():
                span = f" → {m.last_ts.strftime('%Y-%m-%d %H:%M')}"
            out.append(f"_session_ `{m.session_id}` · _{m.first_ts.strftime('%Y-%m-%d %H:%M') if m.first_ts else '?'}{span}_ · _branch_ `{m.git_branch or '?'}` · _hits_ {m.hits}")
        out.append("")

        if m.first_user_msg:
            intent = truncate_code_blocks(m.first_user_msg)
            out.append("**Intent (first user msg):**")
            out.append(f"> {intent[:400].strip()}")
            out.append("")

        if m.decisions:
            out.append("**Decisions / actions:**")
            for d in m.decisions[: None if verbose else 8]:
                out.append(f"- {d}")
            out.append("")

        keep_user = m.matched_user_turns if verbose else m.matched_user_turns[:3]
        if keep_user:
            out.append("**User turns matching topic:**")
            for ts, snip in keep_user:
                tag = ts.strftime("%m-%d %H:%M") if ts else ""
                out.append(f"- _{tag}_ — {truncate_code_blocks(snip)}")
            out.append("")

        keep_asst = m.matched_assistant_turns if verbose else m.matched_assistant_turns[:3]
        if keep_asst:
            out.append("**Assistant turns matching topic:**")
            for ts, snip in keep_asst:
                tag = ts.strftime("%m-%d %H:%M") if ts else ""
                out.append(f"- _{tag}_ — {truncate_code_blocks(snip)}")
            out.append("")

        if m.files_touched:
            out.append("**Files touched (Edit/Write):**")
            for fp in sorted(m.files_touched)[: None if verbose else 15]:
                out.append(f"- `{fp}`")
            out.append("")

        if m.pendings:
            out.append("**Pending markers found:**")
            for p in m.pendings[: None if verbose else 10]:
                out.append(f"- {p}")
            out.append("")

        if m.errors:
            out.append("**Tool errors observed:**")
            for e in m.errors[: None if verbose else 5]:
                out.append(f"- {e}")
            out.append("")

        if m.last_assistant_msg:
            out.append("**Last assistant turn (closing context):**")
            out.append(f"> {truncate_code_blocks(m.last_assistant_msg)[:400].strip()}")
            out.append("")
        if m.last_user_msg and m.last_user_msg != m.first_user_msg:
            out.append("**Last user turn:**")
            out.append(f"> {truncate_code_blocks(m.last_user_msg)[:400].strip()}")
            out.append("")

        out.append("---")
        out.append("")

    out.append("## Resume command")
    out.append("To continue the most-recent matched session in Claude Code:")
    out.append("```bash")
    if matches:
        latest = max(matches, key=lambda x: x.last_ts or datetime.min.replace(tzinfo=timezone.utc))
        out.append(f"claude --resume {latest.session_id}")
    out.append("```")
    out.append("")

    return "\n".join(out)


def format_json(matches: list[SessionMatch]) -> str:
    payload = []
    for m in matches:
        payload.append({
            "session_id": m.session_id,
            "path": str(m.path),
            "ai_title": m.ai_title,
            "first_ts": m.first_ts.isoformat() if m.first_ts else None,
            "last_ts": m.last_ts.isoformat() if m.last_ts else None,
            "git_branch": m.git_branch,
            "cwd": m.cwd,
            "hits": m.hits,
            "score": round(m.score, 2),
            "first_user_msg": m.first_user_msg,
            "last_user_msg": m.last_user_msg,
            "last_assistant_msg": m.last_assistant_msg,
            "decisions": m.decisions,
            "pendings": m.pendings,
            "errors": m.errors,
            "files_touched": sorted(m.files_touched),
            "matched_user_turns": [(ts.isoformat() if ts else None, s) for ts, s in m.matched_user_turns],
            "matched_assistant_turns": [(ts.isoformat() if ts else None, s) for ts, s in m.matched_assistant_turns],
        })
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recall past Claude Code sessions by topic.",
        epilog=(
            "Boolean examples:\n"
            "  recall.py 'Next 16' 'CSP' --and       # both must match\n"
            "  recall.py 'Next 16' 'CSP'             # either matches (OR, default)\n"
            "  recall.py 'Next 16' --not legacy      # match Next 16, drop sessions mentioning legacy\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("topic", nargs="+", help="Keyword or phrase to search. Pass multiple for OR.")
    parser.add_argument("--and", dest="require_all", action="store_true",
                        help="Require ALL topic terms to match (AND instead of OR).")
    parser.add_argument("--not", dest="exclude", action="append", default=[],
                        help="Exclude sessions matching this term (repeatable).")
    parser.add_argument("--regex", action="store_true",
                        help="Treat topic terms and --not values as regex (not literal).")
    parser.add_argument("--all", action="store_true", help="Search across all projects.")
    parser.add_argument("--project", help="Project slug under ~/.claude/projects/")
    parser.add_argument("--limit", type=int, default=5, help="Top-N sessions (default 5).")
    parser.add_argument("--since", type=int, default=60, help="Lookback window in days (default 60).")
    parser.add_argument("--format", choices=["md", "json"], default="md")
    parser.add_argument("--verbose", action="store_true", help="No truncation of turns/files.")
    parser.add_argument(
        "--repo-only",
        action="store_true",
        help="Show only files under the current cwd repo (drop ~/.claude/* skill/memory edits).",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Skip the on-disk digest cache (~/.cache/session-recall/) and re-parse every jsonl.",
    )
    parser.add_argument(
        "--cache-clear",
        action="store_true",
        help="Delete the cache before running.",
    )
    parser.add_argument(
        "--cache-stats",
        action="store_true",
        help="Print cache hit/miss stats to stderr at the end.",
    )
    args = parser.parse_args()

    if args.cache_clear:
        if CACHE_ROOT.exists():
            for p in CACHE_ROOT.glob("*.json"):
                try:
                    p.unlink()
                except OSError:
                    pass

    terms = [t.strip() for t in args.topic if t.strip()]
    if not terms:
        print("error: empty topic", file=sys.stderr)
        return 2

    def compile_term(s: str) -> re.Pattern:
        return re.compile(s if args.regex else re.escape(s), re.IGNORECASE)

    query = Query()
    if args.require_all:
        query.must = [compile_term(t) for t in terms]
    else:
        query.should = [compile_term(t) for t in terms]
    query.must_not = [compile_term(t) for t in args.exclude]

    display_topic = " ".join(f'"{t}"' for t in terms)
    if args.require_all and len(terms) > 1:
        display_topic = " AND ".join(f'"{t}"' for t in terms)
    if args.exclude:
        display_topic += " NOT " + " ".join(f'"{e}"' for e in args.exclude)

    since = datetime.now(timezone.utc) - timedelta(days=args.since) if args.since else None

    project_dirs = resolve_project_dirs(args)
    matches: list[SessionMatch] = []
    cache_hits = cache_misses = 0

    for pdir in project_dirs:
        if not pdir.exists():
            continue
        for jsonl in pdir.glob("*.jsonl"):
            digest, was_hit = load_or_build_digest(jsonl, use_cache=not args.no_cache)
            if digest is None:
                continue
            cache_hits += int(was_hit)
            cache_misses += int(not was_hit)
            m = match_digest(digest, query, since)
            if m:
                matches.append(m)

    matches.sort(key=lambda x: x.score, reverse=True)
    matches = matches[: args.limit]

    if args.repo_only:
        cwd_str = os.path.realpath(os.getcwd())
        claude_home = os.path.realpath(os.path.expanduser("~/.claude"))

        def _under(child: str, parent: str) -> bool:
            """True iff child resolves to a path inside parent. Sibling-safe."""
            try:
                return os.path.commonpath([child, parent]) == parent
            except ValueError:
                return False  # different drives on Windows

        for m in matches:
            m.files_touched = {
                fp for fp in m.files_touched
                if (
                    _under(os.path.realpath(fp), cwd_str)
                    and not _under(os.path.realpath(fp), claude_home)
                )
            }

    if args.format == "json":
        print(format_json(matches))
    else:
        print(format_markdown(matches, display_topic, args.verbose))

    if args.cache_stats:
        total = cache_hits + cache_misses
        rate = (cache_hits / total * 100) if total else 0.0
        print(
            f"\ncache: {cache_hits} hit / {cache_misses} miss / {total} total ({rate:.1f}% hit)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
