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

from fixtures import GREET, SCRIPTS, atlas, make_repo
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

        # The agent answers while the page is open: the poll delivers it in place.
        answer = Path(repo / ".repo-rabbit/cache/a.json")
        answer.write_text(json.dumps({"text": "Raised for **empty** names.", "refs": [GREET]}))
        rabbit(repo, "answer", code_q["id"], str(answer))
        page.wait_for_selector("#x-wk\\.1 + .thread-wrap .note-answer strong", timeout=9000)
        assert "app.py:10–13" in page.inner_text("#x-wk\\.1 + .thread-wrap .note-answer")
        if shots:
            page.screenshot(path=str(shots / "e2e-answer.png"))

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

        # Theme toggle.
        before = page.evaluate("getComputedStyle(document.body).backgroundColor")
        page.click("#theme")
        assert page.evaluate("getComputedStyle(document.body).backgroundColor") != before
        browser.close()
    assert not outside, f"requests left the machine: {outside}"
    assert not errors, f"console errors: {errors}"


if __name__ == "__main__":
    main()
