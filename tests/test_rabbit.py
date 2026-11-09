"""RepoRabbit's automated tests. Standard library only:

    python3 -m unittest discover -s tests
"""

from __future__ import annotations

import contextlib
import html
import http.client
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from fixtures import APP, GREET, SCRIPTS, atlas, make_repo

import rabbit
import renderer
import server
import store
import validate
from store import Paths

INSTALL = SCRIPTS.parent / "install.sh"


class RepoCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = make_repo(Path(tmp.name).resolve() / "repo")
        self.paths = Paths(self.repo)
        self.tmp = Path(tmp.name).resolve()

    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rabbit.main(["--repo", str(self.repo), *args])
        return code, out.getvalue(), err.getvalue()

    def write(self, name: str, value) -> str:
        path = self.tmp / name
        path.write_text(json.dumps(value))
        return str(path)

    def ingest(self, data: dict | None = None) -> None:
        code, out, err = self.cli("ingest", self.write("draft.json", data or atlas()))
        self.assertEqual(code, 0, out + err)

    def stored(self) -> dict:
        return json.loads(self.paths.atlas.read_text())

    def ask(self, block_id: str = "ov.purpose.b", text: str = "About purpose", section: str = "overview") -> dict:
        return store.add_question(self.paths, {
            "question": "Why?", "section_id": section, "block_id": block_id, "selected_text": text})

    def edit_app(self, old: str, new: str) -> None:
        path = self.repo / "app.py"
        path.write_text(path.read_text().replace(old, new))


