# Reviews

Who reviews what is in process.md section 5; this is the procedure: routing, the packet, the
rounds and the verdict. A project's review notes add its deployed-version list, its Codex line
(the shared line, or a legacy points line) and its change-path grep (project placeholders below).

## Routing

- Code r1: Codex Sol for normal, CI, Tiden and release pull requests:
  `codex exec --disable hooks -m gpt-6.1-sol -c model_reasoning_effort=xhigh -c service_tier="default" "<task>" </dev/null`.
  Astra (`bin/review-codex.sh astra`; by hand the line above with `-m gpt-6-astra` and no
  `model_reasoning_effort`) only for r1 on security, access, data, owner-level or product-behaviour
  changes (standing approval, owner 10-19); not on the own-agent grants process.md section 2
  exempts (owner 10-29).
- One Codex reviewer per round: Astra or Sol, never both on the same item. A scoped recheck (r2, r3 or
  the over-cap r4; Rounds 4, 5), also of a card and also after an Astra r1, runs on Sol, not an Anthropic subagent (owner 10-21, 10-23),
  and never on Astra unless the project's review notes say so (owner 10-27; `bin/review-codex.sh`
  refuses Astra from round 2 on, exit 10; the project config's `REVIEW_ASTRA_RECHECK=1` allows every
  Astra recheck, so the review notes name which ones run there).
