---
name: session-recall
description: Recover the relevant context of past Claude Code sessions by topic/keyword without re-opening blank conversations. Mines `~/.claude/projects/<encoded-cwd>/*.jsonl`, ranks sessions by hits + recency, dedupes turns by content hash, drops sintetic turns (isMeta, isCompactSummary, tool_result without is_error, thinking blocks, system-reminders), truncates code blocks >50 lines, and emits a paste-ready context bundle per session with: aiTitle, intent, decisions, files touched (Edit/Write), pending markers (TODO/PENDING/`[ ]`/falta/queda), tool errors, and last user/assistant turn for closing context. Auto-invoke when the user says (ES/EN) "/recall <topic>", "retomá la sesión sobre X", "recuperá el contexto de X", "qué hicimos sobre X", "recall session about X", "resume context for X", "abrime lo de X sin contexto vacío", "compress old conversations about X". Companion to session-recap (time-scoped weekly) and topic-roadmap (cross-artifact theme inventory) — this one is keyword-scoped and replays compressed transcript signal only.
---

# session-recall

Recuperar el contexto relevante de sesiones pasadas por **tema/keyword** sin re-abrir conversaciones vacías. Comprime transcripts a un bundle paste-ready con lo cargado: decisions, files, pending, errors, last turn.

## Cuándo invocar

Triggers literales (ES/EN):
- `/recall <topic>`, `/session-recall <topic>`
- "retomá la sesión sobre X"
- "recuperá el contexto de X"
- "qué hicimos sobre X" (cuando lo que se busca es restaurar contexto, no inventariar PRs)
- "recall context about X", "resume context for X"
- "abrime lo de X sin contexto vacío"
- "comprimí las conversaciones viejas sobre X"

NO invocar para:
- Recap semanal time-scoped → usar [`session-recap`](../session-recap/SKILL.md)
- Inventario PRs + SDD + memorias por tema → usar [`topic-roadmap`](../topic-roadmap/SKILL.md)
- Buscar fix recipe en commits → `git log --grep`
- Tareas one-shot que no requieren historia previa

## Pipeline

### Step 0 — Resolver scope

| Argumento | Default | Notas |
|-----------|---------|-------|
| `<topic>` | obligatorio | keyword o frase, case-insensitive, regex-escaped |
| `--all` | `false` | scan cross-project (todos los slugs bajo `~/.claude/projects/`) |
| `--project=<slug>` | cwd-encoded | slug específico; útil cuando cambiaste de repo |
| `--limit=N` | `5` | top-N sessions devueltas |
| `--since=N` | `60` | ventana lookback en días |
| `--format=md\|json` | `md` | json para consumo programático |
| `--verbose` | `false` | sin truncar turns/files/decisions |

**Slug encoding** — Claude Code reemplaza `/` Y `.` con `-`. El script ya lo maneja (`cwd.replace('/', '-').replace('.', '-')`).

### Step 1 — Ejecutar

```bash
python3 ~/.claude/skills/session-recall/scripts/recall.py "<topic>"
python3 ~/.claude/skills/session-recall/scripts/recall.py "Next 16" --limit=3 --since=30
python3 ~/.claude/skills/session-recall/scripts/recall.py "CSP" --all --since=180
python3 ~/.claude/skills/session-recall/scripts/recall.py "comercios database" --format=json --limit=1
```

### Step 2 — Compresión (qué entra, qué se descarta)

**Drop (ruido):**
- `isMeta: true` events (skill body, system-reminders inyectados como user turn).
- `isCompactSummary: true` events.
- `tool_result` blocks sin `is_error: true` (output de Bash/Read no aporta intent del humano).
- `thinking` blocks del assistant (no son output decidido).
- Contenido duplicado por content-hash SHA1 truncado a 12 chars.
- Code fences > 50 líneas → head/tail con marker `[N lines omitted]`.

**Keep (señal):**
- `aiTitle` event (label pre-computado por Claude Code, perfecto para TL;DR).
- User text turns que matchean el keyword (con bold `**topic**` para localizar visualmente).
- Assistant text turns que matchean el keyword.
- Decisions: líneas con verbos de compromiso (`decidí`, `voy a`, `propongo`, `creé`, `decided`, `going to`, `built`, `wrote`).
- Files touched: `tool_use.input.file_path` de `Edit | Write | MultiEdit | NotebookEdit`.
- Pending markers: `TODO`, `PENDING`, `FIXME`, `TBD`, `sin terminar`, `falta`, `queda`, `[ ]`.
- Tool errors: `tool_result.is_error == true`, contenido truncado a 200 chars.
- First user message (intent original).
- Last user + last assistant turn (closing context).

