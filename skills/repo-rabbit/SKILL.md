---
name: repo-rabbit
description: Map an unfamiliar codebase into an interactive, source-cited Atlas, open it, answer questions highlighted in it, or refresh it after code changes.
argument-hint: "[explore | open the map | investigate my questions | refresh | tour branches]"
context: fork
agent: repo-rabbit
---

The user invoked RepoRabbit in the current repository with this request:

$ARGUMENTS

If the request is empty, run Explore when `.repo-rabbit/atlas.json` does not exist, and Investigate when it exists and has open questions; otherwise, Refresh. Treat the text above as the user's choice of workflow, then follow your RepoRabbit instructions.
