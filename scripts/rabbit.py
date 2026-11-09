#!/usr/bin/env python3
"""RepoRabbit's deterministic half: scan, store, validate, render, serve, refresh.

The repo-rabbit agent does the thinking and calls these commands. Nothing here
calls a model, runs repository code or touches the network. The target is the
git repository containing the working directory, or ``--repo``; nothing depends
on where this script is installed.

    rabbit.py init                     create .repo-rabbit/ and ignore it in git
    rabbit.py scan                     survey the repository (pass 1)
    rabbit.py check DRAFT              validate a draft without saving it
    rabbit.py ingest DRAFT             validate, save and render an Atlas
    rabbit.py validate [--sample N]    audit every stored reference
    rabbit.py render [--ide NAME]      rebuild index.html from atlas.json
    rabbit.py serve | stop | status    run the local Atlas server
    rabbit.py questions [--status S]   list questions with their Atlas context
    rabbit.py claim ID                 show a question as being investigated
    rabbit.py progress [--id ID] TEXT  report what the agent is doing, for the page
    rabbit.py answer ID FILE           save an answer, then mark the question answered
    rabbit.py fail ID --reason TEXT    record that an investigation failed
    rabbit.py branches                 list branches that can be toured
    rabbit.py tour-request --base B X  queue tours of branches X... against base B
    rabbit.py tours                    list tours waiting to be done
    rabbit.py tour-prepare ID          snapshot a branch and compute its diff
    rabbit.py tour-ingest ID FILE      validate and publish a tour
    rabbit.py tour-fail ID --reason T  record that a tour failed
    rabbit.py tour-remove ID           delete a tour and its snapshot
    rabbit.py refresh [--dry-run]      detect changes, re-point references, mark stale blocks
    rabbit.py patch FILE               apply agent edits to the stored Atlas atomically
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import random
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import renderer as render_mod  # noqa: E402
import store  # noqa: E402
import tours as tours_mod  # noqa: E402
from renderer import Renderer, validate_answer_refs  # noqa: E402
from store import Paths, now_iso, read_json, write_json  # noqa: E402
from validate import (  # noqa: E402
    SECTION_ORDER,
    Result,
    SourceIndex,
    audit_stored,
    find_secret,
    is_sensitive_path,
    iter_blocks,
    iter_refs,
    ref_state,
    validate_draft,
    validate_patched,
)

TOOL_VERSION = "1.0"
DEFAULT_PORT = 8742
GITIGNORE_ENTRY = ".repo-rabbit/"
OWN_FILES = (".repo-rabbit/", ".claude/repo-rabbit/", ".claude/skills/repo-rabbit/",
             ".claude/agents/repo-rabbit.md")
MAX_FILES = 20000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_PROGRESS = 300

SKIP_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
             ".mypy_cache", ".ruff_cache", ".tox", "dist", "build", "target", ".next", ".repo-rabbit",
             ".idea", ".vscode", ".gradle", "vendor"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar",
                   ".tgz", ".bz2", ".xz", ".7z", ".whl", ".jar", ".so", ".dylib", ".dll", ".exe",
                   ".bin", ".pt", ".pth", ".onnx", ".safetensors", ".parquet", ".npz", ".npy",
                   ".mp4", ".mp3", ".wav", ".woff", ".woff2", ".ttf", ".eot", ".DS_Store"}
LANGUAGES = {".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript",
             ".tsx": "TypeScript", ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin",
             ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".c": "C", ".h": "C/C++", ".cpp": "C++",
             ".cc": "C++", ".hpp": "C++", ".swift": "Swift", ".scala": "Scala", ".sh": "Shell",
             ".bash": "Shell", ".html": "HTML", ".css": "CSS", ".sql": "SQL", ".md": "Markdown",
             ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML", ".json": "JSON", ".proto": "Protobuf",
             ".tf": "Terraform"}
MANIFEST_NAMES = {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "uv.lock", "poetry.lock",
                  "Pipfile", "package.json", "pnpm-lock.yaml", "yarn.lock", "package-lock.json", "go.mod",
                  "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts", "Gemfile", "composer.json",
                  "Makefile", "CMakeLists.txt", "justfile", "Taskfile.yml", "tox.ini", "noxfile.py"}
INFRA_NAMES = {"Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yaml", "Procfile",
               "fly.toml", "vercel.json", "netlify.toml", "serverless.yml", "skaffold.yaml"}
ENTRY_MARKERS = ("if __name__ ==", "func main(", "fn main(", "public static void main", "int main(",
                 "createServer(", "app.listen(", "[project.scripts]", '"bin":', "#!/usr/bin/env")


class CliError(Exception):
    pass


# -- repository discovery -------------------------------------------------------------


def find_repo(explicit: str | None) -> Path:
    if explicit:
        repo = Path(explicit).expanduser().resolve()
        if not repo.is_dir():
            raise CliError(f"{explicit} is not a directory")
        return repo
    start = Path.cwd().resolve()
    for folder in (start, *start.parents):
        if (folder / ".git").exists():
            return folder
    return start


def _git(repo: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                              timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def revision_of(repo: Path) -> dict:
    head = _git(repo, "rev-parse", "HEAD")
    status = _git(repo, "status", "--porcelain")
    return {"head": head.strip() if head else None, "dirty": bool(status and status.strip())}


def list_files(repo: Path) -> tuple[list[str], list[str]]:
    """``(candidates, excluded)``: repo-relative paths; ``excluded`` were held back as sensitive."""
    listed = _git(repo, "ls-files", "-co", "--exclude-standard", "-z")
    if listed is not None:
        names = [n for n in listed.split("\0") if n]
    else:
        names = []
        for folder, dirs, files in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                names.append((Path(folder) / name).relative_to(repo).as_posix())
    kept, excluded = [], []
    for name in sorted(names):
        parts = Path(name).parts
        if any(p in SKIP_DIRS for p in parts[:-1]):
            continue
        if name.startswith(OWN_FILES):  # RepoRabbit is a tool here, not part of the subject.
            continue
        (excluded if is_sensitive_path(name) else kept).append(name)
    return kept, excluded


def hash_files(paths: Paths, names: list[str], max_bytes: int) -> tuple[dict[str, dict], list[dict]]:
    """Hash text files, reusing earlier hashes when size and mtime are unchanged."""
    cache_path = paths.cache / "hashes.json"
    cache = read_json(cache_path, {}) or {}
    files: dict[str, dict] = {}
    skipped: list[dict] = []
    fresh_cache: dict[str, dict] = {}
    for name in names:
        full = paths.repo / name
        suffix = Path(name).suffix.lower()
        if suffix in BINARY_SUFFIXES or Path(name).name in BINARY_SUFFIXES:
            skipped.append({"path": name, "why": "binary type"})
            continue
        try:
            info = full.stat()
        except OSError:
            continue
        if not full.is_file():
            continue
        if info.st_size > max_bytes:
            skipped.append({"path": name, "why": f"larger than {max_bytes} bytes"})
            continue
        known = cache.get(name)
        if known and known.get("size") == info.st_size and known.get("mtime") == info.st_mtime_ns:
            entry = known
        else:
            data = full.read_bytes()
            if b"\x00" in data[:8192]:
                skipped.append({"path": name, "why": "binary content"})
                continue
            entry = {"size": info.st_size, "mtime": info.st_mtime_ns,
                     "sha256": hashlib.sha256(data).hexdigest(), "lines": data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)}
        fresh_cache[name] = entry
        files[name] = {"sha256": entry["sha256"], "lines": entry["lines"], "size": entry["size"]}
    write_json(cache_path, fresh_cache)
    return files, skipped


def cmd_init(paths: Paths, _args) -> int:
    paths.ensure()
    print(json.dumps({"root": str(paths.root), "gitignore": ensure_gitignore(paths.repo)}))
    return 0


def ensure_gitignore(repo: Path) -> str:
    """Add the entry if it is absent, leaving every existing byte in place."""
    if not (repo / ".git").exists():
        return "not a git repository"
    path = repo / ".gitignore"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    if any(line.strip() in (GITIGNORE_ENTRY, ".repo-rabbit", "/.repo-rabbit/", "/.repo-rabbit") for line in text.splitlines()):
        return "already ignored"
    if text and not text.endswith("\n"):
        text += "\n"
    store.atomic_write_text(path, text + GITIGNORE_ENTRY + "\n")
    return "added"


def cmd_scan(paths: Paths, args) -> int:
    paths.ensure()
    ensure_gitignore(paths.repo)
    names, excluded = list_files(paths.repo)
    truncated = max(0, len(names) - args.max_files)
    considered = names[: args.max_files]
    files, skipped = hash_files(paths, considered, args.max_file_bytes)

    languages: Counter[str] = Counter()
    lines_by_top: Counter[str] = Counter()
    files_by_top: Counter[str] = Counter()
    for name, info in files.items():
        languages[LANGUAGES.get(Path(name).suffix.lower(), "other")] += info["lines"]
        top = name.split("/", 1)[0] if "/" in name else "(root)"
        lines_by_top[top] += info["lines"]
        files_by_top[top] += 1

    def by_name(check) -> list[str]:
        return [n for n in files if check(n)]

    def base(n: str) -> str:
        return n.rsplit("/", 1)[-1]

    entries = []
    for name in files:
        if Path(name).suffix.lower() not in LANGUAGES or files[name]["size"] > 400_000:
            continue
        try:
            text = (paths.repo / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if any(marker in line for marker in ENTRY_MARKERS):
                entries.append(f"{name}:{number}: {line.strip()[:100]}")
                break
    largest = sorted(files, key=lambda n: -files[n]["lines"])[:25]
    report = {
        "repo": str(paths.repo),
        "revision": revision_of(paths.repo),
        "totals": {"candidates": len(names), "hashed": len(files), "lines": sum(f["lines"] for f in files.values())},
        "budget": {"max_files": args.max_files, "max_file_bytes": args.max_file_bytes,
                   "files_over_budget": truncated, "truncated": bool(truncated)},
        "languages_by_lines": languages.most_common(),
        "top_level": [{"dir": d, "files": files_by_top[d], "lines": n} for d, n in lines_by_top.most_common(40)],
        "docs": by_name(lambda n: base(n).lower().startswith(("readme", "contributing", "architecture", "design")) or n.startswith("docs/"))[:60],
        "manifests": by_name(lambda n: base(n) in MANIFEST_NAMES)[:60],
        "infrastructure": by_name(lambda n: base(n) in INFRA_NAMES or n.startswith((".github/workflows/", ".gitlab-ci", ".circleci/", "k8s/", "helm/", "terraform/")) or n.endswith(".tf"))[:60],
        "tests": by_name(lambda n: "test" in base(n).lower() or "/tests/" in f"/{n}" or "/spec/" in f"/{n}")[:60],
        "entry_point_candidates": entries[:60],
        "largest_files": [{"path": n, "lines": files[n]["lines"]} for n in largest],
        "excluded_as_sensitive": excluded,
        "skipped": {"count": len(skipped), "sample": skipped[:20]},
    }
    write_json(paths.cache / "scan.json", {**report, "files": files, "skipped": skipped})
    print(json.dumps(report, indent=1))
    return 0


# -- drafts, ingest, render -----------------------------------------------------------


def load_draft(path: Path) -> dict:
    """A single JSON file, or a directory holding meta.json and one file per section."""
    if path.is_dir():
        meta = _load_json(path / "meta.json")
        sections = [_load_json(path / f"{sid}.json") for sid in SECTION_ORDER]
        return {"schema_version": 1, **meta, "sections": sections}
    return _load_json(path)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CliError(f"{path} does not exist") from None
    except json.JSONDecodeError as exc:
        raise CliError(f"{path}: invalid JSON at line {exc.lineno}: {exc.msg}") from None


def report_result(result: Result, heading: str) -> None:
    print(json.dumps({"check": heading, "ok": result.ok, "errors": result.errors[:200],
                      "error_count": len(result.errors), "warnings": result.warnings[:50],
                      "stats": result.stats}, indent=1))


def settings_of(paths: Paths) -> dict:
    return read_json(paths.settings, {}) or {}


def render_page(paths: Paths, ide: str | None = None) -> None:
    atlas = read_json(paths.atlas)
    if atlas is None:
        raise CliError("no atlas.json yet; run the Explore workflow first")
    settings = settings_of(paths)
    if ide:
        settings["ide"] = ide
        write_json(paths.settings, settings)
    page = Renderer(paths, atlas, settings.get("ide", "vscode"))
    page.reconcile_anchors()
    store.atomic_write_text(paths.index, page.page())


def build_manifest(paths: Paths, atlas: dict, previous: dict | None, files: dict | None = None) -> dict:
    if files is None:
        names = list_files(paths.repo)[0][:MAX_FILES]
        files = hash_files(paths, names, MAX_FILE_BYTES)[0]
    blocks: dict[str, dict] = {}
    refs: list[dict] = []
    for section, group, block in iter_blocks(atlas):
        found = list(iter_refs(block))
        blocks[block["id"]] = {"section": section["id"], "group": group["id"], "refs": len(found)}
        refs += [{"block": block["id"], "path": r["path"], "start": r["start"], "end": r["end"]} for r in found]
    modules = [g["id"] for s in atlas["sections"] for g in s["groups"] if g.get("topic") == "module"]
    return {
        "schema_version": 1,
        "tool_version": TOOL_VERSION,
        "revision": revision_of(paths.repo),
        "created_at": (previous or {}).get("created_at") or now_iso(),
        "updated_at": now_iso(),
        "files": files,
        "analyzed_modules": modules,
        "sections": {s["id"]: [g["id"] for g in s["groups"]] for s in atlas["sections"]},
        "blocks": blocks,
        "references": refs,
        "coverage": atlas["coverage"],
    }


def save_atlas(paths: Paths, atlas: dict, files: dict | None = None) -> None:
    previous = read_json(paths.manifest)
    if paths.atlas.exists():
        store.atomic_write_text(paths.cache / "atlas.previous.json", paths.atlas.read_text(encoding="utf-8"))
    write_json(paths.atlas, atlas)
    write_json(paths.manifest, build_manifest(paths, atlas, previous, files))
    render_page(paths)


def cmd_check(paths: Paths, args) -> int:
    draft = load_draft(Path(args.draft))
    result = validate_draft(draft, paths.repo)
    report_result(result, "draft")
    return 0 if result.ok else 1


def cmd_ingest(paths: Paths, args) -> int:
    paths.ensure()
    ensure_gitignore(paths.repo)
    draft = load_draft(Path(args.draft))
    result = validate_draft(draft, paths.repo)
    report_result(result, "ingest")
    if not result.ok:
        print("Nothing was saved. Fix the errors and run ingest again.", file=sys.stderr)
        return 1
    save_atlas(paths, draft)
    print(f"Atlas written to {paths.index}")
    return 0


def cmd_render(paths: Paths, args) -> int:
    render_page(paths, args.ide)
    print(paths.index)
    return 0


def cmd_validate(paths: Paths, args) -> int:
    atlas = read_json(paths.atlas)
    if atlas is None:
        raise CliError("no atlas.json yet")
    result = audit_stored(atlas, paths.repo)
    report_result(result, "stored atlas")
    if args.sample:
        index = SourceIndex(paths.repo)
        everything = [(b["id"], r) for _, _, b in iter_blocks(atlas) for r in iter_refs(b)]
        chooser = random.Random(args.seed)
        picks = chooser.sample(everything, min(args.sample, len(everything)))
        out = []
        for block_id, ref in picks:
            lines = index.lines(ref["path"]) or []
            out.append({"block": block_id, "location": f"{ref['path']}:{ref['start']}-{ref['end']}",
                        "anchor": ref.get("anchor"), "lines": lines[ref["start"] - 1:min(ref["end"], ref["start"] + 11)]})
        print(json.dumps({"spot_check_sample": out}, indent=1))
    return 0 if result.ok else 1


# -- server ---------------------------------------------------------------------------


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError):
        return False
    return True


def _health(port: int, timeout: float = 1.0) -> dict | None:
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                f"http://127.0.0.1:{port}/api/health", timeout=timeout) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def running_server(paths: Paths) -> dict | None:
    state = read_json(paths.server_state)
    if not state or not _alive(state["pid"]):
        return None
    health = _health(state["port"])
    if health and health.get("app") == "repo-rabbit" and health.get("repo") == str(paths.repo) and health.get("pid") == state["pid"]:
        return state
    return None


def cmd_serve(paths: Paths, args) -> int:
    if not paths.index.exists():
        raise CliError("no Atlas yet; run the Explore workflow first")
    render_page(paths)
    if args.foreground:
        import server

        server.serve_forever(paths, args.port)
        return 0
    state = running_server(paths)
    if state is None:
        paths.server_state.unlink(missing_ok=True)
        with open(os.devnull, "r+b") as devnull:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--repo", str(paths.repo), "serve",
                 "--foreground", "--port", str(args.port)],
                stdin=devnull, stdout=devnull, stderr=devnull, start_new_session=True, cwd=str(paths.repo))
        deadline = time.time() + 10
        while time.time() < deadline:
            state = running_server(paths)
            if state:
                break
            time.sleep(0.1)
        if state is None:
            raise CliError("the server did not start within 10 seconds")
    url = f"http://127.0.0.1:{state['port']}/"
    opened = False
    if not args.no_open:
        try:
            opened = webbrowser.open(url)
        except webbrowser.Error:
            opened = False
    print(json.dumps({"url": url, "pid": state["pid"], "browser_opened": opened}))
    return 0


def cmd_stop(paths: Paths, _args) -> int:
    state = running_server(paths)
    if state is None:
        print("not running")
        return 0
    os.kill(state["pid"], signal.SIGTERM)
    paths.server_state.unlink(missing_ok=True)
    print(f"stopped {state['pid']}")
    return 0


def cmd_status(paths: Paths, _args) -> int:
    state = running_server(paths)
    questions = store.load_questions(paths)
    print(json.dumps({
        "atlas": paths.atlas.exists(), "server": state,
        "questions": dict(Counter(q.get("status") for q in questions)),
        "orphaned": sum(q.get("anchor") == "orphaned" for q in questions),
    }))
    return 0


# -- questions and answers ------------------------------------------------------------


def conversation_before(question: dict, questions: list[dict], answers: list[dict]) -> list[dict]:
    """The earlier turns of this question's thread, oldest first, with what was answered."""
    root = question.get("root_id")
    if not root:
        return []
    turns = []
    for earlier in questions:  # Stored in the order they were asked.
        if earlier["id"] == question["id"]:
            break
        if earlier["id"] == root or earlier.get("root_id") == root:
            replies = [a for a in answers if a.get("question_id") == earlier["id"]]
            turns.append({"question": earlier.get("question"), "status": earlier.get("status"),
                          "answers": [a["text"] for a in replies],
                          "references": [f"{r['path']}:{r['start']}-{r['end']}" for a in replies for r in a.get("refs", [])]})
    return turns


