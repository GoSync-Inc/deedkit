# Reviews

Who reviews what is in process.md section 5; this is the procedure: routing, the packet, the
rounds and the verdict. A project's review notes add its deployed-version list, its Codex points
and its change-path grep (project placeholders below).

## Routing

- Code r1: Codex Sol for normal, CI, Tiden, release and docs pull requests:
  `codex exec --disable hooks -m gpt-6.1-sol -c model_reasoning_effort=xhigh -c service_tier="default" "<task>" </dev/null`.
  Astra (`-m gpt-6-astra`, process.md section 5) only for r1 on security, access, data,
  owner-level or product-behaviour changes, and for a recheck of a blocking finding in security,
  access or data. (DeedKit orchestrator, standing approval item 13, 10-19; owner 10-19 09:1xZ.)
- A scoped recheck (r2) and a card review run on Sol. A recheck of a single fixed finding stays on
  Codex Sol, not an Anthropic subagent, also when Astra ran r1 (owner 10-21).
- Test-only pull requests and process or rule text: no Codex; an Opus subagent reviewer, plus Fable
  for process text.
- Codex-written code: an Opus subagent. Product cards: one Fable round (Card review below).
- Name the model in the pull request's `Review record:`; put `Model: <model> <effort>` (the
  author's) in every pull request description.
- Astra and Fable are the reviews: do not add `/code-review` or `/security-review` on top of them.

## Packet

A review is one packet of files in the author's scratchpad and one report file. Reviewers have no
Tiden access and get projections, never whole exports, other reviewers' reports or the review
history.

- `task.md`: the task goal and the card's criteria, the round, and for a recheck the reviewer's
  own open findings. An Astra task starts with the four fixed questions of process.md section 5,
  verbatim with the project's values filled in (its AGENTS.md section 5), before any task-specific question. A code packet names the deployed versions for
  question 3 (`<deployed consumers>`, from the project's review notes or the orchestrator).
- `diff.patch`: `git diff <merge-base>..<head>`; for a recheck, only the fix range. A criterion
  edit or settle text on the PR's `Tiden branch:` goes in as previous and proposed text.
- `specs.md`: the full text of the specs the PR delivers, flags in `Spec impact:` or changes; IDs
  and titles for the other specs it names or touches. A project helper that lists them (DeedKit:
  `scripts/spec_impact.py`) only helps the author choose. An unflagged `named (Spec)` line whose
  criteria the PR changed still gets its full text.
- `pr.md`: the pull request description.
- `context.md`: the `product-context` answers the author read at the start, saved then and
  attached unchanged, so the reviewer sees the criteria of the specs around the change, including
  those it does not change. An author who read none (process.md section 4) says so in one line.

A card has no diff: its packet holds the proposed text, the search results for similar specs (ID,
title, status), the text of specs it references and `git ls-files -- <expected paths>`.

A missing input is a finding, not a reason to guess. Reviewers do not re-run what CI proves
(the required checks, linters, the full suite in the merge queue); they spend the round on logic,
failure modes, security, compatibility and whether text matches code.

### Card review

A Fable product review (the project's Fable prompt; DeedKit:
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

It returns one line:

```text
VERDICT: APPROVE | CHANGES REQUIRED — <b> blocking, <m> major, <n> minor — <report path>
```

## Rounds

1. One round covers the whole packet: code and criterion text together. One run per round:
   never a duplicate run; an empty or failed run goes to the orchestrator, not to a rerun.
2. Scope freezes when the round starts; an optional addition goes to a follow-up card. A stop
   order or a change to what the owner keeps reaches the author at once.
3. The author fixes every finding in one pass and searches for every term the fixes changed.
4. After a blocking or major finding, the reviewer that raised it rechecks once (r2): the fix
   range and its own open findings, tests only for the fix, no fresh exploration of the
   repository. It is never a fresh full review, and a recheck's minor opens no round.
5. Reviews stop at r2. An r3 runs only for a new blocking finding that r2 raised. A blocking or
   major finding stays open until the reviewer that raised it confirms the fix; never merge with
   it unconfirmed. A fix made after the cap (r2 done) gets one scoped recheck by that reviewer,
   outside the round count, recorded as `recheck` in `Review record:`; a regression test that
   fails before the fix and passes after it is the precondition, not a substitute. A new blocking
   there, or a finding still open, is BLOCKED to the orchestrator, who picks a smaller scope, a
   follow-up card or a revert. Merging with an open blocking or major finding accepts a known
   risk, which the owner keeps.
6. Cards: a contract card gets Astra r1 and at most one scoped recheck; a small card gets only
   the author's self-check.
7. A production operation plan gets at most two Astra rounds before the owner authorizes it.
8. Process text (AGENTS.md, CLAUDE.md, `.claude/`, the project's operations docs and Tiden CI
   document, and this harness) gets the author's checklist: each changed rule matches the
   documents that refer to it, nothing from process.md section 2 changes without the owner's
   quoted words, process.md and AGENTS.md stay within their word budgets and the PR names the
   words it replaced. Astra reviews it only when it changes how a gate works.

## Codex budget

Codex reviews and the owner's other Codex work share one weekly meter across all projects (owner
10-21: DeedKit, the Between production bot and fintrack use one Codex subscription); a point is
one percent. Each project spends at most its daily points (`<codex points>`, the project's review
notes) on a line the project states; the meter reading includes every project's use, so each
project's line is measured against the shared meter. The orchestrator reads the meter (`rate_limits.primary.used_percent`
in the newest file under `~/.codex/sessions`) before it starts a review. Under the line every
review starts. Over it Astra starts only risk-path reviews; the rest wait or, at the
orchestrator's call, go to an Opus reviewer subagent with the same packet and questions. Risk
paths: process.md section 2 items, release, deploy and the project's promotion tool, CI gate
integrity, security and access, migrations, and the live-action code the project lists. Every
Codex run passes `-c service_tier="default"`: the priority tier costs about twice as much per
token.

Standing approval (owner 10-19): the orchestrator may tune review routing within this document
(round cap, recheck scope, severity rubric, the Codex line and the reviewer for non-risk code)
and reports each change in the morning digest. It never removes Astra r1 from a risk path,
the never-downgraded list above, the recheck rules of Rounds 4 and 5 or anything in process.md section 2.

## Pull request lines

- `Change path:` comes from the changed files and process.md section 2. Grep the diff for its
  signals (the project's grep). A hit is an item to read, not a verdict; no hit does not make a
  PR AUTO. Read the changed blocks of every listed file (a workflow's `permissions:` and jobs, SQL
  grants, secret mounts); in doubt the path is OWNER. A card's own level label adds no merge gate.
- `Review record:` one line per reviewer and round with its counts, e.g.
  `Review record: Astra r1 1 blocking, 2 major; r2 0/0`. No findings ledger or per-finding
  machine lines are kept.
- `Owner note:` for a product PR, what the owner will notice in daily use.
- `Spec impact:` and `Settle text:` on every PR (tiden.md, One Tiden branch per pull request).
- `Model: <model> <effort>` on every PR (Routing).
