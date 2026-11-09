"""A tiny repository and a complete, valid Atlas for it, shared by the tests."""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

APP = """\
\"\"\"A tiny app.\"\"\"
import json


def load(path):
    with open(path) as f:
        return json.load(f)


def greet(name):
    if not name:
        raise ValueError("name required")
    return f"hello {name}"


def main():
    print(greet("rabbit"))


if __name__ == "__main__":
    main()
"""

README = "# Tiny\n\nA tiny app for tests.\n\nRun it with `python app.py`.\n"


def make_repo(root: Path, git: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text(APP)
    (root / "README.md").write_text(README)
    (root / ".gitignore").write_text("*.log\n")
    if git:
        for args in (["init", "-q"], ["add", "."],
                     ["-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init"]):
            subprocess.run(["git", "-C", str(root), *args], check=True)
    return root


def ref(anchor: str, end: str | None = None) -> dict:
    out = {"path": "app.py", "anchor": anchor}
    if end:
        out["anchor_end"] = end
    return out


GREET = ref("def greet(name):", 'return f"hello {name}"')
DOC = {"path": "README.md", "anchor": "A tiny app"}


def _fact(key: str) -> dict:
    return {"key": key, "label": key, "text": f"About {key}.", "refs": [GREET]}


def _group(gid: str, topic: str) -> dict:
    return {"id": gid, "title": gid, "topic": topic,
            "blocks": [{"id": f"{gid}.b", "type": "prose", "text": f"About {topic}.", "refs": [GREET]}]}


def atlas() -> dict:
    from validate import MODULE_KEYS, REQUIRED_TOPICS, WORKFLOW_KEYS

    overview = [_group(f"ov.{t}", t) for t in REQUIRED_TOPICS["overview"] if t != "start-here"]
    overview.append({"id": "ov.start", "title": "Start here", "topic": "start-here", "blocks": [
        {"id": "ov.start.list", "type": "list", "ordered": True, "items": [
            {"text": "`app.py`, the whole program.", "refs": [GREET]},
            {"text": "`README.md`, how to run it.", "refs": [DOC], "certainty": "documented"},
            {"text": "`app.py` again, for `main`.", "refs": [ref("def main():")]},
        ]}]})
    architecture = [_group(f"ar.{t}", t) for t in REQUIRED_TOPICS["architecture"] if t != "component-map"]
    architecture.insert(0, {"id": "ar.map", "title": "Map", "topic": "component-map", "blocks": [{
        "id": "ar.map.d", "type": "component_diagram", "caption": "The parts",
        "nodes": [{"id": "cli", "label": "main", "kind": "entry", "target": "mod.app"},
                  {"id": "greet", "label": "greet", "target": "mod.other"},
                  {"id": "fs", "label": "file system", "kind": "external"}],
        "edges": [{"from": "cli", "to": "greet", "label": "calls"},
                  {"from": "greet", "to": "fs", "kind": "runtime"}],
        "refs": [GREET], "certainty": "inferred"}]})
    modules = [{
        "id": gid, "title": gid, "topic": "module", "collapsible": True, "files": ["app.py"],
        "related": [other],
        "blocks": [{"id": f"{gid}.facts", "type": "facts", "items": [_fact(k) for k in MODULE_KEYS]}],
    } for gid, other in (("mod.app", "mod.other"), ("mod.other", "mod.app"))]
    stack = [_group(f"st.{t}", t) for t in REQUIRED_TOPICS["stack"]]
    stack[3]["blocks"][0] = {"id": "st.ci-deploy.b", "type": "prose", "text": "No CI found.",
                             "certainty": "absence"}
    workflows = [{
        "id": f"wf.{i}", "title": f"Workflow {i}", "topic": "workflow",
        "blocks": [
            {"id": f"wf.{i}.steps", "type": "list", "ordered": True, "items": [
                {"text": "`main` calls `greet`.", "refs": [ref("def main():")]}]},
            {"id": f"wf.{i}.seq", "type": "sequence_diagram",
             "participants": [{"id": "m", "label": "main"}, {"id": "g", "label": "greet"}],
             "messages": [{"from": "m", "to": "g", "label": "greet(name)"},
                          {"from": "g", "to": "g", "label": "validate"},
                          {"from": "g", "to": "m", "label": "text", "kind": "return"}],
             "refs": [GREET]},
            {"id": f"wf.{i}.facts", "type": "facts", "items": [_fact(k) for k in WORKFLOW_KEYS]},
        ]} for i in (1, 2, 3)]
    walkthrough = [
        {"id": "wk.steps", "title": "Steps", "blocks": [{
            "id": "wk.1", "type": "step", "number": 1, "title": "Call `greet`", "code": GREET,
            "what": "Builds the text.", "why": "It is the program.", "inputs": "`name`",
            "outputs": "A string.", "next": "`print`.", "behavior": "Raises on empty names."}]},
        {"id": "wk.data", "title": "Data", "topic": "data-trace", "blocks": [{
            "id": "wk.data.t", "type": "table", "headers": ["Step", "Value"],
            "rows": [{"cells": ["1", "`rabbit`"], "refs": [GREET]}]}]},
        {"id": "wk.tree", "title": "Tree", "blocks": [{
            "id": "wk.tree.t", "type": "execution_tree",
            "root": {"label": "main", "kind": "call", "ref": ref("def main():"), "children": [
                {"label": "greet", "kind": "call", "target": "wk.1", "ref": GREET},
                {"label": "empty name", "kind": "error", "note": "raises", "ref": ref("raise ValueError")}]}}]},
        {"id": "wk.next", "title": "Next", "topic": "follow-ons", "blocks": [{
            "id": "wk.next.l", "type": "list", "items": [
                {"text": "Trace `load`.", "certainty": "context"},
                {"text": "Trace the error path.", "certainty": "context"}]}]},
    ]
    return copy.deepcopy({
        "schema_version": 1,
        "project": {"name": "Tiny", "one_liner": "A tiny app."},
        "coverage": {"deep": [{"area": "app.py", "why": "It is everything."}], "scanned": [], "skipped": []},
        "next_steps": [{"text": "Read `load`.", "refs": [ref("def load(path):")]}],
        "sections": [
            {"id": "overview", "intro": "A tiny overview.", "groups": overview},
            {"id": "architecture", "intro": "Small.", "groups": architecture},
            {"id": "modules", "intro": "Two views.", "groups": modules},
            {"id": "stack", "intro": "Python.", "groups": stack},
            {"id": "workflows", "intro": "Three.", "groups": workflows},
            {"id": "walkthrough", "intro": "One.", "groups": walkthrough},
        ],
    })


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True)
    return done.stdout