def question_context(page: Renderer, question: dict, answers: list[dict], questions: list[dict] | None = None) -> dict:
    block_id = question.get("block_id", "")
    block_data = None
    section_title = group_title = None
    for section, group, block in iter_blocks(page.atlas):
        if block["id"] == block_id:
            block_data, section_title, group_title = block, section["id"], group["title"]
    tour = page.tour_of(block_id)
    if tour:
        for group in tour["body"]["groups"]:
            for block in group["blocks"]:
                if block["id"] == block_id:
                    block_data, section_title, group_title = block, "tours", group["title"]
    related = list(iter_refs(block_data)) if block_data else []
    return {
        **question,
        "thread": conversation_before(question, questions or [], answers),
        "tour": {"id": tour["id"], "branch": tour["branch"], "snapshot_dir": str(tour["src"]),
                 "note": "Read and cite this branch's snapshot; reference paths are relative to it."} if tour else None,
        "context": {
            "section": section_title or question.get("section_id"),
            "group": group_title,
            "block_text": (page.anchor_text(block_id) or "")[:4000],
            "block_refs": [f"{r['path']}:{r['start']}-{r['end']}" for r in related],
        },
        "earlier_answers": [a["text"] for a in answers if a.get("question_id") == question["id"]],
    }


def cmd_questions(paths: Paths, args) -> int:
    atlas = read_json(paths.atlas)
    if atlas is None:
        raise CliError("no atlas.json yet")
    page = Renderer(paths, atlas)
    page.reconcile_anchors()
    answers = store.load_answers(paths)
    everything = store.load_questions(paths)
    rows = [question_context(page, q, answers, everything) for q in everything
            if args.status == "all" or q.get("status") == args.status
            or (args.status == "open" and q.get("status") == "investigating")]  # An interrupted run can be resumed.
    print(json.dumps(rows, indent=1, ensure_ascii=False))
    return 0


