# Reviews

There is none (owner 11-11, verbatim: «отмени все ревью в целом», «не только текущих у всех в
будущем», and «общие правила процесса тоже изменим»; process.md section 5). No model, plan or card
review and no orchestrator diff read, on any path, in any project. A change merges on green required
checks, and code carries a failing-first test (process.md section 4; the PR shows the failing base
run). What stays, none of it a review: the live-action rule (process.md section 1), a fresh verified
backup and undo before deleting or rewriting user data (process.md section 2), the owner's quoted
words for an OWNER change (process.md section 3). This file keeps the small-fix route, the
process-text checklist and the pull request lines.

## Small fix

A fix of at most 3 SP with no migration, data policy, egress, grant, allowlist, key, deploy tool, CI
file, gate or user-data DELETE/UPDATE (owner 11-09: it is one line, it is simply fixed, without a
card) is one pull request with a failing test first and no card (task-flow.md; tiden.md, Card
intake). Anything outside that list takes the ordinary route. The author marks it ready after local
checks and arms it like any other pull request (orchestration.md, Merging).

## Process text

Process text (AGENTS.md, CLAUDE.md, `.claude/`, skills, operations docs, briefs/prompts and this
harness's documents/templates) gets the author's checklist: referring documents agree, an
authority or gate change carries the required owner words, word caps hold and the PR names the
replaced rules. A new script, config, helper or executable check is code, even under tests or
templates: it has a failing-first test.

## Pull request lines

- `Change path:` comes from the changed files and process.md sections 2 and 3: AUTO or OWNER. Grep
  the diff for its signals (the project's grep). A hit is an item to read, not a verdict. Read the
  changed blocks of every listed file (a workflow's `permissions:` and jobs, SQL grants, secret
  mounts); in doubt, name the risk in the PR. A card's own level label adds no merge gate.
- `Deletes:` when code the deployed service runs itself (a request path, a scheduled job) executes
  DELETE statements: each statement and the rows a ready card or the data policy names that it
  removes; the author checks the diff against that list. Only these statements leave the
  process.md section 2 deletion gate (no owner question; the files' own change path, section 3).
  Still gated: TRUNCATE, DROP and other destructive SQL in any code (a grep hit, in service code as
  in a migration, is never a `Deletes:` item); a script, command, data fix, deploy or release hook
  or other automation that deletes production data, whoever starts it, the promotion command
  included. Rows neither names: user data is section 2 item 2, other rows need a card first.
- `Owner note:` for a product PR, what the owner will notice in daily use.
- `Spec impact:` and `Settle text:` on every PR (tiden.md, One Tiden branch per pull request).
- `Model: <model> <effort>` (the author's) on every PR.
