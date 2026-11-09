# Reviews

Who reviews what is in process.md section 5; this is the procedure: routing, the packet, the
rounds and the verdict. A project's review notes add its deployed-version list, its Codex line
(the shared line, or a legacy points line) and its change-path grep (project placeholders below).

## Routing

Owner 11-09: "I would cut very hard", "this has to be done urgently. Urgently. It will speed
us up a lot", "Fable only in really product-designed big epics; in ordinary things it is no
longer required". A review of 634 rounds found 15 production-grade defects in 1,826 findings,
almost all on a first look at the paths below; rechecks found one weak one in 306 rounds.

- Review paths: a change that touches sends or calls (outbound messages, calls, provider
  sessions), collector sessions, user-data deletion or rewrite (process.md section 2),
  access, secrets or CI permissions (rulesets, required checks, workflow `permissions:`, grants
  beyond the own-agent ones section 2 exempts, internet exposure), or a live-production operation
  plan. Nothing else gets a review: migrations, backup, deploy tools, CI tooling, release work,
  refactors, ordinary features, small fixes, tests, docs and process text merge on green required
  CI, code with a failing-first test (process.md section 4; the PR shows the failing base run).
- One first look on a review path, never a recheck (Rounds 4):
  - code: Astra r1, `bin/review-codex.sh astra <task> <report> --risk "<the path and its concrete
    failure>"` (by hand: `codex exec --disable hooks -m gpt-6-astra -c service_tier="default"
    "<task>" </dev/null`). Exit 13 (no `--risk`) starts no model or round;
  - a big product epic, and a product card on a review path: one Fable r1 (Card review);
  - a production operation plan: one plan r1 (Rounds 7).