def cmd_claim(paths: Paths, args) -> int:
    try:
        store.claim_question(paths, args.id)
    except KeyError:
        raise CliError(f"no question {args.id}") from None
    store.log_activity(paths, "RepoRabbit started investigating", args.id)
    print(json.dumps({"question": args.id, "status": "investigating"}))
    return 0


def cmd_progress(paths: Paths, args) -> int:
    message = " ".join(args.message.split())
    if not message or len(message) > MAX_PROGRESS:
        raise CliError(f"progress must be 1-{MAX_PROGRESS} characters")
    if find_secret(message):
        raise CliError("progress text contains credential-looking text")
    if args.id and args.id not in {r.get("id") for r in store.load_questions(paths) + store.load_tours(paths)}:
        raise CliError(f"no question {args.id}")
    store.log_activity(paths, message, args.id)
    return 0


def cmd_answer(paths: Paths, args) -> int:
    answer = _load_json(Path(args.file))
    if not isinstance(answer, dict) or not isinstance(answer.get("text"), str) or not answer["text"].strip():
        raise CliError("an answer is a JSON object with a non-empty 'text'")
    unknown = set(answer) - {"text", "refs", "certainty"}
    if unknown:
        raise CliError(f"unknown answer fields: {sorted(unknown)}")
    certainty = answer.get("certainty", "verified")
    if certainty not in ("verified", "inferred", "tested", "documented", "context", "absence"):
        raise CliError(f"unknown certainty {certainty!r}")
    question = next((q for q in store.load_questions(paths) if q.get("id") == args.id), None)
    atlas = read_json(paths.atlas) or {"sections": [], "project": {}}
    tour = Renderer(paths, atlas).tour_of(question.get("block_id", "")) if question else None
    refs, errors = validate_answer_refs(answer.get("refs", []), SourceIndex(tour["src"] if tour else paths.repo))
    if errors:
        raise CliError("the question stays open; bad references:\n  " + "\n  ".join(errors))
    if not refs and certainty not in ("context", "absence"):
        raise CliError("the question stays open; a grounded answer needs at least one reference")
    if find_secret(answer["text"]):
        raise CliError("the question stays open; the answer contains credential-looking text")
    try:
        saved = store.record_answer(paths, args.id, {"text": answer["text"], "refs": refs, "certainty": certainty,
                                                    **({"tour_id": tour["id"]} if tour else {})})
    except KeyError:
        raise CliError(f"no question {args.id}") from None
    store.log_activity(paths, f"Answer saved with {len(refs)} source reference(s)", args.id)
    render_page(paths)
    print(json.dumps({"saved": saved["id"], "question": args.id, "status": "answered"}))
    return 0


