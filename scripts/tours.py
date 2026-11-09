"""Branch tours: pick branches, read them from git, and validate what the agent says about them.

A branch is never checked out, so the user's working tree is left alone. Each
toured branch is copied out of git into ``.repo-rabbit/tours/<id>/src``, and its
references and IDE links point at that private snapshot, which holds exactly the
code the tour describes. Branch names, commit messages and diffs are untrusted.
"""

from __future__ import annotations

import copy
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

import store
from store import Paths, new_id, now_iso, read_json, write_json
from validate import (
    INTERNAL_LINK,
    NO_REF_NEEDED,
    Result,
    SourceIndex,
    _claims,
    _ref_count,
    _tree_nodes,
    check_block,
    check_secrets,
    is_sensitive_path,
    iter_blocks,
    iter_refs,
    load_schema,
    redact,
    resolve_all_refs,
    schema_errors,
)

MAX_BRANCHES = 200
MAX_SET = 12
MAX_FILES = 20000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_FILE_DIFF_LINES = 800
MAX_DIFF_LINES = 8000
MAX_DRAFT_ID = 100
REQUIRED_TOPICS = ("changes", "impact", "walkthrough", "review")
SKIP_PARTS = {".git", ".repo-rabbit", "node_modules"}


class TourError(ValueError):
    pass


