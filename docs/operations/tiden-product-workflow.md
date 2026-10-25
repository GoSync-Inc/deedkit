# Product work through Tiden

Tiden is DeedKit's record of product requirements, planned work, delivery state and test
evidence. `BACKLOG.md` is a navigation entry point and legacy-ID index. The workflow (specs and
statuses, card intake and template, one Tiden branch per pull request, the intent loop, commands)
is harness `tiden.md`; how a change records itself is harness `process.md` and
[AGENTS.md](../../AGENTS.md) section 4; CI details in [TIDEN-CI.md](../../TIDEN-CI.md). This
document holds DeedKit's values.

Use the existing [DeedKit product](https://app.tiden.ai/1dc18e09-4a09-4d74-8300-cd4302622357/products/75bd0580-2af4-4d5a-b236-c5a9d7ff7ce1)
(workspace `1dc18e09-4a09-4d74-8300-cd4302622357`, product
`75bd0580-2af4-4d5a-b236-c5a9d7ff7ce1`; pass them when a worktree is not bound). The `Tiden
health` report (TIDEN-CI.md) fails on a `Done` spec that carries an `[open: live]` or
`[open: measured]` marker.

## Card intake

Steps 1–4, the template and the scales: harness `tiden.md`. DeedKit's step 5:

5. **Queue.** Current breakages and deadlines first; then about 70% product and 30% engineering
   by Effort, at least one refactor when one is ready, each share by Score. From 10-19 no new
   meta-research or process tooling is queued until ten product cards are delivered, unless the
   owner asks; incident fixes are allowed.

The card template, categories and estimation scales are harness `tiden.md`. DeedKit's Effort
reference pull requests: 1 = #59, 2 = #66, 3 = #52, 5 = #68, 8 = #40. `History:` names the DK-*
identity. `How others solve it` studies include the built-ins of Docker, Caddy, Postgres or
GitHub.

## One Tiden branch per pull request

`scripts/spec_impact.py <base> --body-file <body>` builds the `Spec impact:` list (harness
`tiden.md`). Maintenance windows are in TIDEN-CI.md.

Close only an intent branch whose pull request merged or closed and whose session is closed;
proposal and review branches stay open until implemented (a proposal's merge to Tiden main at the
start of its work is the next section), and an intent branch with an open
session or pull request stays open (closing it closes the session). Such a branch can stay
`open` when its auto-merge status (`autoMerge` in `branch merge-preview`) never sees the merge,
for example `no_merged_pr`. Close it with `tiden branch close --name <branch>` (CLI 0.40 or
later): final, and it keeps specs, links and runs. Never `branch delete` it.

## Proposal branch to Tiden main

A proposal branch stays open while it is future-only; never merge it merely to register that
its cards exist. Check A needs a pull request's specs on Tiden main or on the branch it names,
and it refuses a branch that names an Epic or Feature with children, and the CLI cannot move one
card off a branch keeping its number. So once the owner has accepted the proposal's cards into
the ready queue and the first card's work starts, the worker of that first card:

1. runs `tiden branch merge-preview` for the proposal: it must hold only the proposal's cards
   and no conflicts; otherwise stop and report BLOCKED;
2. merges the whole proposal to Tiden main and reads back the merged cards (IDs, statuses);
3. then names each pull request's story on main (`Spec:` / `Delivers:`), not the branch.

Cards on the proposal that are not accepted (Backlog, Review) merge with it and stay Backlog on
main; the queue takes only accepted cards. This is not an exception that needs the owner's words
and is not a relocation: no card is recreated or deleted, and IDs stay. Branch hygiene and
closing: previous section.

## Agent feedback

Agent feedback becomes product work through the owner-started private triage in
[feedback-triage.md](feedback-triage.md): one ledger, clusters per card, a dispositions file.

## Commands

Bulk maintenance (snapshots, provenance, manual watermark advances) follows
[the reconciliation receipt](../quality/tiden-reconciliation-2026-10-16.md#maintenance-procedures).