def cmd_fail(paths: Paths, args) -> int:
    try:
        store.mark_failed(paths, args.id, args.reason)
    except KeyError:
        raise CliError(f"no question {args.id}") from None
    store.log_activity(paths, f"Investigation failed: {args.reason}"[:MAX_PROGRESS], args.id)
    render_page(paths)
    print(json.dumps({"question": args.id, "status": "failed"}))
    return 0


# -- branch tours ---------------------------------------------------------------------


def cmd_branches(paths: Paths, _args) -> int:
    print(json.dumps(tours_mod.list_branches(paths.repo), indent=1))
    return 0


def cmd_tour_request(paths: Paths, args) -> int:
    base = args.base or tours_mod.list_branches(paths.repo)["default"]
    created = tours_mod.request_tours(paths, base, args.branches)
    print(json.dumps({"base": base, "queued": created}, indent=1))
    return 0


def cmd_tours(paths: Paths, args) -> int:
    rows = [t for t in store.load_tours(paths) if args.status == "all" or t.get("status") in
            (("open", "touring") if args.status == "open" else (args.status,))]
    print(json.dumps(rows, indent=1))
    return 0


def cmd_tour_prepare(paths: Paths, args) -> int:
    try:
        print(json.dumps(tours_mod.prepare_tour(paths, args.id), indent=1))
    except tours_mod.TourError as exc:
        if args.id in {t.get("id") for t in store.load_tours(paths)}:
            tours_mod.fail_tour(paths, args.id, str(exc))
        raise
    return 0