class References(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.index = validate.SourceIndex(self.repo)

    def test_anchor_resolves_to_real_lines(self) -> None:
        out = validate.resolve_ref(GREET, self.index)
        self.assertEqual((out["start"], out["end"]), (10, 13))

    def test_explicit_lines_are_checked_against_the_anchor(self) -> None:
        with self.assertRaisesRegex(validate.RefError, "not in that range"):
            validate.resolve_ref({"path": "app.py", "start": 1, "end": 2, "anchor": "def greet"}, self.index)

    def test_ambiguous_anchor_is_refused(self) -> None:
        with self.assertRaisesRegex(validate.RefError, "matches 5 lines"):
            validate.resolve_ref({"path": "app.py", "anchor": "name"}, self.index)

    def test_invented_targets_are_refused(self) -> None:
        cases = [
            ({"path": "nope.py", "start": 1}, "does not exist"),
            ({"path": "app.py", "start": 999}, "outside the file"),
            ({"path": "app.py", "anchor": "def invented("}, "not on any line"),
            ({"path": "app.py", "start": 5, "end": 2}, "start is after end"),
        ]
        for bad, message in cases:
            with self.subTest(bad), self.assertRaisesRegex(validate.RefError, message):
                validate.resolve_ref(bad, self.index)

    def test_paths_cannot_leave_the_repository(self) -> None:
        (self.tmp / "secret.txt").write_text("x\n")
        os.symlink(self.tmp / "secret.txt", self.repo / "link.txt")
        for bad in ("../secret.txt", "/etc/passwd", "a/../app.py", "link.txt", "app.py\x00"):
            with self.subTest(bad), self.assertRaises(validate.RefError):
                validate.resolve_ref({"path": bad, "start": 1}, self.index)

    def test_credentials_files_cannot_be_cited(self) -> None:
        (self.repo / ".env").write_text("A=1\n")
        with self.assertRaisesRegex(validate.RefError, "credentials"):
            validate.resolve_ref({"path": ".env", "start": 1}, self.index)
        self.assertFalse(validate.is_sensitive_path("lambda.env.example"))
        self.assertTrue(validate.is_sensitive_path("lambda.env"))

    def test_ide_links_are_encoded(self) -> None:
        urls = renderer.ide_urls("/Users/me/my repo#1/a b.py", 42, 3)
        self.assertEqual(urls["vscode"], "vscode://file/Users/me/my%20repo%231/a%20b.py:42:3")
        self.assertEqual(urls["cursor"], "cursor://file/Users/me/my%20repo%231/a%20b.py:42:3")
        self.assertEqual(urls["idea"], "idea://open?file=%2FUsers%2Fme%2Fmy%20repo%231%2Fa%20b.py&line=42")

    def test_ref_state_tracks_edits(self) -> None:
        stored = validate.resolve_ref(GREET, self.index)
        self.assertEqual(validate.ref_state(stored, self.index)[0], "fresh")

        self.edit_app("import json", "import json\nimport sys\nimport os")
        moved = validate.ref_state(stored, validate.SourceIndex(self.repo))
        self.assertEqual(moved, ("moved", 12, 15))

        self.edit_app('raise ValueError("name required")', 'raise TypeError("nope")')
        self.assertEqual(validate.ref_state(stored, validate.SourceIndex(self.repo))[0], "changed")

        self.edit_app("def greet(name):", "def salute(who):")
        self.assertEqual(validate.ref_state(stored, validate.SourceIndex(self.repo))[0], "lost")

        (self.repo / "app.py").unlink()
        self.assertEqual(validate.ref_state(stored, validate.SourceIndex(self.repo))[0], "missing")


class Validation(RepoCase):
    def errors(self, data: dict) -> list[str]:
        return validate.validate_draft(data, self.repo).errors

    def test_the_fixture_is_valid(self) -> None:
        self.assertEqual(self.errors(atlas()), [])

    def test_uncited_claim_is_refused(self) -> None:
        data = atlas()
        del data["sections"][0]["groups"][0]["blocks"][0]["refs"]
        self.assertTrue(any("needs at least one source reference" in e for e in self.errors(data)))

    def test_inference_also_needs_evidence(self) -> None:
        data = atlas()
        block = data["sections"][0]["groups"][0]["blocks"][0]
        block["certainty"] = "inferred"
        del block["refs"]
        self.assertTrue(any("inferred claim" in e for e in self.errors(data)))

    def test_missing_required_topic_is_refused(self) -> None:
        data = atlas()
        data["sections"][3]["groups"].pop(0)
        self.assertTrue(any("topic 'languages'" in e for e in self.errors(data)))

    def test_module_needs_every_fact(self) -> None:
        data = atlas()
        data["sections"][2]["groups"][0]["blocks"][0]["items"].pop()
        self.assertTrue(any("key 'debug'" in e for e in self.errors(data)))

    def test_duplicate_ids_and_dangling_links_are_refused(self) -> None:
        data = atlas()
        data["sections"][1]["groups"][1]["blocks"][0]["id"] = "ov.purpose.b"
        data["sections"][1]["intro"] = "See [[does.not.exist|there]]."
        errors = self.errors(data)
        self.assertTrue(any("duplicate id" in e for e in errors))
        self.assertTrue(any("unknown id" in e for e in errors))

    def test_tree_target_must_be_a_step(self) -> None:
        data = atlas()
        data["sections"][5]["groups"][2]["blocks"][0]["root"]["children"][0]["target"] = "ov.purpose.b"
        self.assertTrue(any("is not a step" in e for e in self.errors(data)))

    def test_tree_nodes_need_a_source(self) -> None:
        data = atlas()
        del data["sections"][5]["groups"][2]["blocks"][0]["root"]["children"][1]["ref"]
        self.assertTrue(any("needs a ref or a step target" in e for e in self.errors(data)))

    def test_schema_reports_the_offending_field(self) -> None:
        data = atlas()
        data["sections"][0]["groups"][0]["blocks"][0]["txt"] = "typo"
        self.assertTrue(any("unknown field 'txt'" in e for e in self.errors(data)))

    def test_secrets_are_refused_and_redacted(self) -> None:
        data = atlas()
        token = "ghp_" + "a1B2" * 10
        data["project"]["one_liner"] = f"uses {token}"
        self.assertTrue(any("credential" in e for e in self.errors(data)))
        self.assertNotIn(token, validate.redact(f'TOKEN = "{token}"'))
        self.assertIn("[REDACTED]", validate.redact('api_key = "abc123def456ghi789jkl"'))
        self.assertEqual(validate.redact("api_key = os.environ['API_KEY_NAME']"),
                         "api_key = os.environ['API_KEY_NAME']")


class Ingest(RepoCase):
    def test_bad_reference_saves_nothing(self) -> None:
        data = atlas()
        data["sections"][0]["groups"][0]["blocks"][0]["refs"] = [{"path": "ghost.py", "start": 1}]
        code, out, _ = self.cli("ingest", self.write("d.json", data))
        self.assertEqual(code, 1)
        self.assertIn("ghost.py", out)
        self.assertFalse(self.paths.atlas.exists())

    def test_ingest_writes_every_artifact(self) -> None:
        self.ingest()
        manifest = json.loads(self.paths.manifest.read_text())
        self.assertIn("app.py", manifest["files"])
        self.assertTrue(manifest["revision"]["head"])
        self.assertIn("ov.purpose.b", manifest["blocks"])
        self.assertEqual(manifest["analyzed_modules"], ["mod.app", "mod.other"])
        self.assertEqual(self.cli("validate", "--sample", "5")[0], 0)

    def test_draft_directory_is_merged(self) -> None:
        data = atlas()
        folder = self.tmp / "draft"
        folder.mkdir()
        (folder / "meta.json").write_text(json.dumps({k: data[k] for k in ("project", "coverage", "next_steps")}))
        for section in data["sections"]:
            (folder / f"{section['id']}.json").write_text(json.dumps(section))
        self.assertEqual(self.cli("ingest", str(folder))[0], 0)

    def test_gitignore_is_appended_once_and_preserved(self) -> None:
        self.ingest()
        self.cli("init")
        lines = (self.repo / ".gitignore").read_text().splitlines()
        self.assertEqual(lines, ["*.log", ".repo-rabbit/"])

    def test_rewriting_gitignore_keeps_its_permissions(self) -> None:
        os.chmod(self.repo / ".gitignore", 0o644)
        self.cli("init")
        self.assertEqual((self.repo / ".gitignore").stat().st_mode & 0o777, 0o644)

    def test_no_trailing_newline_is_handled(self) -> None:
        (self.repo / ".gitignore").write_text("*.tmp")
        self.cli("init")
        self.assertEqual((self.repo / ".gitignore").read_text(), "*.tmp\n.repo-rabbit/\n")

    def test_scan_excludes_secrets_and_never_writes_outside(self) -> None:
        (self.repo / ".env").write_text("TOKEN=x\n")
        code, out, _ = self.cli("scan")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertIn(".env", report["excluded_as_sensitive"])
        status = subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"],
                                capture_output=True, text=True, check=True).stdout.split("\n")
        self.assertEqual(sorted(s for s in status if s), [" M .gitignore", "?? .env"])

    def test_scan_reports_truncation(self) -> None:
        _, out, _ = self.cli("scan", "--max-files", "1")
        self.assertTrue(json.loads(out)["budget"]["truncated"])


