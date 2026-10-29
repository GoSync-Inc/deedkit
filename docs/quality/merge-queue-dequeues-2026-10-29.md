# Merge-queue dequeues, 2026-10-15 to 2026-10-29 (ANYM-625)

Question: why are about one in five merge-queue entries removed without merging, and how can the
merge conflicts behind most of them be prevented?

Window: 2026-10-15T00:00Z to 2026-10-29T00:00Z. The first queue event in the repository is
2026-10-16T20:47Z. The data was read at 2026-10-29T01:35Z, after the window closed.

## Method

1. GraphQL timelines of every pull request in the repository (427, all states, 50 per page; none
   had more than 100 queue events):

   ```graphql
   query($cursor:String){repository(owner:"GoSync-Inc",name:"deedkit"){
    pullRequests(first:50, after:$cursor, orderBy:{field:UPDATED_AT,direction:DESC}){
     pageInfo{hasNextPage endCursor}
     nodes{number state mergedAt
      timelineItems(first:100, itemTypes:[ADDED_TO_MERGE_QUEUE_EVENT,REMOVED_FROM_MERGE_QUEUE_EVENT]){
       totalCount nodes{__typename
        ... on AddedToMergeQueueEvent{createdAt actor{login}}
        ... on RemovedFromMergeQueueEvent{createdAt reason actor{login}}}}}}}}
   ```

   Replaying the events in time order, every removal matched an earlier add, and no PR was added
   twice without a removal in between.
2. For each `merge_conflict` removal, the paired entry is the one whose `merged` removal came last
   at or before it. Changed files come from `pullRequest.files`. The removed PR's head at removal
   time comes from its `PULL_REQUEST_COMMIT` / `HEAD_REF_FORCE_PUSHED_EVENT` timeline: the
   `beforeCommit` of the first force push after the removal, else the last commit before it.
3. Reproduction: `git merge-tree --write-tree --name-only --no-messages <paired entry's squash
   commit> <removed head>`, with git choosing the merge base. A conflict counts as "reproduced"
   when the exit status is 1 and paths are listed. For each path, `git log
   <merge-base>..<squash commit> -- <path>` names the `main` commit on the other side, and the
   conflict markers in the written tree give the lines.

## Counts by reason (removals in the window)

| Reason | Removals | Removed by |
| --- | --- | --- |
| `merged` | 356 | merge queue |
| `failed_checks` | 14 | merge queue |
| `merge_conflict` | 13 | merge queue |
| `manual` | 9 | `egerev` (the account all agents use: the event records neither the session nor a reason) |

Non-merge removals: 36 of 392 enqueues (9.2 %) over the 14 days, with 36 re-enqueues. 10-28
alone had 11 of the 13 conflicts. The research figure "8 of 38 (21 %)" was one peak day.

## The 13 conflict removals: all reproduced

| Removed | At (UTC) | Conflicting path (`merge-tree`) | Other side on `main` | Lines | Cause |
| --- | --- | --- | --- | --- | --- |
| #83 | 10-17 08:10 | `.github/workflows/checks.yml` | #86 (paired) | 156-183 | A |
| #264 | 10-23 07:07 | `tests/test_reply_queue.py` | #263 (paired) | end of file (453-544 of 544) | C |
| #349 | 10-28 09:31 | `services/gateway/src/agent_gateway/http_clients.py` | #347 (paired) | 42-49 | A |
| #349 | 10-28 13:31 | `services/calendar/family_calendar/service/api.py`, `core.py` | 371eb114 (ANYM-555), not the paired #366 | api 289-293, core 616-664 | A |
| #358 | 10-28 13:55 | `services/messaging/messaging_service/backup_r2_verifier.py`, `tests/test_backup_r2_verifier.py` | #369 (paired) | source 301-305; test end of file (586-643 of 643) | B (+C) |
| #358 | 10-28 14:35 | `backup_r2_verifier.py` | #351 (paired) | 301-305 | B |
| #381 | 10-28 14:35 | `backup_r2_verifier.py` | #351 (paired) | 258-264 | B |
| #386 | 10-28 15:22 | `services/calls/tests/test_call_intent.py` | #385 (paired) | end of file (553-658 of 658) | C |
| #390 | 10-28 21:53 | `services/messaging/messaging_service/chat_evidence.py` | #404, not the paired #408 | 187-193 | D |
| #415 | 10-28 22:35 | `services/messaging/messaging_service/backup.py` | #414 (paired) | 227-305, three hunks | B |
| #417 | 10-28 22:43 | `tests/test_security_workflow.py` | #416, not the paired #393 | end of file (107-130 of 130) | C |
| #420 | 10-28 22:43 | `tests/test_production.py` | #418, not the paired #393 | end of file (5011-5032 of 5032) | C |
| #420 | 10-28 23:08 | `tests/test_production.py` | #419 (paired) | end of file (5026-5113 of 5113) | C |