def cmd_tour_ingest(paths: Paths, args) -> int:
    result = tours_mod.save_tour(paths, args.id, _load_json(Path(args.file)))
    report_result(result, "tour")
    if not result.ok:
        print("Nothing was published. Fix the errors and run tour-ingest again.", file=sys.stderr)
        return 1
    render_page(paths)
    return 0


def cmd_tour_fail(paths: Paths, args) -> int:
    tours_mod.fail_tour(paths, args.id, args.reason)
    render_page(paths)
    return 0


def cmd_tour_remove(paths: Paths, args) -> int:
    tours_mod.remove_tour(paths, args.id)
    render_page(paths)
    return 0


# -- incremental updates --------------------------------------------------------------


def group_dependents(atlas: dict, groups: set[str]) -> set[str]:
    """Groups whose diagram nodes have an edge into one of ``groups``."""
    dependents: set[str] = set()
    for _, _, block in iter_blocks(atlas):
        if block["type"] != "component_diagram":
            continue
        target_of = {n["id"]: n.get("target") for n in block["nodes"]}
        for edge in block["edges"]:
            if target_of.get(edge["to"]) in groups and target_of.get(edge["from"]):
                dependents.add(target_of[edge["from"]])
    return dependents - groups


def cmd_refresh(paths: Paths, args) -> int:
    atlas = read_json(paths.atlas)
    manifest = read_json(paths.manifest)
    if atlas is None or manifest is None:
        raise CliError("no Atlas yet; run the Explore workflow first")
    names = list_files(paths.repo)[0][:MAX_FILES]
    current, _ = hash_files(paths, names, MAX_FILE_BYTES)
    old = manifest["files"]
    changed = sorted(n for n in current if n in old and old[n]["sha256"] != current[n]["sha256"])
    added = sorted(set(current) - set(old))
    deleted = sorted(set(old) - set(current))
    touched = set(changed) | set(deleted)

    index = SourceIndex(paths.repo)
    revision = manifest.get("revision", {}).get("head")
    moved = 0
    stale_blocks: dict[str, dict] = {}
    cleared: list[str] = []
    for _, _, block in iter_blocks(atlas):
        problems: list[tuple[str, str]] = []
        for ref in iter_refs(block):
            state, start, end = ref_state(ref, index)
            if state == "moved":
                ref.update(start=start, end=end)
                ref.pop("state", None)
                moved += 1
            elif state == "fresh":
                ref.pop("state", None)
            else:
                ref.update(start=start, end=end, state=state)
                problems.append((ref["path"], state))
        if problems:
            reasons = sorted({f"{state}" for _, state in problems})
            block["stale"] = {
                "reason": f"Source this explanation cites has {' or '.join(reasons)} since it was written.",
                "files": sorted({path for path, _ in problems}),
                "since": revision,
            }
            stale_blocks[block["id"]] = block["stale"]
        elif block.get("stale"):
            del block["stale"]
            cleared.append(block["id"])

    for ref in iter_refs({"next_steps": atlas["next_steps"]}):
        state, start, end = ref_state(ref, index)
        ref.update(start=start, end=end)
        ref.pop("state", None)
        if state == "moved":
            moved += 1
        elif state != "fresh":
            ref["state"] = state

    affected: set[str] = set()
    needs_review: set[str] = set()
    for section in atlas["sections"]:
        for group in section["groups"]:
            if touched & set(group.get("files", [])):
                needs_review.add(group["id"])
            if any(b["id"] in stale_blocks for b in group["blocks"]):
                affected.add(group["id"])
    dependents = group_dependents(atlas, affected | needs_review)
    plan = {
        "dry_run": args.dry_run,
        "revision": revision_of(paths.repo),
        "changed_files": changed,
        "added_files": added,
        "deleted_files": deleted,
        "references_moved": moved,
        "stale_blocks": stale_blocks,
        "blocks_restored": cleared,
        "groups_with_stale_blocks": sorted(affected),
        "groups_with_changed_files": sorted(needs_review),
        "dependent_groups_to_review": sorted(dependents),
    }
    if not args.dry_run:
        store.log_activity(paths, f"Refresh checked {len(changed) + len(deleted)} changed file(s); "
                                  f"{len(stale_blocks)} explanation(s) need review")
        save_atlas(paths, atlas, current)
    print(json.dumps(plan, indent=1))
    return 0


