# Review protocol

Who reviews what is in [AGENTS.md](../../AGENTS.md) section 5. This is the procedure: the packet,
the rounds and the verdict.

## Packet

A review is one packet of files in the author's scratchpad and one report file. Reviewers have no
Tiden access and get projections, never whole exports, other reviewers' reports or the review
history.

- `task.md`: the task goal and the card's criteria, the round, and for a recheck the reviewer's
  own open findings. An Astra task starts with the four fixed questions of AGENTS.md section 5,
  verbatim, before any task-specific question.
- `diff.patch`: `git diff <merge-base>..<head>`; for a recheck, only the fix range. A criterion
  edit on the PR's `Tiden branch:` goes in as previous and proposed text.
- `specs.md`: the full text of the specs the PR delivers and of those whose criteria change; IDs
  and titles for the other specs it names or touches.
- `pr.md`: the pull request description.

A card has no diff: its packet holds the proposed text, the search results for similar specs (ID,
title, status), the text of specs it references and `git ls-files -- <expected paths>`.

A Fable product review (docs/operations/prompts/fable-product-review.md) gets the owner's request
in the owner's words, the card, and the user-visible behaviour: tool outputs, flows and examples
from the tests or a synthetic run. No code.

A missing input is a finding, not a reason to guess. Reviewers do not re-run what CI proves
(the required checks, linters, the full suite in the merge queue); they spend the round on logic,
failure modes, security, compatibility and whether text matches code.

## Report and verdict

The reviewer writes `review-<reviewer>-<unit>-r<n>.md` next to the packet. Each finding has a
severity, `file:line`, the failure scenario and a suggested fix:

- **blocking**: a contract, security or data defect would ship;
- **major**: a real defect or an untested criterion;
- **minor** and **nit**: the author fixes them in the PR or drops them.

It returns one line:

```text
VERDICT: APPROVE | CHANGES REQUIRED — <b> blocking, <m> major, <n> minor — <report path>
```

## Rounds

1. One round covers the whole packet: code and criterion text together.
2. Scope freezes when the round starts; an optional addition goes to a follow-up card. A stop
   order or a change to what the owner keeps reaches the author at once.
3. The author fixes every finding in one pass and searches for every term the fixes changed.
4. After a blocking or major finding, the reviewer that raised it rechecks the fix range only,
   with its open findings. It is never a fresh full review, and a recheck's minor opens no round.
5. A blocking or major finding stays open until that reviewer confirms the fix. When the
   recheck leaves one open, the orchestrator decides: one more recheck, a smaller scope or a
   revert. Merging with it open accepts a known risk, which the owner keeps.
6. A production operation plan gets at most two Astra rounds before the owner authorizes it.
