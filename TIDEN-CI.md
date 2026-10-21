# Tiden verification evidence

CI keeps the original Python and Node JUnit reports, their normalized copies,
the exact **tested checkout SHA**, step outcomes and the GitHub Actions URL in
the `ci-evidence` artifact, including failed runs. Python uses xunit1 to retain
source paths; Node's generic `test` classname is qualified by its source file.
Parameterized Python executions retain their parameter names and status.

Test jobs run in parallel and each uploads a `ci-evidence-<job>` fragment:
`checks / verify` the seven checks and its module reports, `checks / load` the
gateway load gate, and each `checks / python (<n>/4)` job its shard of the root
Python/PostgreSQL suite. The root suite runs in four file-level shards
(`scripts/python_shards.py`): `checks / verify` runs shard 1 and the three
`checks / python (<n>/4)` jobs (`python-2` to `python-4`) shards 2-4, each on its
own runner with its own PostgreSQL. Every test module belongs to exactly one shard, chosen by committed
per-file timings (`scripts/python_shards.json`, refreshed with
`python scripts/python_shards.py weights <python.xml>`); a module without a
timing still runs in exactly one shard, and a module pytest would collect that
the plan cannot name (outside `tests/`, through a directory symlink) is a
collection error. Each job runs pytest through the selector of the workflow's
own commit (`ci-tools`), so a baseline of a revision without the selector is
sharded the same way. The `checks / evidence` job merges the fragments into
`ci-evidence` and the four shard reports into one `python.xml`, so test
identities do not change. `anymsg-ci::python` passes only when every shard
passed with its own valid report of executed tests and no failed one, and every
shard job succeeded. Each shard is a job of its own rather than one matrix, so
the evidence job receives each shard job's completed result and captured SHA. A
shard job has no receiving-commit check of its own: its captured SHA must be
verify's, which verify checks, so its result is that of its tests, also in a
baseline of another source. A `python_files` pattern with a directory is a
usage error in a sharded run.
A fragment counts only when it holds just the files and checks its own job
produces and names the tested source SHA that both its job output and verify
captured; a job that did not succeed in this run attempt never contributes an
earlier attempt's fragment. A job without such a fragment contributes no
reports, and its checks take its job result, so they never read as passed. The
evidence is named by the assembling run attempt and is retained either way;
the job then fails when a fragment is missing or a test job failed, was
cancelled or was skipped, and when the prepared evidence records a failed test
or a failed or blocked check (a missing, invalid or only-skipped report),
whether or not Tiden reporting is configured. So `CI gate` never depends on the
outcome of the Tiden publication. The reporting job still fails on a
configuration error, a missing evidence artifact or an unproven receipt.

The native Go suites reach Tiden through the `checks / go` job (ANYM-421): it
runs `go test -race -count=1 -json ./...` in `whatsapp/whatsmeow` and in
`services/release-router` and uploads both streams in its `ci-evidence-go`
fragment as module checks `deedkit-ci::whatsmeow` and
`deedkit-ci::release_router`. The suites also still run in ci.yml's `native`
job, which production promotion requires; a job of another workflow cannot feed
this evidence. Go results are not JUnit, because Tiden lowercases JUnit
identities and the existing Go cases and their links would be orphaned.
`scripts/tiden_ci.py` turns each stream into Tiden result rows named exactly as
Tiden's own Go reporter named them: signature `<Package>::<Test>` byte for byte
(subtests `<Test>/<sub>`; a test with subtests is no case of its own), title the
test name, suite the package's last path element and `file_path` the bare name
of the one `_test.go` file that declares the top-level test. Only those names,
the status and the duration leave the runner; test output stays in the GitHub
artifacts (`ci-evidence-go` 1 day, `ci-evidence` 14 days). A test that started
without a verdict failed. A stream without test results, a malformed event, a
package outside its module, a test whose file is missing or ambiguous, or a
package failure that no failed test explains blocks or fails that module's check
only, and so fails `checks / evidence`. A missing `go` fragment blocks both.
Reused evidence passes only with rows of exactly that shape, unique, and of the
check's own module (read from `go.mod` in the checkout). Each
row's ID is a UUIDv5 of the publishing run attempt, the check and the signature,
so a main push that publishes reused queue or pull request evidence gives the Go
rows its own IDs.

Three root-suite cases run in `checks / verify` steps of their own instead of
the shards (ANYM-429). Both shard command lines `--deselect` them:
`tests/test_collectors.py::test_session_lock_rejects_directory_chowned_to_foreign_uid_as_root`
needs root and runs under `sudo` as module check `deedkit-ci::root_session_lock`
(`root-session-lock.xml`, handed back to the runner user).
`tests/test_audio_native.py::test_optional_cpu_package_loads_without_gpu_or_model_download`
and `::test_faster_whisper_decoder_compatibility_without_model` need
faster-whisper, so they run in the `enricher-test` image with no network, a
read-only root filesystem and the runner's uid, as module check
`deedkit-ci::enricher_audio` (`enricher-audio.xml`). Verify builds that image in
the background, and only the audio step waits for it
(`AUDIO_IMAGE_WAIT_SECONDS`). Such a module passes only when its report holds
exactly its `REQUIRED_EXECUTIONS` (scripts/tiden_ci.py), each once and passed;
a missing, skipped, extra or duplicate case fails it. A missing report blocks
it, and that is what happens for a tested source without the node or the target.
Assembly refuses evidence whose shard reports hold one of these cases.

