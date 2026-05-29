#!/usr/bin/env bash
# session-recall installer
# Installs the skill + agent + script into ~/.claude/
# Idempotent: safe to re-run.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"
SKILL_DIR="$CLAUDE_HOME/skills/session-recall"
AGENT_DIR="$CLAUDE_HOME/agents"
AGENT_FILE="$AGENT_DIR/session-recall.md"

GREEN="\033[38;2;0;136;89m"
DIM="\033[2m"
RESET="\033[0m"

echo -e "${GREEN}session-recall${RESET} ${DIM}installer${RESET}"
echo

# 1. Verify python3
if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 not found. Install Python 3 first." >&2
  exit 1
fi
PY_VER=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
echo "✓ python3 found ($PY_VER)"

# 2. Verify Claude home
if [[ ! -d "$CLAUDE_HOME" ]]; then
  echo
  echo "warning: $CLAUDE_HOME does not exist."
  echo "Claude Code stores its state there. Have you run it at least once?"
  echo "Continuing anyway — directories will be created."
fi

# 3. Install skill
mkdir -p "$SKILL_DIR/scripts" "$SKILL_DIR/reference"
cp "$REPO_ROOT/SKILL.md" "$SKILL_DIR/SKILL.md"
cp "$REPO_ROOT/README.md" "$SKILL_DIR/README.md"
cp "$REPO_ROOT/scripts/recall.py" "$SKILL_DIR/scripts/recall.py"
chmod +x "$SKILL_DIR/scripts/recall.py"
echo "✓ skill installed at $SKILL_DIR"

# 4. Install agent
mkdir -p "$AGENT_DIR"
cp "$REPO_ROOT/agent.md" "$AGENT_FILE"
echo "✓ agent installed at $AGENT_FILE"

# 5. Smoke
if python3 "$SKILL_DIR/scripts/recall.py" --help >/dev/null 2>&1; then
  echo "✓ script smoke OK"
else
  echo "warning: script smoke failed — investigate manually"
fi

echo
echo -e "${GREEN}done.${RESET} open Claude Code and try:"
echo
echo "  /recall <topic>"
echo
echo "or run the script directly:"
echo
echo "  python3 $SKILL_DIR/scripts/recall.py \"<topic>\" --limit=3"
echo
