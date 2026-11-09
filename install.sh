#!/usr/bin/env bash
# Install RepoRabbit for Claude Code.
#
#   ./install.sh                     for every repository on this machine (~/.claude, or $CLAUDE_CONFIG_DIR)
#   ./install.sh --project DIR       for one repository (DIR/.claude)
#   ./install.sh --uninstall         remove it (add --project DIR for a project install)
#
# It then asks once whether RepoRabbit may work without repeated permission prompts.
#   --allow / --no-allow             answer that question up front
#
# It copies three things and touches nothing else in your Claude configuration:
#   agents/repo-rabbit.md, skills/repo-rabbit/, repo-rabbit/
set -euo pipefail

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
UNINSTALL=""
ALLOW=""
PROJECT_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --uninstall) UNINSTALL=1; shift ;;
    --allow) ALLOW=yes; shift ;;
    --no-allow) ALLOW=no; shift ;;
    --project)
      [[ $# -ge 2 && -d "$2" ]] || { echo "error: --project needs an existing directory" >&2; exit 1; }
      TARGET="$(cd "$2" && pwd)/.claude"; PROJECT_ARGS=(--project "$2"); shift 2 ;;
    *) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
  esac
done

PERMISSIONS="$TARGET/repo-rabbit/scripts/setup_permissions.py"

if [[ -n "$UNINSTALL" ]]; then
  [[ -f "$PERMISSIONS" ]] && python3 "$PERMISSIONS" --revoke ${PROJECT_ARGS[@]+"${PROJECT_ARGS[@]}"} >/dev/null || true
  rm -f "$TARGET/agents/repo-rabbit.md"
  rm -rf "$TARGET/skills/repo-rabbit" "$TARGET/repo-rabbit"
  rm -f "$TARGET/repo-rabbit-settings.json"
  echo "RepoRabbit removed from $TARGET. Atlases in your repositories (.repo-rabbit/) were left alone."
  exit 0
fi

[[ -f "$SOURCE/agents/repo-rabbit.md" ]] || { echo "error: run install.sh from a clone of the RepoRabbit repository" >&2; exit 1; }
command -v python3 >/dev/null || { echo "error: python3 is required" >&2; exit 1; }

mkdir -p "$TARGET/agents" "$TARGET/skills"
cp "$SOURCE/agents/repo-rabbit.md" "$TARGET/agents/repo-rabbit.md"
rm -rf "$TARGET/skills/repo-rabbit" "$TARGET/repo-rabbit"
cp -R "$SOURCE/skills/repo-rabbit" "$TARGET/skills/repo-rabbit"
mkdir -p "$TARGET/repo-rabbit"
for item in scripts schemas templates install.sh README.md; do
  cp -R "$SOURCE/$item" "$TARGET/repo-rabbit/$item"
done
find "$TARGET/repo-rabbit" -name __pycache__ -type d -prune -exec rm -rf {} +

echo "RepoRabbit installed to $TARGET."
echo
case "$ALLOW" in
  yes) python3 "$PERMISSIONS" --yes ${PROJECT_ARGS[@]+"${PROJECT_ARGS[@]}"} ;;
  no) echo "Skipped permissions. Claude Code will ask each time. To change that later: python3 $PERMISSIONS" ;;
  *) python3 "$PERMISSIONS" ${PROJECT_ARGS[@]+"${PROJECT_ARGS[@]}"} || true ;;
esac
echo
echo "Start a new Claude Code session in a repository and say: Use repo-rabbit to explore this repository."
