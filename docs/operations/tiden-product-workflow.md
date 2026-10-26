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

Steps 1–4, the template and the scales: harness `tiden.md`. DeedKit's steps 5 and 6:

5. **Queue.** Current breakages and deadlines first; then about 70% product and 30% engineering
   by Effort, at least one refactor when one is ready, each share by Score. From 10-19 no new
   meta-research or process tooling is queued until ten product cards are delivered, unless the
   owner asks; incident fixes are allowed.
6. **Component.** Whoever creates a spec on a Tiden branch (the card writer, or a worker's
   settle) sets its component there, the one that owns the card's expected paths (names in
   ARCHITECTURE.md, UUIDs from `tiden component list`): `tiden spec update --id <id> --branch
   <branch> --component-id <uuid>`, then `tiden spec get <id> --format json` shows `componentId`.
   Not `tiden spec attribute`: it adds the worktree's changed files as component edges (ANYM-506).

   After the merge, one agent reads `componentId` of the merged specs on main with `tiden spec
   get`: for a ready merge the card writer, once its ready-merge run read the specs back; for a
   pull request's own branch the orchestrator, once that squash's sync B run is green (TIDEN-CI.md,
   Main sync). A missing component is not set by a `spec update` on main (harness `tiden.md`,
   Commands): the orchestrator lists it and sets it in the next maintenance window (TIDEN-CI.md,
   Main sync).

The card template, categories and estimation scales are harness `tiden.md`. DeedKit's Effort
reference pull requests: 1 = #59, 2 = #66, 3 = #52, 5 = #68, 8 = #40. `History:` names the DK-*
identity. `How others solve it` studies include the built-ins of Docker, Caddy, Postgres or
GitHub.

## One Tiden branch per pull request

`scripts/spec_impact.py <base> --body-file <body>` builds the `Spec impact:` list (harness
`tiden.md`). Maintenance windows are in TIDEN-CI.md.

Close only an intent branch whose pull request merged or closed and whose session is closed;
proposal and review branches stay open until implemented, and an intent branch with an open
session or pull request stays open (closing it closes the session). Such a branch can stay
`open` when its auto-merge status (`autoMerge` in `branch merge-preview`) never sees the merge,
for example `no_merged_pr`. Close it with `tiden branch close --name <branch>` (CLI 0.40 or
later): final, and it keeps specs, links and runs. Never `branch delete` it.

## Agent feedback

Agent feedback becomes product work through the owner-started private triage in
[feedback-triage.md](feedback-triage.md): one ledger, clusters per card, a dispositions file.

## Commands

Bulk maintenance (snapshots, provenance, manual watermark advances) follows
[the reconciliation receipt](../quality/tiden-reconciliation-2026-10-16.md#maintenance-procedures).