def _git(repo: Path, *args: str, data: bytes | None = None) -> bytes | None:
    command = ["git", "-C", str(repo), "-c", "core.fsmonitor=false", "-c", "core.quotePath=false", *args]
    try:
        done = subprocess.run(command, input=data, capture_output=True, timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def _text(repo: Path, *args: str) -> str | None:
    out = _git(repo, *args)
    return None if out is None else out.decode("utf-8", errors="replace")


# -- choosing branches ----------------------------------------------------------------


def tour_dir(paths: Paths, tour_id: str) -> Path:
    if not re.fullmatch(r"t[0-9a-f]{12}", tour_id):
        raise TourError(f"invalid tour id {tour_id!r}")
    return paths.root / "tours" / tour_id


def list_branches(repo: Path) -> dict:
    """Local and remote-tracking branches, newest first, with the likely base."""
    listed = _text(repo, "for-each-ref", "--sort=-committerdate",
                   "--format=%(refname)%00%(objectname)%00%(committerdate:iso-strict)%00%(subject)",
                   "refs/heads", "refs/remotes")
    if listed is None:
        raise TourError("this directory is not a git repository with branches")
    rows, total = [], 0
    for line in listed.splitlines():
        ref, sha, date, subject = (line.split("\0") + ["", "", ""])[:4]
        if ref.endswith("/HEAD"):
            continue
        total += 1
        if len(rows) < MAX_BRANCHES:
            remote = ref.startswith("refs/remotes/")
            name = ref.removeprefix("refs/remotes/" if remote else "refs/heads/")
            rows.append({"name": name, "ref": ref, "sha": sha, "date": date, "subject": subject, "remote": remote})
    names = {r["name"] for r in rows}
    default = None
    origin = _text(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    for candidate in ((origin or "").strip().removeprefix("refs/remotes/origin/"), "main", "master"):
        if candidate in names:
            default = candidate
            break
    if default is None and origin and origin.strip().removeprefix("refs/remotes/") in names:
        default = origin.strip().removeprefix("refs/remotes/")
    if default is None and rows:
        default = rows[0]["name"]
    current = (_text(repo, "branch", "--show-current") or "").strip() or None
    by_name = {r["name"]: r for r in rows}
    for row in rows:
        row["ahead"] = row["behind"] = None
        if default and row["name"] != default:
            counts = _text(repo, "rev-list", "--left-right", "--count", f"{by_name[default]['sha']}...{row['sha']}")
            if counts and len(counts.split()) == 2:
                row["behind"], row["ahead"] = (int(n) for n in counts.split())
    return {"default": default, "current": current, "branches": rows, "total": total,
            "truncated": total > MAX_BRANCHES}


def request_tours(paths: Paths, base: Any, branches: Any) -> list[dict]:
    """Queue one tour per selected branch. Every name must be a branch that really exists."""
    known = {r["name"]: r for r in list_branches(paths.repo)["branches"]}
    if not isinstance(base, str) or base not in known:
        raise TourError(f"unknown base branch: {str(base)[:120]!r}")
    if not isinstance(branches, list) or not branches or not all(isinstance(b, str) for b in branches):
        raise TourError("choose at least one branch to tour")
    chosen = list(dict.fromkeys(branches))
    if len(chosen) > MAX_SET:
        raise TourError(f"choose at most {MAX_SET} branches at a time")
    for name in chosen:
        if name not in known:
            raise TourError(f"unknown branch: {name[:120]!r}")
        if name == base:
            raise TourError(f"{name} is the base; choose a different branch to tour")
    set_id = new_id("set")
    created: list[dict] = []
    with store.locked(paths):
        existing = store.read_jsonl(paths.tours)[0]
        pending = {(t["branch"], t["base"]) for t in existing if t.get("status") in ("open", "touring")}
        for name in chosen:
            if (name, base) in pending:
                continue
            record = {"id": new_id("t").replace("_", ""), "set_id": set_id, "branch": name, "base": base, "status": "open",
                      "requested_at": now_iso()}
            store._append_jsonl(paths.tours, record)
            created.append(record)
    return created


def _lookup(paths: Paths, tour_id: str) -> dict:
    record = next((t for t in store.load_tours(paths) if t.get("id") == tour_id), None)
    if record is None:
        raise TourError(f"no tour {tour_id}")
    return record


def fail_tour(paths: Paths, tour_id: str, reason: str) -> None:
    _lookup(paths, tour_id)
    store.update_tours(paths, {tour_id: {"status": "failed", "error": reason[:500]}})
    store.log_activity(paths, f"Tour failed: {reason}"[:300], tour_id)


def remove_tour(paths: Paths, tour_id: str) -> None:
    _lookup(paths, tour_id)
    shutil.rmtree(tour_dir(paths, tour_id), ignore_errors=True)
    store.delete_tours(paths, {tour_id})


# -- reading a branch from git --------------------------------------------------------


def write_snapshot(repo: Path, sha: str, dest: Path) -> dict:
    """Copy a commit's text files into ``dest``, without touching the working tree."""
    listing = _git(repo, "ls-tree", "-r", "-l", "-z", sha)
    if listing is None:
        raise TourError(f"could not read commit {sha[:10]}")
    wanted: list[tuple[str, str]] = []
    skipped = sensitive = 0
    total = 0
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        header, _, raw_path = entry.partition(b"\t")
        mode, kind, blob, size = header.split()
        try:
            rel = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            skipped += 1
            continue
        parts = PurePosixPath(rel).parts
        if is_sensitive_path(rel):
            sensitive += 1
            continue
        # Symlinks and submodules (other modes) could point outside the snapshot.
        if kind != b"blob" or mode not in (b"100644", b"100755") or ".." in parts or rel.startswith("/") \
                or SKIP_PARTS & set(parts) or int(size) > MAX_FILE_BYTES \
                or total + int(size) > MAX_TOTAL_BYTES or len(wanted) >= MAX_FILES:
            skipped += 1
            continue
        total += int(size)
        wanted.append((rel, blob.decode()))
    stream = _git(repo, "cat-file", "--batch", data="".join(f"{b}\n" for _, b in wanted).encode())
    if stream is None:
        raise TourError(f"could not read the files of commit {sha[:10]}")
    position = written = 0
    for rel, _blob in wanted:
        end_of_header = stream.index(b"\n", position)
        size = int(stream[position:end_of_header].split()[2])
        content = stream[end_of_header + 1:end_of_header + 1 + size]
        position = end_of_header + 1 + size + 1
        if b"\0" in content[:8192]:
            skipped += 1
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        written += 1
    return {"files": written, "skipped": skipped, "withheld_as_sensitive": sensitive}


def parse_diff(text: str) -> tuple[list[dict], bool]:
    """Unified diff into per-file records, with honest flags where it was cut."""
    files: list[dict] = []
    current: dict | None = None
    old_line = new_line = kept = 0
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            current = {"path": "", "old_path": None, "status": "modified", "additions": 0, "deletions": 0,
                       "hunks": [], "binary": False, "withheld": False, "truncated": False}
            match = re.match(r'diff --git "?a/(.*?)"? "?b/(.*?)"?$', line)
            if match:
                current["path"] = match.group(2)
            files.append(current)
        elif current is None:
            continue
        elif line.startswith("new file mode"):
            current["status"] = "added"
        elif line.startswith("deleted file mode"):
            current["status"] = "deleted"
        elif line.startswith("rename from "):
            current["status"], current["old_path"] = "renamed", line[len("rename from "):]
        elif line.startswith("rename to "):
            current["path"] = line[len("rename to "):]
        elif line.startswith("Binary files "):
            current["binary"] = True
        elif line.startswith("--- ") and not current["hunks"]:
            if line[4:].rstrip("\t") != "/dev/null" and current["status"] == "deleted":
                current["path"] = line[4:].rstrip("\t").removeprefix("a/").strip('"')
        elif line.startswith("+++ ") and not current["hunks"]:
            if line[4:].rstrip("\t") != "/dev/null":
                current["path"] = line[4:].rstrip("\t").removeprefix("b/").strip('"')
        elif line.startswith("@@ "):
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if match:
                old_line, new_line = int(match.group(1)), int(match.group(2))
                current["hunks"].append({"header": line, "lines": []})
        elif current["hunks"] and line[:1] in ("+", "-", " "):
            kind, body = line[0], line[1:]
            current["additions"] += kind == "+"
            current["deletions"] += kind == "-"
            hunk_size = sum(len(h["lines"]) for h in current["hunks"])
            if hunk_size >= MAX_FILE_DIFF_LINES or kept >= MAX_DIFF_LINES:
                current["truncated"] = True
            else:
                current["hunks"][-1]["lines"].append(
                    [kind, None if kind == "+" else old_line, None if kind == "-" else new_line, body])
                kept += 1
            old_line += kind != "+"
            new_line += kind != "-"
    for record in files:
        if is_sensitive_path(record["path"]):
            record.update(withheld=True, hunks=[])
        for hunk in record["hunks"]:
            for row in hunk["lines"]:
                row[3] = redact(row[3])
    return files, any(f["truncated"] for f in files)


def affected_groups(atlas: dict | None, changed: set[str]) -> list[dict]:
    """Atlas modules and workflows that cite a changed file. Computed, not inferred."""
    found = []
    for section in (atlas or {}).get("sections", []):
        if section["id"] not in ("modules", "workflows"):
            continue
        for group in section["groups"]:
            paths = set(group.get("files", [])) | {r.get("path") for b in group["blocks"] for r in iter_refs(b)}
            hits = sorted(paths & changed)
            if hits:
                found.append({"id": group["id"], "title": group["title"], "section": section["id"], "files": hits})
    return found


def prepare_tour(paths: Paths, tour_id: str) -> dict:
    """Snapshot the branch and compute its diff. Reports what the agent should read."""
    record = _lookup(paths, tour_id)
    refs = {r["name"]: r for r in list_branches(paths.repo)["branches"]}
    for role in ("branch", "base"):
        if record[role] not in refs:
            raise TourError(f"{record[role]} no longer exists")
    sha, base_sha = refs[record["branch"]]["sha"], refs[record["base"]]["sha"]
    merge_base = (_text(paths.repo, "merge-base", base_sha, sha) or "").strip()
    if not merge_base:
        raise TourError(f"{record['branch']} shares no history with {record['base']}")
    if merge_base == sha:
        raise TourError(f"{record['branch']} has no commits that aren't already on {record['base']}")
    diff = _text(paths.repo, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--find-renames", "-U3",
                 merge_base, sha)
    log = _text(paths.repo, "log", "--format=%h%x00%s", "--no-merges", "--max-count=60", f"{merge_base}..{sha}")
    if diff is None or log is None:
        raise TourError("git could not produce the diff")
    files, truncated = parse_diff(diff)

    folder = tour_dir(paths, tour_id)
    shutil.rmtree(folder, ignore_errors=True)
    (folder / "src").mkdir(parents=True)
    snapshot = write_snapshot(paths.repo, sha, folder / "src")

    changed_lines = {f["path"]: [row[2] for h in f["hunks"] for row in h["lines"] if row[0] == "+"] for f in files}
    commits = [{"sha": a, "subject": b} for a, b in (ln.split("\0", 1) for ln in log.splitlines() if "\0" in ln)]
    atlas = read_json(paths.atlas)
    changed = {f["path"] for f in files}
    meta = {
        "sha": sha, "base_sha": base_sha, "merge_base": merge_base, "commits": commits,
        "additions": sum(f["additions"] for f in files), "deletions": sum(f["deletions"] for f in files),
        "files_changed": len(files), "diff_truncated": truncated, "snapshot": snapshot,
        "affected": affected_groups(atlas, changed), "changed_lines": changed_lines,
    }
    write_json(folder / "changes.json", files)
    write_json(folder / "meta.json", meta)
    store.update_tours(paths, {tour_id: {"status": "touring", "started_at": now_iso(), "sha": sha}})
    store.log_activity(paths, f"Prepared {record['branch']}: {len(files)} file(s) changed", tour_id)
    return {
        "id": tour_id, "branch": record["branch"], "base": record["base"], "merge_base": merge_base,
        "snapshot_dir": str(folder / "src"), "diff_file": str(folder / "changes.json"),
        "commits": commits,
        "files": [{k: f[k] for k in ("path", "status", "additions", "deletions", "binary", "withheld", "truncated")}
                  for f in files],
        "diff_truncated": truncated, "snapshot": snapshot, "affected_atlas_groups": meta["affected"],
    }


# -- validating and storing the agent's tour ------------------------------------------


def _local_ids(tour: dict) -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    steps: set[str] = set()
    for group in tour["groups"]:
        ids.add(group["id"])
        for block in group["blocks"]:
            ids.add(block["id"])
            if block["type"] == "step":
                steps.add(block["id"])
    return ids, steps


def atlas_ids(atlas: dict | None) -> set[str]:
    ids: set[str] = set()
    for _, group, block in iter_blocks(atlas or {}):
        ids |= {group["id"], block["id"]}
    return ids


def check_tour(tour: dict, snapshot: Path, atlas: dict | None) -> Result:
    """Reject a tour whose claims can't be traced to the branch's real code."""
    result = Result()
    errors = result.errors
    schema = load_schema()
    errors += schema_errors(tour, schema["$defs"]["tour"], schema)
    if errors:
        return result
    errors += resolve_all_refs(tour, SourceIndex(snapshot))
    every: list[str] = [i for g in tour["groups"] for i in (g["id"], *(b["id"] for b in g["blocks"]))]
    local, steps = _local_ids(tour)
    if len(every) != len(local):
        errors.append("ids must be unique within a tour")
    errors += [f"id {i!r} is longer than {MAX_DRAFT_ID} characters" for i in local if len(i) > MAX_DRAFT_ID]
    known = local | atlas_ids(atlas)
    topics = {g.get("topic") for g in tour["groups"]}
    errors += [f"the tour has no group with topic {t!r}" for t in REQUIRED_TOPICS if t not in topics]
    for group in tour["groups"]:
        errors += [f"{group['id']}: related group {o!r} does not exist" for o in group.get("related", []) if o not in known]
        for block in group["blocks"]:
            for where, claim in _claims(block):
                certainty = claim.get("certainty", "verified")
                if certainty not in NO_REF_NEEDED and not _ref_count(claim):
                    errors.append(f"{where}: a {certainty} claim needs at least one source reference")
            check_block(block, known, steps, errors, result.warnings)
    numbers = [b["number"] for g in tour["groups"] for b in g["blocks"] if b["type"] == "step"]
    if numbers != list(range(1, len(numbers) + 1)):
        errors.append("walkthrough steps must be numbered 1..N in order")
    walkthrough = [b for g in tour["groups"] if g.get("topic") == "walkthrough" for b in g["blocks"]]
    if walkthrough and not any(b["type"] == "step" or b.get("certainty") == "absence" for b in walkthrough):
        errors.append("the walkthrough needs a step, or a prose block with certainty 'absence' saying why not")

    def strings(node: Any) -> Any:
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for key, value in node.items():
                if key not in ("refs", "ref", "code"):
                    yield from strings(value)
        elif isinstance(node, list):
            for item in node:
                yield from strings(item)

    for text in strings(tour):
        errors += [f"internal link to unknown id {m.group(1)!r}" for m in INTERNAL_LINK.finditer(text) if m.group(1) not in known]
    errors += check_secrets(tour)
    return result


_LINK = re.compile(r"\[\[([a-z0-9][a-z0-9._-]*)((?:\|[^\]]*)?)\]\]")


def namespaced(tour: dict, tour_id: str) -> dict:
    """Prefix local ids with the tour id, so tours can't collide with the Atlas or each other."""
    out = copy.deepcopy(tour)
    local, _ = _local_ids(tour)

    def fix(value: str) -> str:
        return f"{tour_id}.{value}" if value in local else value

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            return _LINK.sub(lambda m: f"[[{fix(m.group(1))}{m.group(2)}]]", node)
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {k: (v if k in ("refs", "ref", "code") else walk(v)) for k, v in node.items()}
        return node

    out = walk(out)
    for group in out["groups"]:
        group["id"] = fix(group["id"])
        if "related" in group:
            group["related"] = [fix(r) for r in group["related"]]
        for block in group["blocks"]:
            block["id"] = fix(block["id"])
            if block["type"] == "component_diagram":
                for node in block["nodes"]:
                    if "target" in node:
                        node["target"] = fix(node["target"])
            elif block["type"] == "execution_tree":
                for node in _tree_nodes(block["root"]):
                    if "target" in node:
                        node["target"] = fix(node["target"])
    return out


def save_tour(paths: Paths, tour_id: str, tour: dict) -> Result:
    """Validate a drafted tour. Saved and marked done only if every check passes."""
    record = _lookup(paths, tour_id)
    if record.get("status") != "touring":
        raise TourError(f"run tour-prepare {tour_id} first")
    folder = tour_dir(paths, tour_id)
    result = check_tour(tour, folder / "src", read_json(paths.atlas))
    if result.ok:
        write_json(folder / "tour.json", namespaced(tour, tour_id))
        store.update_tours(paths, {tour_id: {"status": "done", "finished_at": now_iso()}})
        store.log_activity(paths, f"Tour of {record['branch']} is ready", tour_id)
    return result


def load_ready(paths: Paths) -> list[dict]:
    """Finished tours with their body and metadata; the larger diff is loaded on demand."""
    ready = []
    for record in store.load_tours(paths):
        if record.get("status") != "done":
            continue
        folder = tour_dir(paths, record["id"])
        body, meta = read_json(folder / "tour.json"), read_json(folder / "meta.json")
        if body and meta:
            ready.append({**record, "body": body, "meta": meta, "src": folder / "src"})
    return ready


def load_changes(paths: Paths, tour_id: str) -> list[dict]:
    return read_json(tour_dir(paths, tour_id) / "changes.json", []) or []


def overlaps(ready: list[dict], changes: dict[str, list[dict]]) -> list[dict]:
    """Within each selected set, the files that more than one branch changed."""
    sets: dict[str, list[dict]] = {}
    for tour in ready:
        sets.setdefault(tour["set_id"], []).append(tour)
    rows = []
    for members in sets.values():
        if len(members) < 2:
            continue
        touched: dict[str, list[str]] = {}
        for tour in members:
            for record in changes.get(tour["id"], []):
                touched.setdefault(record["path"], []).append(tour["branch"])
        for path, branches in sorted(touched.items()):
            if len(branches) > 1:
                rows.append({"path": path, "branches": branches})
    return rows
