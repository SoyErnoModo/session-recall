"""Pytest fixtures for session-recall tests.

Builds a synthetic ~/.claude/projects/<slug>/ tree in a tmp_path so tests
do not depend on the developer's real transcripts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import recall  # noqa: E402 — import after sys.path mutation


def make_event(role: str, text: str, ts: str | None = None, **extras) -> dict:
    base = {
        "type": role if role in ("user", "assistant") else "user",
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }
    if ts:
        base["timestamp"] = ts
    base.update(extras)
    return base


def make_tool_use(name: str, file_path: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": name,
                    "input": {"file_path": file_path},
                }
            ],
        },
        "timestamp": "2026-05-29T10:00:00Z",
    }


def make_tool_error(text: str) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_1",
                    "is_error": True,
                    "content": text,
                }
            ],
        },
    }


def write_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


@pytest.fixture
def fake_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a synthetic projects dir at tmp_path/projects with 3 sessions.

    Patches recall.PROJECTS_ROOT, recall.CACHE_ROOT, and HOME so the script
    does not touch the user's real data.
    """
    projects_root = tmp_path / "projects"
    cache_root = tmp_path / "cache"
    slug = "test-project-slug"
    pdir = projects_root / slug
    pdir.mkdir(parents=True)

    # Session A — matches "Next 16" + has decisions + pending + error
    write_jsonl(
        pdir / "sess-a.jsonl",
        [
            {"type": "ai-title", "sessionId": "sess-a", "aiTitle": "Migrate to Next 16"},
            make_event(
                "user",
                "necesitamos migrar a Next 16 esta semana",
                ts="2026-05-25T10:00:00Z",
                gitBranch="feat/migrate",
                cwd="/repo",
            ),
            make_event(
                "assistant",
                "Voy a empezar con el codemod de Next 16. Decisión: usamos Turbopack.",
                ts="2026-05-25T10:01:00Z",
            ),
            make_tool_use("Edit", "/repo/next.config.js"),
            make_tool_use("Write", "/home/user/.claude/skills/foo/SKILL.md"),
            make_tool_error("Exit code 1: missing dep"),
            make_event(
                "user",
                "TODO: validar el build de Next 16 en preprod",
                ts="2026-05-25T10:02:00Z",
            ),
            make_event(
                "assistant",
                "Listo. Falta hacer el smoke test en preprod después del deploy.",
                ts="2026-05-25T10:03:00Z",
            ),
        ],
    )

    # Session B — matches CSP but not Next 16, and mentions "legacy"
    write_jsonl(
        pdir / "sess-b.jsonl",
        [
            {"type": "ai-title", "sessionId": "sess-b", "aiTitle": "Tighten CSP"},
            make_event(
                "user", "hay que endurecer la CSP legacy del sitio",
                ts="2026-05-26T09:00:00Z", cwd="/repo",
            ),
            make_event(
                "assistant", "Voy a sacar el unsafe-inline de la CSP.",
                ts="2026-05-26T09:01:00Z",
            ),
        ],
    )

    # Session C — matches both Next 16 AND CSP, no "legacy"
    write_jsonl(
        pdir / "sess-c.jsonl",
        [
            {"type": "ai-title", "sessionId": "sess-c", "aiTitle": "Next 16 + CSP combo"},
            make_event(
                "user", "validar que la CSP no rompa con Next 16",
                ts="2026-05-27T11:00:00Z", cwd="/repo",
            ),
            make_event(
                "assistant", "Voy a verificar la CSP contra Next 16 en preprod.",
                ts="2026-05-27T11:01:00Z",
            ),
        ],
    )

    monkeypatch.setattr(recall, "PROJECTS_ROOT", projects_root)
    monkeypatch.setattr(recall, "CACHE_ROOT", cache_root)
    monkeypatch.setenv("HOME", str(tmp_path))

    return projects_root
