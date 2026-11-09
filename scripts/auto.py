"""Optional auto-start: run the investigation as soon as a question is saved.

Off by default, and enabled per repository from the Atlas page. The setting lives
in the user's Claude configuration directory, never in the repository, so a
cloned repository can't turn it on. rabbit.py, the helper the agent is allowed to
run, has no way to change it.

A run is ``claude -p`` with the repo-rabbit agent and a fixed prompt. Question
text stays on disk and is never placed on a command line. No permission is added:
headless runs get the same one-time rules as any other, in default mode.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import store
from setup_permissions import rules_for
from store import Paths, atomic_write_text, now_iso

PROMPT = "Investigate my open RepoRabbit questions."
MAX_RUNS_PER_HOUR = 12
RUN_BUDGET_USD = "3"


class AutoError(ValueError):
    pass


def _config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()


def _settings_file() -> Path:
    return _config_dir() / "repo-rabbit-settings.json"


def _read_settings() -> dict:
    try:
        data = json.loads(_settings_file().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def is_enabled(paths: Paths) -> bool:
    entry = _read_settings().get("repos", {}).get(str(paths.repo.resolve()), {})
    return entry.get("auto_investigate") is True


def _write_enabled(paths: Paths, enabled: bool) -> None:
    data = _read_settings()
    repos = data.setdefault("repos", {})
    key = str(paths.repo.resolve())
    if enabled:
        repos[key] = {"auto_investigate": True, "enabled_at": now_iso()}
    else:
        repos.pop(key, None)
    atomic_write_text(_settings_file(), json.dumps(data, indent=2) + "\n")


def _helper_root() -> Path:
    """The copy the agent will use: an installed one wins over one inside the repository."""
    installed = _config_dir() / "repo-rabbit"
    return installed.resolve() if (installed / "scripts" / "rabbit.py").exists() else Path(__file__).resolve().parent.parent


def _allow_rules(paths: Paths) -> set[str]:
    found: set[str] = set()
    for file in (_config_dir() / "settings.json", paths.repo / ".claude" / "settings.json",
                 paths.repo / ".claude" / "settings.local.json"):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            found |= set(data.get("permissions", {}).get("allow", []))
    return found


def unavailable_reason(paths: Paths) -> str | None:
    """Why a headless run could not succeed, in words the user can act on."""
    if shutil.which("claude") is None:
        return "The claude command wasn't found on this server's PATH."
    root = _helper_root()
    if not set(rules_for(root)) <= _allow_rules(paths):
        return (f"RepoRabbit needs its one-time permission rules first. In your terminal, run: "
                f"python3 {root}/scripts/setup_permissions.py")
    return None


class AutoRunner:
    """Runs at most one investigation at a time, and stops when runs stop helping."""

    debounce = 5.0  # Lets a burst of highlights share one run.
    run_timeout = 15 * 60

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._closed = False
        self._process: subprocess.Popen | None = None
        self._started: list[float] = []
        self._stuck: set[str] = set()  # Questions the last failed run couldn't answer.
        self.running = False
        self.last_error: str | None = None
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # -- what the server calls ---------------------------------------------------

    def notify(self) -> None:
        """A question was saved, or auto-start was turned on."""
        self._wake.set()

    def state(self) -> dict:
        reason = unavailable_reason(self.paths)
        return {"enabled": is_enabled(self.paths), "available": reason is None, "reason": reason,
                "running": self.running, "last_error": self.last_error}

    def set_enabled(self, enabled: bool) -> dict:
        if enabled:
            reason = unavailable_reason(self.paths)
            if reason:
                raise AutoError(reason)
        _write_enabled(self.paths, enabled)
        self.last_error = None
        self._stuck = set()
        if enabled:
            self.notify()
        else:
            self._terminate()
        return self.state()

    def close(self) -> None:
        self._closed = True
        self._wake.set()
        self._terminate()

    # -- the worker --------------------------------------------------------------

    def _pending(self) -> set[str]:
        return {q["id"] for q in store.load_questions(self.paths) if q.get("status") in ("open", "investigating")}

    def _loop(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            if self._closed:
                return
            time.sleep(self.debounce)
            try:
                self._drain()
            except OSError as exc:
                self.last_error = f"Could not start RepoRabbit: {exc}"
            finally:
                self.running = False

    def _drain(self) -> None:
        while is_enabled(self.paths) and not self._closed:
            before = self._pending()
            if not before or before <= self._stuck:  # Only a new question justifies another try.
                return
            self._started = [t for t in self._started if time.time() - t < 3600]
            if len(self._started) >= MAX_RUNS_PER_HOUR:
                self.last_error = f"Paused after {MAX_RUNS_PER_HOUR} runs in an hour. Ask in Claude Code to continue."
                return
            self._started.append(time.time())
            self.running = True
            store.log_activity(self.paths, f"Auto-start: RepoRabbit is picking up {len(before)} question(s)")
            problem = self._run_once()
            self.running = False
            if not is_enabled(self.paths) or self._closed:
                return
            if not before - self._pending():
                self._stuck = before
                self.last_error = problem or "The last run finished without answering anything, so auto-start stopped there."
                store.log_activity(self.paths, f"Auto-start stopped: {self.last_error}"[:300])
                return
            self.last_error = None
            self._stuck = set()

    def _command(self) -> list[str]:
        claude = shutil.which("claude")
        if claude is None:
            raise OSError("the claude command wasn't found")
        command = [claude, "-p", "--agent", "repo-rabbit", "--permission-mode", "default"]
        if _supports_budget(claude):
            command += ["--max-budget-usd", RUN_BUDGET_USD]
        return [*command, "--", PROMPT]

    def _run_once(self) -> str | None:
        """Returns a description of what went wrong, if anything."""
        log = self.paths.cache / "auto-run.log"
        with open(log, "wb") as output:
            process = subprocess.Popen(self._command(), cwd=str(self.paths.repo), stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        with self._lock:
            self._process = process
        try:
            code = process.wait(timeout=self.run_timeout)
        except subprocess.TimeoutExpired:
            self._terminate()
            return f"The run took longer than {self.run_timeout // 60} minutes and was stopped."
        finally:
            with self._lock:
                self._process = None
        return None if code == 0 else f"The run exited with code {code}. Details are in .repo-rabbit/cache/auto-run.log."

    def _terminate(self) -> None:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass


_budget_support: dict[str, bool] = {}


def _supports_budget(claude: str) -> bool:
    if claude not in _budget_support:
        try:
            done = subprocess.run([claude, "--help"], capture_output=True, text=True, timeout=30, check=False)
            _budget_support[claude] = "--max-budget-usd" in done.stdout
        except (OSError, subprocess.TimeoutExpired):
            _budget_support[claude] = False
    return _budget_support[claude]