- No Sol code rounds, no Fable on finished code unless the task brief names it for that task
  (owner 11-09; then it takes Astra's r1 place, `Agent(subagent_type: "general-purpose",
  model: "fable")`), never a second reviewer on the same item. The authoring model never adds or
  changes a review gate. `bin/review-codex.sh` r1 refuses Markdown-only (tests allowed beside it;
  exit 11) and test-only (exit 12) diffs unless `--risk` names a review path. Test-only means every
  path in `git diff --name-only <merge-base>..<head>` is a test case or fixture by path: under a
  top-level test directory (`tests/`, `test/`, or the test paths the project's review notes name),
  in a `__tests__/` directory, or a test file by name with a code extension (`test_*.py`,
  `*_test.py`, `*_test.go`, `*.test.{js,jsx,ts,tsx,sh}`, `*.spec.{js,jsx,ts,tsx}`). A runner a
  required check invokes is not a test case.
- VISION.md (tiden.md, Brainstorm and vision): one Fable round, the B5 ritual's (owner 10-27).
- Pinned-copy refresh in a project (README, Pinned copy; `bin/pin-refresh.sh`): no review (owner
  10-29): the script refuses any other changed file than the copies and the two version pointers.
- Small fix (owner 11-09: "such a card needs no review, it is one line; it is simply fixed under a
  fix, without a card"): at most 3 SP, no migration, data policy, egress, grant, allowlist, key,
  deploy tool, CI file, gate, user-data DELETE/UPDATE or review path: one pull request with a
  failing test first, no card. The orchestrator reads the diff and arms (orchestration.md,
  Merging). The brief or the worker's estimate names the route; a diff outside it, or in doubt,
  takes the routes above. Its PR lines: `Review record: none (small fix)` and `Diff read:`.
- No task gets a per-task est.sh forecast (backlog estimates stay), a fourth (tests) question or
  an Opus subagent review; do not add `/code-review` or `/security-review` on top of Astra or Fable.
- Pre-production mode (orchestration.md): the same routing.

## Packet

Code packets: `bin/review-packet.sh` collects the files below from committed source, PR/check
metadata and supplied requirements (task-flow.md). Its manifest binds source and input hashes;
`review-codex.sh` refuses stale/modified generated packets before the round (exit 9). Legacy
manual packets remain valid and receive the same absolute input-path map in the model prompt.

A review is one packet of files in the author's scratchpad and one report file. Reviewers have no
Tiden access and get projections, never whole exports, other reviewers' reports or the review
history.

- `task.md`: the task goal, the card's criteria and the review path. An Astra task starts with
  the three fixed questions of process.md section 5, verbatim with the project's values filled in
  (the `Harness values:` of its AGENTS.md). A code packet names the deployed versions for
  question 3 (`<deployed consumers>`) in a line that starts with `Deployed:` (`Deployed: none
  (<why>)` only where nothing consumes the change); a question that does not apply is answered
  `n/a: <why>`, never dropped.
- `diff.patch`: `git diff <merge-base>..<head>`.
- `specs.md`: the full text of the specs the PR delivers or changes; IDs and titles of the others
  it names.
- `pr.md`: the pull request description.
- `context.md`: the `product-context` answers the author read at the start, unchanged; an author
  who read none says `context: none read` in `task.md`.
- Screenshots of the changed screens when the diff touches UI paths (the project config's
  `REVIEW_UI_PATHS`; unset: `^(web|mobile|frontend)/`, `''`: off): a line that starts with
  `screenshots:` in `task.md` or `pr.md` naming them, or `screenshots/` in the packet.

`bin/review-codex.sh` checks this list and refuses an incomplete packet (exit 9, naming each
missing item); exit 9 starts no run: fix the packet and launch again in the same turn.
`--no-<item> "<reason>"` (diff, specs, pr, context, screenshots) is only for an item that cannot
exist for this packet; the items and reasons head the reviewer's prompt and go into
`review-rounds`. No flag skips the `Deployed:` line or Astra's questions. A production operation
plan (Rounds 7) passes `--no-diff "production plan" --no-specs "production plan" --no-pr
"production plan" --no-context "production plan"`. A harness pull request has no Tiden product
(`--no-specs "harness"`, `context: none read`); its `Deployed:` line names the consumers (each
project's `config.sh`, the pinned copies, briefs and handoffs). A missing input is a finding, not
a reason to guess. Reviewers do not re-run what CI proves; they spend the round on logic, failure
modes, security, compatibility and whether text matches code.

### Card review

A Fable product review (the project's Fable prompt, for example
`docs/operations/prompts/fable-product-review.md`) is one round on a big product epic or a product
card on a review path; it gets the owner's request in the owner's words, the card and the proposed
user-visible behaviour. No code, no recheck, no Codex card round. Other cards get the author's
self-check. A product PR states what the owner will notice in an `Owner note:` line.

## Report and verdict

The reviewer writes `review-<reviewer>-<unit>-r1.md` next to the packet. Each finding has a
severity, `file:line`, the failure scenario and a suggested fix:

- **blocking**: a contract, security or data defect would ship;
- **major**: a real defect ships (wrong behaviour, a data or security risk);
- **minor** and **nit**: the author fixes them in the PR or drops them. An untested criterion is
  at most minor (owner 11-09). Wording, packet and "not verified" items are not reported.

Severity is the consequence left after a named, independent, deployed control: a miss that such
a control bounds is minor. A changed verifier never counts as its own control. Never downgraded:
an unmet process.md section 2 authority, a provider-session or send action, a data-loss or backup
defect, and broken failure isolation (a fail-closed stop of a critical path the project names,
such as call audio, collection or backup, is itself a failure).

A product-policy risk the owner accepted (an owner decision quoted in the card or brief) is no
finding: the report lists it under `Owner should know` (owner 10-18, 10-24).

It returns one line:

```text
VERDICT: APPROVE | CHANGES REQUIRED — <b> blocking, <m> major, <n> minor — <report path>
```

## Rounds

1. One round covers the whole packet: code and criterion text together. One run: never a
   duplicate run; an empty or failed run goes to the orchestrator, not to a rerun. The one
   exception is `bin/review-codex.sh`'s own single retry when Codex is at capacity before any
   report; it is the same run.
2. Scope freezes when the round starts; an optional addition goes to a follow-up card. A stop
   order or a change to what the owner keeps reaches the author at once.
3. The author fixes every finding in one pass, in the same turn: never park a pull request.
4. There is no r2 (owner 11-09): `bin/review-codex.sh` refuses every round after r1 of a
   packet and every second Codex r1 on the same branch or the same plan (exit 7). Major and minor
   findings: the author fixes them, no new round; with no blocking finding the author marks the
   PR ready and arms once the ready run finished (orchestration.md, Merging). A blocking finding:
   the author fixes it with a regression test that fails before the fix and passes after it (a
   plan or card: the recorded fix), marks the PR ready and reports; the orchestrator reads the fix
   diff (orchestration.md, Merging), writes `Diff read:` and arms.
5. A blocking finding the author cannot fix is the worker's BLOCKED (needs: a smaller scope, a
   follow-up card or a revert, the orchestrator's pick, listed under "Decided by agents"); a
   merged cause that breaks production or a user-facing path or exposes data is an `incident`
   (orchestration.md, Owner asks). Never merge with a known blocking finding open; nobody accepts
   a known risk (process.md section 2).
6. Cards: Card review above; no Astra or Sol card round.
7. Production operations follow process.md section 2 (owner 11-04: ship without gates). A
   production operation plan gets one review round and no recheck; a tested runbook (one
   already run successfully), the promotion tool's deploy and a migration that rides it get none.
   Deleting or rewriting user data and access, keys, secrets or internet exposure get Astra r1;
   any other plan one Sol round. The author records each fix; a blocking finding the author
   cannot fix means that step does not run, never a hold of other work. A configuration backup is
   not a database backup. Incident recovery defers review as process.md section 2 permits.
8. Process text (AGENTS.md, CLAUDE.md, `.claude/`, skills, operations docs, briefs/prompts and
   this harness's documents/templates) gets the author's checklist and no model review: referring
   documents agree, authority/gate changes have the required owner words, word caps hold and the
   PR names replaced rules. OWNER changes still need the owner's quoted request. A new
   script/config/helper or executable check is code, even under tests or templates.

## Codex budget

Codex reviews and the owner's other Codex work share one weekly meter across all projects (owner
10-21); a point is one percent. The line is the shared pace, 100% over the meter's window, plus
the add-on (`CODEX_LINE_OFFSET`, 90), capped at 100, so the gate can fire only in a fresh window
already above 90% (owner 10-23, 10-24). A points line (`<codex points>`) is legacy. Over the line
only review-path reviews start (`--risk-path`); the rest of a fresh window waits. Every Codex run
passes `-c service_tier="default"`: the priority tier costs about twice as much per token.

Standing approval (owner 10-19): the orchestrator may tune routing within this document (severity
rubric, the Codex line) and reports each change in the morning digest. It never adds a recheck or
a review off the review paths, and never removes Astra r1 from a review path, the never-downgraded
list or a process.md section 2 item or gate. Routing (reviewer, model, who merges) is never asked
of the owner (orchestration.md, Owner asks).

## Pull request lines

- `Change path:` comes from the changed files and process.md sections 2 and 3. Grep the diff for
  its signals (the project's grep). A hit is an item to read, not a verdict. Read the changed
  blocks of every listed file (a workflow's `permissions:` and jobs, SQL grants, secret mounts); in
  doubt the path is AUTO+review with Astra. A card's own level label adds no merge gate.
- `Deletes:` when code the deployed service runs itself (a request path, a scheduled job) executes
  DELETE statements: each statement and the rows a ready card or the data policy names that it
  removes; the reviewer checks the diff against that list. Only these statements leave the
  process.md section 2 deletion gate (no owner question; the files' own change path, section 3).
  Still gated: TRUNCATE, DROP and other destructive SQL in any code (a grep hit, in service code as
  in a migration, is never a `Deletes:` item); a script, command, data fix, deploy or release hook
  or other automation that deletes production data, whoever starts it, the promotion command
  included. Rows neither names: user data is section 2 item 2, other rows need a card first.
- `Review record:` one line, e.g. `Review record: Astra r1 1 blocking, 2 major` or `Review
  record: none (<route>)`, plus `Diff read: <orchestrator> <head SHA>` where the orchestrator
  reads the diff (small fix, blocking fix). No findings ledger.
- `Owner note:` for a product PR, what the owner will notice in daily use.
- `Spec impact:` and `Settle text:` on every PR (tiden.md, One Tiden branch per pull request).
- `Model: <model> <effort>` (the author's) on every PR.