The separate reporting job receives no database or messenger credentials. It
uses Tiden CLI 0.33.0 from the official release with a pinned SHA-256 checksum.
Fork pull requests do not receive the Tiden secret or publish evidence. Package
release workflows that do not pass this optional secret still verify packages.

## Configuration

In GitHub Actions repository settings, add:

- Secret `TIDEN_API_TOKEN`: a Tiden workspace API credential.
- Variables `TIDEN_WORKSPACE_ID` and `TIDEN_PRODUCT_ID` for the linked product.
- Optional variable `TIDEN_BRANCH_MAP`: a JSON object mapping Git branches to
  existing Tiden spec branches, for example `{"codex/example":"intent/example"}`.
  `main` maps to Tiden `main` automatically.

A mapping must name an existing Tiden branch and always wins. Otherwise a pull
request run uses the intent branch linked in the pull request description, as the
intent loop requires (paused; see
[the product workflow](docs/operations/tiden-product-workflow.md#one-tiden-branch-per-pull-request)):

```text
Tiden intent branch: [intent/<date>-<slug>](https://app.tiden.ai/<workspace>/products/<product>/branches/<id>)
```

CI reads only that line, from the pull request event file rather than an
environment variable, and keeps only the branch name and IDs. The link must be in
this workspace and product, its text must name the branch its ID resolves to, and
that `intent/` branch must be open. Exactly one such branch becomes an explicit
target; any other link is ignored. The event is captured when a run starts and a
re-run reuses it, so add the line before opening or pushing the pull request.
Without a usable link, a pull request run publishes nothing: the
`tiden-route.json` receipt records `skipped` with the reason, and the job log
shows a notice when the line is missing or a warning when its link is unusable;
the check stays green. The merged code reaches Tiden through the main push run.
Any other run without an explicit target, such as a manual dispatch without
`tiden_branch`, is reported under its own Git branch name. Tiden skips live-doc
sync for a branch name it does not know, so the publisher first creates a Tiden
branch of that name when none exists. Tiden may instead attribute the run to the
intent branch whose session (`tiden intent start` on that Git branch) recorded it.
The publisher reads the result back and accepts only that same-named branch or an
`intent/` branch, never `main`. For such runs, Git branch names Tiden cannot use
(uppercase, dots) still need a mapping. The `tiden-route.json` receipt records the
chosen branch and why.
Do not label a feature run as `main` just to make the main gate green. Failed
evidence association fails the publication step. Without a token the job clearly
states that reporting is not configured. No workspace credentials belong in Git.

A publication failure is a warning, not a red gate. In this document a failed
publication is a failed CLI install or publication step, and the reporting job
still succeeds. That covers a Tiden error, a refused route or association, a
contradicting verdict or count and a hang past the step timeout. Every step of
the reporting job has its own timeout, and together they stay below the job's
20 minutes, so a hung Tiden call ends as a step failure and never as a cancelled
job. The job then logs a `Tiden evidence not published` warning naming the
failed step and writes `tiden-publication.json` (`published: false`: the
publication was not verified). It retains that file with any Tiden response in
the `tiden-receipt` artifact. The merge queue, docs-only and pull request reuse
receipts are not publications and still fail the job when unproven, and so do a
configuration error and a missing evidence artifact.

The run's evidence in Tiden is then unverified. Tiden may hold no run for the
commit, a partial one or a completed run whose association or counts were not
confirmed. A main commit records no release (the release record warns), and
`production.py verify` does not require the publication, so the commit stays
promotable. The agent reads the run itself, because no Tiden check is sure to
notice: a publication that failed after Tiden completed its run leaves a run
that reads as passed, and the intent close reads the product's latest run,
which may be another pull request's. On a pull request that runs the loop, the
author reads its last run's reporting job before `tiden intent close`. After a merge, the agent
that merged it reads the `main` push run of the squash before reporting the
merge as checked. When the reporting job warned, the agent reruns that run with
**Re-run all jobs**.
`Tiden health` measures only the current `main` head, and drift reads only
anchors, so neither replaces that read. Republishing always takes **Re-run all
jobs**, the full suite: the reporting job no longer fails, so **Re-run failed
jobs** selects nothing. A rerun of the reporting job alone gets an attempt URL
that its `ci-evidence` does not name, so Tiden drops the seventeen check rows and
the count check fails.

CI runs on `push` only for `main`. A feature branch is verified by its pull
request run, so a push does not repeat CI or publish a second copy of the
evidence to a same-named Tiden branch.

Each `main` push run has its own concurrency group, so neither a later merge
nor a manual dispatch cancels it: every merged SHA keeps a complete CI result
for promotion and for Tiden. Concurrent push runs can therefore publish to
Tiden `main` out of SHA order; match evidence by its recorded SHA. A newer pull
request run still cancels the older run of the same pull request, and a newer
dispatch the older one of the same ref. A pull request run builds the container
image targets only when its merge commit changes an image input listed in
`.github/workflows/images.yml`, or when the merge diff cannot be established;
otherwise the build is skipped and `CI gate` stays green. A failed change
detection or build turns `CI gate` red. A merge queue run never builds them:
the pull request run built any changed input, and the main push builds every
target before publication. Push and dispatch runs always build them. The push
run publishes the eight images, then confirms their attestations, in parallel
threads; every target finishes its own write-once decision before the first
refusal fails the job.

A merge queue run (`merge_group`, Git branch `gh-readonly-queue/...`) verifies
the temporary group commit with the same required checks, `CI gate` and
`checks / verify` (or reuses a full pull request run of its identical tree,
below), but publishes nothing to Tiden and creates no Tiden branch.
This takes precedence over every target above, including an override, a map
entry or a pull request link. A queued group can contain a fork's unmerged code,
so a `merge_group` run never receives `TIDEN_API_TOKEN`. A queue run checks no
Tiden configuration and installs no CLI: its reporting job records `skipped` with the reason in
`tiden-route.json`, logs a notice and stays green. The merged code reaches Tiden
through the main push run. Each queue run has its own concurrency group and
cancels no other run. Queue runs take their workflow files from the group commit,
so review workflow changes in a fork's pull request before queuing it.

A queue run whose group commit has exactly the Git tree a passing, full pull
request run tested reuses that run's verification instead of testing the same
code again (ANYM-358). On `merge_group` only, the job `Reuse the pull request
verification` (`contents: read`, `actions: read`, no secret) reads the pull
request named by the group's `gh-readonly-queue/main/pr-<N>-<sha>` ref; it must
be open, into `main` and from this repository. The latest `pull_request` run of
its head must have succeeded with `checks / verify`, `checks / load`,
`checks / evidence` and `CI gate` passed in its latest attempt, so a docs-only
run never qualifies. Its one current `ci-evidence` artifact is pinned by digest,
downloaded and validated as data by the push-reuse rules below. The tested
merge commit must be GitHub's merge of that head, and its tree must equal the
group commit's tree. A re-read must show the same run. Anything else, including
any error, means full verification, never a failed run. On a reuse
`checks / verify`, `checks / python`, `checks / load` and `checks / evidence` are skipped;
`native / synthetic` runs. `checks / tiden` runs no Tiden
command: its receipt records `skipped` with the reused run attempt's URL
(`tested_in`), the merge commit it tested and the group commit, and the job fails
without them. `CI gate` accepts that skip only for this decision on this group
commit. The decision script runs from the group commit's tree like every queue
workflow, so the fork exclusion selects evidence and is no sandbox; queued
workflow changes are still reviewed. Such a queue run has a skipped
`checks / verify`; its main push reuses the pull request run's evidence by the
chain rules below.

A queue run that ran the suite is the authoritative test run of a merged SHA; the
main push run of that SHA validates and publishes it instead of testing the
commit again (ANYM-322). Its first job, `Reuse the merge queue verification`,
one of the two CI jobs with `actions: read`, finds the latest merge queue run of the exact commit by the
rules `production.py verify` applies (ANYM-321), waits up to five minutes for it
to complete, and pins its latest attempt and the one `ci-evidence` artifact
created during that attempt. Without them, or on any lookup error, the push run
verifies in full. After that choice the job downloads the pinned artifact by ID
and digest as data, extracts exactly the expected files within size limits, and
validates them: every SHA field names the commit, the pinned attempt of a main
queue run assembled the evidence, all fifteen checks passed, and every report
holds passing executions that match the recorded counts. It reads the queue run
and the artifact again, then uploads the evidence unchanged as this run's
`ci-evidence` with a `publication.json` record that names this push run as
publisher and the queue run attempt and artifact as producer. A failure there
fails the push run: `checks` is skipped and `CI gate` turns red. On a validated
reuse `checks / verify`, `checks / python`, `checks / load` and `checks / evidence` are skipped;
`native / synthetic`, the image builds, `publish` and the release record run as on
every push. `CI gate` accepts a skipped `checks / verify` only for a validated
reuse of this commit; otherwise `checks / verify` must have tested this commit.

After a queue run that reused a pull request run (the PR→queue chain), the
push run reuses that pull request run's evidence instead. The queue run's
latest attempt must have passed `CI gate` and `Reuse the pull request
verification` with `checks / verify` skipped, never by a docs-only decision;
the pull request named by its group ref must have merged into `main` as exactly
this commit, and the latest run of its head must have passed every job a full
pull request run needs, be the run, at that or a later attempt, that the reuse
job's one passed step `Reused pull request run <id> attempt <n>` names, and
have been created before the queue run's latest attempt started, so it is the
run the queue decision evaluated; a deleted or replaced producer is refused,
never substituted. The job pins that run's latest attempt and its one
current artifact, validates the evidence by the rules above for the merge
commit it tested and the pull request's head branch, and then requires that
merge commit to be GitHub's merge of that head and its Git tree hash to equal
this commit's tree in the API and in the job's own checkout, read by Git
without replacement objects or inherited `GIT_*` settings. A changed queue or
pull request run, a rerun or any mismatch fails the push run. The record also
names the pull request, its head branch, the queue run attempt and the tree;
`tiden_ci.py publish` and `release` accept it only when the tree GitHub records
for the tested commit (one anonymous read of the public API), the record's tree
and their own checkout's tree of this commit are one tree. The Tiden run then names this commit, with
`tested_sha` the tested merge commit and `tested_in` the pull request run
attempt. `production.py verify` follows the same chain to the pull request run
when the push run skipped `checks / verify` after such a queue run; within one
deploy it re-reads the queue run, the pull request run and the push run.
`tiden_ci.py publish` and `release` accept the record only when it names their
own job's repository, run, attempt and commit. The Tiden run on `main`, its
fifteen check row IDs and the release then carry this push run's attempt URL,
with `tested_in`, also kept in the route receipt, naming the queue run attempt,
while executions and outcomes stay the queue run's. Queue evidence without a valid record still publishes and
records nothing. After a reuse, rerun a `checks / tiden` whose publication warned,
or a failed `Tiden release record`, with **Re-run all jobs**: a partial rerun has an attempt the record does
not name. Validating the artifact does not replace reviewing a fork's workflow
changes before queuing.

A pull request or merge queue run that changes only documentation skips the
test jobs (ANYM-323). The `checks / documentation` job classifies the run as
docs-only when every change is a plain addition, edit or deletion of a path that
matches `*.md` or `.claude/**` and none of the deny rules in
`scripts/docs_only.py`: `README.md` in any directory, `SETUP.md`,
`THIRD_PARTY_NOTICES.md`, `TELEMETRY.md`, license texts (`LICENSE*`, `COPYING*`,
`licenses/**`) and everything under `services/`, `packages/` and `whatsapp/`.
Those are the Markdown files code reads, packages or ships. A pull request run
classifies GitHub's two-parent merge of exactly this head against the base; a
merge queue run classifies its group commit against its first parent, which
must be the group's base. Any of these means full verification: a mode or type
change, an empty or uncertain diff, any other path, a failed decision, a
`Tiden intent branch:` line in the pull request description, or any other
event. On a docs-only run `checks / verify`, `checks / python`, `checks / load` and
`checks / evidence` are skipped, and the skipped `checks / verify` still
satisfies the required check. `CI gate` accepts the skip only for the docs-only
decision on this merge or group commit, `native / synthetic` and the image builds
keep their rules, and `checks / tiden` records a skipped route with the reason.
Main pushes, dispatch runs and package releases never take this path. The main
push of a docs-only group finds no queue run whose `checks / verify` passed, so
it verifies in full, and `production.py verify` accepts no commit without that
green push run. A document a test reads therefore reaches `main` untested only
until its push run, which then fails and needs a fix forward. Editing a pull
request description does not rerun CI, and a rerun reuses the captured event, so
adding a `Tiden intent branch:` line later needs a new push.

For an explicit baseline rerun, dispatch `CI` on the reporting-code branch with
`source_ref` set to the exact source revision and `tiden_branch` to the matching
existing spec branch. The checkout SHA, rather than the workflow SHA, is recorded.
Normal pull requests record their actual tested merge SHA.

## Pull request check

The `checks / Tiden PR check` job runs `scripts/tiden_pr_check.py` (check A,
ANYM-405) on every pull request run. It reads only from Tiden and checks the
bookkeeping a merge needs (AGENTS.md section 4):

- the body lines `Spec:`, `Delivers:` and `Tiden branch:` are well formed, name
  story-level specs that exist on Tiden main or on the named branch, and the
  named branch is open;
- the merge preview of that open branch (read-only `branch merge-preview`) changes
  every spec the sync would merge and nothing the body does not name: it fails
  when the preview changes a spec the body does not name, changes no spec, or
  holds more than spec changes (deletions, test or component changes, conflicts,
  an intent session, blocking sessions). A named ID the preview does not change
  passes only as a spec on Tiden main. A preview of an unknown or malformed shape
  is not checked (below), as sync B's step `merge` refuses the same shapes; a
  forbidden class is judged first, so a preview with one fails even when a spec
  number in it is malformed;
- every test the pull request adds has a `# spec: ANYM-n` or `// spec: ANYM-n`
  marker directly above it and its decorators, or is in the reviewed `NO_SPEC`
  list; existing tests need none;
- every changed code file outside drift's `EXCLUDED` list has a `repo_file`
  anchor on Tiden main, or the body names a spec for the sync to anchor it to.

Only a verdict fails the run, and so `CI gate`. A verdict is the check's list
of problems in the job summary, or a refusal of the pull request's own content:
a test file that does not parse, or a changed path that is not UTF-8. The body
comes from the event that started the run, and a rerun replays the same event:
after you edit the body, push a commit to check again. Fork pull requests and
runs without `TIDEN_API_TOKEN` skip with a summary line. Any other outcome
fails nothing: a Tiden CLI failure (unreachable, timeout, an error status or a
malformed reply), another refusal, an unexpected error, a failed checkout, Python
setup or CLI install, or a check killed at its step timeout. The job warns and
keeps a `tiden-pr-check-receipt` artifact with the last line the check printed,
when the artifact service accepts it; the sync and `Tiden drift` still check
after the merge, and a rerun of the job checks now.

## Main sync

`.github/workflows/tiden-sync.yml` runs `scripts/tiden_sync.py` (sync B,
ANYM-405) after the CI run of every main push completes, whatever its
conclusion: merged code needs its bookkeeping even when a later CI job failed.
It is a workflow of its own, so a red or dropped sync never touches the `CI`
push run that production promotion requires. For the merged
pull request named by `(#n)` in the commit subject, it:

1. merges the body's `Tiden branch:` when its preview holds only the named
   specs (check A applies the same rule, with the same parser, before the merge);
2. links each marked test the commit adds to its specs (a Go test,
   `// spec: ANYM-n` above `func TestX`, links the cases whose signature is
   `<module path>[/<package dir>]::TestX` or starts with `<that>::TestX/`, the
   module path read from the nearest `go.mod` at the commit); a marked Go test
   that already existed in a changed file is linked the same way, so subtests
   added by a new table row are covered, and is skipped when it has no case;
3. anchors each unanchored changed code file to the primary spec;
4. sets each `Delivers:` spec to Done, or to Review while it has an open live or
   measured check;
5. reads everything back.

Each failure prints `Failed at step <name>: <reason>` and fails the run. A rerun
finishes what an earlier run left. If the push run's publication left a newly
added marked test without an imported case, the sync fails at step `links`.
A green sync does not verify that publication: read the push run's own Tiden
receipt, and rerun all jobs of that CI run to publish again, which also starts
the sync again; dispatch the sync by SHA if that run is dropped.

The workflow runs main's tools with main checked out, never the commit's tree,
with `contents: read` only, which also covers reading the merged pull request.
Runs never
overlap and a run in progress is never cancelled (one concurrency group), because
the sync is the only automated writer of anchors and replaces whole source lists.
GitHub keeps one waiting run and drops an older waiting one. A later run does not
cover an earlier commit, so dispatch a dropped run again from main with its SHA
(`gh workflow run tiden-sync.yml --ref main -f sha=<sha>`).

The run title (`run-name`) is `Tiden sync <sha>`: the commit the run processes,
which is `github.event.workflow_run.head_sha` (the CI run's commit) for a
`workflow_run` run and `inputs.sha` for a dispatch. The run's own `head_sha` is
the default-branch commit and says nothing about it, so `Tiden health` finds the
sync of its TARGET by this title (ANYM-438).

The sync writes to Tiden since check A ran green on three pull requests. The
agent that merged waits for the sync run of its squash and reads back the result
(AGENTS.md section 4). A failed or dropped run is recovered only by dispatching
it again by SHA, as above, or by rerunning it, so that it joins the concurrency
group. A local `scripts/tiden_sync.py` runs only with `--dry-run`: a local write
or a source repair by hand is outside the group, and a B run that starts during
it can lose anchors, since both replace whole source lists. Such a write needs a
maintenance window agreed with the owner, with the workflow disabled and no
run in progress for the whole operation.

## Drift watermark

`.github/workflows/tiden-drift.yml` keeps the drift watermark current after every
merge (ANYM-355). It is a workflow of its own, so a red run never touches the
`CI` push run that production promotion requires. Each run checks out `main` as
it is when the run starts, not its event commit, and runs
`scripts/tiden_drift.py`:

1. It reads the watermark W (`tiden drift status`) and lists the files changed
   from W to that checkout, leaving out deletions and the categories in the
   checker's `EXCLUDED` list (Markdown and `docs/`, `.claude/`, tests and their
   fixtures, lockfiles and dependency pins). Tests trace to specs through test
   links, not anchors.
2. Every remaining path needs a `repo_file` anchor on some spec on Tiden main.
   If one lacks it, the run fails, lists the paths in its summary and leaves the
   watermark. Drift can finish before the sync of the same push: wait for that
   sync run (dispatch it by SHA if it failed or was dropped), then rerun the
   failed run. A path no sync anchors (a commit without a pull request, or a
   sync that failed at step anchors) is added as a `repo_file` source of the spec
   it implements with `tiden spec update --id <spec> --sources-file <file>` only
   in a maintenance window, with the sync workflow disabled and no run in
   progress (main sync). The command replaces the whole list, so resend every
   existing source with its fields, then rerun the failed run.
3. Otherwise it runs `tiden drift advance --from W --to <checkout> --reason
   empty_sync` once, requires `"advanced": true` and reads the watermark back.
   The server compares `--from` with the current watermark and answers a stale
   one with `"advanced": false` (verified 2026-10-18). Manual audited advances
   merge a drift-sync branch instead (docs/operations/tiden-product-workflow.md).

Runs never overlap (one concurrency group, no cancelling). GitHub may replace a
waiting run with a newer one; the run that executes covers the newest merge. A
run whose checkout the watermark already reached passes without an advance.
Diverged history, an unreadable or incomplete spec listing, or any CLI failure
before the advance fails the run with nothing sent. A failure after the advance
was sent leaves its result unknown: the run is not retried in place, and a rerun
reads the server watermark again before it acts. Without `TIDEN_API_TOKEN` the
workflow records that reporting is not configured and passes. Widening `EXCLUDED`
is a reviewed code change to the checker and its test, never a data edit.

The agent that merged a pull request checks that the `Tiden drift` run on its
squash commit, or a later one, is green before it reports the merge as checked
(AGENTS.md); a red run names the files to anchor.

## Tiden health

`.github/workflows/tiden-health.yml` reports after every merge whether Tiden
still matches the code (ANYM-360). It runs when a `CI` run of a `main` push
completes, whatever its conclusion, and on dispatch from `main`; the job's `if:`
and a shell guard refuse any other upstream event or branch. Runs never overlap
(one concurrency group, no cancelling). Each run checks out `main` as it is when
it starts and freezes that HEAD as TARGET. It has no persisted
Git credential, the pinned CLI 0.33.0 called by its path, and the Tiden token only
in its report step. Its permissions are `contents: read` and `actions: read`
(ANYM-438, the sync lookup below, no write scope); the job's `GITHUB_TOKEN`
reaches the report step alone, as `GH_TOKEN`. It is not a required check: a red run blocks no merge, release
or promotion, and `CI`, `CI gate`, `checks / verify`, the seven `anymsg-ci::*`
checks, `deploy/main-ruleset.json` and `production.py verify` do not read it.

`scripts/tiden_health.py` computes four metrics for TARGET, each `pass`, `fail`
or `unknown` with its reason, and exits 0 when all pass, 1 when any fails (after
printing every finding) and 2 when none fails and any is unknown. The run summary
and the `tiden-health` artifact (`tiden-health.json`, uploaded even after a
failure) hold every state, the numbers, TARGET and the evidence: the Tiden run's
sequence, SHA, environment and completion time, the observation time and the
upstream CI run. It writes nothing to Tiden. Missing configuration (the token
or either ID) still produces that full report with every metric unknown, so the
run is never green without one; a Git or CLI command that times out or cannot
start makes the metrics that need it unknown, and the others are still measured.

| Metric | Fails when | Unknown when |
| --- | --- | --- |
| Drift | the watermark W is still behind TARGET 15 minutes after the start | W is not on `main`'s history or diverges from TARGET, or `drift status` fails |
| Unlinked tests | the unlinked share of active cases is 5% or more in a suite or overall; a suite has case rows but none active ("not reporting"); an active case has an unassigned path; an active unlinked case is not in `config/tiden-unlinked-tests.json`; that list is missing or has an invalid entry | no usable test evidence for TARGET (below), or a listing is incomplete |
| Stale anchors | a `repo_file` source of this repository (or the former `anymsg` name, or none) names a path not tracked at TARGET, unless it is historical (below) | the spec listing is incomplete, or a source has a missing or repeated ID or no type |
| Done with open checks | a `Done` spec carries `[open: live]` or `[open: measured]` outside inline code and fenced code (text that documents the marker is not one) | the initial Done audit date in the script is not set yet, or a `Done` spec's text was not readable |

Test evidence is only the Tiden run on branch `main` (by branch ID),
environment `github-actions-synthetic`, build SHA equal to TARGET, status passed
or failed and live-doc sync `succeeded`; when several match, the newest by
sequence. The report waits up to 30 minutes from its start for it. A live-doc
sync that failed or was skipped, a result whose case is not on Tiden main, or no
such run by the deadline makes the metric unknown; the newest older main run is
then shown only with its own SHA. A case is active when it has a result in that
run (`run results --latest-only`) and linked when its `linkedRequirementCount`
is above zero. Other case rows are stale: counted and listed in the artifact,
never deleted. The suites are fixed in the script, one path rule each: root
`tests/`, calendar, calls frontend, calls, gateway, release router Go
(`services/release-router/`, or an `externalId` starting with
`s:github.com/GoSync-Inc/deedkit/services/release-router::`, since Tiden's Go
reporter records bare file names; checked before every other rule), whatsmeow Go (also a bare `*_test.go` path),
WhatsApp Node, npm and CI verifications (`.github/`). Every
listing counts only when it ends with an empty `nextPageToken`, its integer
`totalCount` equals its rows, its IDs are unique and, per spec, `sourceCount`
equals its sources, each with a unique ID and a type. The artifact keeps every
stale anchor path in full; the summary shortens and escapes it. CLI errors name
the command only, never the response.

The unlinked-test count is read only after the sync of TARGET (ANYM-438). `Tiden
health` and `Tiden sync` both start from the same `CI` run, in different
concurrency groups, so without this order the health run could list a test that
the sync was about to link. Before it reads the case inventory, the report lists
the `Tiden sync` runs of `main` created since one day before the commit date of
TARGET (a run that processes TARGET cannot be older; a rerun keeps its
`created_at`, so none is hidden by age). The lookup is
`gh api repos/<repo>/actions/workflows/tiden-sync.yml/runs`, every page until
the rows equal `total_count`; its pages share one budget of 120 seconds or what is left of the deadline below, whichever is less, and a
window GitHub cannot list completely (it caps a filtered search at 1000 runs)
is a lookup error. The report keeps those whose title is `Tiden sync <TARGET>`
(above), ignoring runs with conclusion `skipped`. While any kept run is queued,
in progress or waiting it checks again every 30 seconds; when none is active, the
kept run that completed last decides (`updated_at`, which a rerun moves forward;
ties go to the greater run ID, then attempt). The wait shares the 30-minute
deadline of the test evidence, counted from the start of the report; it resets no
other deadline. A decisive run with conclusion `success` is the normal path,
once a second look after the case inventory read finds the kept runs unchanged
(same runs, attempts, states and completion times); if a rerun or a new run
began meanwhile, the inventory is discarded, the wait starts again within the
same deadline and the inventory is read again.
Another conclusion, no kept run, a run still active at the deadline, or a failed
lookup (`gh` missing or timed out, API denied, malformed JSON, incomplete pages)
makes only the unlinked-test metric unknown, with the run URL and conclusion or
the command name, because the links of TARGET may be missing; the other three
metrics, the summary and the artifact are as before. `tiden-health.json` and the
summary hold a `sync` object: `target`, `run_id`, `run_attempt`, `url`,
`conclusion` and `completed_at` (all `null` when no run finished), and
`inventory_read_at`, when the case inventory read began. Run locally without
`--sync-repository`, the report skips this wait and says so (`checked: false`).
Rerun a failed `Tiden health` run after a missing or failed sync is repaired, as
before; the ordering race itself needs no rerun.

A historical anchor is provenance, not staleness: the 2026-10-16 reconciliation
kept the former repository's anchors with their receipts and anchored each moved
file again under `services/messaging/`
([receipt](docs/quality/tiden-reconciliation-2026-10-16.md)). An untracked
anchor of repository `github.com/GoSync-Inc/anymsg` whose metadata has a
non-empty string `historical_source_id`, and whose spec also has a `repo_file`
anchor (of this repository, the former one or none) on
`services/messaging/<its path>` tracked at TARGET, is counted as `historical`
and listed in the artifact, never stale (owner decision 2026-10-19). Every other
untracked anchor still fails.

`config/tiden-unlinked-tests.json` is the reviewed no-spec list: per case its
`caseId`, `externalId`, a `category` (`missing-spec`, `helper`, `trivial`,
`duplicate` or `dead-code`) and a one-line `reason`. It changes only through a
reviewed pull request. A listed case is exempt only from the "not in the list"
rule and still counts as unlinked in the share. The rule does not depend on when
a case was created: a case first published on an intent branch fails when it
becomes active on `main`, on every run until it is linked or listed.

After the `main` push run of a squash publishes its evidence, sync B links each
marked new test case of the change on Tiden main. The agent that merged it waits
for that sync run, reads back the links and reruns a failed `Tiden health` run
(`AGENTS.md`). The measuring command gives the same
report locally, read-only, without fetching:

```sh
uv run python scripts/tiden_health.py --product-id <pid> --workspace-id <wid> [--target <sha>]
```

## Environments

The 2026-10-16 cleanup left four Tiden run environments. CI publishes with
`--environment github-actions-synthetic`, set in `scripts/tiden_ci.py`. A local
synthetic run, published by hand with `tiden run create` or `tiden run exec`,
passes `--environment local-synthetic`; no repository script sets it.
`production` belongs to the live installation (TELEMETRY.md). After `CI gate`
passes on a main push, the `release` job in `ci.yml` records release
`deedkit@<sha>` in `github-actions-synthetic` with that run's URL. Tiden captures
a release gate from the main gate when the release is first created and keeps it
when the release is recorded again. So the job records only while this commit's
passed run is the latest completed `main` run, at least a minute after it
completed, and then checks that the run completed last before the release's
creation time is this commit's. Otherwise it warns: when another commit's run
completed last it records nothing (receipt `state: not_attempted`), and when the release
captured another run's gate its `tiden-release-receipt` artifact records
`gateFromThisCommit: false`. When the commit's evidence publication was not
verified, the job warns `Tiden release not recorded` before any Tiden call
and records nothing. Temporarily (ANYM-411), an unavailable Tiden only warns
`Tiden release not recorded`: a failed CLI download, or a Tiden call that exits
non-zero, prints unparseable output (for `gate check`, neither a verdict nor the
empty output of a missing gate) or passes its timeout. The receipt records
`state`: `not_attempted` when no create call was made, `not_verified` when one
was sent, keeping the known release identity; an uncertain create is never
retried. A checksum mismatch, a missing product or workspace ID, a validation
error, a rewritten release identity and any other error still fail the job and
with it the main run, which `production.py verify` then refuses. Every step has
a timeout. The script ends its Tiden calls by its own deadline and anything
else, manifest validation included, by an overall deadline at least 30 seconds
before its step's, failing the job in that case; it rewrites the receipt at
every state change, so the warning and the receipt upload always run. The `production`
record still fails on every Tiden error. The rule lasts until three consecutive
automatic `Tiden health` runs (`workflow_run`, distinct TARGET SHAs, each
upstream CI run `success`) report `## Tiden health: PASS`; a FAIL, UNKNOWN or
missing report resets the streak. Then a pull request makes a Tiden error in
the release record fail the job again (ANYM-411 criterion 6). The
`production` release is recorded by the dispatch in
[production.md](docs/operations/production.md). `synthetic` is
historical: release 0.9.16 is attached to it, so keep it, but send no new runs
there. Tiden creates an environment for any unknown slug, so a misspelled or new
name silently makes a duplicate. One environment per kind of run keeps the
release gate and per-environment regressions unambiguous.

## Verdicts and coverage

`run create` always receives `--branch` and `--intent-session none`. A mapped or
pull-request-linked run must land on exactly that branch ID. Any other unmapped
run must land on the same-named or an intent branch, as described above. The job
waits for live-doc sync and checks the resulting branch before reporting success.
The authentic run then adopts existing case identities or imports the reported
cases. Reported step failures and missing required verifications are also recorded
as failed or blocked; optional test skips remain skipped. A test run is not a
statement that every specification is covered.

Link specs only to tests whose assertions actually verify them. Use
`tiden test link-spec <test-id> --spec-id <spec-id> --branch <branch>` after
reviewing the body. File proximity alone is not sufficient. CI does not use
`test ingest` or rewrite spec links, so future runs preserve existing links.
The `tiden-receipt` artifact records the run, association and verdict.

## Maintaining the product map

[ARCHITECTURE.md](ARCHITECTURE.md) defines the logical component boundaries.
They differ from distribution packages and running services. Preserve existing
spec/test identities when changing component assignments, and retain historical
source anchors alongside the reviewed revision.

Reverse-engineered specs are candidates for review. Check API arguments, state
semantics, schema versions and deployment exceptions against the implementation;
a source-file anchor does not guarantee that every sentence is accurate.
Maintained baseline contracts use `Active`; that is not a claim that someone is
currently implementing them. Tiden is also the primary work queue: future work
uses `Backlog`, implementations and operational acceptance awaiting review use
`Review`, and bounded delivered work uses `Done` with its evidence. Keep the
implemented boundary and remaining acceptance explicit. Do not confuse `Backlog`
with missing test coverage or set `Done` merely because a related test passes.
Follow the [product workflow](docs/operations/tiden-product-workflow.md) and the
standard Tiden skills. [BACKLOG.md](BACKLOG.md) only locates the product and
historical IDs; it is not a second queue. Do not bulk-change baseline statuses
without checking their verification scope.

The 2026-10-16 reconciliation measured that the current gate includes Backlog,
Review and General index specs. The whole-product gate therefore also exposes
unimplemented plans as `no_test`: the branch verdict is `NOT VERIFIED`, while
main reports `BLOCKED` for those coverage gaps. Status is not a release-scope
filter. Preserve that result and inspect its per-spec causes; do not fabricate
coverage or weaken CI to make a planning inventory pass a release gate.

Create new future-only specs on independent proposal branches. The existing
merged backlog needs a verified identity/history-preserving relocation; COW
updates or deleting/recreating cards do not establish that preservation. Keep
real defects and required operational acceptance visible in the current scope.

Readiness also requires a captured candidate release gate and an explicit review
of unresolved production issues, including unattributed ones. The native main
gate's production-issue signal is not a replacement for release-scope triage.
Missing release evidence is unknown. Report the native verdict separately from
the project's readiness decision. See the workflow's readiness section.

Messaging telemetry names its release `deedkit@<sha>` from the exact source
commit in `DEEDKIT_SOURCE_SHA`, and falls back to `anymsg@<package version>`
(0.9.19 at this revision) when the runtime has no valid commit. The fallback does
not identify an exact deployed commit. Runtime events use only the `production`
and `local-synthetic` environments (TELEMETRY.md). Preserve historical release/test
identities and require an explicit SHA/environment/build association before
claiming a release is verified.

Review spec and test-link changes in the matching Tiden branch before merging
them to main. A Git merge does not by itself merge the spec branch. After both
merges, verify the actual main gate and its evidence, rather than using a feature
branch's projection as the main result. Existing test links survive ordinary CI
reports; authoritative imports must not silently replace them.

`no_test` means no accepted linked test; `not_run` means linked evidence has not
met the gate's run/freshness requirements. Neither label alone proves a runtime
bug. Passing synthetic tests also do not prove live-provider history coverage or
production infrastructure health. Keep those limits visible.

The original synthetic reports can contain failure diagnostics. Keep artifact
access and retention appropriate for the repository; never test against live
messenger sessions or personal archives.

## DeedKit module migration

The migration extends this same repository and product. Its behavior/proof
matrix and intent-to-PR-to-CI procedure are in
[platform acceptance](docs/quality/platform-acceptance.md). Local worktrees and
uncommitted files are not GitHub App sync inputs. While the intent loop is
paused, the migration PR names its card's `Tiden branch:` and its results reach
Tiden through the main push run; otherwise link the migration PR to its intent
branch and publish real results there before reviewing the merge. After
merge, verify both main's gate and the GitHub App processing/drift state.

The integrated CI includes gateway, calendar, calls and calls-frontend reports,
plus the measured gateway load gate, which runs on a runner of its own in
parallel with the suite. Existing messaging check/test identities
and links remain intact. Module assertions support reviewed contracts in the
same product; a successful messaging run alone cannot verify another module.

## Native and adapter proofs

The host CI supplies its disposable PostgreSQL service to the native backup
drill and installs `age`. The drill uses real `pg_dump`, encryption, the local
receiver, decryption, `pg_restore` and quarantine checks. Missing prerequisites
fail CI; a local test container without them explicitly skips these checks.
This proves local recovery behavior, not remote SSH delivery or off-site quota.

Provider tests use synthetic Telegram clients and an offline loader around the
actual WhatsApp entrypoint. They verify capture, identity checks, command lanes
and recovery states without opening an account. Local model tests verify adapter
arguments and result handling; they do not measure transcription accuracy.
Review broad contracts against the combined criterion set from their adapters,
database roles and public interfaces, rather than a single child test.
