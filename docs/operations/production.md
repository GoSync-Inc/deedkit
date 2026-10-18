# Verified production promotion

For ready-before-route gateway and messaging HTTP promotion, use the opt-in
[seamless HTTP release protocol](seamless-release.md). It preserves the existing
main/CI gate and keeps provider/session owners outside overlap. The `apply`
sequence described below still performs an explicit stop/start of selected
services; it is not a seamless HTTP promotion.

Production changes follow PR → required CI → merge into `main` → CI on the
actual main SHA → prepare → explicit apply. Use `scripts/production.py` for
promotion. Direct source copies, ad hoc image overrides and `compose up --build`
skip this contract. Root access can bypass deployment policy; keep that access
for recovery and route routine changes through this tool.

## Required CI and main protection

`CI gate` runs even when dependencies fail or skip and accepts only success from
the shared package/module checks, native Go race tests/vet, and all eight
production image builds with isolated imports. The existing shared checks cover
synthetic PostgreSQL, Python/Node, installed distributions, release inventories,
gateway/calendar/calls, the isolated native backup restore and load budgets.
Tiden reports actual outcomes separately.
No test receives a provider session or production database.

Protect `main` with a repository ruleset: required GitHub Actions `CI gate` and
`checks / verify`, PR-only squash merges through the GitHub merge queue, no bypass
actors (administrators included), conversation resolution, and disabled force
pushes/deletion. A single maintainer may use zero required approving reviews;
this still requires a PR and green checks. Native verification is reusable and
unconditional in CI, so path filters cannot hide it.

The queue replaces strict up-to-date branches: it tests each pull request on top
of the current `main` and the entries ahead of it, so a branch need not be updated
after every merge. It merges one pull request per squash commit, so every `main`
SHA gets its own push CI for promotion. A queue run takes its workflow files from
the group commit; queue a fork's pull request only after reviewing its workflow
changes (TIDEN-CI.md).