**Score**:
```
score = hits + max(0, 30 - age_days) / 30 * 5
```
Recency bonus máximo 5, decae linealmente a 0 en 30 días.

### Step 3 — Output

Markdown con secciones por session:
1. **TL;DR** — lista numerada con aiTitle + fecha + hits + branch.
2. Por session: intent, decisions, matched user/assistant turns, files, pendings, errors, last turn.
3. **Resume command** al final — `claude --resume <session-id>` de la sesión más reciente.

JSON con shape:
```json
[{
  "session_id": "...",
  "ai_title": "...",
  "first_ts": "...", "last_ts": "...",
  "git_branch": "...", "cwd": "...",
  "hits": 30, "score": 34.8,
  "first_user_msg": "...", "last_user_msg": "...", "last_assistant_msg": "...",
  "decisions": [...], "pendings": [...], "errors": [...],
  "files_touched": [...],
  "matched_user_turns": [[ts, snippet], ...],
  "matched_assistant_turns": [[ts, snippet], ...]
}]
```

## Recovery + handoff

El output trae al final el comando `claude --resume <session-id>`. Dos vías:

1. **Resume real**: correr `claude --resume <id>` en una nueva terminal → restaura el .jsonl completo en memoria.
2. **Paste-context**: copiar el bundle markdown completo al inicio de una sesión nueva como contexto inicial → más liviano, conserva sólo lo cargado.

Elegir según necesidad: resume real para continuar implementación; paste-context para sintetizar/planear sin arrastrar 200 turns viejos.

## Compañeros en el sistema de memoria

- [`session-recap`](../session-recap/SKILL.md) — time-scoped (semana/N días), cruza GitHub + git + branches. Esta skill cubre el "qué hicimos esta semana".
- [`topic-roadmap`](../topic-roadmap/SKILL.md) — theme-scoped, inventario cross-artifact (PRs + SDD changes + RFCs + memorias) con Mermaid. Cubre el "todo lo relacionado a X y a dónde va".
- [`session-recall`] (este skill) — keyword-scoped, sólo transcripts, comprimido. Cubre el "abrime el contexto de X sin re-abrir todo".

Reglas no overlap:
- Si necesitás restaurar **conversación previa** → `session-recall`.
- Si necesitás **lo que hice esta semana** → `session-recap`.
- Si necesitás **mapa completo de un tema** (PRs + specs + docs + branches) → `topic-roadmap`.

## Auto-invocación

El skill se auto-invoca cuando el user escribe los triggers de arriba. Para deep-dive — multi-topic synthesis, dedup cross-project, prose narrative — invocar el agent companion `session-recall` (en `~/.claude/agents/session-recall.md`).

## Notas de implementación

- Script en `scripts/recall.py`, ejecutable directo (`#!/usr/bin/env python3`).
- Sin dependencias externas — sólo stdlib.
- TCC-safe: vive en `~/.claude/skills/`, lee `~/.claude/projects/` — ambos fuera de `~/Documents/` así que launchd/Python no necesita TCC grant explícito ([[reference_bitacora_deck_audit_cron]]).
- Shape verificado contra `.jsonl` reales (Modo-landing, 264 transcripts, ago 2026): events tienen `type` top-level (`user`/`assistant`/`ai-title`/`queue-operation`/`attachment`/`system`/`pr-link`), `message.content` puede ser str o list de blocks ([[reference_claude_transcript_mining]]).
- Slug encoding gotcha: dots en el username (`hernan.desouza`) se convierten a `-`. Implementación: `cwd.replace('/', '-').replace('.', '-')`.

## Anti-patrones

- **No usar regex de pending overly broad** — `queda`/`falta` matchean mid-sentence false positives. Recall > precision para v1; si molesta, agregar flag `--strict-pending`.
- **No filtrar files_touched de auto-memory** — si la sesión incluyó `distill-learnings`, los memory MDs SON parte del trabajo, no ruido.
- **No subir el limit default** — top 5 ya satura context. Si querés más, pasá `--limit=N` explícito.
- **No correr con `--all` sin `--since`** — cross-project sin window puede escanear 5k+ jsonl. Default 60d es razonable.