def _find_block(atlas: dict, block_id: str) -> tuple[dict, int] | None:
    for section in atlas["sections"]:
        for group in section["groups"]:
            for i, block in enumerate(group["blocks"]):
                if block["id"] == block_id:
                    return group, i
    return None


def _find_group(atlas: dict, group_id: str) -> tuple[dict, int] | None:
    for section in atlas["sections"]:
        for i, group in enumerate(section["groups"]):
            if group["id"] == group_id:
                return section, i
    return None


def apply_op(atlas: dict, op: dict) -> None:
    kind = op.get("op")
    if kind in ("replace_block", "remove_block", "confirm_block"):
        found = _find_block(atlas, op.get("id", ""))
        if found is None:
            raise CliError(f"{kind}: no block {op.get('id')!r}")
        group, i = found
        if kind == "replace_block":
            if op["block"]["id"] != op["id"]:
                raise CliError("replace_block must keep the block's id so notes stay attached")
            group["blocks"][i] = op["block"]
        elif kind == "remove_block":
            del group["blocks"][i]
        else:
            block = group["blocks"][i]
            for ref in iter_refs(block):
                ref.pop("hash", None)
                ref.pop("state", None)
                # The anchor from analysis time may be gone; the current lines define the ref now.
                ref.pop("anchor", None)
            block.pop("stale", None)
    elif kind == "add_block":
        found = _find_group(atlas, op.get("group", ""))
        if found is None:
            raise CliError(f"add_block: no group {op.get('group')!r}")
        section, i = found
        blocks = section["groups"][i]["blocks"]
        at = len(blocks)
        if op.get("after"):
            at = next((n + 1 for n, b in enumerate(blocks) if b["id"] == op["after"]), at)
        blocks.insert(at, op["block"])
    elif kind in ("replace_group", "remove_group"):
        found = _find_group(atlas, op.get("id", ""))
        if found is None:
            raise CliError(f"{kind}: no group {op.get('id')!r}")
        section, i = found
        if kind == "replace_group":
            section["groups"][i] = op["group"]
        else:
            del section["groups"][i]
    elif kind == "add_group":
        section = next((s for s in atlas["sections"] if s["id"] == op.get("section")), None)
        if section is None:
            raise CliError(f"add_group: no section {op.get('section')!r}")
        section["groups"].append(op["group"])
    elif kind == "set_coverage":
        atlas["coverage"] = op["coverage"]
    elif kind == "set_next_steps":
        atlas["next_steps"] = op["next_steps"]
    else:
        raise CliError(f"unknown patch op {kind!r}")