No shared-file hypotheses are left: every removal reproduces. In 9 of 13 the other side is the
paired entry. In the other 4 it is an earlier `main` commit the removed PR had not been rebased
onto. Generated or committed build outputs played no part: every conflicting path is hand-written
source, a test module or the CI workflow. (#420 is ANYM-626's own PR. Its third enqueue merged
after its test moved from the end of `tests/test_production.py` to the middle.)

## Causes and proposed fixes

| Cause | Removals | Fix | Owner |
| --- | --- | --- | --- |
| A. Two PRs edit the same region of a file that the lane rules already serialize (`ci` lane, limit 1: #83/#86) or require to be disjoint (module lanes `services/calendar/`, `services/gateway/`: #349 with #347 and with ANYM-555) | 3 | Pre-enqueue check: `git merge-tree` of the PR head against `main` and against the head of every entry already in the queue (`mergeQueue(branch:"main"){entries{nodes{headCommit{oid}}}}`). On a conflict, rebase instead of enqueueing. | Orchestrator: `~/deedkit-orchestrator/bin/wait-merge.sh` or the enqueue step (finding sent to the orchestrator, no repository change) |
| B. Parallel PRs edit backup modules, which no lane covers: `backup_r2_verifier.py` (#351, #358, #369, #381 open together on 10-28) and `backup.py` (#414, #415) | 4 | A `backup` lane (`services/messaging/messaging_service/backup*.py` and their tests, 2 concurrent PRs on disjoint files) in `docs/operations/parallel-development.md` | Repository: proposal card ANYM-636 (Tiden branch `proposal/2026-10-29-backup-lane`, a draft for its card review) |
| C. Two PRs append tests at the end of the same test module | 5 (+1 inside #358) | The pre-enqueue check from A. Also an author habit: put a new test next to related tests, not at the end of a large shared module. A `merge=union` driver for test modules is **not** proposed: Python silently keeps the last of two same-named test functions, and the only ruff run in CI is report-only (`security.yml:106-112`, `continue-on-error: true`), so no required check would catch the redefinition, and a union merge could drop a test unnoticed. | Orchestrator, with A; the habit as a worker-brief line (orchestrator) |
| D. A single overlap in an unlaned messaging module (`chat_evidence.py`, #390 with #404) | 1 | The pre-enqueue check from A. One case does not justify a lane. | Orchestrator, with A |

The pre-enqueue check covers 11 of the 13 directly. In each of those, the other side was already
on `main` or in the queue at least 2 min before the removed PR was enqueued. #264 was enqueued
37 s after #263 had merged, and the queue took 8 min to find the conflict. The other 2 came in the
13:12Z batch enqueue on 10-28: #358 was enqueued 1 s after #369, and #349 4 s after 371eb114
merged. A check that runs in parallel with a batch would miss them, so a batch has to enqueue one
PR at a time, running each check after the previous enqueue. The `backup` lane would have caught 4
of the 13 even earlier, at dispatch.

Cost per avoided conflict removal: about one queue run (~10 min) and ~34 billable job-minutes
(mean merge_group run, 2026-10-14..28, ANYM-626 PR #420), plus a re-enqueue. With build
concurrency 6 (ANYM-626, live since 2026-10-29T01:28Z), the speculative builds behind a removed
entry are rebuilt too.

## Unknowns

- Why the 9 `manual` removals happened: the event records only the shared `egerev` account.
- The 14 `failed_checks` removals were not analysed (out of scope: ANYM-625 covers conflicts).