class Rendering(RepoCase):
    def page(self) -> str:
        return self.paths.index.read_text()

    def test_all_sections_diagrams_and_snippets_render(self) -> None:
        self.ingest()
        page = self.page()
        for name in ("The Burrow", "Tunnel Map", "Explore the Tunnels", "Toolkit", "Follow the Rabbit",
                     "Down the Rabbit Hole", "Rabbit Notes"):
            self.assertIn(name, page)
        self.assertEqual(page.count("<svg"), 1 + 1 + 3)  # brand, component map, three sequences
        self.assertIn('class="t-keyword">def</span>', page)
        self.assertIn('data-line="10"', page)
        self.assertIn(f"vscode://file{self.repo}/app.py:10:1", page)
        self.assertIn("app.py:10–13", page)

    def test_page_has_no_external_resources(self) -> None:
        self.ingest()
        page = self.page()
        self.assertEqual(re.findall(r"""(?:src|href)=["']https?:""", page), [])
        self.assertIsNone(re.search(r"<link[^>]+stylesheet", page))
        self.assertIn("default-src 'none'", html.unescape(page))

    def test_csp_hash_matches_the_inline_script(self) -> None:
        import base64
        import hashlib
        self.ingest()
        page = self.page()
        body = re.search(r'<script id="rr-main">([\s\S]*?)</script>', page).group(1)
        expected = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        self.assertIn(f"sha256-{expected}", page)

    def test_repository_text_is_escaped(self) -> None:
        evil = '<img src=x onerror=alert(1)>"\'</script>'
        (self.repo / "app.py").write_text(APP + f"\n# {evil}\n")
        data = atlas()
        marker = len((APP + f"\n# {evil}\n").splitlines())
        data["project"]["name"] = evil[:100]
        data["sections"][0]["intro"] = evil
        data["sections"][1]["groups"][0]["blocks"][0]["nodes"][0]["label"] = evil[:50]
        data["sections"][4]["groups"][0]["blocks"][1]["messages"][0]["label"] = evil
        data["sections"][0]["groups"][0]["blocks"].append(
            {"id": "evil.code", "type": "code", "ref": {"path": "app.py", "start": marker}, "caption": evil})
        self.ingest(data)
        self.assertNotIn("<img src=x", self.page())
        self.assertEqual(self.page().count("</script>"), 3)

    def test_json_islands_cannot_close_their_script(self) -> None:
        self.ingest()
        self.ask()
        store.update_questions(self.paths, {store.load_questions(self.paths)[0]["id"]: {"question": "</script><b>"}})
        rabbit.render_page(self.paths)
        self.assertNotIn("</script><b>", self.page())

    def test_snippets_redact_secrets(self) -> None:
        token = "ghp_" + "a1B2" * 10
        path = self.repo / "config.py"
        path.write_text(f'GITHUB_TOKEN = "{token}"\n')
        data = atlas()
        data["sections"][0]["groups"][0]["blocks"].append(
            {"id": "cfg.code", "type": "code", "ref": {"path": "config.py", "start": 1}})
        self.ingest(data)
        self.assertNotIn(token, self.page())
        self.assertIn("[REDACTED]", self.page())

    def test_inline_markup(self) -> None:
        self.assertEqual(renderer.inline("*series* and **bold**"), "<em>series</em> and <strong>bold</strong>")
        self.assertEqual(renderer.inline("a * b * c, 2*3*4"), "a * b * c, 2*3*4")
        self.assertEqual(renderer.inline("`*not*`"), "<code>*not*</code>")

    def test_unlabeled_links_show_the_target_title(self) -> None:
        data = atlas()
        data["sections"][0]["intro"] = "See [[wk.1]]."
        self.ingest(data)
        self.assertIn('href="#x-wk.1">Step 1: Call greet</a>', self.paths.index.read_text())

    def test_tree_shows_the_source_of_its_target_step(self) -> None:
        self.ingest()
        tree = self.paths.index.read_text().split('data-block-id="wk.tree.t"')[1].split("</ul>")[0]
        self.assertIn("app.py:10–13", tree)

    def test_highlighter_closes_multiline_tokens(self) -> None:
        lines = renderer.highlight_lines('"""one\ntwo"""\nx = 1', "a.py")
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertEqual(line.count("<span"), line.count("</span>"))

    def test_cyclic_graphs_still_lay_out(self) -> None:
        block = {"id": "c", "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}, {"id": "c", "label": "C"}],
                 "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}, {"from": "c", "to": "a"},
                           {"from": "a", "to": "c"}, {"from": "b", "to": "b"}]}
        self.assertIn("<svg", renderer.component_svg(block, set()))


