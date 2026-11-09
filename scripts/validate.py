"""Deterministic checks: schema, source references, required coverage, secrets.

The agent reasons; this module refuses to let unverifiable output through. A
reference is resolved against the real file, and an Atlas with a reference that
does not resolve is rejected rather than rendered.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "atlas.schema.json"

SECTION_ORDER = ("overview", "architecture", "modules", "stack", "workflows", "walkthrough")

# A group's ``topic`` says which required area it covers.
REQUIRED_TOPICS = {
    "overview": ("purpose", "users", "capabilities", "commands", "entry-points", "components",
                 "start-here"),
    "architecture": ("component-map", "responsibilities", "static-dependencies",
                     "runtime-interactions", "interfaces", "external-services", "state",
                     "configuration", "error-boundaries", "patterns"),
    "stack": ("languages", "dependencies", "build-test", "ci-deploy", "infrastructure",
              "data-stores", "external-apis", "observability", "configuration"),
}
MODULE_KEYS = ("responsibility", "key_files", "key_symbols", "interfaces", "io", "callers",
               "dependencies", "external", "errors", "tests", "debug")
WORKFLOW_KEYS = ("trigger", "functions", "data", "branches", "external_async", "errors", "result",
                 "evidence")

NO_REF_NEEDED = {"context", "absence"}

SENSITIVE_NAMES = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", ".npmrc", ".pypirc", "credentials",
    "credentials.json", "secrets.*", "*.secret", "*.secrets",
)
SENSITIVE_EXCEPTIONS = ("*.example", "*.sample", "*.template", "*.dist")

SECRET_PATTERNS = [
    re.compile(p) for p in (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bgh[pousr]_[A-Za-z0-9]{36,}\b",
        r"\bgithub_pat_[A-Za-z0-9_]{40,}\b",
        r"\bsk-[A-Za-z0-9_-]{32,}\b",
        r"\bxox[abprs]-[A-Za-z0-9-]{20,}\b",
        r"\bAIza[0-9A-Za-z_-]{35}\b",
        r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b",
    )
]
SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (\b[\w.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|private[_-]?key|access[_-]?key)[\w.-]*
     ["']?\s*[:=]\s*["']?)
    ([A-Za-z0-9+/_.~-]{16,})
    """
)


def is_sensitive_path(rel: str) -> bool:
    name = PurePosixPath(rel).name
    if any(fnmatch.fnmatch(name, p) for p in SENSITIVE_EXCEPTIONS):
        return False
    return any(fnmatch.fnmatch(name.lower(), p) for p in SENSITIVE_NAMES)


def find_secret(text: str) -> str | None:
    """A short description of the first credential-looking string, if any."""
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return pattern.pattern
    match = SECRET_ASSIGNMENT.search(text)
    if match and _looks_random(match.group(2)):
        return f"credential assignment to {match.group(1).strip()[:40]}"
    return None


def _looks_random(value: str) -> bool:
    return bool(re.search(r"\d", value)) and bool(re.search(r"[A-Za-z]", value))


def redact(text: str) -> str:
    """Replace credential-looking values, keeping the shape of the code readable."""
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)

    def fix(match: re.Match) -> str:
        return match.group(1) + "[REDACTED]" if _looks_random(match.group(2)) else match.group(0)

    return SECRET_ASSIGNMENT.sub(fix, text)


# -- a small JSON Schema validator, for the keywords atlas.schema.json uses ------------


