"""Drive the real Atlas in Chrome: select, ask, persist, answer, reload, theme, search.

Run against a throwaway copy of a repository, never your real notes:

    uv run --with playwright python tests/e2e_browser.py [SHOT_DIR]

It uses the installed Google Chrome (no browser download), builds the fixture
repository in a temporary directory, and fails on any network request that
leaves 127.0.0.1 or any console error.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from fixtures import GREET, SCRIPTS, atlas, make_branches, make_repo, tour_draft
from playwright.sync_api import sync_playwright

HELPER = str(SCRIPTS / "rabbit.py")


def rabbit(repo: Path, *args: str) -> str:
    done = subprocess.run([sys.executable, HELPER, "--repo", str(repo), *args], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


SELECT = """([blockId, needle]) => {
  const block = document.getElementById('x-' + blockId);
  block.scrollIntoView({block: 'center'});
  const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const at = node.nodeValue.indexOf(needle);
    if (at >= 0) {
      const range = document.createRange();
      range.setStart(node, at); range.setEnd(node, at + needle.length);
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
      document.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
      return true;
    }
  }
  return false;
}"""


def wait(page, expression: str, seconds: float = 6) -> None:
    """Poll with evaluate: wait_for_function needs eval, which the Atlas's CSP rightly forbids."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if page.evaluate(expression):
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"never became true: {expression}")


def main() -> None:
    shots = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo(Path(tmp).resolve() / "repo")
        make_branches(repo)
        draft = Path(tmp) / "draft.json"
        draft.write_text(json.dumps(atlas()))
        rabbit(repo, "ingest", str(draft))
        url = json.loads(rabbit(repo, "serve", "--no-open", "--port", "0"))["url"]
        try:
            run(repo, url, shots)
        finally:
            rabbit(repo, "stop")
    print("e2e passed")