class Notes(RepoCase):
    def answer(self, qid: str, body: dict) -> tuple[int, str, str]:
        return self.cli("answer", qid, self.write("answer.json", body))

    def question(self, qid: str) -> dict:
        return next(q for q in store.load_questions(self.paths) if q["id"] == qid)

    def test_answer_is_saved_before_status_changes(self) -> None:
        self.ingest()
        qid = self.ask()["id"]
        code, out, err = self.answer(qid, {"text": "Because `greet`.", "refs": [GREET]})
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.question(qid)["status"], "answered")
        saved = store.load_answers(self.paths)
        self.assertEqual(saved[0]["refs"][0]["start"], 10)
        self.assertIn(qid, self.paths.index.read_text())

    def test_bad_reference_leaves_question_open(self) -> None:
        self.ingest()
        qid = self.ask()["id"]
        code, _, err = self.answer(qid, {"text": "x", "refs": [{"path": "ghost.py", "start": 1}]})
        self.assertEqual(code, 2)
        self.assertIn("stays open", err)
        self.assertEqual(self.question(qid)["status"], "open")
        self.assertEqual(store.load_answers(self.paths), [])

    def test_uncited_answer_is_refused(self) -> None:
        self.ingest()
        qid = self.ask()["id"]
        self.assertEqual(self.answer(qid, {"text": "Trust me."})[0], 2)
        self.assertEqual(self.question(qid)["status"], "open")

    def test_failed_append_leaves_question_open(self) -> None:
        self.ingest()
        qid = self.ask()["id"]
        original = store._append_jsonl
        def boom(*_a, **_k):
            raise OSError("disk full")
        store._append_jsonl = boom
        self.addCleanup(setattr, store, "_append_jsonl", original)
        with self.assertRaises(OSError):
            store.record_answer(self.paths, qid, {"text": "x", "refs": []})
        store._append_jsonl = original
        self.assertEqual(self.question(qid)["status"], "open")

    def test_failure_is_recorded(self) -> None:
        self.ingest()
        qid = self.ask()["id"]
        self.assertEqual(self.cli("fail", qid, "--reason", "code absent")[0], 0)
        self.assertEqual(self.question(qid)["error"], "code absent")

    def test_answers_survive_regeneration(self) -> None:
        self.ingest()
        qid = self.ask()["id"]
        self.answer(qid, {"text": "Original answer.", "refs": [GREET]})
        self.ingest()
        self.assertEqual(len(store.load_answers(self.paths)), 1)
        self.assertEqual(self.question(qid)["status"], "answered")
        self.assertEqual(self.question(qid)["anchor"], "ok")
        self.assertIn("Original answer.", self.paths.index.read_text())

    def test_removed_block_orphans_but_keeps_the_note(self) -> None:
        self.ingest()
        qid = self.ask()["id"]
        data = atlas()
        data["sections"][0]["groups"][0]["blocks"][0]["id"] = "ov.purpose.renamed"
        self.ingest(data)
        self.assertEqual(self.question(qid)["anchor"], "orphaned")
        self.ingest()
        self.assertEqual(self.question(qid)["anchor"], "ok")

    def test_changed_selection_is_marked_drifted(self) -> None:
        self.ingest()
        qid = self.ask(text="words that are not there")["id"]
        rabbit.render_page(self.paths)
        self.assertEqual(self.question(qid)["anchor"], "drifted")

    def test_torn_final_line_loses_nothing(self) -> None:
        self.ingest()
        first = self.ask()["id"]
        with open(self.paths.questions, "a") as f:
            f.write('{"id": "torn')
        second = self.ask()["id"]
        known = {q["id"] for q in store.load_questions(self.paths)}
        self.assertEqual(known, {first, second})
        store.update_questions(self.paths, {first: {"status": "failed"}})
        self.assertIn("torn", self.paths.quarantine.read_text())

    def test_concurrent_writers_do_not_lose_records(self) -> None:
        self.ingest()
        threads = [threading.Thread(target=self.ask) for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(store.load_questions(self.paths)), 40)

    def test_questions_carry_context(self) -> None:
        self.ingest()
        self.ask()
        rows = json.loads(self.cli("questions")[1])
        self.assertEqual(rows[0]["context"]["block_refs"], ["app.py:10-13"])
        self.assertIn("About purpose", rows[0]["context"]["block_text"])


