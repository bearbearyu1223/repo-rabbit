"""The local Atlas server: page, notes feed and question intake. Standard library only.

It binds to 127.0.0.1 and has exactly three routes, so there is no file-serving
code to escape from. It never calls a model: questions wait on disk until the
user asks the agent to investigate them.
"""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from renderer import Renderer, anchor_index
from store import Paths, add_question, read_json, write_json
from validate import RefError, SourceIndex

MAX_BODY = 64 * 1024
MAX_QUESTION = 4000
MAX_SELECTION = 8000
SECTION_IDS = {"overview", "architecture", "modules", "stack", "workflows", "walkthrough", "notes"}
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,118}$")


class BadRequest(ValueError):
    pass


def validate_question(payload: Any, atlas: dict, repo: Path) -> dict:
    """Check a submitted question field by field; return only the known fields."""
    if not isinstance(payload, dict):
        raise BadRequest("the body must be a JSON object")
    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        raise BadRequest("write a question or note first")
    if len(question) > MAX_QUESTION:
        raise BadRequest(f"questions are limited to {MAX_QUESTION} characters")
    kind = payload.get("kind", "question")
    if kind not in ("question", "note"):
        raise BadRequest("kind must be 'question' or 'note'")
    section_id, block_id = payload.get("section_id"), payload.get("block_id")
    if section_id not in SECTION_IDS:
        raise BadRequest("unknown section_id")
    if not isinstance(block_id, str) or not _ID.match(block_id):
        raise BadRequest("invalid block_id")
    anchors = anchor_index(atlas)
    if block_id not in anchors:
        raise BadRequest("that block is not in the Atlas (reload the page)")
    if anchors[block_id] != section_id:
        raise BadRequest("block_id is not in that section")
    selected = payload.get("selected_text", "")
    if not isinstance(selected, str) or len(selected) > MAX_SELECTION:
        raise BadRequest("invalid selected_text")
    offset = payload.get("text_offset")
    if offset is not None and (not isinstance(offset, int) or isinstance(offset, bool) or offset < 0):
        raise BadRequest("invalid text_offset")
    clean = {"question": question.strip(), "kind": kind, "section_id": section_id,
             "block_id": block_id, "selected_text": selected, "text_offset": offset}
    source = payload.get("source_file")
    if source is not None:
        clean.update(_validate_source(payload, source, repo))
    return clean


def _validate_source(payload: dict, source: Any, repo: Path) -> dict:
    if not isinstance(source, str):
        raise BadRequest("invalid source_file")
    index = SourceIndex(repo)
    try:
        index.abs_path(source)
    except RefError as exc:
        raise BadRequest(str(exc)) from None
    lines = index.lines(source)
    start, end = payload.get("source_start"), payload.get("source_end")
    if lines is None:
        raise BadRequest("source_file does not exist")
    for value in (start, end):
        if not isinstance(value, int) or isinstance(value, bool):
            raise BadRequest("source lines must be integers")
    if not 1 <= start <= end <= len(lines):
        raise BadRequest("source lines are outside the file")
    return {"source_file": source, "source_start": start, "source_end": end}


class Handler(BaseHTTPRequestHandler):
    server_version = "RepoRabbit"
    sys_version = ""
    paths: Paths

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: Any) -> None:
        self._send(status, json.dumps(value).encode(), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        """Refuse DNS-rebinding: a browser sends the attacker's name in Host."""
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        port = self.server.server_address[1]
        return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def _renderer(self) -> Renderer | None:
        atlas = read_json(self.paths.atlas)
        return Renderer(self.paths, atlas) if atlas else None

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._json(403, {"error": "unexpected Host header"})
            return
        route = urlsplit(self.path).path
        if route in ("/", "/index.html"):
            try:
                self._send(200, self.paths.index.read_bytes(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._json(404, {"error": "no Atlas yet; run the Explore workflow"})
        elif route == "/api/notes":
            renderer = self._renderer()
            self._json(200 if renderer else 404, renderer.notes_payload() if renderer else {"error": "no Atlas"})
        elif route == "/api/health":
            self._json(200, {"app": "repo-rabbit", "repo": str(self.paths.repo), "pid": os.getpid()})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not (self._host_ok() and self._origin_ok()):
            self._json(403, {"error": "cross-origin request refused"})
            return
        if urlsplit(self.path).path != "/api/questions":
            self._json(404, {"error": "not found"})
            return
        # Requiring this type forces a CORS preflight, which this server never approves.
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self._json(415, {"error": "send application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json(411, {"error": "Content-Length required"})
            return
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            self._json(413, {"error": "request too large"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            atlas = read_json(self.paths.atlas)
            if atlas is None:
                raise BadRequest("no Atlas yet")
            record = add_question(self.paths, validate_question(payload, atlas, self.paths.repo))
        except (BadRequest, json.JSONDecodeError, UnicodeDecodeError) as exc:
            message = str(exc) if isinstance(exc, BadRequest) else "the body is not valid JSON"
            self._json(400, {"error": message})
            return
        self._json(201, {"question": record, "notes": Renderer(self.paths, atlas).notes_payload()})


def make_server(paths: Paths, port: int = 0) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"paths": paths})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def serve_forever(paths: Paths, port: int) -> None:
    """Bind, record where we are, and serve until killed."""
    try:
        server = make_server(paths, port)
    except OSError:
        server = make_server(paths, 0)  # The preferred port is taken; any free one will do.
    write_json(paths.server_state, {"pid": os.getpid(), "port": server.server_address[1]})
    try:
        server.serve_forever()
    finally:
        server.server_close()
