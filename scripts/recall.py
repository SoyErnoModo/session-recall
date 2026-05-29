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
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# Markers that flag "pending work" in free-text.
# Tightened 2026-05-29: require an action-context anchor (verb/noun pair) instead of
# matching `queda`/`falta` mid-sentence as standalone words.
# Why: in real transcripts "queda" matched "lo que queda dicho", "Listo, queda así" —
# noise. Now we demand the marker is followed by something that looks like a task.
PENDING_PATTERNS = [
    re.compile(r"\bTODO\b[: ]", re.I),
    re.compile(r"\bPENDING\b[: ]", re.I),
    re.compile(r"\bFIXME\b[: ]", re.I),
    re.compile(r"\bTBD\b\)?", re.I),
    re.compile(r"\bsin terminar\b", re.I),
    re.compile(r"\bquedó? pendiente\b", re.I),
    re.compile(r"\bqueda\s+(por|pendiente|hacer|para)\b", re.I),
    re.compile(r"\bfalta(?:n|ría|rían)?\s+(?:que\s+)?(?:\w+\s+){0,3}\b(?:hacer|implementar|wirear|validar|revisar|testear|merge|mergear|deployar|pushear|escribir|crear|completar|terminar|cerrar|chequear|investigar)\b", re.I),
    re.compile(r"\bnos\s+(?:queda|falta)\b", re.I),
    re.compile(r"\bnext\s+step\b", re.I),
    re.compile(r"\bblocked\s+on\b", re.I),
    re.compile(r"\bwaiting\s+(?:on|for)\b", re.I),
    re.compile(r"\bneed(?:s|ed)?\s+to\b", re.I),
    re.compile(r"-\s*\[\s\]"),  # markdown checkbox
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


def scan_jsonl(path: Path, pattern: re.Pattern, since: datetime | None) -> SessionMatch | None:
    match = SessionMatch(path=path, session_id=path.stem)
    seen_hashes: set[str] = set()
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
                    match.ai_title = ev.get("aiTitle") or match.ai_title
                    continue

                if t not in {"user", "assistant"}:
                    continue

                ts = parse_ts(ev.get("timestamp"))
                if since and ts and ts < since:
                    continue
                if ts:
                    match.first_ts = match.first_ts or ts
                    match.last_ts = ts

                if not match.git_branch:
                    match.git_branch = ev.get("gitBranch") or ""
                if not match.cwd:
                    match.cwd = ev.get("cwd") or ""

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
                                    if fp:
                                        match.files_touched.add(str(fp))
                            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                                err_text = block_text(b)[:200]
                                if err_text and err_text not in match.errors:
                                    match.errors.append(err_text)

                text = event_text(ev)
                if not text:
                    continue

                h = hash_content(text)
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)

                role = msg.get("role") if isinstance(msg, dict) else None

                if role == "user":
                    if not match.first_user_msg:
                        match.first_user_msg = text[:500]
                    match.last_user_msg = text[:500]
                elif role == "assistant":
                    match.last_assistant_msg = text[:500]

                if not pattern.search(text):
                    continue

                match.hits += 1
                snippet = pattern.sub(lambda m: f"**{m.group(0)}**", text)
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
    except OSError:
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
    parser = argparse.ArgumentParser(description="Recall past Claude Code sessions by topic.")
    parser.add_argument("topic", nargs="+", help="Keyword or phrase to search.")
    parser.add_argument("--all", action="store_true", help="Search across all projects.")
    parser.add_argument("--project", help="Project slug under ~/.claude/projects/")
    parser.add_argument("--limit", type=int, default=5, help="Top-N sessions (default 5).")
    parser.add_argument("--since", type=int, default=60, help="Lookback window in days (default 60).")
    parser.add_argument("--format", choices=["md", "json"], default="md")
    parser.add_argument("--verbose", action="store_true", help="No truncation of turns/files.")
    args = parser.parse_args()

    topic = " ".join(args.topic).strip()
    if not topic:
        print("error: empty topic", file=sys.stderr)
        return 2

    pattern = re.compile(re.escape(topic), re.IGNORECASE)
    since = datetime.now(timezone.utc) - timedelta(days=args.since) if args.since else None

    project_dirs = resolve_project_dirs(args)
    matches: list[SessionMatch] = []

    for pdir in project_dirs:
        if not pdir.exists():
            continue
        for jsonl in pdir.glob("*.jsonl"):
            m = scan_jsonl(jsonl, pattern, since)
            if m:
                matches.append(m)

    matches.sort(key=lambda x: x.score, reverse=True)
    matches = matches[: args.limit]

    if args.format == "json":
        print(format_json(matches))
    else:
        print(format_markdown(matches, topic, args.verbose))
    return 0


if __name__ == "__main__":
    sys.exit(main())