class Refresh(RepoCase):
    def plan(self, *args: str) -> dict:
        code, out, err = self.cli("refresh", *args)
        self.assertEqual(code, 0, out + err)
        return json.loads(out)

    def block(self, data: dict, block_id: str) -> dict:
        return next(b for _, _, b in validate.iter_blocks(data) if b["id"] == block_id)

    def test_no_changes_is_a_no_op(self) -> None:
        self.ingest()
        before = self.stored()
        plan = self.plan()
        self.assertEqual((plan["changed_files"], plan["stale_blocks"]), ([], {}))
        self.assertEqual(self.stored(), before)

    def test_moved_lines_follow_without_going_stale(self) -> None:
        self.ingest()
        self.edit_app("import json", "import json\nimport sys")
        plan = self.plan()
        self.assertEqual(plan["changed_files"], ["app.py"])
        self.assertEqual(plan["stale_blocks"], {})
        self.assertGreater(plan["references_moved"], 0)
        self.assertEqual(self.block(self.stored(), "ov.purpose.b")["refs"][0]["start"], 11)
        self.assertEqual(self.cli("validate")[0], 0)

    def test_changed_code_marks_blocks_stale_and_keeps_others(self) -> None:
        self.ingest()
        qid = self.ask()["id"]
        store.record_answer(self.paths, qid, {"text": "kept", "refs": [], "certainty": "context"})
        self.edit_app('raise ValueError("name required")', 'raise TypeError("needed")')
        plan = self.plan()
        self.assertIn("ov.purpose.b", plan["stale_blocks"])
        self.assertNotIn("ov.start.list.x", plan["stale_blocks"])
        self.assertIn("mod.app", plan["groups_with_changed_files"])
        data = self.stored()
        self.assertTrue(self.block(data, "ov.purpose.b")["stale"])
        self.assertNotIn("stale", self.block(data, "wk.next.l"))
        self.assertIn("May be out of date", self.paths.index.read_text())
        self.assertEqual(len(store.load_answers(self.paths)), 1)
        self.assertEqual(self.cli("validate")[0], 0)  # Known-stale is a warning, not a failure.

    def test_stale_marks_persist_until_reviewed(self) -> None:
        self.ingest()
        self.edit_app('raise ValueError("name required")', 'raise TypeError("needed")')
        self.plan()
        plan = self.plan()
        self.assertEqual(plan["changed_files"], [])
        self.assertIn("ov.purpose.b", plan["stale_blocks"])

    def test_patch_and_confirm_clear_stale(self) -> None:
        self.ingest()
        self.edit_app('raise ValueError("name required")', 'raise TypeError("needed")')
        self.plan()
        new_block = {"id": "ov.purpose.b", "type": "prose", "text": "Rewritten.", "refs": [GREET]}
        patch = {"ops": [{"op": "replace_block", "id": "ov.purpose.b", "block": new_block},
                         {"op": "confirm_block", "id": "wk.1"}]}
        code, out, err = self.cli("patch", self.write("p.json", patch))
        self.assertEqual(code, 0, out + err)
        data = self.stored()
        self.assertEqual(self.block(data, "ov.purpose.b")["text"], "Rewritten.")
        self.assertNotIn("stale", self.block(data, "wk.1"))
        self.assertIn("stale", self.block(data, "wf.1.facts"))
        refreshed = self.plan()
        self.assertNotIn("wk.1", refreshed["stale_blocks"])
        self.assertNotIn("ov.purpose.b", refreshed["stale_blocks"])

    def test_invalid_patch_changes_nothing(self) -> None:
        self.ingest()
        before = self.paths.atlas.read_bytes()
        patch = {"ops": [
            {"op": "remove_block", "id": "wk.tree.t"},
            {"op": "replace_block", "id": "ov.purpose.b", "block": {
                "id": "ov.purpose.b", "type": "prose", "text": "x", "refs": [{"path": "ghost.py", "start": 1}]}},
        ]}
        self.assertEqual(self.cli("patch", self.write("p.json", patch))[0], 1)
        self.assertEqual(self.paths.atlas.read_bytes(), before)

    def test_replace_block_must_keep_its_id(self) -> None:
        self.ingest()
        patch = {"ops": [{"op": "replace_block", "id": "ov.purpose.b", "block": {
            "id": "other", "type": "prose", "text": "x", "refs": [GREET]}}]}
        self.assertEqual(self.cli("patch", self.write("p.json", patch))[0], 2)

    def test_deleted_file_is_reported_and_marked(self) -> None:
        self.ingest()
        (self.repo / "README.md").unlink()
        plan = self.plan()
        self.assertEqual(plan["deleted_files"], ["README.md"])
        self.assertIn("ov.start.list", plan["stale_blocks"])

    def test_answer_citations_show_staleness(self) -> None:
        self.ingest()
        qid = self.ask()["id"]
        self.cli("answer", qid, self.write("a.json", {"text": "x", "refs": [GREET]}))
        self.edit_app('raise ValueError("name required")', 'raise TypeError("needed")')
        atlas_now = self.stored()
        notes = renderer.Renderer(self.paths, atlas_now).notes_payload()
        self.assertTrue(notes["answers"][qid][0]["stale"])

    def test_dependents_are_found_through_the_diagram(self) -> None:
        self.ingest()
        self.assertEqual(rabbit.group_dependents(self.stored(), {"mod.other"}), {"mod.app"})


