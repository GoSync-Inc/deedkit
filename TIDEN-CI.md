# Tiden verification evidence

CI keeps the original Python and Node JUnit reports, their normalized copies,
the exact **tested checkout SHA**, step outcomes and the GitHub Actions URL in
the `ci-evidence` artifact, including failed runs. Python uses xunit1 to retain
source paths; Node's generic `test` classname is qualified by its source file.
Parameterized Python executions retain their parameter names and status.

Test jobs run in parallel and each uploads a `ci-evidence-<job>` fragment:
`checks / verify` the seven checks and its module reports, `checks / load` the
gateway load gate. The `checks / evidence` job merges them into `ci-evidence`.
A fragment counts only when it holds just the files and checks its own job
produces and names the tested source SHA that both its job output and verify
captured; a job that did not succeed in this run attempt never contributes an
earlier attempt's fragment. A job without such a fragment contributes no
reports, and its checks take its job result, so they never read as passed. The
evidence is named by the assembling run attempt and is retained either way;
the job then fails when a fragment is missing or a test job failed, was
cancelled or was skipped, whether or not Tiden reporting is configured.

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
intent loop requires:

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
evidence association fails the reporting job. Without a token the job clearly
states that reporting is not configured. No workspace credentials belong in Git.

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
detection or build turns `CI gate` red. Push, merge queue and dispatch runs
always build them.

A merge queue run (`merge_group`, Git branch `gh-readonly-queue/...`) verifies
the temporary group commit with the same required checks, `CI gate` and
`checks / verify`, but publishes nothing to Tiden and creates no Tiden branch.
This takes precedence over every target above, including an override, a map
entry or a pull request link. A queued group can contain a fork's unmerged code,
so a `merge_group` run never receives `TIDEN_API_TOKEN`. A queue run checks no
Tiden configuration and installs no CLI: its reporting job records `skipped` with the reason in
`tiden-route.json`, logs a notice and stays green. The merged code reaches Tiden
through the main push run. Each queue run has its own concurrency group and
cancels no other run. Queue runs take their workflow files from the group commit,
so review workflow changes in a fork's pull request before queuing it.

The queue run is the authoritative test run of a merged SHA; the main push run
of that SHA validates and publishes it instead of testing the commit again
(ANYM-322). Its first job, `Reuse the merge queue verification`, the only CI job
with `actions: read`, finds the latest merge queue run of the exact commit by the
rules `production.py verify` applies (ANYM-321), waits up to five minutes for it
to complete, and pins its latest attempt and the one `ci-evidence` artifact
created during that attempt. Without them, or on any lookup error, the push run
verifies in full. After that choice the job downloads the pinned artifact by ID
and digest as data, extracts exactly the expected files within size limits, and
validates them: every SHA field names the commit, the pinned attempt of a main
queue run assembled the evidence, all thirteen checks passed, and every report
holds passing executions that match the recorded counts. It reads the queue run
and the artifact again, then uploads the evidence unchanged as this run's
`ci-evidence` with a `publication.json` record that names this push run as
publisher and the queue run attempt and artifact as producer. A failure there
fails the push run: `checks` is skipped and `CI gate` turns red. On a validated
reuse `checks / verify`, `checks / load` and `checks / evidence` are skipped;
`native / synthetic`, the image builds, `publish` and the release record run as on
every push. `CI gate` accepts a skipped `checks / verify` only for a validated
reuse of this commit; otherwise `checks / verify` must have tested this commit.
`tiden_ci.py publish` and `release` accept the record only when it names their
own job's repository, run, attempt and commit. The Tiden run on `main`, its
thirteen check row IDs and the release then carry this push run's attempt URL,
with `tested_in`, also kept in the route receipt, naming the queue run attempt,
while executions and outcomes stay the queue run's. Queue evidence without a valid record still publishes and
records nothing. After a reuse, rerun a failed `checks / tiden` or `Tiden release
record` with **Re-run all jobs**: a partial rerun has an attempt the record does
not name. Validating the artifact does not replace reviewing a fork's workflow
changes before queuing.

A pull request run that changes only reviewed documentation nothing reads
skips the test jobs (ANYM-323). On pull request runs only, the `checks / documentation`
job classifies the run as docs-only when every change in GitHub's two-parent
merge of exactly this head is a plain edit or deletion of a path named in
`.github/docs-only-paths.txt`. A scan of every other tracked file of the merge
commit that is not Markdown (the tests, scripts, packaging files, Dockerfiles
and workflows of every module, and anything else) must also find, in each
whole file outside Python comments, neither a changed path, nor its file name,
nor a directory or glob read of Markdown, however its parts are spread. The
files whose directory or glob matches a review found harmless are listed, each
with a reason, in the list's `## scan exclusions` section; no file may name a
changed document. Any of these means full verification: a
new file, a mode or type change, an empty or uncertain diff, an unlisted file, a
scan hit, a failed decision, or a `Tiden intent branch:` line in the pull
request description. On a docs-only run `checks / verify`, `checks / load` and
`checks / evidence` are skipped, and the skipped `checks / verify` still
satisfies the required check. `CI gate` accepts the skip only for the docs-only
decision on this merge commit, `native / synthetic` and the image builds keep
their rules, and `checks / tiden` records a skipped route with the reason. Merge
queue runs, main pushes, dispatch runs and package releases always verify in
full, so the queue run still tests every merged commit. Editing a pull request
description does not rerun CI, and a rerun reuses the captured event, so adding
a `Tiden intent branch:` line later needs a new push. Add a document to the list
only after a review finds that no test, script, packaging file, Dockerfile or
workflow reads it, including through a composed path.

For an explicit baseline rerun, dispatch `CI` on the reporting-code branch with
`source_ref` set to the exact source revision and `tiden_branch` to the matching
existing spec branch. The checkout SHA, rather than the workflow SHA, is recorded.
Normal pull requests record their actual tested merge SHA.

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
   watermark. Anchor each on the spec it implements, then rerun the failed run.
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
creation time is this commit's. Otherwise it warns, and its
`tiden-release-receipt` artifact records `gateFromThisCommit: false`. A Tiden
error fails the job and with it the main run, as a failed evidence publication
does, and `production.py verify` then refuses that commit. The
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
uncommitted files are not GitHub App sync inputs. Link the migration PR to its
intent branch and publish real results there before reviewing the merge. After
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
