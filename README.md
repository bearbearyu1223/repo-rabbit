<p align="center"><img src="assets/logo.svg" width="96" alt="RepoRabbit logo: a rabbit silhouette with one ear up and one flopped over"></p>

<h1 align="center">RepoRabbit</h1>

<p align="center"><em>Every codebase is a rabbit hole. Know where you're going.</em></p>

RepoRabbit is a [Claude Code](https://claude.com/claude-code) subagent that explores an unfamiliar repository and builds an interactive map of it, called an **Atlas**. The Atlas explains what the system does and how it fits together, traces real execution paths, and links every code claim to the exact lines behind it.

When something is confusing, highlight it and ask. The next time you open the Atlas, the answer is waiting exactly where you asked.

## What you get

| Section | What it answers |
|---|---|
| **The Burrow** (Overview) | What is this, who uses it, how do I run it, and which files should I read first? |
| **Tunnel Map** (Architecture) | What are the components and boundaries, and which design decisions matter? |
| **Explore the Tunnels** (Modules) | What does each module own, who calls it, and where do I start debugging it? |
| **Toolkit** (Tech Stack) | What is used, and is it really used, or only declared? |
| **Follow the Rabbit** (Workflows) | What runs, in what order, when something happens? |
| **Down the Rabbit Hole** (Walkthroughs) | One real use case, step by step, with code, data changes and an execution tree |
| **Side Tunnels** (Branch Tours) | What does this branch change, what does that affect, and how does the new code run? |
| **Rabbit Notes** (Q&A) | Your questions and notes, and every answer so far |

Each claim is labeled as read in source, inferred, asserted by a test, taken from docs, or searched for and not found, so you can tell facts from educated guesses.

## Install

You need Claude Code and Python 3.9 or newer. There are no Python packages to install and no API key to configure beyond your Claude Code login.

```bash
git clone https://github.com/bearbearyu1223/repo-rabbit.git
cd repo-rabbit
./install.sh
```

This installs RepoRabbit for every repository on your machine, under `~/.claude` (or `$CLAUDE_CONFIG_DIR`). To add it to a single project instead, run `./install.sh --project /path/to/project`.

The installer shows you the exact permission rules RepoRabbit needs and asks once whether to add them, so it can work without repeated prompts. It's your choice, and [Permissions and safety](#permissions-and-safety) explains what they cover.

**Start a new Claude Code session afterwards.** Claude Code loads agents at startup.

## Use

Open Claude Code inside any repository and say what you want. You can also type `/repo-rabbit` followed by the request.

| Say this | What happens |
|---|---|
| `Use repo-rabbit to explore this repository.` | Investigates the code and generates the Atlas |
| `Use repo-rabbit to open the map.` | Starts a local server and opens the Atlas in your browser |
| `Ask repo-rabbit to investigate my open questions.` | Researches your questions and saves grounded answers |
| `Use repo-rabbit to refresh the Atlas.` | Updates only what your code changes affected |
| `Use repo-rabbit to tour feature-a and feature-b against main.` | Explains what those branches change |

### A typical session

1. Ask RepoRabbit to **explore**. It reads the repository and reports what it covered deeply, what it only scanned, and what it skipped.
2. Ask it to **open the map**, and start with the Start Here reading list.
3. **Click any source reference** to jump to that line in your IDE. Choose VS Code, Cursor or IntelliJ IDEA at the bottom of the sidebar.
4. **Highlight anything confusing**, click **Ask RepoRabbit**, and save your question. Nothing is sent anywhere at this point: the question waits on your disk.
5. Ask Claude to **investigate your open questions**. Rabbit Notes tells you when questions are waiting and gives you the words to paste. Or turn on [auto-start](#auto-start), and it begins by itself.
6. **Keep the conversation going.** Under any answer, click **Ask a follow-up**. RepoRabbit reads the whole thread, so it builds on what it already said, and the reply lands in the same place.
7. **Watch it work.** While it investigates, the page shows each question's status and what it's reading right now. Answers appear beside your highlights within a few seconds, with a record of how RepoRabbit worked them out.
8. After the code changes, ask it to **refresh**. Your questions and answers carry over, and explanations whose code changed are flagged until they've been reviewed.

### Auto-start

By default, RepoRabbit only runs when you ask. To skip the copy and paste, tick **Start investigating automatically** in Rabbit Notes. Each question or follow-up you save then starts RepoRabbit in the background, and the answer appears beside your highlight.

- It's off by default and set per repository. The setting is stored in your Claude configuration, not the repository, so a cloned project can't turn it on.
- Each run uses your Claude usage. It's often around $0.30 to $0.60 per question, and each run is capped at $3.
- It needs the one-time permission rules and grants nothing beyond them. Questions saved close together share a run, and notes to yourself never start one.
- It runs one investigation at a time, at most 12 an hour, and stops itself if a run answers nothing. Unticking it stops a running investigation, and so does stopping the server.

### Touring branches

Once you have an Atlas, open **Side Tunnels** in the sidebar, choose a base branch, tick any set of up to 12 branches, and click **Tour**. Then paste the prompt it gives you into Claude Code. You can also name the branches in chat instead.

Each tour shows what the branch does, which modules and workflows it affects, a step-by-step walkthrough of the new code path, what a reviewer should check, and the full diff. If you tour several branches together, it also lists the files more than one of them changed, so you can see likely merge trouble first.

Your checkout is never touched: RepoRabbit reads each branch from git into a private snapshot, so you can tour branches with uncommitted work in progress. Source links open that branch's exact code. If a branch gets new commits later, its tour says so, and you can select it again.

## What it writes to your repository

Everything goes into `.repo-rabbit/`, which RepoRabbit adds to your `.gitignore`. Your source files are not modified.

```text
.repo-rabbit/
├── index.html       the Atlas (generated; can be rebuilt from atlas.json)
├── atlas.json       the structured source of truth
├── manifest.json    revision, file hashes and analysis coverage
├── questions.jsonl  your questions and notes
├── answers.jsonl    the answers
├── activity.jsonl   what RepoRabbit reported while working
├── tours.jsonl      branch tours you've requested
├── tours/           each tour and its private snapshot of the branch
└── cache/           drafts, hashes and server state
```

Deleting `questions.jsonl` or `answers.jsonl` deletes your learning history. Everything else can be regenerated.

## How much it costs

RepoRabbit runs on your Claude Code plan or API usage, and exploring reads a lot of code. As one data point, on a 7,400-line Python repository at the default model, an Explore took about 9 minutes and $3.30, answering one question about $0.60, a refresh after a one-file change about $0.60, and a tour of two small branches together about 4 minutes and $1.40. Your numbers will differ with repository size and model.

For large repositories, tell it where to focus, for example: `Use repo-rabbit to explore this repository, focusing on the payments service.` The Atlas always reports what was left out.

## Permissions and safety

RepoRabbit treats the repositories it reads as untrusted.

**What the agent is told to do:** not run repository code without your approval, not read credential files, write only under `.repo-rabbit/`, and ignore instructions embedded in code or docs. These are instructions to the model, backed by Claude Code's permission prompts, and not a sandbox. The agent has Bash, so review what you approve, especially in a repository you don't trust.

**What the helper scripts enforce:**

- An Atlas or answer with a reference that doesn't resolve to a real file and line is rejected.
- Credential files can't be cited, secret-looking values are redacted from code snippets, and an Atlas containing one is refused. Detection is pattern-based, so an unusual secret format could get through.
- All repository text is escaped, and a Content-Security-Policy blocks injected scripts. The page makes no external network requests.
- The server binds to `127.0.0.1`, refuses cross-origin requests, validates every field, and has no route that reads arbitrary files. It never calls a model on its own: the only exception is auto-start, if you turn it on, which launches Claude Code with a fixed prompt.

**The one-time permission step.** RepoRabbit needs to run its own helper often and write its output. The installer offers to add exactly three rules to your Claude Code settings, after showing them to you:

| Rule | Lets RepoRabbit |
|---|---|
| `Bash(python3 <install path>/scripts/rabbit.py *)` | Run its own helper, and no other script |
| `Read(/<install path>/**)` | Read its own schema and templates |
| `Edit(**/.repo-rabbit/**)` | Write only inside `.repo-rabbit/` |

Your existing settings are preserved and backed up first. The helper cannot change these rules, and `./install.sh --uninstall` removes exactly these three. To decide later, run `python3 ~/.claude/repo-rabbit/scripts/setup_permissions.py`, or add `--revoke` to take them back. Without the rules, Claude Code asks each time, and in auto mode it may refuse the helper outright.

## Troubleshooting

| Problem | Fix |
|---|---|
| Claude says it doesn't know `repo-rabbit` | Start a new Claude Code session after installing |
| RepoRabbit says its helper was refused | Run `python3 ~/.claude/repo-rabbit/scripts/setup_permissions.py` in your terminal, then start a new session |
| A question shows "waiting" | Unless auto-start is on, RepoRabbit only runs when asked. Paste the prompt from Rabbit Notes into Claude Code |
| Auto-start can't be turned on | Rabbit Notes says why. Either run the permission command it shows, or make sure `claude` is on the `PATH` of the terminal that started the server, then restart the server |
| Auto-start stopped after a run | Rabbit Notes gives the reason, and the full output is in `.repo-rabbit/cache/auto-run.log`. Untick and tick the box to try again, or paste the prompt into Claude Code |
| Auto-start paused after 12 runs | That's the hourly limit. Ask in Claude Code to continue |
| The page says notes are read-only | You opened the file directly. Ask Claude to open the map, or run `python3 ~/.claude/repo-rabbit/scripts/rabbit.py serve` from the repository |
| Clicking a source link does nothing | Pick your IDE at the bottom of the sidebar, and make sure that IDE is installed and handles its link type (`vscode://`, `cursor://` or `idea://`) |
| Explanations are marked "May be out of date" | The cited code changed. Ask RepoRabbit to refresh the Atlas |
| A note is marked orphaned | The part of the Atlas it was attached to no longer exists. The note stays in Rabbit Notes |

## Update or uninstall

```bash
git pull && ./install.sh   # update
./install.sh --uninstall   # remove; Atlases in your repositories stay
```

Add `--project /path/to/project` for a project install.

Before you update, stop any running Atlas server by saying `Use repo-rabbit to stop the server.` Afterwards, start a new Claude Code session and open the map again. Otherwise you keep running the old version.

## How it works

The agent does the reasoning, and a small standard-library helper does everything that must be exact.

| Path | Job |
|---|---|
| `agents/repo-rabbit.md` | The agent: workflows, evidence rules and required Atlas content |
| `skills/repo-rabbit/SKILL.md` | The `/repo-rabbit` command, which delegates to the agent |
| `scripts/rabbit.py` | Command line: scan, check, ingest, validate, render, serve, questions, answer, refresh, patch, tours |
| `scripts/validate.py` | Schema checks, reference resolution, staleness and secret detection |
| `scripts/renderer.py` | JSON to escaped HTML, SVG diagrams, syntax highlighting and IDE links |
| `scripts/server.py` | The local Atlas server |
| `scripts/auto.py` | Optional auto-start of investigations |
| `scripts/tours.py` | Branch selection, snapshots, diffs and tour validation |
| `scripts/setup_permissions.py` | The one-time permission step, kept apart from the helper the agent runs |
| `scripts/store.py` | Crash-safe storage for questions and answers |
| `schemas/atlas.schema.json` | The Atlas format |
| `templates/atlas.html` | The page itself |

**Why the links can be trusted.** The agent cites code by anchor text, such as `"anchor": "def login("`, and the helper finds the line in the real file. Code snippets are read from the file when the page is built, so the agent never types them.

**How refresh stays incremental.** Each reference stores a hash of the lines it cites. If those lines moved, only the numbers change. If they changed, that explanation is flagged for the agent to review, and everything else is left alone. Atlas block ids stay fixed, so your notes stay attached.

## Limitations

- Developed and tested on macOS with Chrome. Safari, Firefox, Linux and Windows have not been tested, and on Windows concurrent writers are not locked.
- Live agent runs so far cover one small Python repository. Very large repositories are untested.
- IDE links are generated and unit-tested, but opening them in a real IDE has not been automatically verified.
- Syntax highlighting is approximate, and languages without a rule render uncolored.
- A selection inside a diagram outlines the whole diagram rather than the selected text.

## Development

```bash
python3 -m unittest discover -s tests                          # standard library only
uv run --no-project --with playwright python tests/e2e_browser.py  # drives the page in installed Chrome
```

## License

[MIT](LICENSE)