def cmd_patch(paths: Paths, args) -> int:
    """All operations apply together, or the stored Atlas is left untouched."""
    atlas = read_json(paths.atlas)
    if atlas is None:
        raise CliError("no atlas.json yet")
    patch = _load_json(Path(args.file))
    working = copy.deepcopy(atlas)
    for op in patch.get("ops", []):
        apply_op(working, op)
    result = validate_patched(working, paths.repo)
    report_result(result, "patch")
    if not result.ok:
        print("Nothing was saved. Fix the errors and apply the patch again.", file=sys.stderr)
        return 1
    save_atlas(paths, working)
    return 0


# -- command line ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rabbit.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", help="target repository (default: the git root of the working directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, fn) -> argparse.ArgumentParser:
        sp = sub.add_parser(name)
        sp.set_defaults(fn=fn)
        return sp

    add("init", cmd_init)
    sp = add("scan", cmd_scan)
    sp.add_argument("--max-files", type=int, default=MAX_FILES)
    sp.add_argument("--max-file-bytes", type=int, default=MAX_FILE_BYTES)
    sp = add("check", cmd_check)
    sp.add_argument("draft")
    sp = add("ingest", cmd_ingest)
    sp.add_argument("draft")
    sp = add("render", cmd_render)
    sp.add_argument("--ide", choices=render_mod.IDES)
    sp = add("validate", cmd_validate)
    sp.add_argument("--sample", type=int, default=0, help="also print N random references to spot-check")
    sp.add_argument("--seed", type=int, default=None)
    sp = add("serve", cmd_serve)
    sp.add_argument("--port", type=int, default=DEFAULT_PORT)
    sp.add_argument("--no-open", action="store_true")
    sp.add_argument("--foreground", action="store_true")
    add("stop", cmd_stop)
    add("status", cmd_status)
    sp = add("questions", cmd_questions)
    sp.add_argument("--status", default="open", choices=["open", "investigating", "answered", "failed", "noted", "all"])
    sp = add("claim", cmd_claim)
    sp.add_argument("id")
    sp = add("progress", cmd_progress)
    sp.add_argument("--id")
    sp.add_argument("message")
    sp = add("answer", cmd_answer)
    sp.add_argument("id")
    sp.add_argument("file")
    sp = add("fail", cmd_fail)
    sp.add_argument("id")
    sp.add_argument("--reason", required=True)
    add("branches", cmd_branches)
    sp = add("tour-request", cmd_tour_request)
    sp.add_argument("--base")
    sp.add_argument("branches", nargs="+")
    sp = add("tours", cmd_tours)
    sp.add_argument("--status", default="open", choices=["open", "done", "failed", "all"])
    sp = add("tour-prepare", cmd_tour_prepare)
    sp.add_argument("id")
    sp = add("tour-ingest", cmd_tour_ingest)
    sp.add_argument("id")
    sp.add_argument("file")
    sp = add("tour-fail", cmd_tour_fail)
    sp.add_argument("id")
    sp.add_argument("--reason", required=True)
    sp = add("tour-remove", cmd_tour_remove)
    sp.add_argument("id")
    sp = add("refresh", cmd_refresh)
    sp.add_argument("--dry-run", action="store_true")
    sp = add("patch", cmd_patch)
    sp.add_argument("file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(Paths(find_repo(args.repo)), args)
    except (CliError, tours_mod.TourError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