- Process text (Rounds 8): no Codex; one Opus subagent round, plus Fable only when the text
  changes process.md section 2 or 3, an owner item or a gate (owner 10-29). Test-only pull
  requests, Codex-written too, are AUTO (process.md section 3): no review round, green required
  checks merge them, in this harness too (Rounds 8). `bin/review-codex.sh` r1 refuses a diff of only
  Markdown, tests allowed beside it (exit 11; other files under `templates/` are config or code and
  keep the review), or only tests (exit 12), unless `--risk "<reason>"` (a gate change, a runner).
  Test-only means every path in `git diff
  --name-only <merge-base>..<head>` is a test case or fixture by path: under a top-level test
  directory (`tests/`, `test/`, or the test paths the project's review notes name), in a
  `__tests__/` directory, or a test file by name with a code extension (`test_*.py`, `*_test.py`,
  `*_test.go`, `*.test.{js,jsx,ts,tsx,sh}`, `*.spec.{js,jsx,ts,tsx}`). Not test-only: a helper,
  CI file, config or fixture outside those paths; a runner or entry point a required check
  invokes (not a test case), even under a test directory; a file whose diff the project's
  change-path grep hits.
- VISION.md (tiden.md, Brainstorm and vision): AUTO+review, one Fable round, no Codex; it is
  never the AUTO of non-process docs. That ritual round (B5's, on the ritual's answers; a later PR's one
  Fable round) is its whole review: no extra Opus subagent or Rounds 8 round (owner 10-27; process.md
  section 3).
- Pinned-copy refresh in a project (README, Pinned copy; `bin/pin-refresh.sh`): AUTO, no review
  round (owner 10-29): the script copies each file from `git -C ~/agent-harness show <sha>:<file>` and
  refuses any other changed file than those and the two version pointers; the Rounds 8 checklist does
  not apply (the copied text was reviewed here).
- Codex-written code: an Opus subagent. Product cards: one Fable round (Card review below).
- Name the model in the pull request's `Review record:`; put `Model: <model> <effort>` (the
  author's) in every pull request description.
- Astra and Fable are the reviews: do not add `/code-review` or `/security-review` on top of them.

## Packet

A review is one packet of files in the author's scratchpad and one report file. Reviewers have no
Tiden access and get projections, never whole exports, other reviewers' reports or the review
history. One packet directory serves all rounds of a pull request: `bin/review-codex.sh` counts
the rounds there (`review-rounds`, Rounds 5).

- `task.md`: the task goal and the card's criteria, the round, and for a recheck the open
  findings of r1. An Astra task starts with the four fixed questions of process.md section 5,
  verbatim with the project's values filled in (the `Harness values:` of its AGENTS.md), before any task-specific question. A code packet names the deployed versions for
  question 3 (`<deployed consumers>`, from the project's review notes or the orchestrator) in a
  line that starts with `Deployed:` (`Deployed: none (<why>)` only where nothing consumes the
  change); a question that does not apply is answered `n/a: <why>`, never dropped.
- `diff.patch`: `git diff <merge-base>..<head>`; for a recheck, only the fix range. A criterion
  edit or settle text on the PR's `Tiden branch:` goes in as previous and proposed text.
- `specs.md`: the full text of the specs the PR delivers, flags in `Spec impact:` or changes; IDs
  and titles for the other specs it names or touches. A project helper that lists them (for example
  `scripts/spec_impact.py`) only helps the author choose. An unflagged `named (Spec)` line whose
  criteria the PR changed still gets its full text.
- `pr.md`: the pull request description.
- `context.md`: the `product-context` answers the author read at the start, saved then and
  attached unchanged, so the reviewer sees the criteria of the specs around the change, including
  those it does not change. An author who read none (process.md section 4) says so in one line,
  `context: none read`, in `task.md`.
- Screenshots of the changed screens when the diff touches UI paths (the project config's
  `REVIEW_UI_PATHS`; unset: `^(web|mobile|frontend)/`, `''`: off): a line that starts with
  `screenshots:` in `task.md` or `pr.md` naming them, or `screenshots/` in the packet. A change
  under those paths that changes no screen says so on that line.

A card has no diff: its packet holds the proposed text, the search results for similar specs (ID,
title, status), the text of specs it references and `git ls-files -- <expected paths>`.

`bin/review-codex.sh` checks this list before r1 and refuses an incomplete packet (exit 9,
naming each missing item; review-speed study 10-29: 14% of findings were missing packet items).
Exit 9 and 10 start no run and count no round: fix the packet (or relaunch on Sol) and launch again
in the same turn, no orchestrator step. Key lines and the questions may carry a list marker, a
number or bold. A card
review passes `--card`: only `task.md` is checked (an Astra card: also the four questions); the
card contents below still apply. `--no-<item> "<reason>"` (diff, specs, pr, context, screenshots)
is only for an item that cannot exist for this packet, never one the author did not produce; the
items and reasons head the reviewer's prompt and go into `review-rounds`. No flag skips the
`Deployed:` line or Astra's questions. Packets without a pull request: a production operation plan
(Rounds 7) passes `--no-diff "production plan" --no-specs "production plan" --no-pr "production
plan" --no-context "production plan"` and keeps its `Deployed:` line and the questions; the epic
review (Rounds 9) below. A harness pull request has no Tiden product (`--no-specs "harness"`,
`context: none read`), but its merge is live in every project at once (scripts run from
`origin/main`), so its `Deployed:` line names the consumers: each project's `config.sh` from an
older template, the pinned copies at their versions, briefs and handoffs. A missing input is a finding, not a reason to guess. Reviewers do not re-run what CI proves
(the required checks, linters, the full suite in the merge queue); they spend the round on logic,
failure modes, security, compatibility and whether text matches code.

### Card review

A Fable product review (the project's Fable prompt, for example
`docs/operations/prompts/fable-product-review.md`) is one round on a product card; it gets the
owner's request in the owner's words, the card and the proposed user-visible behaviour. No code.
Fable does not review the finished behaviour; a product PR states what the owner will notice in
an `Owner note:` line instead.

## Report and verdict

The reviewer writes `review-<reviewer>-<unit>-r<n>.md` next to the packet. Each finding has a
severity, `file:line`, the failure scenario and a suggested fix:

- **blocking**: a contract, security or data defect would ship;
- **major**: a real defect ships (wrong behaviour, a data or security risk) or a promised
  criterion is untested;
- **minor** and **nit**: the author fixes them in the PR or drops them.

Severity is the consequence left after a named, independent, deployed control: a miss that such
a control bounds is minor. A changed verifier never counts as its own control. Never downgraded:
an unmet process.md section 2 authority, a provider-session or send action, a data-loss or backup defect,
and broken failure isolation (a fail-closed stop of a critical path the project names, such as
call audio, collection or backup, is itself a failure).

A product-policy risk the owner accepted (an owner decision quoted in the card or brief) is no
finding: the report lists it under `Owner should know`. It is a product choice, not a known
defect accepted; every other finding keeps its severity (owner 10-18, 10-24).

It returns one line:

```text
VERDICT: APPROVE | CHANGES REQUIRED — <b> blocking, <m> major, <n> minor — <report path>
```

## Rounds

1. One round covers the whole packet: code and criterion text together. One run per round:
   never a duplicate run; an empty or failed run goes to the orchestrator, not to a rerun. The one
   exception is `bin/review-codex.sh`'s own single retry when Codex is at capacity before any
   report; it is the same run.
2. Scope freezes when the round starts; an optional addition goes to a follow-up card. A stop
   order or a change to what the owner keeps reaches the author at once.
3. The author fixes every finding in one pass and searches for every term the fixes changed,
   starting at once and launching the next round in the same turn: never park a pull request
   between rounds. When that fix push leaves only the recheck open, mark the pull request ready
   at it so CI runs alongside; once no blocking or major finding is open and the ready run has
   finished, the author arms the merge (orchestration.md, Merging; owner 10-29).
4. After a blocking or major finding, one recheck (r2) runs on Sol (Routing; for a no-Codex review, the subagent that raised it): the fix
   range and the open findings, tests only for the fix, no fresh exploration of the
   repository. It is never a fresh full review, never waits for CI (owner 10-29), and a
   recheck's minor opens no round.
5. The cap is r2. After it, on migrations, auth/access, data and production paths, one scoped
   recheck (r3, Rounds 4) runs by itself, with no BLOCKED and no orchestrator go-ahead; that one r3
   covers both a new blocking finding that r2 raised and a blocking or major fix made after r2, and
   a regression test that fails before the fix and passes after it is its precondition. On other
   code that test plus green CI confirms a fix made after r2 and r3 is the author's option (owner
   10-29); an r4 still needs the orchestrator's grant. The orchestrator may grant at most one more
   round per pull request: the over-cap r4, also a scoped recheck, recorded as `over-cap` with
   its reason in `Review record:` (`bin/review-codex.sh --over-cap "<reason>"`, which refuses r4
   without it and any r5). Plan and doc reviews (process text, Rounds 8, and plans other than those of Rounds 7) have
   no test to confirm a fix: a fix made after r2 gets the one r3, and no over-cap round. Cards (Rounds 6) and production operation plans (Rounds 7) keep their one
   recheck: no r3 or r4. A blocking or major finding stays open until a recheck (on other code
   after r2: its regression test plus CI) confirms the fix; never merge with it unconfirmed. A finding still open after the last round allowed is the
   worker's BLOCKED (needs: a smaller scope, a follow-up card or a revert). The orchestrator picks
   the cheapest that keeps the defect off `main`: drop the part with the finding, close or narrow
   the pull request and redo the part as a follow-up card, or revert a cause already merged; a
   merged cause that breaks production or a user-facing path or exposes data is an `incident`
   (orchestration.md, Owner asks). It lists the pick under "Decided by agents" (orchestration.md).
   Nobody accepts a known risk (process.md section 2, owner 10-27). Only when all three would miss
   a deadline the owner's words set (DECISIONS.md) or the card's date does it go to the owner, as a
   `product` ask: move the deadline or cut the scope, never merge with the finding open.
6. Cards: a contract card touching access, data, migrations or release gets Astra r1 and at most one
   scoped recheck on Sol; another contract card gets Fable (a product card) or the author's
   self-check, no Codex (owner 10-29); a small card gets only the author's self-check.
7. A production operation plan gets Astra r1 and a Sol recheck only after a blocking or major
   finding, tiered by risk (owner 10-29). Astra r1 is required for any step without an
   undo, destructive SQL (DROP, TRUNCATE, DELETE, RENAME, narrowing ALTER), a restore, data
   deletion, and access, keys, secrets or internet exposure, even inside a plan an exemption below
   fits; for the first plan of any other operation; for any plan not exempted below; and in doubt.
   No review for a new (not edited) additive
   migration (tables, columns, indexes only, including `CREATE INDEX CONCURRENTLY` run by hand), a
   small deviation of an already-reviewed plan that has a tested undo and an inline mock test of
   the deviation, or the standard promotion-tool deploy; the agent runs it after the backup gate
   (process.md section 2) and lists it under "Decided by agents". An Astra r1 with no
   blocking or major finding, or a Sol recheck that confirms its fixes, authorises the plan
   (process.md section 2 gates: a fresh verified backup, the undo); the agent runs it and lists it
   under "Decided by agents". A plan that itself decides one of process.md section 2's five goes to
   the owner for that item only. A finding still open after the recheck: the plan does not run; a
   narrowed plan is a new plan (Astra r1 again) or a follow-up card.
8. Process text (AGENTS.md, CLAUDE.md, `.claude/`, skills, brief and prompt templates, the
   project's operations docs and Tiden CI document, and this harness's documents and templates; its
   scripts are code and a test-only PR here is AUTO, Routing) is never AUTO (process.md section 3)
   and gets the author's checklist: each changed rule matches the documents that refer to it,
   no change lets agents decide a process.md section 2 item or weakens a section 2 gate without the owner's quoted words, process.md and
   AGENTS.md stay within their word caps and the PR names the words it replaced. Astra reviews
   it only when it changes how a gate works (`bin/review-codex.sh --risk "<reason>"`). VISION.md is never AUTO either; its review is the
   brainstorm ritual's Fable round, nothing more (Routing).
9. Epic review (optional, the orchestrator's call; owner 10-29): when an epic closes after 6 or
   more merged PRs (its stories and refactor-slot PRs), the orchestrator may start one Sol run (Rounds 1, its
   capacity retry included) once the `main` push run containing the last squash is green, on
   `git diff <base>..<last squash> -- <paths those PRs changed>`
   (base: the parent of the first of them to merge). Its packet is `task.md` (the stories with
   their PRs, the other PRs that changed those paths in the range, the question: only cross-PR
   issues no single PR review could see, such as contracts that disagree, duplicated or dead code,
   an epic criterion no PR covers), `diff.patch` and `specs.md` (the epic's specs); no `pr.md` or
   `context.md` (`--no-pr "epic review" --no-context "epic review"`, plus `--no-screenshots "epic
   review"` when it touches UI paths); its `task.md` has a `Deployed:` line. An epic of process text gets an Opus subagent instead; over the Codex line it
   waits for the next window as a STATE.md NEXT line. It counts in no PR's rounds and does not
   hold the close; its verdict line is appended to the archived PLAN block. A blocking or major
   finding is a current defect (a review branch, tiden.md; first in the queue, Card intake step 5)
   that the orchestrator puts first in the queue or answers with a revert (as in Rounds 5; one that
   breaks production or a user-facing path or exposes data is an `incident`), listed under "Decided
   by agents" with its undo; never a reopened PR. The
   orchestrator files or drops minor ones and skips a gap P7 already carded.

## Codex budget

Codex reviews and the owner's other Codex work share one weekly meter across all projects (owner
10-21: the owner's projects use one Codex subscription); a point is
one percent. The line is the shared pace, 100% over the meter's window, plus the add-on
(`CODEX_LINE_OFFSET`, 90), capped at 100. With 90 the line reaches 100 after about a tenth of the
window (about 17 h of 7 days), so the gate can fire only in a fresh window, when the meter is
already above 90%; after that every review starts, and risk-path and other reviews differ only
by reviewer routing. The reason: Codex always stays with a large reserve and limits reset unused
(owner 10-23; 10-24: the add-on in every project; 10-24: no single limit is needed, or it must be
much larger); the line stays as a stop for a meter nearly used up at the window's
start, the owner's other Codex work being what can use it up. Every project runs this same line; the
meter has no per-project split, so a project's share cannot be measured on it
(incident 10-22: a points line read OVER at 28% after 2.2 of 7 days). A points line
(`<codex points>`) is legacy, kept for configs that still set a number. The orchestrator reads
the meter (`rate_limits.primary.used_percent` in the newest file under `~/.codex/sessions`)
before it starts a review. Under the line every review starts. Over it Astra starts only
risk-path reviews; the rest wait or, at the orchestrator's call, go to an Opus reviewer subagent
with the same packet and questions. Risk
paths: process.md section 2 items and gates, release, deploy and the project's promotion tool, CI gate
integrity, security and access (not the own-agent grants process.md section 2 exempts), migrations, and the live-action code the project lists. Every
Codex run passes `-c service_tier="default"`: the priority tier costs about twice as much per
token.

Standing approval (owner 10-19): the orchestrator may tune review routing within this document
(round cap, recheck scope, severity rubric, the Codex line and the reviewer for non-risk code)
and reports each change in the morning digest. It never removes Astra r1 from a risk path,
the never-downgraded list above, the recheck rules of Rounds 4 and 5 or a process.md section 2 item or gate,
and never raises the round cap past Rounds 5 (one over-cap round per pull request). Routing
(reviewer, model, level, rounds, an over-cap round, the Codex budget, who merges) is never asked of
the owner (orchestration.md, Owner asks): the orchestrator decides it within this document.

## Pull request lines

- `Change path:` comes from the changed files and process.md section 2. Grep the diff for its
  signals (the project's grep). A hit is an item to read, not a verdict; no hit does not make a
  PR AUTO. Read the changed blocks of every listed file (a workflow's `permissions:` and jobs, SQL
  grants, secret mounts); in doubt the path is AUTO+review with Astra. A card's own level label adds no merge gate.
- `Deletes:` when code the deployed service runs itself (a request path, a scheduled job) executes
  DELETE statements: each statement and the rows a ready card or the data policy names that it
  removes; the reviewer checks the diff against that list. Only these statements leave the
  process.md section 2 deletion gate (no owner question; the files' own change path, section 3).
  Still gated: TRUNCATE, DROP and other destructive SQL in any code (a grep hit, in service code as
  in a migration, is never a `Deletes:` item); a script, command, data fix,
  deploy or release hook or other automation that deletes production data, whoever starts it,
  the promotion command included. Rows neither names: user data is section 2 item 2, other rows
  need a card first.
- `Review record:` one line per reviewer and round with its counts, naming one Codex reviewer per
  round, e.g. `Review record: Astra r1 1 blocking, 2 major; Sol r2 1 major; Sol r3 1 major; Sol r4
  over-cap (<reason>) 0/0`. No findings ledger or per-finding
  machine lines are kept.
- `Owner note:` for a product PR, what the owner will notice in daily use.
- `Spec impact:` and `Settle text:` on every PR (tiden.md, One Tiden branch per pull request).
- `Model: <model> <effort>` on every PR (Routing).
