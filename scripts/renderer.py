"""Turn atlas.json into a self-contained index.html.

Every string from the repository or the agent passes through ``html.escape``
before it reaches a page. Code snippets are read from the real file when the
page is built, never stored or typed by the agent, and diagrams are generated
here as inline SVG, so the page makes no network requests.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import html
import json
import math
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import quote

import tours as tours_mod
from store import Paths, load_activity, load_answers, load_questions, load_tours, read_json, update_questions
from validate import RefError, SourceIndex, iter_refs, redact, ref_state, resolve_ref

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "atlas.html"

SECTIONS = {
    "overview": ("The Burrow", "Overview"),
    "architecture": ("Tunnel Map", "Architecture"),
    "modules": ("Explore the Tunnels", "Modules"),
    "stack": ("Toolkit", "Tech Stack"),
    "workflows": ("Follow the Rabbit", "Workflows"),
    "walkthrough": ("Down the Rabbit Hole", "Walkthroughs"),
}
IDES = ("vscode", "cursor", "idea")
CERTAINTY_LABEL = {
    "verified": "read in source", "inferred": "inference", "tested": "asserted by a test",
    "documented": "from docs", "absence": "searched, not found",
}
STEP_LABELS = {"what": "What the code does", "why": "Why this step exists", "inputs": "Inputs",
               "outputs": "Outputs", "behavior": "Errors and async behavior",
               "next": "What happens next"}
TREE_KINDS = {"call": "call", "branch": "branch", "io": "external I/O", "async": "async boundary",
              "error": "error path"}


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def ide_urls(abs_path: str, line: int, col: int = 1) -> dict[str, str]:
    """The deep link for each supported IDE. Paths are percent-encoded."""
    path = quote(abs_path, safe="/")
    return {
        "vscode": f"vscode://file{path}:{line}:{col}",
        "cursor": f"cursor://file{path}:{line}:{col}",
        "idea": f"idea://open?file={quote(abs_path, safe='')}&line={line}",
    }


# -- syntax highlighting --------------------------------------------------------------

_KEYWORDS = {
    "python": """False None True and as assert async await break class continue def del elif else
        except finally for from global if import in is lambda nonlocal not or pass raise return
        try while with yield""",
    "js": """async await break case catch class const continue default delete do else enum export
        extends false finally for function if implements import in instanceof interface let new
        null of private protected public return static super switch this throw true try type
        typeof undefined var void while yield""",
    "go": """break case chan const continue default defer else fallthrough false for func go goto
        if import interface map nil package range return select struct switch true type var""",
    "rust": """as async await break const continue crate dyn else enum extern false fn for if impl
        in let loop match mod move mut pub ref return self Self static struct super trait true
        type unsafe use where while""",
    "java": """abstract boolean break byte case catch char class const continue default do double
        else enum extends false final finally float for fun if implements import instanceof int
        interface long namespace new null override package private protected public return short
        static super switch this throw throws true try val var void volatile while""",
    "c": """auto bool break case catch char class const continue default delete do double else
        enum extern false float for if include inline int long namespace new nullptr private
        protected public return short signed sizeof static struct switch template this throw true
        try typedef union unsigned using virtual void volatile while""",
    "shell": """case do done elif else esac export fi for function if in local readonly return
        set shift then unset until while""",
    "ruby": """alias and begin break case class def do else elsif end ensure false for if in
        module next nil not or redo rescue retry return self super then true undef unless until
        when while yield""",
    "data": "true false null yes no",
}
_LANGS = {
    ".py": "python", ".pyi": "python", ".js": "js", ".jsx": "js", ".ts": "js", ".tsx": "js",
    ".mjs": "js", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "java", ".cs": "java",
    ".scala": "java", ".swift": "java", ".c": "c", ".h": "c", ".cc": "c", ".cpp": "c", ".hpp": "c",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".rb": "ruby", ".json": "data",
    ".yaml": "data", ".yml": "data", ".toml": "data",
}
_HASH_COMMENT = {"python", "shell", "ruby", "data"}
_KEYWORD_SETS = {k: frozenset(v.split()) for k, v in _KEYWORDS.items()}


def language_of(path: str) -> str | None:
    return _LANGS.get(PurePosixPath(path).suffix.lower())


def _token_regex(lang: str) -> re.Pattern:
    comment = r"#[^\n]*" if lang in _HASH_COMMENT else r"//[^\n]*|/\*[\s\S]*?\*/"
    strings = r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\''
    if lang == "python":
        strings = r'[rbfuRBFU]{0,2}(?:"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|' + strings + ")"
    if lang == "js":
        strings += r"|`(?:\\.|[^`\\])*`"
    if lang == "go":
        strings += r"|`[^`]*`"
    decorator = r"|@[A-Za-z_][\w.]*" if lang in ("python", "java") else ""
    return re.compile(
        rf"(?P<comment>{comment})|(?P<string>{strings})"
        rf"|(?P<number>\b\d[\w.]*\b){decorator and '|(?P<decorator>' + decorator[1:] + ')'}"
        r"|(?P<word>[A-Za-z_]\w*)"
    )


def highlight_lines(code: str, path: str) -> list[str]:
    """Escaped, highlighted HTML for each line; a multi-line token is closed per line."""
    lang = language_of(path)
    if lang is None:
        return [esc(line) for line in code.split("\n")]
    keywords = _KEYWORD_SETS[lang]
    regex = _token_regex(lang)
    out: list[str] = [""]

    def emit(text: str, cls: str | None) -> None:
        pieces = text.split("\n")
        for i, piece in enumerate(pieces):
            if i:
                out.append("")
            if piece:
                out[-1] += f'<span class="t-{cls}">{esc(piece)}</span>' if cls else esc(piece)

    pos, previous = 0, ""
    for match in regex.finditer(code):
        emit(code[pos:match.start()], None)
        kind = match.lastgroup or ""
        token = match.group()
        cls: str | None = kind
        if kind == "word":
            if token in keywords:
                cls = "keyword"
            elif previous in ("def", "class", "func", "fn", "function"):
                cls = "name"
            else:
                cls = None
            previous = token
        emit(token, cls)
        pos = match.end()
    emit(code[pos:], None)
    return out


# -- inline text ----------------------------------------------------------------------

_LINK = re.compile(r"\[\[([a-z0-9][a-z0-9._-]*)(?:\|([^\]]*))?\]\]")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*(?![\s*])([^*\n]+?)(?<![\s*])\*(?![\w*])")


def plain(text: str) -> str:
    """The characters a reader sees once inline markup is gone."""
    text = _LINK.sub(lambda m: m.group(2) or m.group(1), text)
    return _ITALIC.sub(r"\1", _BOLD.sub(r"\1", text)).replace("`", "")


def inline(text: str) -> str:
    out = []
    for part in re.split(r"(`[^`\n]+`)", text):
        if len(part) > 1 and part.startswith("`") and part.endswith("`"):
            out.append(f"<code>{esc(part[1:-1])}</code>")
            continue
        escaped = _ITALIC.sub(r"<em>\1</em>", _BOLD.sub(r"<strong>\1</strong>", esc(part)))
        out.append(_LINK.sub(lambda m: f'<a class="xref" href="#x-{m.group(1)}">'
                                       f'{m.group(2) or m.group(1)}</a>', escaped))
    return "".join(out)


def rich(text: str) -> str:
    """Paragraphs, bullet lists and fenced code, for prose and answers."""
    out: list[str] = []
    chunks = re.split(r"(^```[^\n]*\n[\s\S]*?^```\s*$)", text, flags=re.M)
    for chunk in chunks:
        fenced = re.match(r"^```[^\n]*\n([\s\S]*?)^```\s*$", chunk, flags=re.M)
        if fenced:
            out.append(f'<pre class="code plain"><code>{esc(redact(fenced.group(1)))}</code></pre>')
            continue
        for para in re.split(r"\n\s*\n", chunk):
            para = para.strip()
            if not para:
                continue
            lines = para.split("\n")
            if all(re.match(r"\s*[-*] ", ln) for ln in lines):
                bare = (re.sub(r"^\s*[-*] ", "", ln) for ln in lines)
                out.append("<ul>" + "".join(f"<li>{inline(ln)}</li>" for ln in bare) + "</ul>")
            else:
                out.append(f"<p>{inline(' '.join(ln.strip() for ln in lines))}</p>")
    return "".join(out)


def wrap_words(text: str, width: int, max_lines: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: max(0, width - 1)].rstrip() + "…"
    return [ln if len(ln) <= width + 4 else ln[:width + 3] + "…" for ln in lines]


# -- diagrams -------------------------------------------------------------------------


def _layout_layers(ids: list[str], edges: list[tuple[str, str]]):
    """Sugiyama-style layers: returns rank per node and edges with dummy waypoints."""
    outgoing: dict[str, list[str]] = {i: [] for i in ids}
    state: dict[str, int] = {}
    forward: list[tuple[str, str, bool]] = []
    for a, b in edges:
        outgoing[a].append(b)

    reversed_edges: set[tuple[str, str]] = set()

    def visit(node: str) -> None:
        state[node] = 1
        for nxt in outgoing[node]:
            if nxt == node:
                continue
            if state.get(nxt) == 1:
                reversed_edges.add((node, nxt))
            elif nxt not in state:
                visit(nxt)
        state[node] = 2

    for node in ids:
        if node not in state:
            visit(node)
    for a, b in edges:
        if a != b:
            forward.append((b, a, True) if (a, b) in reversed_edges else (a, b, False))

    rank = {i: 0 for i in ids}
    for _ in ids:
        changed = False
        for a, b, _flip in forward:
            if rank[b] < rank[a] + 1:
                rank[b] = rank[a] + 1
                changed = True
        if not changed:
            break

    layers: dict[int, list[str]] = {}
    for node in ids:
        layers.setdefault(rank[node], []).append(node)
    routes: list[dict] = []
    dummy = 0
    for a, b, flip in forward:
        chain = [a]
        for r in range(rank[a] + 1, rank[b]):
            name = f"\0d{dummy}"
            dummy += 1
            layers.setdefault(r, []).append(name)
            chain.append(name)
        chain.append(b)
        routes.append({"chain": chain, "flip": flip})
    return rank, layers, routes


def _order_and_place(layers, routes, node_w, dummy_w, gap):
    up: dict[str, list[str]] = {}
    down: dict[str, list[str]] = {}
    for route in routes:
        for a, b in zip(route["chain"], route["chain"][1:]):
            down.setdefault(a, []).append(b)
            up.setdefault(b, []).append(a)
    ranks = sorted(layers)
    position = {n: i for r in ranks for i, n in enumerate(layers[r])}

    def sweep(order, neighbours):
        for r in order:
            def key(n):
                near = [position[m] for m in neighbours.get(n, [])]
                return sum(near) / len(near) if near else position[n]
            layers[r].sort(key=key)
            for i, n in enumerate(layers[r]):
                position[n] = i

    for _ in range(6):
        sweep(ranks[1:], up)
        sweep(list(reversed(ranks[:-1])), down)

    def width(n: str) -> int:
        return dummy_w if n.startswith("\0") else node_w

    x: dict[str, float] = {}
    for r in ranks:
        cursor = 0.0
        for n in layers[r]:
            x[n] = cursor + width(n) / 2
            cursor += width(n) + gap
    span = max(sum(width(n) + gap for n in layers[r]) - gap for r in ranks)
    for r in ranks:
        used = sum(width(n) + gap for n in layers[r]) - gap
        for n in layers[r]:
            x[n] += (span - used) / 2

    for _ in range(10):
        for neighbours, order in ((up, ranks), (down, list(reversed(ranks)))):
            for r in order:
                wanted = {n: (sum(x[m] for m in neighbours[n]) / len(neighbours[n])
                              if neighbours.get(n) else x[n]) for n in layers[r]}
                previous_right = -math.inf
                for n in layers[r]:
                    left = max(wanted[n] - width(n) / 2, previous_right + gap)
                    x[n] = left + width(n) / 2
                    previous_right = left + width(n)
    low = min(x[n] - width(n) / 2 for n in x)
    for n in x:
        x[n] -= low
    return x, max(x[n] + width(n) / 2 for n in x)


def component_svg(block: dict, anchor_ids: set[str]) -> str:
    nodes = {n["id"]: n for n in block["nodes"]}
    edge_meta = {(e["from"], e["to"]): e for e in block["edges"]}
    ids = list(nodes)
    rank, layers, routes = _layout_layers(ids, [(e["from"], e["to"]) for e in block["edges"]])
    node_w, node_h, gap, rank_gap, margin = 178, 58, 26, 92, 12
    x, width = _order_and_place(layers, routes, node_w, 14, gap)
    y = {n: margin + r * (node_h + rank_gap) + node_h / 2 for r, ns in layers.items() for n in ns}
    total_h = margin * 2 + (max(layers) + 1) * node_h + max(layers) * rank_gap
    total_w = width + margin * 2

    edge_svg: list[str] = []
    label_svg: list[str] = []
    for route in routes:
        chain = route["chain"]
        source, target = (chain[-1], chain[0]) if route["flip"] else (chain[0], chain[-1])
        meta = edge_meta.get((source, target), {})
        cls = "dg-edge runtime" if meta.get("kind") == "runtime" else "dg-edge"
        pts: list[tuple[float, float]] = []
        for i, name in enumerate(chain):
            cx, cy = x[name] + margin, y[name]
            if name.startswith("\0"):
                pts.append((cx, cy))
            elif i == 0:
                pts.append((cx, cy + node_h / 2))
            else:
                pts.append((cx, cy - node_h / 2 - 2))
        if route["flip"]:
            pts.reverse()
        path = f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            mid = (y0 + y1) / 2
            path += f" C{x0:.1f},{mid:.1f} {x1:.1f},{mid:.1f} {x1:.1f},{y1:.1f}"
        edge_svg.append(f'<path class="{cls}" d="{path}" marker-end="url(#{esc(block["id"])}-arrow)"/>')
        if meta.get("label"):
            index = max(0, len(pts) // 2 - 1)
            (x0, y0), (x1, y1) = pts[index], pts[index + 1]
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2
            label = str(meta["label"])
            w = len(label) * 5.6 + 8
            label_svg.append(
                f'<rect class="dg-label-bg" x="{mx - w / 2:.1f}" y="{my - 8:.1f}" width="{w:.1f}" height="15" rx="3"/>'
                f'<text class="dg-label" x="{mx:.1f}" y="{my + 3:.1f}" text-anchor="middle">{esc(label)}</text>')

    node_svg: list[str] = []
    for node_id, node in nodes.items():
        cx, cy = x[node_id] + margin, y[node_id]
        kind = node.get("kind", "module")
        lines = wrap_words(node["label"], 25, 2)
        detail = wrap_words(node.get("detail", ""), 31, 1)
        count = len(lines) + len(detail)
        start = cy - (count - 1) * 7.5 + 4
        texts = "".join(
            f'<text class="dg-title" x="{cx:.1f}" y="{start + i * 15:.1f}" text-anchor="middle">{esc(t)}</text>'
            for i, t in enumerate(lines))
        texts += "".join(
            f'<text class="dg-detail" x="{cx:.1f}" y="{start + (len(lines) + i) * 15:.1f}" text-anchor="middle">{esc(t)}</text>'
            for i, t in enumerate(detail))
        body = (f'<rect class="dg-box" x="{cx - node_w / 2:.1f}" y="{cy - node_h / 2:.1f}" '
                f'width="{node_w}" height="{node_h}" rx="{20 if kind == "store" else 7}"/>{texts}')
        target = node.get("target")
        if target and target in anchor_ids:
            body = f'<a class="dg-link" href="#x-{esc(target)}">{body}</a>'
        node_svg.append(f'<g class="dg-node kind-{esc(kind)}">{body}</g>')

    marker = (f'<defs><marker id="{esc(block["id"])}-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
              'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
              '<path class="dg-arrow" d="M0,0 L10,5 L0,10 z"/></marker></defs>')
    title = esc(block.get("caption") or "Component diagram")
    return (f'<div class="diagram"><svg role="img" aria-label="{title}" viewBox="0 0 {total_w:.0f} {total_h:.0f}" '
            f'style="min-width:{min(total_w, 720):.0f}px;max-width:{total_w:.0f}px">{marker}'
            f'{"".join(edge_svg)}{"".join(node_svg)}{"".join(label_svg)}</svg></div>')


def sequence_svg(block: dict) -> str:
    people = block["participants"]
    column = 176
    margin = 14
    head_h = 42
    index = {p["id"]: i for i, p in enumerate(people)}

    def cx(i: int) -> float:
        return margin + column / 2 + i * column

    rows: list[str] = []
    y = head_h + 26.0
    for message in block["messages"]:
        a, b = index[message["from"]], index[message["to"]]
        kind = message.get("kind", "call")
        if a == b:
            lines = wrap_words(message["label"], 30, 3)
            x0 = cx(a)
            for i, line in enumerate(lines):
                rows.append(f'<text class="sq-label" x="{x0 + 8:.1f}" y="{y + i * 13:.1f}">{esc(line)}</text>')
            top = y + (len(lines) - 1) * 13 + 7
            rows.append(f'<path class="sq-msg {esc(kind)}" d="M{x0:.1f},{top:.1f} h30 v18 h-28" '
                        f'marker-end="url(#{esc(block["id"])}-arr)"/>')
            y = top + 18 + 26
            continue
        width = max(14, int((abs(b - a) * column - 16) / 6.1))
        lines = wrap_words(message["label"], width, 3)
        mid = (cx(a) + cx(b)) / 2
        for i, line in enumerate(lines):
            rows.append(f'<text class="sq-label" x="{mid:.1f}" y="{y + i * 13:.1f}" text-anchor="middle">{esc(line)}</text>')
        ly = y + (len(lines) - 1) * 13 + 7
        direction = 1 if b > a else -1
        rows.append(f'<path class="sq-msg {esc(kind)}" d="M{cx(a):.1f},{ly:.1f} L{cx(b) - direction * 3:.1f},{ly:.1f}" '
                    f'marker-end="url(#{esc(block["id"])}-arr)"/>')
        y = ly + 26
    height = y + 2
    total = margin * 2 + column * len(people)
    heads: list[str] = []
    for i, person in enumerate(people):
        heads.append(f'<line class="sq-life" x1="{cx(i):.1f}" y1="{head_h}" x2="{cx(i):.1f}" y2="{height:.1f}"/>')
        heads.append(f'<rect class="sq-head" x="{cx(i) - column / 2 + 8:.1f}" y="4" width="{column - 16}" height="{head_h - 8}" rx="6"/>')
        label = wrap_words(person["label"], 24, 2)
        for j, line in enumerate(label):
            text_y = head_h / 2 + 4 + (j - (len(label) - 1) / 2) * 13
            heads.append(f'<text class="sq-name" x="{cx(i):.1f}" y="{text_y:.1f}" text-anchor="middle">{esc(line)}</text>')
    marker = (f'<defs><marker id="{esc(block["id"])}-arr" viewBox="0 0 10 10" refX="9" refY="5" '
              'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
              '<path class="sq-arrow" d="M0,0 L10,5 L0,10 z"/></marker></defs>')
    title = esc(block.get("caption") or "Sequence diagram")
    return (f'<div class="diagram"><svg role="img" aria-label="{title}" viewBox="0 0 {total} {height:.0f}" '
            f'style="min-width:{min(total, 640)}px;max-width:{total}px">{marker}{"".join(heads)}{"".join(rows)}</svg></div>')


# -- the page -------------------------------------------------------------------------


def anchor_index(atlas: dict, ready: Iterable[dict] = ()) -> dict[str, str]:
    """Every id a question may attach to, with the section that holds it."""
    ids: dict[str, str] = {"coverage.report": "overview", "next.steps": "overview"}
    for tour in ready:
        ids[f"{tour['id']}.summary"] = "tours"
        for number in range(tour["meta"]["files_changed"]):
            ids[f"{tour['id']}.file.{number}"] = "tours"
        for group in tour["body"]["groups"]:
            ids[group["id"]] = "tours"
            if group.get("summary"):
                ids[f"summary.{group['id']}"] = "tours"
            for block in group["blocks"]:
                ids[block["id"]] = "tours"
    for section in atlas["sections"]:
        ids[f"intro.{section['id']}"] = section["id"]
        for group in section["groups"]:
            ids[group["id"]] = section["id"]
            if group.get("summary"):
                ids[f"summary.{group['id']}"] = section["id"]
            for block in group["blocks"]:
                ids[block["id"]] = section["id"]
    return ids


_NON_TEXT_KEYS = {"id", "type", "kind", "key", "certainty", "target", "from", "to", "stale",
                  "refs", "ref", "code", "number", "ordered"}


_FIXED_LABELS = ("What it does", "Why it might help", "Trade-offs", "Evidence", *CERTAINTY_LABEL.values(),
                 *TREE_KINDS.values())


class Renderer:
    def __init__(self, paths: Paths, atlas: dict, ide: str = "vscode") -> None:
        self.paths = paths
        self.atlas = atlas
        self.ide = ide if ide in IDES else "vscode"
        self.index = SourceIndex(paths.repo)
        self.ready = tours_mod.load_ready(paths)
        self.anchors = anchor_index(atlas, self.ready)
        self.anchor_ids = set(self.anchors)
        self.changed_lines: dict[str, list[int]] = {}
        self.link_titles: dict[str, str] = {}
        self.step_code: dict[str, dict] = {}
        all_groups = [g for section in atlas["sections"] for g in section["groups"]]
        all_groups += [g for tour in self.ready for g in tour["body"]["groups"]]
        for group in all_groups:
            self.link_titles[group["id"]] = group["title"]
            for block in group["blocks"]:
                if block["type"] == "step":
                    self.step_code[block["id"]] = block["code"]
                    self.link_titles[block["id"]] = f"Step {block['number']}: {plain(block['title'])}"
                elif block.get("title"):
                    self.link_titles[block["id"]] = plain(block["title"])

    @contextlib.contextmanager
    def in_tour(self, tour: dict):
        """Resolve references against the branch's snapshot instead of the working tree."""
        saved = self.index, self.changed_lines
        self.index = SourceIndex(tour["src"])
        self.changed_lines = tour["meta"].get("changed_lines", {})
        try:
            yield
        finally:
            self.index, self.changed_lines = saved

    def tour_of(self, block_id: str) -> dict | None:
        head = block_id.removeprefix("summary.").split(".", 1)[0]
        return next((t for t in self.ready if t["id"] == head), None)

    def label_links(self, markup: str) -> str:
        """Give an unlabeled ``[[id]]`` link the title of what it points to."""
        return re.sub(r'(<a class="xref" href="#x-([a-z0-9._-]+)">)\2</a>',
                      lambda m: f"{m.group(1)}{esc(self.link_titles.get(m.group(2), m.group(2)))}</a>", markup)

    # -- references --------------------------------------------------------------

    def ref_label(self, ref: dict) -> tuple[str, str, int, int]:
        state, start, end = ref_state(ref, self.index)
        span = f"{start}–{end}" if end != start else f"{start}"
        return f"{ref['path']}:{span}", state, start, end

    def ref_link(self, ref: dict) -> str:
        label, state, start, _end = self.ref_label(ref)
        if state == "missing":
            return f'<span class="ref ref-missing" title="This file no longer exists">{esc(label)}</span>'
        abs_path = str(self.index.repo / ref["path"])
        urls = ide_urls(abs_path, start)
        title = {"fresh": "Open in your IDE", "moved": "Open in your IDE (lines moved since analysis)",
                 "changed": "This code changed since analysis", "lost": "The cited code is gone from this file"}[state]
        return (f'<a class="ref ref-{state}" href="{esc(urls[self.ide])}" title="{title}" '
                f'data-vscode="{esc(urls["vscode"])}" data-cursor="{esc(urls["cursor"])}" '
                f'data-idea="{esc(urls["idea"])}">{esc(label)}</a>')

    def crumbs(self, holder: dict) -> str:
        certainty = holder.get("certainty", "verified")
        refs = list(iter_refs({"refs": holder.get("refs", [])}))
        if certainty == "context" or (not refs and certainty == "verified"):
            return ""
        label = CERTAINTY_LABEL.get(certainty, certainty)
        links = "".join(self.ref_link(r) for r in refs)
        return f'<div class="crumbs"><span class="certainty c-{esc(certainty)}">{esc(label)}</span>{links}</div>'

    # -- blocks ------------------------------------------------------------------

    def snippet(self, ref: dict) -> str:
        label, state, start, end = self.ref_label(ref)
        lines = self.index.lines(ref["path"])
        if lines is None or state == "missing":
            return f'<p class="code-missing">{esc(ref["path"])} is no longer available.</p>'
        source = redact("\n".join(lines[start - 1:end]))
        rendered = highlight_lines(source, ref["path"])
        added = set(self.changed_lines.get(ref["path"], ()))
        body = "\n".join(f'<span class="line{" changed" if start + i in added else ""}" data-line="{start + i}" '
                         f'data-n="{start + i}">{line or " "}</span>' for i, line in enumerate(rendered))
        return (f'<figure class="snippet"><figcaption>{self.ref_link(ref)}</figcaption>'
                f'<pre class="code" data-path="{esc(ref["path"])}"><code>{body}</code></pre></figure>')

    def block_html(self, block: dict) -> str:
        kind = block["type"]
        inner = getattr(self, f"_b_{kind}")(block)
        stale = ""
        if block.get("stale"):
            files = ", ".join(block["stale"].get("files", []))
            stale = (f'<div class="stale-banner" role="note"><strong>May be out of date.</strong> '
                     f'{esc(block["stale"]["reason"])}{f" ({esc(files)})" if files else ""}</div>')
        cls = f"block b-{kind}" + (" is-stale" if block.get("stale") else "")
        tag = "article" if kind == "step" else "div"
        bid = esc(block["id"])
        return f'<{tag} class="{cls}" id="x-{bid}" data-block-id="{bid}">{stale}{inner}</{tag}>'

    def _b_prose(self, b: dict) -> str:
        return f'<div class="text">{rich(b["text"])}</div>{self.crumbs(b)}'

    def _item(self, item: dict) -> str:
        return f'<div class="text">{rich(item["text"])}</div>{self.crumbs(item)}'

    def _b_list(self, b: dict) -> str:
        tag = "ol" if b.get("ordered") else "ul"
        title = f'<h4>{inline(b["title"])}</h4>' if b.get("title") else ""
        return f'{title}<{tag}>{"".join(f"<li>{self._item(i)}</li>" for i in b["items"])}</{tag}>'

    def _b_facts(self, b: dict) -> str:
        rows = "".join(f'<div class="fact"><dt>{esc(i["label"])}</dt><dd>{self._item(i)}</dd></div>' for i in b["items"])
        return f"<dl>{rows}</dl>"

    def _b_table(self, b: dict) -> str:
        title = f'<h4>{inline(b["title"])}</h4>' if b.get("title") else ""
        cited = any(r.get("refs") or r.get("certainty") for r in b["rows"])
        head = "".join(f"<th>{esc(h)}</th>" for h in b["headers"]) + ("<th>Evidence</th>" if cited else "")
        rows = []
        for row in b["rows"]:
            cells = "".join(f"<td>{inline(c)}</td>" for c in row["cells"])
            if cited:
                cells += f"<td>{self.crumbs(row)}</td>"
            rows.append(f"<tr>{cells}</tr>")
        return f'{title}<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'

    def _b_code(self, b: dict) -> str:
        caption = f'<p class="caption">{inline(b["caption"])}</p>' if b.get("caption") else ""
        return caption + self.snippet(b["ref"])

    def _b_decision(self, b: dict) -> str:
        pattern = f'<span class="pattern">{esc(b["pattern"])}</span>' if b.get("pattern") else ""
        parts = "".join(f'<div class="fact"><dt>{label}</dt><dd><div class="text">{rich(b[key])}</div></dd></div>'
                        for key, label in (("what", "What it does"), ("why", "Why it might help"),
                                           ("tradeoffs", "Trade-offs")))
        return f'<h4>{inline(b["title"])}{pattern}</h4><dl>{parts}</dl>{self.crumbs(b)}'

    def _caption(self, b: dict) -> str:
        return f'<p class="caption">{inline(b["caption"])}</p>' if b.get("caption") else ""

    def _b_component_diagram(self, b: dict) -> str:
        legend = ('<div class="legend"><span class="lg lg-entry">entry point</span><span class="lg lg-module">module</span>'
                  '<span class="lg lg-store">state</span><span class="lg lg-external">external</span>'
                  '<span class="lg lg-static">static import</span><span class="lg lg-runtime">runtime call</span></div>')
        return self._caption(b) + component_svg(b, self.anchor_ids) + legend + self.crumbs(b)

    def _b_sequence_diagram(self, b: dict) -> str:
        return self._caption(b) + sequence_svg(b) + self.crumbs(b)

    def _b_step(self, b: dict) -> str:
        fields = "".join(f'<div class="fact"><dt>{label}</dt><dd><div class="text">{rich(b[key])}</div></dd></div>'
                         for key, label in STEP_LABELS.items() if b.get(key))
        return (f'<header class="step-head"><span class="step-n">Step {int(b["number"])}</span>'
                f'<h4>{inline(b["title"])}</h4></header>{self.snippet(b["code"])}<dl>{fields}</dl>{self.crumbs(b)}')

    def _tree(self, node: dict) -> str:
        kind = node["kind"]
        note = f'<span class="tree-note">{inline(node["note"])}</span>' if node.get("note") else ""
        target = node.get("target")
        source = node.get("ref") or self.step_code.get(target or "")
        ref = self.ref_link(source) if source else ""
        label = f'<a class="tree-jump" href="#x-{esc(target)}">{inline(node["label"])}</a>' if target else f'<span>{inline(node["label"])}</span>'
        row = (f'<span class="tree-kind k-{kind}" title="{TREE_KINDS[kind]}">{TREE_KINDS[kind]}</span>'
               f'{label}{note}{ref}')
        kids = node.get("children", [])
        if not kids:
            return f'<li><div class="tree-row">{row}</div></li>'
        inner = "".join(self._tree(k) for k in kids)
        return f'<li><details open><summary class="tree-row">{row}</summary><ul>{inner}</ul></details></li>'

    def _b_execution_tree(self, b: dict) -> str:
        return f'{self._caption(b)}<ul class="tree">{self._tree(b["root"])}</ul>{self.crumbs(b)}'

    # -- groups and sections -----------------------------------------------------

    def group_html(self, group: dict) -> str:
        gid = esc(group["id"])
        summary = ""
        if group.get("summary"):
            summary = f'<div class="group-summary" id="x-summary.{gid}" data-block-id="summary.{gid}">{inline(group["summary"])}</div>'
        related = ""
        if group.get("related"):
            titles = {g["id"]: g["title"] for s in self.atlas["sections"] for g in s["groups"]}
            chips = "".join(f'<a class="chip" href="#x-{esc(r)}">{esc(titles.get(r, r))}</a>' for r in group["related"])
            related = f'<nav class="related" aria-label="Related">Related {chips}</nav>'
        files = ""
        body = "".join(self.block_html(b) for b in group["blocks"])
        title = inline(group["title"])
        stale = any(b.get("stale") for b in group["blocks"])
        flag = '<span class="stale-dot" title="Contains possibly out-of-date explanations"></span>' if stale else ""
        if group.get("collapsible"):
            state = " open" if group.get("open") else ""
            return (f'<details class="group" id="x-{gid}" data-title="{esc(group["title"])}"{state}><summary><h3>{title}{flag}</h3>{summary}</summary>'
                    f'<div class="group-body">{related}{files}{body}</div></details>')
        return f'<section class="group" id="x-{gid}" data-title="{esc(group["title"])}"><h3>{title}{flag}</h3>{summary}{related}{files}{body}</section>'

    def section_html(self, section: dict) -> str:
        sid = esc(section["id"])
        trail, technical = SECTIONS[section["id"]]
        groups = "".join(self.group_html(g) for g in section["groups"])
        extra = self.coverage_html() if section["id"] == "overview" else ""
        return (f'<section class="pane" id="s-{sid}" data-section="{sid}"><header class="pane-head">'
                f'<p class="eyebrow">{esc(trail)}</p><h2>{esc(technical)}</h2>'
                f'<div class="intro" id="x-intro.{sid}" data-block-id="intro.{sid}">{rich(section["intro"])}</div></header>'
                f'{groups}{extra}</section>')

    def coverage_html(self) -> str:
        cov = self.atlas["coverage"]

        def rows(items: list[dict]) -> str:
            return "".join(f"<li><strong>{inline(i['area'])}</strong>: {inline(i['why'])}</li>" for i in items) or "<li>Nothing.</li>"

        budget = f'<p>{inline(cov["budget"])}</p>' if cov.get("budget") else ""
        ran = ""
        if cov.get("commands_run"):
            ran = "<h4>Commands executed during analysis</h4><ul>" + "".join(f"<li><code>{esc(c)}</code></li>" for c in cov["commands_run"]) + "</ul>"
        next_items = "".join(f"<li>{self._item(i)}</li>" for i in self.atlas["next_steps"])
        return (f'<section class="group" id="x-coverage" data-title="Analysis coverage"><h3>Analysis coverage</h3>'
                f'<div class="block" id="x-coverage.report" data-block-id="coverage.report">{budget}'
                f'<div class="cov-grid"><div><h4>Read deeply</h4><ul>{rows(cov["deep"])}</ul></div>'
                f'<div><h4>Scanned</h4><ul>{rows(cov["scanned"])}</ul></div>'
                f'<div><h4>Skipped</h4><ul>{rows(cov["skipped"])}</ul></div></div>{ran}</div></section>'
                f'<section class="group" id="x-next" data-title="Where to dig next"><h3>Where to dig next</h3>'
                f'<div class="block" id="x-next.steps" data-block-id="next.steps"><ol>{next_items}</ol></div></section>')

    def tours_html(self) -> str:
        changes = {t["id"]: tours_mod.load_changes(self.paths, t["id"]) for t in self.ready}
        try:
            current = {b["name"]: b["sha"] for b in tours_mod.list_branches(self.paths.repo)["branches"]}
        except tours_mod.TourError:
            current = {}
        overlap = ""
        rows = tours_mod.overlaps(self.ready, changes)
        if rows:
            cells = "".join(f"<tr><td><code>{esc(r['path'])}</code></td><td>{esc(', '.join(r['branches']))}</td></tr>" for r in rows)
            overlap = ('<section class="group" id="x-tours-overlap" data-title="Files changed by more than one branch">'
                       '<h3>Files changed by more than one branch</h3><p class="caption">In the set you selected, these files '
                       'were changed by several branches, so check them first when merging. This compares file names and does '
                       f'not attempt a merge.</p><div class="table-wrap"><table><thead><tr><th>File</th><th>Branches</th></tr></thead><tbody>{cells}</tbody></table></div></section>')
        tours = "".join(self.tour_html(t, changes[t["id"]], current) for t in reversed(self.ready))
        return (f'<section class="pane" id="s-tours" data-section="tours"><header class="pane-head"><p class="eyebrow">Side Tunnels</p>'
                '<h2>Branch Tours</h2><div class="intro"><p>Pick the branches you want to understand. Each tour explains what the branch '
                'changes, what that affects, and how the new code runs, without touching your checkout.</p></div></header>'
                f'<div id="tour-picker"></div><div id="tour-status"></div>{overlap}{tours}</section>')

    def tour_html(self, tour: dict, changes: list[dict], current: dict[str, str]) -> str:
        tid, meta = esc(tour["id"]), tour["meta"]
        with self.in_tour(tour):
            groups = "".join(self.group_html(g) for g in tour["body"]["groups"])
            files = "".join(self.diff_html(tour, i, f) for i, f in enumerate(changes))
        moved = ""
        if current.get(tour["branch"]) != meta["sha"]:
            gone = tour["branch"] not in current
            moved = (f'<div class="stale-banner" role="note"><strong>{"This branch no longer exists." if gone else "This branch has new commits."}</strong> '
                     'The tour describes the snapshot below. Select the branch again for a new tour.</div>')
        chips = "".join(f'<a class="chip" href="#x-{esc(a["id"])}">{esc(a["title"])}</a>' for a in meta["affected"])
        affected = f'<nav class="related" aria-label="Affected Atlas sections">Touches {chips}</nav>' if chips else ""
        commits = "".join(f'<li><code>{esc(c["sha"])}</code> {esc(c["subject"])}</li>' for c in meta["commits"][:25])
        extra = len(meta["commits"]) - 25
        if extra > 0:
            commits += f"<li>…and {extra} more</li>"
        cut = '<div class="stale-banner" role="note">This diff was too large to show in full. Files marked "cut short" are incomplete here.</div>' if meta["diff_truncated"] else ""
        return (f'<details class="group tour" id="x-{tid}" data-title="{esc(tour["branch"])}" open><summary><h3><code>{esc(tour["branch"])}</code></h3>'
                f'<span class="tour-stat">onto <code>{esc(tour["base"])}</code> · {len(meta["commits"])} commit(s) · {int(meta["files_changed"])} file(s) · '
                f'<span class="plus">+{int(meta["additions"])}</span> <span class="minus">−{int(meta["deletions"])}</span> · <code>{esc(meta["sha"][:8])}</code></span></summary>'
                f'<div class="group-body">{moved}{affected}<div class="block" id="x-{tid}.summary" data-block-id="{tid}.summary"><div class="text">{rich(tour["body"]["summary"])}</div></div>'
                f'{groups}<section class="group"><h3>Changed files</h3>{cut}{files}</section>'
                f'<section class="group"><h3>Commits</h3><ul class="commits">{commits}</ul></section></div></details>')

    def diff_html(self, tour: dict, number: int, record: dict) -> str:
        bid = f'{esc(tour["id"])}.file.{number}'
        title = f'<code>{esc(record["path"])}</code>'
        if record.get("old_path"):
            title = f'<code>{esc(record["old_path"])}</code> → {title}'
        body: list[str] = []
        first_new = None
        for hunk in record["hunks"]:
            body.append(f'<span class="line hunk">{esc(hunk["header"])}</span>')
            for kind, old, new, text in hunk["lines"]:
                first_new = first_new or (new if kind == "+" else None)
                cls = {"+": "add", "-": "del", " ": "ctx"}[kind]
                attrs = f' data-line="{int(new)}" data-n="{int(new)}"' if new else f' data-n="{int(old)}"'
                body.append(f'<span class="line {cls}"{attrs}>{esc(text) or " "}</span>')
        link = ""
        if record["status"] != "deleted" and self_exists(tour, record):
            urls = ide_urls(str(tour["src"] / record["path"]), first_new or 1)
            link = (f'<a class="ref ref-fresh" href="{esc(urls[self.ide])}" title="Open this branch\'s version" data-vscode="{esc(urls["vscode"])}" '
                    f'data-cursor="{esc(urls["cursor"])}" data-idea="{esc(urls["idea"])}">open</a>')
        notes = ""
        if record.get("withheld"):
            notes = '<p class="caption">Contents withheld because this looks like a credentials file.</p>'
        elif record.get("binary"):
            notes = '<p class="caption">Binary file.</p>'
        elif record.get("truncated"):
            notes = '<p class="caption">Cut short: this file\'s diff is too long to show in full.</p>'
        code = f'<pre class="code diff" data-path="{esc(record["path"])}"><code>{chr(10).join(body)}</code></pre>' if body else ""
        return (f'<details class="diff-file" id="x-{bid}" data-block-id="{bid}"><summary><span class="status">{esc(record["status"])}</span> {title} '
                f'<span class="plus">+{int(record["additions"])}</span> <span class="minus">−{int(record["deletions"])}</span> {link}</summary>{notes}{code}</details>')

    def nav_html(self) -> str:
        out = []
        for section in self.atlas["sections"]:
            sid = section["id"]
            trail, technical = SECTIONS[sid]
            subs = "".join(f'<li><a href="#x-{esc(g["id"])}">{inline(g["title"])}</a></li>' for g in section["groups"])
            out.append(f'<li class="nav-item" data-nav="{esc(sid)}"><a class="nav-main" href="#s-{esc(sid)}">'
                       f'<span class="nav-trail">{esc(trail)}</span><span class="nav-tech">{esc(technical)}</span></a>'
                       f'<ul class="nav-sub">{subs}</ul></li>')
        subs = "".join(f'<li><a href="#x-{esc(t["id"])}">{esc(t["branch"])}</a></li>' for t in reversed(self.ready))
        out.append('<li class="nav-item" data-nav="tours"><a class="nav-main" href="#s-tours"><span class="nav-trail">Side Tunnels</span>'
                   f'<span class="nav-tech">Branch Tours</span></a><ul class="nav-sub">{subs}</ul></li>')
        out.append('<li class="nav-item" data-nav="notes"><a class="nav-main" href="#s-notes">'
                   '<span class="nav-trail">Rabbit Notes <b id="notes-count" class="count" hidden></b></span>'
                   '<span class="nav-tech">Questions and Answers</span></a></li>')
        return "".join(out)

    # -- anchors and notes -------------------------------------------------------

    def block_text(self, node: Any) -> str:
        """All visible text of an anchor target, for checking a saved selection."""
        parts: list[str] = []

        def collect(value: Any) -> None:
            if isinstance(value, str):
                parts.append(plain(value))
            elif isinstance(value, list):
                for item in value:
                    collect(item)
            elif isinstance(value, dict):
                for key, item in value.items():
                    if key not in _NON_TEXT_KEYS:
                        collect(item)
                for key in ("refs",):
                    for ref in value.get(key, []) if isinstance(value.get(key), list) else []:
                        parts.append(self.ref_label(ref)[0])
                for key in ("ref", "code"):
                    ref = value.get(key)
                    if isinstance(ref, dict) and "path" in ref:
                        parts.append(self.ref_label(ref)[0])
                        _, _, start, end = self.ref_label(ref)
                        lines = self.index.lines(ref["path"]) or []
                        parts.append(redact("\n".join(lines[start - 1:end])))

        collect(node)
        return "\n".join([*parts, *STEP_LABELS.values(), *_FIXED_LABELS])

    def anchor_text(self, block_id: str) -> str | None:
        section_id = self.anchors.get(block_id)
        if section_id is None:
            return None
        if block_id.startswith(("coverage.", "next.")):
            return self.block_text(self.atlas)  # Synthetic targets: accept any Atlas text.
        for section in self.atlas["sections"]:
            if block_id == f"intro.{section['id']}":
                return plain(section["intro"])
            for group in section["groups"]:
                if block_id == group["id"]:
                    return self.block_text(group)
                if block_id == f"summary.{group['id']}":
                    return plain(group.get("summary", ""))
                for block in group["blocks"]:
                    if block["id"] == block_id:
                        return self.block_text(block)
        return self.tour_anchor_text(block_id)

    def tour_anchor_text(self, block_id: str) -> str | None:
        tour = self.tour_of(block_id)
        if tour is None:
            return None
        if block_id == f"{tour['id']}.summary":
            return plain(tour["body"]["summary"])
        if ".file." in block_id:
            lines = [row[3] for f in tours_mod.load_changes(self.paths, tour["id"]) for h in f["hunks"] for row in h["lines"]]
            heads = [f["path"] for f in tours_mod.load_changes(self.paths, tour["id"])]
            return "\n".join([*lines, *heads, "added", "modified", "deleted", "renamed"])
        with self.in_tour(tour):
            for group in tour["body"]["groups"]:
                if block_id == group["id"]:
                    return self.block_text(group)
                if block_id == f"summary.{group['id']}":
                    return plain(group.get("summary", ""))
                for block in group["blocks"]:
                    if block["id"] == block_id:
                        return self.block_text(block)
        return None

    def reconcile_anchors(self) -> int:
        """Record whether each question's anchor still exists. Never removes a question."""
        changes: dict[str, dict] = {}
        for question in load_questions(self.paths):
            text = self.anchor_text(question.get("block_id", ""))
            if text is None:
                state = "orphaned"
            else:
                wanted = [re.sub(r"\s+", "", p) for p in str(question.get("selected_text", "")).split("\n")]
                haystack = re.sub(r"\s+", "", text)
                state = "ok" if all(p in haystack for p in wanted) else "drifted"
            if question.get("anchor") != state:
                changes[question["id"]] = {"anchor": state}
        return update_questions(self.paths, changes)

    def answer_context(self, answer: dict):
        tour = next((t for t in self.ready if t["id"] == answer.get("tour_id")), None)
        return self.in_tour(tour) if tour else contextlib.nullcontext()

    def answer_html(self, answer: dict) -> str:
        return self.label_links(f'<div class="text">{rich(answer["text"])}</div>{self.crumbs(answer)}')

    def notes_payload(self) -> dict:
        questions = load_questions(self.paths)
        answers: dict[str, list[dict]] = {}
        for answer in load_answers(self.paths):
            with self.answer_context(answer):
                stale = any(ref_state(r, self.index)[0] != "fresh" for r in answer.get("refs", []))
                answers.setdefault(answer["question_id"], []).append({
                    "id": answer["id"], "created_at": answer["created_at"],
                    "html": self.answer_html(answer), "stale": stale,
                })
        activity: dict[str, list[dict]] = {}
        latest = None
        for entry in load_activity(self.paths):
            row = {"at": entry.get("at", ""), "message": str(entry.get("message", ""))}
            latest = {**row, "question_id": entry.get("question_id")}
            if entry.get("question_id"):
                activity.setdefault(entry["question_id"], []).append(row)
        payload = {"questions": questions, "answers": answers,
                   "activity": {k: v[-40:] for k, v in activity.items()}, "latest": latest,
                   "tours": load_tours(self.paths)}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
        return {**payload, "version": digest}

    # -- output ------------------------------------------------------------------

    def page(self) -> str:
        template = TEMPLATE.read_text(encoding="utf-8")
        match = re.search(r'<script id="rr-main">([\s\S]*?)</script>', template)
        assert match, "template lost its main script"
        digest = base64.b64encode(hashlib.sha256(match.group(1).encode()).digest()).decode()
        csp = ("default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
               f"script-src 'sha256-{digest}'; connect-src 'self'; base-uri 'none'; form-action 'none'")
        manifest = read_json(self.paths.manifest, {}) or {}
        revision = manifest.get("revision", {})
        meta = {"ide": self.ide, "project": self.atlas["project"]["name"],
                "sections": {**{s: list(v) for s, v in SECTIONS.items()}, "tours": ["Side Tunnels", "Branch Tours"]},
                "tours_ready": [t["id"] for t in self.ready]}
        rev_text = (revision.get("head") or "no git revision")[:10]
        if revision.get("dirty"):
            rev_text += " + local changes"
        values = {
            "CSP": esc(csp),
            "TITLE": esc(f"{self.atlas['project']['name']} · RepoRabbit Atlas"),
            "PROJECT": esc(self.atlas["project"]["name"]),
            "ONE_LINER": inline(self.atlas["project"]["one_liner"]),
            "REVISION": esc(rev_text),
            "UPDATED": esc(manifest.get("updated_at", "")),
            "NAV": self.nav_html(),
            "CONTENT": self.label_links("".join(self.section_html(s) for s in self.atlas["sections"]) + self.tours_html()),
            "META_JSON": _json_island(meta),
            "NOTES_JSON": _json_island(self.notes_payload()),
        }
        return re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: values[m.group(1)], template)


def self_exists(tour: dict, record: dict) -> bool:
    return (tour["src"] / record["path"]).is_file()


def _json_island(value: Any) -> str:
    """JSON that cannot break out of a <script> element."""
    text = json.dumps(value, ensure_ascii=False)
    for bad, safe in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                      (" ", "\\u2028"), (" ", "\\u2029")):
        text = text.replace(bad, safe)
    return text


def validate_answer_refs(refs: Iterable[dict], index: SourceIndex) -> tuple[list[dict], list[str]]:
    resolved, errors = [], []
    for ref in refs:
        try:
            resolved.append(resolve_ref(ref, index))
        except RefError as exc:
            errors.append(str(exc))
    return resolved, errors
