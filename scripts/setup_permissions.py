#!/usr/bin/env python3
"""Let RepoRabbit work without repeated permission prompts, once the user agrees.

    setup_permissions.py            show the exact rules and ask before adding them
    setup_permissions.py --yes      add them without asking (you already decided)
    setup_permissions.py --revoke   remove exactly those rules
    setup_permissions.py --status   say whether they are present

Deliberately separate from rabbit.py: the rules below let the agent run rabbit.py,
so nothing rabbit.py can do may change them. They cover this copy of the helper,
reading RepoRabbit's own files, and writing inside .repo-rabbit/, and nothing else.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from store import atomic_write_text  # noqa: E402

BACKUP_SUFFIX = ".repo-rabbit.bak"


def rules_for(helper_root: Path) -> list[str]:
    return [
        f"Bash(python3 {helper_root}/scripts/rabbit.py *)",
        f"Read(/{helper_root}/**)",  # A leading // is an absolute path in a permission rule.
        "Edit(**/.repo-rabbit/**)",  # Edit rules also cover the Write tool.
    ]


def settings_path(args) -> Path:
    if args.settings:
        return Path(args.settings).expanduser().resolve()
    if args.project:
        return Path(args.project).expanduser().resolve() / ".claude" / "settings.local.json"
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser() / "settings.json"


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        sys.exit(f"error: {path} is not valid JSON (line {exc.lineno}); fix it first. Nothing was changed.")
    if not isinstance(data, dict):
        sys.exit(f"error: {path} is not a JSON object. Nothing was changed.")
    return data


def save(path: Path, data: dict) -> None:
    if path.exists() and not Path(str(path) + BACKUP_SUFFIX).exists():
        shutil.copy2(path, str(path) + BACKUP_SUFFIX)
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def confirm(path: Path, rules: list[str]) -> bool:
    print(f"RepoRabbit would add these rules to {path}:\n")
    print("  " + "\n  ".join(rules))
    print("\nThey let it run its own helper, read its own files, and write inside .repo-rabbit/")
    print("without asking each time. Reading code in your repository already needs no approval.")
    print("Anything else it tries still goes through Claude Code's normal permission checks.\n")
    if not sys.stdin.isatty():
        print("Not added: no terminal to ask on. To agree, run this again with --yes.")
        return False
    try:
        return input("Allow these? [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="add the rules without asking")
    parser.add_argument("--revoke", action="store_true", help="remove the rules")
    parser.add_argument("--status", action="store_true", help="report whether the rules are present")
    parser.add_argument("--project", help="use this project's .claude/settings.local.json")
    parser.add_argument("--settings", help="use this settings file")
    parser.add_argument("--helper-root", default=str(Path(__file__).resolve().parent.parent),
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    path = settings_path(args)
    rules = rules_for(Path(args.helper_root).resolve())
    data = load(path)
    allow = data.get("permissions", {}).get("allow", [])

    if args.status:
        missing = [r for r in rules if r not in allow]
        print("allowed" if not missing else f"not allowed: {len(missing)} of {len(rules)} rules missing from {path}")
        return 0 if not missing else 1
    if args.revoke:
        kept = [r for r in allow if r not in rules]
        if kept != allow:
            data["permissions"]["allow"] = kept
            save(path, data)
        print(f"RepoRabbit's rules are not in {path}.")
        return 0
    if all(r in allow for r in rules):
        print("RepoRabbit is already allowed.")
        return 0
    if not args.yes and not confirm(path, rules):
        return 0 if not sys.stdin.isatty() else 1
    data.setdefault("permissions", {}).setdefault("allow", [])
    data["permissions"]["allow"] += [r for r in rules if r not in data["permissions"]["allow"]]
    save(path, data)
    print(f"Done. RepoRabbit can work without repeated prompts. Undo with: {sys.argv[0]} --revoke")
    return 0


if __name__ == "__main__":
    sys.exit(main())