def run(repo: Path, url: str, shots: Path | None) -> None:
    outside: list[str] = []
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        page.on("request", lambda r: outside.append(r.url) if not r.url.startswith((url, "data:")) else None)
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)

        # Navigation between panes and a deep link into a collapsed module.
        page.click('a[href="#s-modules"]')
        assert page.is_visible("#s-modules") and not page.is_visible("#s-overview")
        page.evaluate("location.hash = '#x-mod.other.facts'")
        wait(page, "document.getElementById('x-mod.other').open === true")

        # IDE selector rewrites links.
        page.click('a[href="#s-walkthrough"]')
        link = "#x-wk\\.1 figcaption a.ref"
        assert page.get_attribute(link, "href").startswith("vscode://file/")
        page.select_option("#ide", "idea")
        assert page.get_attribute(link, "href").startswith("idea://open?file=")
        page.select_option("#ide", "cursor")
        assert page.get_attribute(link, "href").startswith("cursor://file/")
        page.select_option("#ide", "vscode")

        # Execution tree jumps to its step.
        page.click(".tree-jump")
        wait(page, "document.getElementById('x-wk.1').classList.contains('flash')")

        # Search finds content in another pane.
        page.fill("#search", "Trace the error")
        page.click("#search-results a")
        assert page.is_visible("#x-wk\\.next\\.l")

        # Ask about prose.
        page.click('a[href="#s-overview"]')
        assert page.evaluate(SELECT, ["ov.purpose.b", "About purpose"])
        page.wait_for_selector("#ask-pop:not([hidden])")
        page.click("#ask-pop")
        page.fill("#ask-text", "What purpose?")
        page.click("#ask-send")
        page.wait_for_selector('mark.note')
        assert page.inner_text("mark.note") == "About purpose"
        assert page.inner_text("#x-ov\\.purpose\\.b + .thread-wrap .note-q") == "What purpose?"
        assert page.inner_text("#notes-count") == "1"

        # Ask about code: source lines are captured.
        page.click('a[href="#s-walkthrough"]')
        assert page.evaluate(SELECT, ["wk.1", "ValueError"])
        page.wait_for_selector("#ask-pop:not([hidden])")
        page.click("#ask-pop")
        page.fill("#ask-text", "When is this raised?")
        page.click("#ask-send")
        wait(page, "document.querySelectorAll('mark.note').length === 2")
        questions = json.loads(rabbit(repo, "questions"))
        by_text = {q["question"]: q for q in questions}
        code_q = by_text["When is this raised?"]
        assert (code_q["source_file"], code_q["source_start"], code_q["source_end"]) == ("app.py", 12, 12), code_q
        assert by_text["What purpose?"]["block_id"] == "ov.purpose.b"

        # The page says what to do next, then shows the agent working, live.
        page.click('a[href="#s-notes"]')
        assert page.evaluate("getComputedStyle(document.getElementById('live')).display") == "none"
        assert "paste this into Claude Code" in page.inner_text("#flow-msg")
        assert "Ask repo-rabbit to investigate" in page.inner_text("#flow-msg code")
        rabbit(repo, "claim", code_q["id"])
        rabbit(repo, "progress", "--id", code_q["id"], "Reading how greet validates")
        page.wait_for_selector("#flow-msg:has-text('Reading how greet validates')", timeout=9000)
        assert "Reading how greet validates" in page.inner_text("#live")
        page.click('a[href="#s-walkthrough"]')
        assert "Reading how greet validates" in page.inner_text("#x-wk\\.1 + .thread-wrap .timeline")
        assert page.inner_text("#x-wk\\.1 + .thread-wrap .status") == "investigating"

        # The agent answers while the page is open: the poll delivers it in place.
        answer = Path(repo / ".repo-rabbit/cache/a.json")
        answer.write_text(json.dumps({"text": "Raised for **empty** names.", "refs": [GREET]}))
        rabbit(repo, "answer", code_q["id"], str(answer))
        page.wait_for_selector("#x-wk\\.1 + .thread-wrap .note-answer strong", timeout=9000)
        assert "app.py:10–13" in page.inner_text("#x-wk\\.1 + .thread-wrap .note-answer")
        assert page.locator("#x-wk\\.1 + .thread-wrap .timeline summary").inner_text() == "How RepoRabbit worked this out"
        if shots:
            page.screenshot(path=str(shots / "e2e-answer.png"))

        # Continue the conversation under the answer; typing survives the live updates.
        thread = "#x-wk\\.1 + .thread-wrap"
        page.click(f"{thread} .followup .link-btn")
        page.fill(f"{thread} .followup textarea", "Half-typed thought")
        rabbit(repo, "progress", "Something unrelated happened")
        page.wait_for_timeout(4800)
        assert page.input_value(f"{thread} .followup textarea") == "Half-typed thought"
        page.fill(f"{thread} .followup textarea", "What about whitespace-only names?")
        page.click(f"{thread} .followup button.send")
        page.wait_for_selector(f"{thread} .note-turn .note-q")
        assert page.inner_text(f"{thread} .note-turn .note-q") == "What about whitespace-only names?"
        assert page.locator(f"{thread} .followup").count() == 0  # Nothing to follow up on until it's answered.
        marks_before = page.locator("mark.note").count()
        pending = next(q for q in json.loads(rabbit(repo, "questions")) if q["question"].startswith("What about"))
        assert pending["root_id"] == code_q["id"] and pending["thread"][0]["question"] == "When is this raised?", pending
        assert "empty" in pending["thread"][0]["answers"][0]
        answer.write_text(json.dumps({"text": "Whitespace passes the **check**.", "refs": [GREET]}))
        rabbit(repo, "answer", pending["id"], str(answer))
        page.wait_for_selector(f"{thread} .note-turn .note-answer strong", timeout=9000)
        assert page.locator("mark.note").count() == marks_before
        assert page.locator(f"{thread} .followup .link-btn").count() == 1
        if shots:
            page.locator(thread).screenshot(path=str(shots / "e2e-followup.png"))

        # A reload, then the panel, then navigation back to the anchor.
        page.reload()
        page.click('a[href="#s-notes"]')
        assert "Answered 1" in page.inner_text("#filters")
        page.click('#filters button:has-text("Answered")')
        assert page.locator("#notes-list .note-card").count() == 1
        page.click("#notes-list .link-btn")
        assert page.is_visible("#x-wk\\.1 + .thread-wrap")

        # Regeneration keeps the answer beside its anchor.
        rabbit(repo, "ingest", str(repo.parent / "draft.json"))
        page.reload()
        page.click('a[href="#s-walkthrough"]')
        assert page.inner_text("mark.note.answered") == "ValueError"

        # Pick a set of branches, then the tour arrives by itself while the page is open.
        page.click('a[href="#s-tours"]')
        page.wait_for_selector("#tour-list input[value='feature-a']")
        assert page.input_value("#tour-base") == "main"
        assert page.locator("#tour-list input[value='main']").count() == 0
        assert page.is_disabled("#tour-go")
        page.fill("#tour-filter", "feature-a")
        assert page.locator("#tour-list label").count() == 1
        page.check("#tour-list input[value='feature-a']")
        page.fill("#tour-filter", "")
        assert page.is_checked("#tour-list input[value='feature-a']")
        page.check("#tour-list input[value='feature-b']")
        assert page.inner_text("#tour-go") == "Tour 2 branches"
        page.click("#tour-go")
        page.wait_for_selector("#tour-status code:has-text('feature-b')")
        assert "tour my selected branches" in page.inner_text("#tour-status")
        queued = json.loads(rabbit(repo, "tours"))
        assert [t["branch"] for t in queued] == ["feature-a", "feature-b"]
        tid = queued[0]["id"]
        head = subprocess.run(["git", "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True).stdout
        rabbit(repo, "tour-prepare", tid)
        rabbit(repo, "progress", "--id", tid, "Reading shout")
        page.wait_for_selector("#tour-status small:has-text('Reading shout')", timeout=9000)
        tour_file = Path(repo / ".repo-rabbit/cache/tour.json")
        tour_file.write_text(json.dumps(tour_draft()))
        rabbit(repo, "tour-ingest", tid, str(tour_file))
        page.wait_for_selector(f"#x-{tid}", timeout=12000)
        assert page.is_visible(f"#x-{tid}")
        assert "/.repo-rabbit/tours/" in page.get_attribute(f"[id='x-{tid}.w.1'] figcaption a.ref", "href")
        assert page.locator(f"[id='x-{tid}.w.1'] .line.changed").count() >= 2
        assert "feature-a" in page.inner_text('.nav-item[data-nav="tours"]')
        page.click(f"[id='x-{tid}.file.0'] summary")
        assert page.locator(f"[id='x-{tid}.file.0'] .line.add").count() >= 1
        assert subprocess.run(["git", "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True).stdout == head

        # Ask about a line that exists only on the branch.
        assert page.evaluate(SELECT, [f"{tid}.w.1", "shout"])
        page.wait_for_selector("#ask-pop:not([hidden])")
        page.click("#ask-pop")
        page.fill("#ask-text", "Why upper-case?")
        page.click("#ask-send")
        wait(page, "document.querySelectorAll('mark.note').length >= 1")
        tour_q = next(q for q in json.loads(rabbit(repo, "questions")) if q["question"] == "Why upper-case?")
        assert tour_q["tour"]["id"] == tid and tour_q["source_file"] == "app.py", tour_q
        if shots:
            page.screenshot(path=str(shots / "e2e-tour.png"))

        # The auto-start control is present, off, and explains why it can't be enabled yet.
        page.click('a[href="#s-notes"]')
        assert page.is_visible("#auto-row") and not page.is_checked("#auto-on")

        # Theme toggle.
        before = page.evaluate("getComputedStyle(document.body).backgroundColor")
        page.click("#theme")
        assert page.evaluate("getComputedStyle(document.body).backgroundColor") != before
        browser.close()
    assert not outside, f"requests left the machine: {outside}"
    assert not errors, f"console errors: {errors}"


if __name__ == "__main__":
    main()