def commit_all(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


def make_branches(repo: Path) -> None:
    """main, plus two branches that both change app.py and one that also tracks awkward files."""
    git(repo, "branch", "-M", "main")
    git(repo, "switch", "-qc", "feature-a")
    with (repo / "app.py").open("a") as f:
        f.write("\n\ndef shout(name):\n    return greet(name).upper()\n")
    (repo / "extra.py").write_text("VALUE = 1\n")
    commit_all(repo, "Add shout")
    git(repo, "switch", "-q", "main")
    git(repo, "switch", "-qc", "feature-b")
    path = repo / "app.py"
    path.write_text(path.read_text().replace('return f"hello {name}"', 'return f"hi {name}"'))
    commit_all(repo, "Say hi")
    git(repo, "switch", "-q", "main")


def tour_draft() -> dict:
    shout = {"path": "app.py", "anchor": "def shout(name):", "anchor_end": "return greet(name).upper()"}
    return {
        "summary": "Adds a louder greeting. See [[w.1]].",
        "groups": [
            {"id": "c", "title": "What changed", "topic": "changes", "blocks": [
                {"id": "c.p", "type": "prose", "text": "Adds `shout`.", "refs": [shout]}]},
            {"id": "i", "title": "Impact", "topic": "impact", "blocks": [
                {"id": "i.p", "type": "prose", "text": "Touches [[mod.app|the app]].", "refs": [shout]}]},
            {"id": "w", "title": "The new path", "topic": "walkthrough", "blocks": [
                {"id": "w.1", "type": "step", "number": 1, "title": "Shout", "code": shout, "what": "Upper-cases.",
                 "why": "Louder.", "inputs": "`name`", "outputs": "Text.", "next": "Returns."},
                {"id": "w.t", "type": "execution_tree", "root": {"label": "shout", "kind": "call", "target": "w.1"}}]},
            {"id": "r", "title": "Review", "topic": "review", "blocks": [
                {"id": "r.l", "type": "list", "items": [
                    {"text": "No test covers `shout`.", "certainty": "absence"}]}]},
        ],
    }
