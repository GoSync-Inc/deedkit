# Review protocol

Who reviews what is in [AGENTS.md](../../AGENTS.md) section 5. This is the procedure: the packet,
the rounds and the verdict.

## Packet

A review is one packet of files in the author's scratchpad and one report file. Reviewers have no
Tiden access and get projections, never whole exports, other reviewers' reports or the review
history.

- `task.md`: the task goal and the card's criteria, the round, and for a recheck the reviewer's
  own open findings. An Astra task starts with the four fixed questions of AGENTS.md section 5,
  verbatim, before any task-specific question. A code packet names the deployed versions for
  question 3: the production SHA, the host `production.py` SHA, the schema and the installed
  gateway (read-only `production.py status`, or from the orchestrator).
- `diff.patch`: `git diff <merge-base>..<head>`; for a recheck, only the fix range. A criterion
  edit on the PR's `Tiden branch:` goes in as previous and proposed text.
- `specs.md`: the full text of the specs the PR delivers and of those whose criteria change; IDs
  and titles for the other specs it names or touches.
- `pr.md`: the pull request description.

A card has no diff: its packet holds the proposed text, the search results for similar specs (ID,
title, status), the text of specs it references and `git ls-files -- <expected paths>`.

A Fable product review (docs/operations/prompts/fable-product-review.md) is one round on a
product card; it gets the owner's request in the owner's words, the card and the proposed
user-visible behaviour. No code. Fable does not review the finished behaviour; a product PR
states what the owner will notice in an `Owner note:` line instead.

A missing input is a finding, not a reason to guess. Reviewers do not re-run what CI proves
(the required checks, linters, the full suite in the merge queue); they spend the round on logic,
failure modes, security, compatibility and whether text matches code.

## Report and verdict

The reviewer writes `review-<reviewer>-<unit>-r<n>.md` next to the packet. Each finding has a
severity, `file:line`, the failure scenario and a suggested fix:

- **blocking**: a contract, security or data defect would ship;
- **major**: a real defect ships (wrong behaviour, a data or security risk) or a promised
  criterion is untested;
- **minor** and **nit**: the author fixes them in the PR or drops them.

Severity is the consequence left after a named, independent, deployed control: a miss that such
a control bounds is minor. A changed verifier never counts as its own control. Never downgraded:
an unmet section 2 authority, a provider-session or send action, a data-loss or backup defect,
and broken failure isolation (a fail-closed stop of call audio, collection or backup is itself a
failure).

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
   major finding stays open until that reviewer confirms the fix; a major still open after r2
   closes with a test that fails before the fix and passes after it, otherwise the orchestrator
   picks a smaller scope, a follow-up card or a revert. Merging with an open blocking or major
   finding accepts a known risk, which the owner keeps.
6. Cards: a contract card gets Astra r1 and at most one scoped recheck; a small card gets only
   the author's self-check.
7. A production operation plan gets at most two Astra rounds before the owner authorizes it.
8. Process text (AGENTS.md, CLAUDE.md, `.claude/`, `docs/operations/`, TIDEN-CI.md) gets the
   author's checklist: each changed rule matches the documents that refer to it, nothing from
   AGENTS.md section 2 changes without the owner's quoted words, AGENTS.md stays within 1,000
   words and the PR names the words it replaced. Astra reviews it only when it changes how a
   gate works.

## Codex budget

Astra and the owner's other Codex work share one weekly meter; a point is one percent. DeedKit
spends at most 4 points a day: the line is `68% + 4 × days since 10-19 03:30Z` until the
10-24 21:08Z reset, then `4 × days since the last reset`. The orchestrator reads the meter
(`rate_limits.primary.used_percent` in the newest file under `~/.codex/sessions`) before it
starts a review. Under the line every review starts. Over it Astra starts only risk-path
reviews; the rest wait or, at the orchestrator's call, go to an Opus reviewer subagent with the
same packet and questions. Risk paths: AGENTS.md section 2 items, release, deploy and
`production.py`, CI gate integrity, security and access, migrations, sends and live
provider-session code. Every Astra run passes `-c service_tier="default"`: the priority tier
costs about twice as much per token.

Standing approval (owner 10-19): the orchestrator may tune review routing within this document
(round cap, recheck scope, severity rubric, the Codex line and the reviewer for non-risk code)
and reports each change in the morning digest. It never removes Astra r1 from a risk path,
the never-downgraded list above or anything in AGENTS.md section 2.

## Pull request lines

- `Change path:` comes from the changed files and AGENTS.md section 2. Grep the diff for its
  signals. A hit is an item to read, not a verdict; no hit does not make a PR AUTO. Read the
  changed blocks of every listed file (a workflow's `permissions:` and jobs, SQL grants, secret
  mounts); in doubt the path is OWNER:

  ```sh
  git diff --name-only origin/main... | grep -E '^\.github/workflows/|main-ruleset\.json|\.sql$|migrations/|gitleaks|compose|\.claude/settings\.json|^AGENTS\.md$'
  git diff -U5 origin/main... | grep -E -i '^[-+].*(secret|permissions|: *write|write-all|\b(GRANT|REVOKE|DROP|TRUNCATE|DELETE FROM|RENAME)\b|ALTER )'
  ```

  A card's own level label adds no merge gate; a host-tool change still waits for the owner to
  install it before autodeploy resumes (docs/operations/production.md).
- `Review record:` one line per reviewer and round with its counts, e.g.
  `Review record: Astra r1 1 blocking, 2 major; r2 0/0`. No findings ledger or per-finding
  machine lines are kept.
- `Owner note:` for a product PR, what the owner will notice in daily use.