class Server(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.ingest()
        self.httpd = server.make_server(self.paths, 0)
        self.port = self.httpd.server_address[1]
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, method: str, path: str, body=None, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        final = {"Content-Type": "application/json"} if body is not None else {}
        final.update(headers or {})
        data = json.dumps(body) if isinstance(body, dict) else body
        conn.request(method, path, body=data, headers=final)
        response = conn.getresponse()
        return response.status, response.read()

    def payload(self, **changes) -> dict:
        return {"question": "Why?", "section_id": "overview", "block_id": "ov.purpose.b",
                "selected_text": "About purpose", **changes}

    def test_binds_to_loopback_only(self) -> None:
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")

    def test_serves_the_atlas(self) -> None:
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"RepoRabbit", body)

    def test_nothing_else_on_disk_is_reachable(self) -> None:
        for path in ("/atlas.json", "/../app.py", "/..%2f..%2fapp.py", "/etc/passwd", "/questions.jsonl"):
            with self.subTest(path):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_question_persists_with_every_field(self) -> None:
        status, body = self.request("POST", "/api/questions", self.payload(
            text_offset=3, source_file="app.py", source_start=10, source_end=13))
        self.assertEqual(status, 201, body)
        saved = store.load_questions(self.paths)[0]
        self.assertTrue(saved["id"].startswith("q_"))
        self.assertEqual(saved["block_id"], "ov.purpose.b")
        self.assertEqual(saved["section_id"], "overview")
        self.assertEqual((saved["source_file"], saved["source_start"], saved["source_end"]), ("app.py", 10, 13))
        self.assertEqual(saved["status"], "open")
        self.assertTrue(saved["created_at"])

    def test_note_does_not_enter_the_investigation_queue(self) -> None:
        self.request("POST", "/api/questions", self.payload(kind="note"))
        self.assertEqual(store.load_questions(self.paths)[0]["status"], "noted")
        self.assertEqual(json.loads(self.cli("questions")[1]), [])

    def test_invalid_payloads_are_refused(self) -> None:
        cases = {
            "unknown block": self.payload(block_id="nope"),
            "wrong section": self.payload(section_id="stack"),
            "bad id": self.payload(block_id="../../x"),
            "empty question": self.payload(question="  "),
            "huge question": self.payload(question="x" * 5000),
            "traversal": self.payload(source_file="../secret", source_start=1, source_end=1),
            "absolute": self.payload(source_file="/etc/passwd", source_start=1, source_end=1),
            "lines out of range": self.payload(source_file="app.py", source_start=1, source_end=9999),
            "lines not ints": self.payload(source_file="app.py", source_start="1", source_end=2),
            "bad kind": self.payload(kind="exec"),
        }
        for name, body in cases.items():
            with self.subTest(name):
                self.assertEqual(self.request("POST", "/api/questions", body)[0], 400)
        self.assertEqual(self.request("POST", "/api/questions", "not json")[0], 400)
        self.assertEqual(store.load_questions(self.paths), [])

    def test_unknown_fields_are_dropped(self) -> None:
        self.request("POST", "/api/questions", self.payload(status="answered", id="q_mine", extra="x"))
        saved = store.load_questions(self.paths)[0]
        self.assertEqual(saved["status"], "open")
        self.assertNotEqual(saved["id"], "q_mine")
        self.assertNotIn("extra", saved)

    def test_cross_origin_and_rebinding_are_refused(self) -> None:
        body = self.payload()
        self.assertEqual(self.request("POST", "/api/questions", body, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/questions", body, {"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/questions", json.dumps(body), {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(store.load_questions(self.paths), [])

    def test_oversized_body_is_refused(self) -> None:
        status, _ = self.request("POST", "/api/questions", "x" * (server.MAX_BODY + 1))
        self.assertEqual(status, 413)

    def test_notes_feed_includes_answers(self) -> None:
        qid = self.ask()["id"]
        self.cli("answer", qid, self.write("a.json", {"text": "**Bold** answer.", "refs": [GREET]}))
        _, body = self.request("GET", "/api/notes")
        notes = json.loads(body)
        self.assertIn("<strong>Bold</strong>", notes["answers"][qid][0]["html"])
        self.assertIn("vscode://file", notes["answers"][qid][0]["html"])


class Install(RepoCase):
    def test_install_works_from_another_repository(self) -> None:
        home = self.tmp / "home"
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(home)}
        subprocess.run(["bash", str(INSTALL)], check=True, env=env, capture_output=True)
        helper = home / "repo-rabbit" / "scripts" / "rabbit.py"
        self.assertTrue((home / "agents" / "repo-rabbit.md").exists())
        self.assertTrue((home / "skills" / "repo-rabbit" / "SKILL.md").exists())
        other = make_repo(self.tmp / "other")
        draft = self.tmp / "draft.json"
        draft.write_text(json.dumps(atlas()))
        done = subprocess.run(["python3", str(helper), "ingest", str(draft)], cwd=other, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue((other / ".repo-rabbit" / "index.html").exists())
        self.assertIn(f"vscode://file{other}/app.py", (other / ".repo-rabbit" / "index.html").read_text())
        self.assertFalse((home / "repo-rabbit" / ".repo-rabbit").exists())

        subprocess.run(["bash", str(INSTALL), "--uninstall"], check=True, env=env, capture_output=True)
        self.assertFalse((home / "agents" / "repo-rabbit.md").exists())
        self.assertTrue((other / ".repo-rabbit" / "index.html").exists())

    def test_project_install_stays_inside_the_project(self) -> None:
        other = make_repo(self.tmp / "other")
        home = self.tmp / "untouched"
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(home)}
        subprocess.run(["bash", str(INSTALL), "--project", str(other)], check=True, env=env, capture_output=True)
        self.assertTrue((other / ".claude" / "agents" / "repo-rabbit.md").exists())
        self.assertTrue((other / ".claude" / "repo-rabbit" / "scripts" / "rabbit.py").exists())
        self.assertFalse(home.exists())
        subprocess.run(["bash", str(INSTALL), "--uninstall", "--project", str(other)], check=True, env=env, capture_output=True)
        self.assertFalse((other / ".claude" / "agents" / "repo-rabbit.md").exists())

    def test_agent_definition_has_required_frontmatter(self) -> None:
        text = (SCRIPTS.parent / "agents" / "repo-rabbit.md").read_text()
        head = text.split("---")[1]
        self.assertRegex(head, r"(?m)^name: repo-rabbit$")
        self.assertRegex(head, r"(?m)^description: .{40,}$")

    def test_non_git_directories_work(self) -> None:
        plain = make_repo(self.tmp / "plain", git=False)
        self.repo, self.paths = plain, Paths(plain)
        self.ingest()
        self.assertTrue(self.paths.index.exists())
        self.assertFalse((plain / ".gitignore").read_text().count(".repo-rabbit"))


if __name__ == "__main__":
    unittest.main()