def _type_ok(value: Any, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    return isinstance(value, {"string": str, "array": list, "object": dict}[name])


def schema_errors(value: Any, schema: dict, root: dict, where: str = "$") -> list[str]:
    if "$ref" in schema:
        target: Any = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        return schema_errors(value, target, root, where)
    errors: list[str] = []
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(value, n) for n in names):
            return [f"{where}: expected {'/'.join(names)}, got {type(value).__name__}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{where}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{where}: too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{where}: longer than {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{where}: {value!r} does not match {schema['pattern']}")
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where}: below {schema['minimum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{where}: needs at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{where}: allows at most {schema['maxItems']} item(s)")
        if "items" in schema:
            for i, item in enumerate(value):
                errors += schema_errors(item, schema["items"], root, f"{where}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{where}: missing required field {name!r}")
        for name, item in value.items():
            if name in props:
                errors += schema_errors(item, props[name], root, f"{where}.{name}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{where}: unknown field {name!r}")
    if "oneOf" in schema:
        # Blocks are discriminated by a const ``type``, so report the branch that matched it.
        results = [schema_errors(value, s, root, where) for s in schema["oneOf"]]
        passing = sum(not r for r in results)
        if passing != 1:
            typed = [r for r in results if not any(".type:" in e for e in r)]
            errors += typed[0] if typed else [f"{where}: matches {passing} block shapes"]
    return errors


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


# -- source references ----------------------------------------------------------------


class RefError(ValueError):
    pass


def span_hash(lines: list[str]) -> str:
    joined = "\n".join(line.rstrip() for line in lines)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


class SourceIndex:
    """Read-only, cached access to files inside one repository."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo.resolve()
        self._lines: dict[str, list[str] | None] = {}

    def abs_path(self, rel: str) -> Path:
        if not rel or "\x00" in rel or "\\" in rel:
            raise RefError(f"{rel!r}: not a repository-relative POSIX path")
        pure = PurePosixPath(rel)
        if pure.is_absolute() or ".." in pure.parts or rel != str(pure):
            raise RefError(f"{rel!r}: must be a normalized path relative to the repository")
        if is_sensitive_path(rel):
            raise RefError(f"{rel!r}: looks like a credentials file; RepoRabbit will not cite it")
        candidate = (self.repo / pure).resolve()
        try:
            candidate.relative_to(self.repo)
        except ValueError:
            raise RefError(f"{rel!r}: resolves outside the repository") from None
        return candidate

    def lines(self, rel: str) -> list[str] | None:
        """The file's lines, or None if it is missing, binary or not allowed."""
        if rel not in self._lines:
            self._lines[rel] = self._read(rel)
        return self._lines[rel]

    def _read(self, rel: str) -> list[str] | None:
        try:
            path = self.abs_path(rel)
            data = path.read_bytes()
        except (RefError, OSError):
            return None
        if b"\x00" in data[:8192]:
            return None
        return data.decode("utf-8", errors="replace").splitlines()


def _match_anchor(line: str, anchor: str) -> bool:
    if anchor.startswith("^"):
        return line.strip() == anchor[1:].strip()
    return anchor in line


def _locate_unique(lines: list[str], anchor: str, path: str) -> int:
    hits = [i + 1 for i, line in enumerate(lines) if _match_anchor(line, anchor)]
    if not hits:
        raise RefError(f"{path}: anchor {anchor!r} is not on any line")
    if len(hits) > 1:
        shown = ", ".join(map(str, hits[:8]))
        raise RefError(f"{path}: anchor {anchor!r} matches {len(hits)} lines ({shown}); make it "
                       "longer, or give start/end and keep the anchor as a check")
    return hits[0]


def resolve_ref(ref: dict, index: SourceIndex) -> dict:
    """Turn a draft reference into a verified one, or raise :class:`RefError`."""
    path = ref.get("path", "")
    index.abs_path(path)
    lines = index.lines(path)
    if lines is None:
        raise RefError(f"{path}: file does not exist or is not text")
    anchor = ref.get("anchor")
    start = ref.get("start")
    if start is None:
        if not anchor:
            raise RefError(f"{path}: a reference needs start or anchor")
        start = _locate_unique(lines, anchor, path)
    end = ref.get("end")
    anchor_end = ref.get("anchor_end")
    if anchor_end:
        found = next((i + 1 for i in range(start - 1, len(lines))
                      if _match_anchor(lines[i], anchor_end)), None)
        if found is None:
            raise RefError(f"{path}: anchor_end {anchor_end!r} is not at or after line {start}")
        if end is not None and end != found:
            raise RefError(f"{path}: end {end} disagrees with anchor_end at line {found}")
        end = found
    end = start if end is None else end
    if start > end:
        raise RefError(f"{path}:{start}-{end}: start is after end")
    if start < 1 or end > len(lines):
        raise RefError(f"{path}:{start}-{end}: outside the file ({len(lines)} lines)")
    span = lines[start - 1:end]
    if anchor and not any(_match_anchor(line, anchor) for line in span):
        raise RefError(f"{path}:{start}-{end}: anchor {anchor!r} is not in that range")
    first = next((line.strip() for line in span if line.strip()), "")
    out = {"path": path, "start": start, "end": end, "hash": span_hash(span)}
    chosen = (anchor or first)[:300]
    if chosen and not find_secret(chosen):  # The anchor is stored; a credential must not be.
        out["anchor"] = chosen
    return out


def ref_state(ref: dict, index: SourceIndex) -> tuple[str, int, int]:
    """Where a stored reference points now: ``(state, start, end)``.

    * ``fresh``: the same lines are still at the same place.
    * ``moved``: the same lines exist elsewhere in the file; only the numbers change.
    * ``changed``: the lines differ, but the anchor still finds the place.
    * ``lost``: the file exists, but neither the lines nor the anchor do.
    * ``missing``: the file is gone.
    """
    start, end = ref["start"], ref["end"]
    lines = index.lines(ref["path"])
    if lines is None:
        return "missing", start, end
    length = end - start + 1
    expected = ref.get("hash")
    if expected is None:
        return ("fresh" if end <= len(lines) else "lost"), start, min(end, max(len(lines), 1))
    if end <= len(lines) and span_hash(lines[start - 1:end]) == expected:
        return "fresh", start, end
    candidates = [i for i in range(len(lines) - length + 1)
                  if span_hash(lines[i:i + length]) == expected]
    if candidates:
        best = min(candidates, key=lambda i: abs(i + 1 - start))
        return "moved", best + 1, best + length
    anchor = ref.get("anchor") or ""
    hits = [i + 1 for i, line in enumerate(lines) if anchor and _match_anchor(line, anchor)]
    if hits:
        best = min(hits, key=lambda n: abs(n - start))
        return "changed", best, min(best + length - 1, len(lines))
    return "lost", min(start, max(len(lines), 1)), min(end, max(len(lines), 1))


def iter_blocks(atlas: dict) -> Iterator[tuple[dict, dict, dict]]:
    for section in atlas.get("sections", []):
        for group in section.get("groups", []):
            for block in group.get("blocks", []):
                yield section, group, block


def iter_refs(node: Any) -> Iterator[dict]:
    """Every reference object anywhere inside ``node``."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "refs" and isinstance(value, list):
                yield from (r for r in value if isinstance(r, dict))
            elif key in ("ref", "code") and isinstance(value, dict):
                yield value
            else:
                yield from iter_refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_refs(item)


def iter_strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from iter_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_strings(item)


def resolve_all_refs(node: Any, index: SourceIndex, only_new: bool = False) -> list[str]:
    """Resolve references in place; return the errors.

    With ``only_new``, references that already carry a hash are left alone: they
    were verified earlier, and judging them afresh is the job of ``refresh``.
    """
    errors: list[str] = []
    for ref in iter_refs(node):
        if only_new and "hash" in ref:
            continue
        try:
            resolved = resolve_ref(ref, index)
        except RefError as exc:
            errors.append(str(exc))
            continue
        ref.clear()
        ref.update(resolved)
    return errors


# -- whole-Atlas checks ---------------------------------------------------------------


@dataclass
class Result:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def _claims(block: dict) -> Iterator[tuple[str, dict]]:
    """Every claim in a block that must either carry a citation or say why not."""
    kind = block["type"]
    if kind in ("prose", "decision", "step", "component_diagram", "sequence_diagram"):
        yield block["id"], block
    elif kind == "list":
        for i, item in enumerate(block["items"]):
            yield f"{block['id']}.items[{i}]", item
    elif kind == "facts":
        for i, item in enumerate(block["items"]):
            yield f"{block['id']}.{item.get('key', i)}", item
    elif kind == "table":
        for i, row in enumerate(block["rows"]):
            yield f"{block['id']}.rows[{i}]", row


def _tree_nodes(node: dict) -> Iterator[dict]:
    yield node
    for child in node.get("children", []):
        yield from _tree_nodes(child)


def _ref_count(claim: dict) -> int:
    return sum(1 for _ in iter_refs(claim))


INTERNAL_LINK = re.compile(r"\[\[([a-z0-9][a-z0-9._-]*)(?:\|[^\]]*)?\]\]")


def check_structure(atlas: dict) -> Result:
    """Required sections and topics, citations on claims, and internal links."""
    result = Result()
    errors, warnings = result.errors, result.warnings
    sections = {s["id"]: s for s in atlas["sections"]}
    if tuple(s["id"] for s in atlas["sections"]) != SECTION_ORDER:
        errors.append(f"sections must be exactly {SECTION_ORDER}, in that order")

    seen: set[str] = set()
    group_ids: set[str] = set()
    block_ids: set[str] = set()
    step_ids: set[str] = set()
    for section in atlas["sections"]:
        for group in section["groups"]:
            if group["id"] in seen:
                errors.append(f"duplicate id {group['id']!r}")
            seen.add(group["id"])
            group_ids.add(group["id"])
            for block in group["blocks"]:
                if block["id"] in seen:
                    errors.append(f"duplicate id {block['id']!r}")
                seen.add(block["id"])
                block_ids.add(block["id"])
                if block["type"] == "step":
                    step_ids.add(block["id"])

    for section_id, topics in REQUIRED_TOPICS.items():
        have = {g.get("topic") for g in sections.get(section_id, {}).get("groups", [])}
        for topic in topics:
            if topic not in have:
                errors.append(f"section {section_id!r} has no group with topic {topic!r}")

    claims = uncited = 0
    by_certainty: dict[str, int] = {}
    for _, group, block in iter_blocks(atlas):
        for where, claim in _claims(block):
            claims += 1
            certainty = claim.get("certainty", "verified")
            by_certainty[certainty] = by_certainty.get(certainty, 0) + 1
            if certainty not in NO_REF_NEEDED and not _ref_count(claim):
                uncited += 1
                errors.append(f"{where}: a {certainty} claim needs at least one source reference "
                              "(or certainty 'context'/'absence' if it makes no code claim)")
        if block["type"] == "component_diagram":
            ids = [n["id"] for n in block["nodes"]]
            if len(set(ids)) != len(ids):
                errors.append(f"{block['id']}: duplicate diagram node id")
            for edge in block["edges"]:
                for end in (edge["from"], edge["to"]):
                    if end not in ids:
                        errors.append(f"{block['id']}: edge references unknown node {end!r}")
            for node in block["nodes"]:
                if "target" in node and node["target"] not in seen:
                    errors.append(f"{block['id']}: node target {node['target']!r} does not exist")
        elif block["type"] == "sequence_diagram":
            ids = [p["id"] for p in block["participants"]]
            for message in block["messages"]:
                for end in (message["from"], message["to"]):
                    if end not in ids:
                        errors.append(f"{block['id']}: message references unknown {end!r}")
        elif block["type"] == "execution_tree":
            for node in _tree_nodes(block["root"]):
                if "target" in node and node["target"] not in step_ids:
                    errors.append(f"{block['id']}: tree target {node['target']!r} is not a step")
                if "ref" not in node and "target" not in node:
                    errors.append(f"{block['id']}: tree node {node['label']!r} needs a ref or a step target")
        elif block["type"] == "step":
            count = block["code"].get("end", 0) - block["code"].get("start", 0) + 1
            if count > 40:
                warnings.append(f"{block['id']}: snippet is {count} lines; aim for 5-20")
    for section in atlas["sections"]:
        for group in section["groups"]:
            for other in group.get("related", []):
                if other not in group_ids:
                    errors.append(f"{group['id']}: related group {other!r} does not exist")
    for text in iter_strings(atlas):
        for match in INTERNAL_LINK.finditer(text):
            if match.group(1) not in seen:
                errors.append(f"internal link to unknown id {match.group(1)!r}")

    _check_start_here(sections.get("overview"), errors)
    _check_modules(sections.get("modules"), errors)
    _check_workflows(sections.get("workflows"), errors, warnings)
    _check_walkthrough(sections.get("walkthrough"), errors)
    result.stats.update(claims=claims, uncited=uncited, by_certainty=by_certainty,
                        groups=len(group_ids), blocks=len(block_ids), steps=len(step_ids))
    return result


def _fact_keys(group: dict) -> set[str]:
    return {i.get("key") for b in group["blocks"] if b["type"] == "facts" for i in b["items"]}


def _check_start_here(section: dict | None, errors: list[str]) -> None:
    for group in (section or {}).get("groups", []):
        if group.get("topic") == "start-here":
            lists = [b for b in group["blocks"] if b["type"] == "list"]
            if not lists or not 3 <= len(lists[0]["items"]) <= 5:
                errors.append("start-here must be a list of 3-5 files")


def _check_modules(section: dict | None, errors: list[str]) -> None:
    groups = [g for g in (section or {}).get("groups", []) if g.get("topic") == "module"]
    if not groups:
        errors.append("section 'modules' has no group with topic 'module'")
    for group in groups:
        for key in MODULE_KEYS:
            if key not in _fact_keys(group):
                errors.append(f"module {group['id']!r} is missing a fact with key {key!r}")
        if not group.get("files"):
            errors.append(f"module {group['id']!r} needs a files list (refresh depends on it)")


def _check_workflows(section: dict | None, errors: list[str], warnings: list[str]) -> None:
    groups = [g for g in (section or {}).get("groups", []) if g.get("topic") == "workflow"]
    if not groups:
        errors.append("section 'workflows' has no group with topic 'workflow'")
    elif len(groups) < 3:
        warnings.append(f"only {len(groups)} workflow(s); 3-5 are expected where the repo has them")
    elif len(groups) > 5:
        warnings.append(f"{len(groups)} workflows; keep the 3-5 most instructive")
    for group in groups:
        for key in WORKFLOW_KEYS:
            if key not in _fact_keys(group):
                errors.append(f"workflow {group['id']!r} is missing a fact with key {key!r}")
        types = [b["type"] for b in group["blocks"]]
        if "sequence_diagram" not in types:
            errors.append(f"workflow {group['id']!r} needs a sequence_diagram")
        if not any(b["type"] == "list" and b.get("ordered") for b in group["blocks"]):
            errors.append(f"workflow {group['id']!r} needs an ordered list of execution steps")


def _check_walkthrough(section: dict | None, errors: list[str]) -> None:
    blocks = [b for g in (section or {}).get("groups", []) for b in g["blocks"]]
    steps = [b for b in blocks if b["type"] == "step"]
    if not steps:
        errors.append("the walkthrough needs at least one step")
    if [s["number"] for s in steps] != list(range(1, len(steps) + 1)):
        errors.append("walkthrough steps must be numbered 1..N in order")
    if not any(b["type"] == "execution_tree" for b in blocks):
        errors.append("the walkthrough needs an execution_tree")
    topics = {g.get("topic"): g for g in (section or {}).get("groups", [])}
    if "follow-ons" not in topics:
        errors.append("the walkthrough needs a group with topic 'follow-ons'")
    else:
        lists = [b for b in topics["follow-ons"]["blocks"] if b["type"] == "list"]
        if not lists or not 2 <= len(lists[0]["items"]) <= 3:
            errors.append("follow-ons must be a list of 2-3 use cases")
    if "data-trace" not in topics:
        errors.append("the walkthrough needs a group with topic 'data-trace'")


def check_secrets(atlas: dict) -> list[str]:
    errors = []
    for text in iter_strings(atlas):
        found = find_secret(text)
        if found:
            errors.append(f"credential-looking text in the Atlas ({found}): {text[:40]!r}...")
    return errors


def validate_draft(atlas: dict, repo: Path) -> Result:
    """Schema, then references (resolved in place), then structure and secrets."""
    result = Result()
    schema = load_schema()
    result.errors += schema_errors(atlas, schema, schema)
    if result.errors:
        return result
    index = SourceIndex(repo)
    result.errors += resolve_all_refs(atlas, index)
    structure = check_structure(atlas)
    result.errors += structure.errors
    result.warnings += structure.warnings
    result.errors += check_secrets(atlas)
    result.stats.update(structure.stats)
    result.stats["references"] = sum(1 for _ in iter_refs(atlas))
    for section in atlas["sections"]:
        for group in section["groups"]:
            for rel in group.get("files", []):
                lines = index.lines(rel)
                if lines is None:
                    result.errors.append(f"{group['id']}: listed file {rel!r} does not exist")
    return result


def validate_patched(atlas: dict, repo: Path) -> Result:
    """Validate an edited Atlas: new references are resolved, earlier ones kept as they are."""
    result = Result()
    schema = load_schema()
    result.errors += schema_errors(atlas, schema, schema)
    if result.errors:
        return result
    result.errors += resolve_all_refs(atlas, SourceIndex(repo), only_new=True)
    structure = check_structure(atlas)
    result.errors += structure.errors
    result.warnings += structure.warnings
    result.errors += check_secrets(atlas)
    result.stats.update(structure.stats)
    return result


def audit_stored(atlas: dict, repo: Path) -> Result:
    """Check every stored reference against the files as they are now."""
    result = Result()
    schema = load_schema()
    result.errors += schema_errors(atlas, schema, schema)
    index = SourceIndex(repo)
    states: dict[str, int] = {}
    for _, group, block in iter_blocks(atlas):
        for ref in iter_refs(block):
            state, start, end = ref_state(ref, index)
            states[state] = states.get(state, 0) + 1
            label = f"{ref['path']}:{ref['start']}-{ref['end']} (block {block['id']})"
            if state == "fresh":
                continue
            if block.get("stale"):
                result.warnings.append(f"{label}: {state}, already marked stale")
            elif state == "moved":
                result.warnings.append(f"{label}: moved to {start}-{end}; run refresh")
            else:
                result.errors.append(f"{label}: {state}; run refresh")
    for ref in iter_refs({"next_steps": atlas.get("next_steps", [])}):
        state = ref_state(ref, index)[0]
        states[state] = states.get(state, 0) + 1
        if state == "moved":
            result.warnings.append(f"{ref['path']}:{ref['start']}-{ref['end']} (next_steps): moved; run refresh")
        elif state != "fresh" and ref.get("state") != state:
            result.errors.append(f"{ref['path']}:{ref['start']}-{ref['end']} (next_steps): {state}; run refresh")
        elif state != "fresh":
            result.warnings.append(f"{ref['path']}:{ref['start']}-{ref['end']} (next_steps): {state}, known")
    result.stats["references"] = sum(states.values())
    result.stats["states"] = states
    structure = check_structure(atlas)
    result.errors += structure.errors
    result.warnings += structure.warnings
    result.errors += check_secrets(atlas)
    result.stats.update(structure.stats)
    return result