A merge through the queue is asynchronous. Once its checks pass, queue the pull
request with `gh pr merge <n> --squash --auto` (repository auto-merge is on),
**Merge when ready**, or the GraphQL `enqueuePullRequest` mutation with
`expectedHeadOid` set to the reviewed head SHA. It reaches `main` only after its
group passes; a failed group drops it from the queue unmerged. If repository
auto-merge is ever disabled, `gh pr merge` cannot queue a pull request: it calls
`enablePullRequestAutoMerge` and fails with "Auto merge is not allowed"
(cli/cli#13398).

`main` is protected by the ruleset `deploy/main-ruleset.json` (live as ruleset
25274451, with the merge queue); classic branch protection has been removed
([parallel development](parallel-development.md#merging)). An administrator
updates the ruleset with
`gh api --method PUT repos/GoSync-Inc/deedkit/rulesets/<id> --input deploy/main-ruleset.json`
(it was created with
`gh api --method POST repos/GoSync-Inc/deedkit/rulesets --input deploy/main-ruleset.json`)
and verifies the effective rules with
`gh api repos/GoSync-Inc/deedkit/rules/branches/main`. Keep classic protection
absent: GitHub enforces it together with the ruleset, and
a pull request enters the queue only after it passes the branch protection
checks, so a strict up-to-date rule would again require updating each branch
first. `deploy/main-protection.json` remains the rollback state. To roll back,
empty the queue (`mergeQueue(branch:"main"){entries{totalCount}}` is 0, or use
`dequeuePullRequest`), restore classic protection with
`gh api --method PUT repos/GoSync-Inc/deedkit/branches/main/protection --input deploy/main-protection.json`,
read it back, and only then delete the ruleset, so `main` is never unprotected.

## Host setup

Keep administration behind the existing Tailscale/OpenSSH route. CI needs no SSH
key, tailnet enrollment, provider token or production database access.

Install a reviewed copy of `scripts/production.py` as
`/usr/local/lib/deedkit/production.py`, owned by root and writable only by root.
Use Python 3.12+, Git, Docker and Compose v2 on the Linux host. Do not execute
the deployment tool from an unreviewed feature branch on the host.

Copy `deploy/production.example.json` to `/etc/deedkit/production.json` and
adapt it to the existing host. The operator owns this file; mode `0600` is
recommended. It lists **all** existing Compose files in their exact order,
explicit environment files, the current working directory/project name and only
the currently running application services to replace. Obtain those paths from
`com.docker.compose.project.*` container labels, including existing private
overlays. Do not replace the host's Compose topology with the example.

Available build targets are `runtime` (API, Telegram, subscriptions), `indexer`,
`enricher-local`, `whatsapp-native`, `gateway`, `calendar`, `calls` and `backup`.
The backup target supplies the producer and isolated restore helper; building it
does not start a capture or a restore. The two
calendar services share the `calendar` image; a legacy service named `voice`
can select `calls`. Explicitly select only the deployed native WhatsApp adapter;
this tool does not convert browser sessions. Databases and unrelated services
are never selected. State, mounts, networks, ports and private settings are
retained from the resolved configuration.
All profiles are included when resolving the snapshot so existing profiled
collectors/workers remain visible. Apply still names only the explicitly selected
services and uses `--no-deps`; loading profiles does not launch other services.

Before initial enrollment, compare the resolved configuration with the running
containers and reconcile missing shell variables or settings drift. Container
labels list Compose paths but do not capture shell environment overrides. The
prepared snapshot preserves the resolved files; an explicit candidate review
must confirm that they are the intended production settings.

For public ingress, a running proxy and its Compose labels do not prove that a
working deployment manifest or restart path still exists. Before changing its
routing, retain a reviewed reproducible manifest pinned to its immutable image,
with the existing certificate/state volumes and networks declared external under
their exact names, the same listeners/configuration mounts, and persistent
restart/startup settings. Resolve and compare it with the inspected live
container, and validate the proxy configuration without recreating or reloading
the serving listener. Missing source files or disabled restart persistence remain
live enrollment blockers; source CI cannot establish these host conditions.
See the [first HTTP enrollment procedure](seamless-release.md#internal-router-and-first-enrollment).

Host-mounted code and import-path overrides are refused, including legacy
Telegram bridge files mounted outside the image. Move such extensions into the
reviewed source/image and test them in CI before selecting that service. The
tool accepts mounts under the established data/config roots (`/data`,
`/sessions`, `/spool`, `/run/secrets`, `/run/deedkit`, `/models`,
`/processing-locks`, `/broker`, `/voice-broker`); it refuses executable file
mounts and other roots, including Compose `secrets`/`configs` targets. It never
silently drops a legacy extension.

A selected service's `entrypoint`, `command` and healthcheck may name a mounted
path only as the value of `--config`, `--policy`, `--home` or `--dsn-file`.
The tool refuses a script, `python -m` module or `working_dir` under a mount
root, inline shell code (`sh -c`, `CMD-SHELL`), `node -e`/`--require`, and
inline `python -c` except as a healthcheck probe that names no mount root.
It refuses every `PYTHON*` variable except `PYTHONUNBUFFERED`,
`PYTHONDONTWRITEBYTECODE`, `PYTHONFAULTHANDLER`, `PYTHONIOENCODING`, `PYTHONUTF8`,
`PYTHONNOUSERSITE`, `PYTHONHASHSEED` and `PYTHONWARNINGS`; `LD_*`, `NODE_OPTIONS`,
`NODE_PATH` and similar loader variables; and `PATH` or `HOME` under a mount root.
`privileged`, `cap_add`, `devices`, `volumes_from`, unconfined
seccomp/AppArmor, host network/PID/IPC/UTS/user/cgroup namespaces, a shared PID
namespace and Compose lifecycle hooks are refused too. These checks keep routine
promotion on reviewed images; they are not a sandbox against a host root operator.

The messaging `runtime` image includes the existing `telegram_session_owner` and
`telegram_broker` modules. For an existing broker-enabled collector, use
`python -m telegram_session_owner --config /run/secrets/telegram_config` from the
image, preserving its broker socket/token/policy configuration and session/data
mounts. Remove only the replaced Python file mounts and `PYTHONPATH` override
from the reviewed candidate; keep the default collector command for installations
without this extension. CI and preparation import the modules without connecting
an account or starting a broker.

The state directory and Docker filesystem each need at least **15 GiB free**
before preparation. A refusal does not authorize pruning shared Docker objects
or deleting runtime data. Free capacity through a reviewed, targeted operation
or add storage before retrying.

## CI-published images

Every `main` push run publishes the eight images its `images / build (<target>)`
jobs built and smoke-tested as `ghcr.io/gosync-inc/deedkit-<target>:<sha40>`,
where `<sha40>` is the full source commit. Preparation pulls them by verified
digest instead of building them ([Prepare and inspect](#prepare-and-inspect),
ANYM-301); only `http-router` and `check-image` still build on the host.

- **Tested bytes, no rebuild.** On pull requests and `main` pushes, each build job
  saves the image `check-image` tested (`docker save`) and dry-runs the
  publication against a throwaway registry on its runner; only a `main` push keeps
  the archive, as a one-day artifact. The `publish` job in
  `ci.yml` runs only on a `main` push after `CI gate`, pushes those archives with
  `skopeo` and refuses an image whose `org.opencontainers.image.revision`,
  `org.opencontainers.image.source` or `io.deedkit.production.build` label does not
  match the commit, or whose pushed manifest does not carry the tested config
  digest (`scripts/publish_images.py`). There is no `latest` or other moving tag.
- **Write-once tags.** A tag that already exists is never pushed again. A full
  rerun reuses an existing attested image of the same commit; an image pushed by
  an attempt that failed before attesting it is attested only when this attempt
  still holds the same tested archive. Anything else fails the job without moving
  the tag. If a tag is left pushed but unattested and its archive expired, the
  commit has no published images until the owner deletes that package version.
- **Provenance.** Each newly pushed digest gets a SLSA build-provenance
  attestation from `actions/attest` with `push-to-registry`, without the
  organization's artifact storage record. It is signed through public-good
  Sigstore (the repository is public) and verifiable with
  `gh attestation verify oci://ghcr.io/gosync-inc/deedkit-<target>@<digest>
  --repo GoSync-Inc/deedkit --signer-workflow
  GoSync-Inc/deedkit/.github/workflows/ci.yml --source-ref refs/heads/main
  --source-digest <sha40> --deny-self-hosted-runners`. The job ends by verifying
  every target this way.
- **Permissions.** Only `publish` holds `packages`, `id-token` and `attestations`
  write, through the job-scoped `GITHUB_TOKEN`; no secret is stored. Pull request,
  merge queue and dispatch runs never run it.
- **Failure.** A failed publication fails the `main` push run, so
  `production.py verify` refuses that commit, and the Tiden release record waits
  for `publish`. Publishing is not a required check.

- **Secret scan.** Before the archive is kept, each build job scans it with
  `scripts/scan_image.py` and the gitleaks 8.30.1 binary pinned by SHA-256 as in
  `security.yml` (ANYM-303). Every layer is written as its own validated
  `layer-<n>.tar` (layer count and SHA-256 must match the config's `diff_ids`, the
  tar's every header, payload and end marker is checked, gzip is decompressed,
  other encodings are refused) and
  scanned with the image config (`Env`, labels, history). Nothing is merged or
  extracted, so a file deleted, overwritten or hidden by a later layer is still
  scanned: a public image ships every layer. A finding, any other gitleaks error
  or any gitleaks log line except the findings summary and the documented skip of
  an archive nested inside a layer fails the job, so the commit is not published. Each job
  first scans the committed canary (`tests/fixtures/gitleaks-canary/`) and fails
  if it is not detected. Supported content: text files in every layer and the
  config; files gitleaks classifies as binary are not scanned, and archives inside
  a layer (`.gz`, `.whl`, `.zip`, `.jar`) are not opened. The job log names only the
  rule, layer, path and line, never the secret.
- **Scan policy.** The rules are gitleaks 8.30.1's defaults
  (`config/gitleaks/gitleaks-8.30.1-default.toml`, kept verbatim) in
  `.gitleaks-images.toml`, without the default global path allowlist that exempts
  whole trees such as `lib/python3.x` and `node_modules`. The image cannot change
  the policy: `.gitleaks.toml`, `.gitleaksignore` and inline `gitleaks:allow` in an
  image are ignored. Every specific rule (private keys, provider tokens) covers
  the whole image. The two heuristic rules, `generic-api-key` and `jwt`, match
  ordinary code in hash-locked third-party packages, so they are not applied
  inside installed third-party distributions (`site-packages`, `dist-packages`)
  and system files (`/usr/lib`, `/usr/include` and their merged-usr aliases `/lib*`); they still apply to our own
  packages wherever they are installed (`OWN_PACKAGES` in `scripts/scan_image.py`,
  kept complete by a test), to any third-party file a link from our code points
  at, and to everything else, including `/usr/share` and `/usr/local`, where our
  Dockerfiles copy files. Any
  other false positive gets an `[[allowlists]]` entry at the end of
  `.gitleaks-images.toml` with a `description` naming the package and why the
  match is not a secret, its `targetRules`, and an exact file `paths` regex (or an
  exact value); never a whole tree. Current entries: the Python base image's
  public `GPG_KEY` fingerprint (exact value), PEM marker strings in python-rsa and google-auth, and the Debian
  snakeoil key in the postgres base image of the backup target (owner risk
  acceptance 2026-10-17). Such a change is reviewed like any CI change and
  rebuilds every image.

Owner actions, not performed by agents: the first publication creates the eight
packages as private, linked to this repository. The owner decided (2026-10-17)
that they become public and the host gets no GitHub token. Package visibility is
package-wide, so the owner's one-time cutover is:

1. Let every `main` push run of a commit that does not contain the secret scan
   finish, and never rerun such a run afterwards: a rerun keeps its original
   commit and would publish without the scan. CI cannot enforce this.
2. Keep only image digests whose own publication run scanned them: the tag's
   commit contains the scan (`git merge-base --is-ancestor <scan merge SHA>
   <sha40 tag>`) and that run's `Scan every layer ...` step passed. Delete every
   other version, together with its attestation referrer versions (untagged, or
   tagged `sha256-<digest>`), and keep the referrers of every kept digest
   (deleting them breaks attestation verification).
3. Confirm that the first `main` run after the scan merged published all eight
   targets with a passing scan, then set each of the eight packages public once
   in its package settings on GitHub (an organization owner can do this whatever
   the member package-creation setting allows).

If a pre-scan rerun publishes after the cutover, delete that version and its
referrers again. Keep Actions allowed to create repository-linked packages in the
organization's package settings. Deleting a package version or changing its
visibility is the owner's.

## Attestation verifier

Preparation verifies each image's provenance on the host without any GitHub
token, with `tools/attest-verify`, a small Go program on sigstore-go. The owner
installs it once (and again whenever its recorded hash changes on `main`); the
host never builds it, and agents do not install it.

1. On a development machine, at the `main` commit to deploy, build it with the
   pinned toolchain exactly as `tools/attest-verify/README.md` states
   (`GOTOOLCHAIN=go1.26.9 CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath
   -buildvcs=false -ldflags=-buildid= -o attest-verify .`). The build is
   reproducible: its SHA-256 must equal `tools/attest-verify/attest-verify.linux-amd64.sha256`,
   which a CI test rebuilds and compares.
2. Copy it to the host as `/usr/local/libexec/deedkit/attest-verify`, owned by
   root, mode `0755`, not a symlink. `production.py` refuses to run any other
   file: before every call it checks that the file is regular, owned by root or
   the operator running `production.py`, not writable by group or others, and
   that its SHA-256 equals its own pinned `VERIFIER_SHA256`.
3. Install `production.py` from the same `main` commit as the release you
   prepare. The pulled image's `io.deedkit.production.build` label is the recipe
   CI computed for that commit, and the host compares it with the recipe its own
   `production.py` computes: an older installed tool refuses a commit that
   changed a build recipe ("pulled image does not name the candidate source
   revision and build").

For `ghcr.io/gosync-inc/deedkit-<target>:<sha40>` the verifier resolves the tag
anonymously to exactly one single-image manifest (an index, an ambiguous or a
missing tag is refused), finds the Sigstore bundle attached to that digest and
verifies it offline against the public-good Sigstore trust root embedded in the
binary: signature, certificate chain, signed certificate timestamp, Rekor
inclusion and signing-time validity, and that the statement's subject is that
exact digest. On the same bundle it then requires the certificate identity
`https://github.com/GoSync-Inc/deedkit/.github/workflows/ci.yml@refs/heads/main`,
issuer `https://token.actions.githubusercontent.com`, source repository
`https://github.com/GoSync-Inc/deedkit`, source ref `refs/heads/main`, source
commit `<sha40>`, a `github-hosted` runner and the predicate type
`https://slsa.dev/provenance/v1`; predicate claims never stand in for these
certificate checks. `production.py` runs it with an empty environment, so no
credential reaches it, and passes its one-line refusal reason on. Any refusal
stops preparation before the first pull. The trust root is fixed at build time:
when Sigstore rotates its keys, verification of newer bundles fails closed until
a reviewed pull request refreshes `trusted_root.json` and the owner installs the
new build.

## Prepare and inspect

```sh
python3 /usr/local/lib/deedkit/production.py verify --sha <40-character-main-sha>
python3 /usr/local/lib/deedkit/production.py prepare --sha <40-character-main-sha>
```

The guard queries GitHub directly. Only the latest successful **push** CI run
for the exact current `main` SHA qualifies, with successful package, native and
aggregate jobs from its current attempt, including exactly one passed `publish`
job (preparation pulls what it published). A PR run, manual baseline run, earlier
successful run followed by failure, missing job or skipped check is refused.
One exception (ANYM-321): a push run whose `checks / verify` was skipped because
it reused the merge queue's verification qualifies only when the latest merge
queue run of the same SHA, from this repository's `gh-readonly-queue/main/`
queue, completed successfully with one successful `checks / verify` and
`CI gate` in its latest attempt. The push run must still contain exactly one
`checks / verify` job, concluded `skipped`; an absent job is refused. Every other
required job must still pass in the push run itself. Both runs are read again after their jobs, so a rerun that
starts in between is refused, and run listings are followed across pages. The
result names the queue run that supplied the test verification next to the
promoted push run.
The public repository supports anonymous API reads; optional `GH_TOKEN` may be
provided by the operator's secret mechanism to increase API limits. Never copy a
maintainer's broad token onto the host for routine deployment.

Preparation fetches only `main` and checks the fetched SHA again. It then
obtains the selected targets: each CI-published target is first verified
([Attestation verifier](#attestation-verifier)), all of them before the first
pull, then pulled as `ghcr.io/gosync-inc/deedkit-<target>@sha256:<digest>` and
tagged; `http-router` is built from the clean detached checkout. Every image is
smoke-tested with no network, sessions, mounts or database. Source downloads,
pulls and image builds do not stop or replace running services. Images carry
the exact source SHA in the `org.opencontainers.image.revision` label, and every
build (in CI or on the host) receives it as the `DEEDKIT_SOURCE_SHA` build
argument. The messaging images (`runtime`, `indexer`,
`enricher-local`, `whatsapp-native`, `backup`) also keep it in the
`DEEDKIT_SOURCE_SHA` environment variable, so their telemetry reports
`deedkit@<sha>` (TELEMETRY.md); preparation refuses a newly obtained messaging
image whose variable or label differs from the candidate. Apply checks only the label,
so images built before this variable existed stay usable for rollback. The
prepared Compose snapshots pin Docker image IDs. Inspect `prepared.json`, the
image inventory and the private `*.before.json`/`*.json` Compose diff under
`<state_directory>/<sha>/`. Full snapshots can contain secrets: keep them on the
host with mode `0600`, outside Git, packages and CI artifacts. Public receipts
contain only revision, CI URL, image IDs and, for each pulled target, the verified
registry reference `ghcr.io/gosync-inc/deedkit-<target>@sha256:<digest>`.
The checkout uses image-readable permissions for public source files so Docker
COPY preserves access for unprivileged application users. Its enclosing release
directory remains private (`0700`); resolved snapshots remain `0600`.
The previous snapshot pins each selected container's actual image ID, including
when the old configuration used a mutable tag; it does not depend on the tag
continuing to reference the previous image.

Each target is tagged `deedkit-production-<target>:<sha>`, and a tag is never
rebuilt, re-pulled or moved. Builds are not reproducible, and with the containerd
image store a retag can remove the image that running containers use. Every
build, in CI or on the host, labels the image with its revision, the source
repository and an `io.deedkit.production.build` digest of its Dockerfile, stage
and build arguments. A pulled image must carry these labels too, and its image ID
must be the verified config digest (classic image store) or manifest digest
(containerd image store). If the tag already exists and these labels (and, for a
messaging image, its `DEEDKIT_SOURCE_SHA`) match the candidate, and, for a
CI-published target, the image is the verified CI image, preparation reuses that
image and smoke-tests it again. So `seamless_release.py prepare` after
`production.py prepare` of the same SHA gets the same image IDs. If the tag
exists but does not match, for example another revision, an image built by an
older tool without that digest, a messaging image without `DEEDKIT_SOURCE_SHA`,
or an image the old host-building tool built for this revision (right labels,
other content), preparation refuses and changes no tag. Removing such a
stale tag is a separate operator step, taken only after `docker ps --all` shows
that no container uses its image. Lookups and builds take
`/var/lib/deedkit-production/image-build.lock`, so two preparations on a host set
up by this guide, from either tool or any state directory, never build the same
tag at once; the second is refused. The verification itself runs before the
lock. Without that directory, for example on a CI
runner, no image lock is taken. `check-image` builds the working tree under
`deedkit-check-<target>:<sha>`, always fresh, and never reuses or touches a
production tag.

Before applying, follow [backup/migration and rollback](platform-cutover.md).
This tool does not run migrations, prove data compatibility, authorize external
actions, or perform provider readback. A schema change needs the module's explicit
migration procedure and a fresh compatible backup first. Confirm that the pinned
backup/restore runtimes support both the current database schema and the planned
target. Resolve delivery failures, including receiver quota/capacity failures,
before taking the pre-migration snapshot; use a reviewed retention/storage change
that preserves the protected restore anchor. Confirm durable receiver delivery,
retain the snapshot ID/time/schema, image revision, ciphertext size/hash and
receipt privately, and verify an isolated quarantine restore using the module's
[backup procedure](../../BACKUP.md). Calculate freshness from the snapshot time,
not from a later delivery retry. A failed delivery, local-only snapshot or stale
receipt does not establish this gate. The deployment tool does not verify these
live backup conditions; preparation and successful source CI are not migration
approval.

For R2 delivery, the opt-in [independent receiver verifier](../../BACKUP_R2.md#automated-independent-restore-on-the-receiver)
can establish this restore evidence on the receiver using a root-only recovery
key and isolated helper/PostgreSQL containers. It requires its own reviewed
enrollment and actual successful restore; an enabled timer, upload receipt or
synthetic CI run does not establish the live backup gate.

## Apply and verify

```sh
python3 /usr/local/lib/deedkit/production.py apply --sha <same-sha>
python3 /usr/local/lib/deedkit/production.py status
```

Apply locks the host, checks current main/CI again and validates the prepared
image labels, configuration hashes and existing container identities, and that
each `<project>.before.json` still has the digest `prepared.json` recorded when
preparation wrote it. A container identity is its ID, image and a digest of its
inspected `Config`, `HostConfig` and `Mounts`, so a `docker update` of resources
or restart policy counts as a change. If another operator changed a runtime or
config after preparation, prepare a fresh reviewed candidate. The selected services stop before their replacements start; `up` uses
`--no-build --pull never --no-deps --wait`. Databases and other Compose projects
are not restarted. Routine promotion may briefly interrupt selected services.
The messaging API, gateway and calendar API define Compose liveness healthchecks
(their unauthenticated health routes, with an allowed Host, on loopback), so
`--wait` also requires them to report healthy; the first probe runs about 30
seconds after start. Collectors, workers, indexer and enrichment expose no
container-local health signal and are verified only as running. Docker does not
restart an unhealthy container; monitor `docker ps`/`health_status` events.

`current.json` is written only after every selected replacement runs its prepared
image. It records image/start verification; it is not a claim that every upstream
provider or client is healthy. `result.json` and `current.json` also record the
verified registry references, `applied_at` (UTC) and the rollback data (version
1): per project the selected services, the digest of its `<project>.before.json`
bound at preparation, and the Compose snapshot digest and running container
identities right after apply, captured the way preparation captures its
baseline. That final capture must still show the containers this apply started,
on the prepared images, with the Compose configuration unchanged; otherwise apply
refuses and leaves `attempted.json` without a result. Use each module's approved
health/readback procedure.
No test send, ACK, call, invitation or new provider session is a health probe.

An interrupted/failed apply leaves `attempted.json`, retains previous image IDs
and refuses blind retries. A multi-project failure can leave some projects
updated. Inspect actual container/image state before recovery. Never assume an
automatic rollback is data-safe; restore compatibility/backup as the module
runbook requires, stop the new session owner, then restore the recorded previous
image/config ([platform-cutover.md](platform-cutover.md)). Do not use `down -v`,
prune unrelated objects or restart a stale calendar writer. Keep the previous
images until acceptance.

## Roll back the current release

```sh
python3 /usr/local/lib/deedkit/production.py rollback --sha <current-applied-sha>
```

Rollback is a production change: the owner confirms it like `apply`. It returns
the current release's selected services to the images and configuration that
ran before it, from `<state>/<sha>/<project>.before.json`. It does not call
`verify()` (the previous revision is no longer current `main`), does not run or
reverse migrations and does not restore data: when the release changed the
schema or data, follow the module's backup and migration runbook instead.

Under the host lock it refuses before stopping anything unless all of these
hold: `<sha>` is `current.json`'s release with status `applied` and has a
`result.json` with rollback data; no release has an `attempted.json` without
`result.json` or a `rollback-attempted.json` without `rollback.json`; the
production configuration is the one the release was prepared with; each
`<project>.before.json` exists and matches its recorded digest; the live Compose
snapshot digest and the running container identities equal those recorded right
after apply (so a container recreated since, even with the same image but other
mounts, environment or configuration, or one reconfigured in place with
`docker update`, is refused); and every previous image ID
is still on the host (it never pulls). It then writes `rollback-attempted.json`,
stops the selected services, starts them from the before-snapshot with
`up -d --no-build --pull never --no-deps --wait`, checks that each runs its
previous image, writes `rollback.json` and rewrites `current.json` with status
`rolled-back` and the previous image IDs. A second rollback of the same release,
a rollback of an older release and a failed rollback's retry are refused;
inspect the state by hand then. A release applied before rollback data existed
has no tool rollback and keeps the manual procedure above, and so does a release
whose selected container is no longer running (exited or stopped): rollback then
refuses with "each selected service must already have exactly one running
container".

## Image retention report

```sh
python3 /usr/local/lib/deedkit/production.py images
```

The report lists the image IDs behind the eight CI-published targets'
`deedkit-production-<target>:*` tags in two groups, with reasons. Kept: the
images of `current.json` (including those a rollback restored), of the current
release's `*.before.json`, of the two newest other `result.json` by `applied_at`
(a release without `applied_at` is kept, order unknown), of every `prepared.json`
without `result.json`, and of every container, running or stopped. Everything
else is a removal candidate. It removes nothing: deleting an image stays a
manual owner step by image ID, after checking that no container uses it. Never
use `docker image prune`, `docker system prune` or volume removal for this.

There is deliberately no unattended restart timer. Only `apply` changes runtime
processes, after review of the candidate and any required live-session authority.

## Record the production release

After a successful `apply`, the release owner records the release in Tiden from
GitHub, never from the server: the Tiden token exists only as a GitHub secret.

```sh
gh workflow run production-release.yml --ref main \
  -f sha=<same-sha> -f environment=production -f released_at=<apply time, YYYY-MM-DDTHH:MM:SSZ>
```

Use the UTC time `apply` finished. The workflow runs only from `main`. It
repeats `production.py verify` for the SHA, then records `deedkit@<sha>` in
environment `production` with name `DeedKit <sha12>`, the main push CI run URL
and that time. Tiden captures a release's gate from the current main gate when
the release is first created and keeps it on a repeated dispatch, so the
workflow records only while that SHA's passed run is the latest completed `main`
run in Tiden, and fails if the main run completed last before the release's
creation belongs to another commit. Dispatch it before the next merge reaches
`main`; if a merge lands first, the deployed revision gets no production release
record, and the readiness review treats its release gate as unknown
([tiden-product-workflow.md](tiden-product-workflow.md)). A release that
telemetry created before the dispatch may have no captured gate; the workflow
warns, and its `tiden-release-receipt` artifact records the gate status.
Recording a release changes nothing in production, and its gate is one input to
the readiness decision, not live acceptance.

GitHub documents [required checks and administrator enforcement](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
and [failure/skip handling for dependent jobs](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-jobs).
