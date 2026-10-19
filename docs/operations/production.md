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

The calls service admits its owner panel, owner APIs and `/control` only from a
loopback transport peer or a peer in `VOICE_TRUSTED_PEERS` (ANYM-304); the
`/v1/<operation>` agent route and the Twilio callbacks keep their own
authentication. Under Docker the owner's connection through the published
loopback port (the panel and the SSH-tunnelled control MCP) reaches the container
from the host's address on the bridge network that carries the published port,
never from loopback. Trust exactly that network's IPv4 gateway, one address: it
belongs to the host and Docker assigns it to no container. Never trust the
`integration` subnet or any wider network. Docker picks that network by gateway
priority, then other properties (internal, IPv4/IPv6 gateway), with name order
only as the tie-breaker. Do not infer it from the names: start the service, then
read each network's gateway with
`docker network inspect <network> --format '{{(index .IPAM.Config 0).Gateway}}'`
and confirm the choice with the check below. With both networks equal, IPv4-only
bridges and the default names, a synthetic run on 10-19 showed the gateway of
`deedkit-calls_calls-egress`. Recheck after the network is recreated or the
container's networks or gateway priorities change.

Where the value comes from:

- The repository launcher (`scripts/deedkit.py`, `config/modules/calls.toml`) passes
  `deploy/private/calls.env` to Compose as `--env-file`, so `CALLS_TRUSTED_PEERS=<gateway>`
  belongs in that file or the shell. A project `.env` is not read then, and a
  `VOICE_TRUSTED_PEERS` line in `calls.env` is overridden by the overlay's `environment` entry.
- The current production host runs calls as the enrolled project
  `personal-os-voice` (service `voice`, port `127.0.0.1:13844`, one network
  `personal-os-probe_frontend`), not from this overlay. Its source document is the
  plan A project's `files` entry
  (`/var/lib/deedkit-production/e3a52a34…/personal-os-voice.json` on 10-19). The release
  copies are generated from it. Add `"VOICE_TRUSTED_PEERS": "<gateway>"` (`172.20.0.1` on
  10-19) to `services.voice.environment` there. That changes the project's Compose
  document, so the next `deploy` pauses for configuration drift. Apply the change by
  hand: pause, edit, `prepare`/review/`apply` at the newest green `main` SHA, `status`,
  then remove the pause. Code before ANYM-304 ignores the variable, so the value can go
  in first. After ANYM-304 an invalid value stops the service at start.

Check it, without a call or a provider action:

1. `docker inspect <container> --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^VOICE_TRUSTED_PEERS='`
   shows the single gateway address.
2. From the host, `/api/health` through the published port with the owner credential returns 200.
3. From the host, `/control/mcp` with the control token completes MCP `initialize` and `tools/list`
   (for example the owner's `agent_mcp.py` proxy listing the tools).
4. From another container on the same network, the same two requests with valid credentials return
   403 `Local peer required`.
5. The gateway's `calls_capabilities` succeeds with a principal key, and `/v1/capabilities` without
   one returns 401.

A 403 in step 2 or 3 means the configured address is not the host-side peer.

The state directory, Docker's root and, when it exists, `/var/lib/containerd`
(the containerd image store keeps image content there, often on another disk than
Docker's root) each need at least **15 GiB free** before preparation verifies,
fetches or pulls anything; the refusal names `production.py images --remove`
(below). A refusal does not authorize pruning shared Docker objects or deleting
runtime data. Free capacity through a reviewed, targeted operation
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
  `production.py verify` refuses that commit, promotion finds no attestation of
  an image it did not publish, and the Tiden release record waits for `publish`. Publishing is not a required check.

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
python3 /usr/local/lib/deedkit/production.py prepare --sha <40-character-main-sha>
```

Promotion makes no GitHub REST API request and needs no token (ANYM-406).
Its git runs isolated from the operator's environment and configuration
(below, Continuous deployment).
`prepare`, `apply`, `prepare-router` and `seamless_release.py` prepare and
promote prove a candidate the same way each time they check it:

1. `main` contains it. `git ls-remote https://github.com/GoSync-Inc/deedkit.git
   refs/heads/main` must print exactly one line, the head; main's history up to
   that head is fetched treeless (`--filter=tree:0`, commits only) into a
   temporary repository, and the SHA must be the head or one of its ancestors.
   So a commit main has moved past stays promotable and merges never wait for a
   release. A git failure here is reported as "main could not be read" and
   `deploy` retries it on its next run.
2. CI passed on it. The pinned verifier ([Attestation verifier](#attestation-verifier))
   must accept the attestation of `ghcr.io/gosync-inc/deedkit-runtime` at that
   SHA. Only the `publish` job of a `main` push run of `ci.yml` can sign it, and
   `publish` runs only after `CI gate` passed in that run: the commit's own
   `checks / verify`, or the validated reuse of the merge queue run or of the
   pull request run of the identical tree that the queue reused. The reuse job
   (`scripts/ci_reuse.py`) applies the queue rules below inside CI, with the
   job's token, before `CI gate` can pass. An attestation never changes, so a
   later failed or cancelled rerun of the same commit's run does not withdraw it.

The evidence (`ci` in the receipts) records the SHA, the main head that was read
and the verified reference and digests of the proof image; it names no CI run.

`production.py verify --sha <sha>` is the GitHub REST check the release record
(`production-release.yml`) runs with the runner's token, and is no longer a
promotion step. It requires a commit `main` contains (`compare/main...<sha>`)
and the latest successful **push** CI run for that SHA, with successful package,
native and aggregate jobs from its current attempt, including exactly one passed
`publish` job. A PR run, manual baseline run, earlier successful run followed
by failure, missing job or skipped check is refused.
One exception (ANYM-321): a push run whose `checks / verify` was skipped because
it reused the merge queue's verification qualifies only when the latest merge
queue run of the same SHA, from this repository's `gh-readonly-queue/main/`
queue, completed successfully with one successful `checks / verify` and
`CI gate` in its latest attempt. The push run must still contain exactly one
`checks / verify` job, concluded `skipped`; an absent job is refused. Every other
required job must still pass in the push run itself. Both runs are read again after their jobs, so a rerun that
starts in between is refused, and run listings are followed across pages. The
result names the queue run that supplied the test verification next to the
promoted push run. When that queue run itself skipped `checks / verify` because
it reused a full pull request run of the identical tree (ANYM-358), the push run
qualifies only through that chain: the queue run's latest attempt passed
`CI gate` and `Reuse the pull request verification` with `checks / verify`
skipped and no docs-only decision; the pull request named by its
`gh-readonly-queue/main/pr-<N>-<sha>` branch merged into `main` as exactly this
SHA from this repository; and the latest `pull_request` run of its head, on its
head branch, completed successfully with `checks / verify`, `checks / load`,
`checks / evidence` and `CI gate` passed in its latest attempt, is the run, at
that or a later attempt, that the reuse job's one passed step `Reused pull
request run <id> attempt <n>` names, and was created before the queue run's
latest attempt started, so it is the run the queue decision evaluated; a
deleted or replaced producer is refused, never substituted. Both runs are
read again, and the result names the pull request run. The tree equality is
the queue decision's, checked again by the main push run before it publishes.
Run on the host, it spends the anonymous quota (60 requests an hour); an
exhausted quota is refused as "GitHub rate limit exhausted, resets at <time>".

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

Apply locks the host, checks main and the attestation again and validates the prepared
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

A plan A stop never cuts a call (ANYM-442): restarting the calls service ends
every unfinished call as `interrupted`, and the Telegram collector carries
Telegram call audio, so apply waits while any call is active in any running
container of a configured `calls` service (found by its Compose labels). It
checks after its local checks, again after the last CI check, and before each
later project's `stop`; it polls every 10 seconds for at most 15 minutes per
wait and prints one stderr line, `calls check: no active call` or
`calls check: waiting for ...` with the call IDs and statuses (`waiting_for_browser`,
`dialing`, `ringing`, `connecting`, `active`) or an error category. Each read
runs in a container of its own from the calls container's image: no network,
no capabilities, read-only root, 64 MiB and 16 processes, the calls user and
the calls state directory mounted read-only. The probe interrupts its query
after 0.25 seconds and dies after 1 second, so the service's own writes wait at
most that long; a probe the tool gave up on is killed by name
(`deedkit-calls-probe-<hex>`). The host's root opens no file in the calls state
directory and makes no request to the calls service. At the bound apply refuses
with `a call is active: ...` or, when the last read failed (busy, a hot
journal, a missing mount, a probe timeout or output that is not IDs and
statuses), `whether a call is active could not be read: <category>`; it never
proceeds on doubt and never stops a service for it. A refusal at the first
wait writes nothing (retry the same `apply` later). A refusal before a later
project leaves `attempted.json` with some projects updated: recover it as an
interrupted multi-project apply (above). A call that starts in the seconds
between the last read and the `stop` is still cut; the calls service has no
drain switch. Undo: revert the change; the hand check (read `voice.sqlite3`
read-only for those five statuses before an apply) stays the fallback.

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

A release whose `up --wait` failed while every replacement it started runs the
prepared image (running, restarting or exited) is recorded as `unhealthy` in its
`result.json`, with rollback data for the projects it touched; `current.json`
keeps naming the release before it. `rollback --sha <that-sha>` restores those
projects and leaves `current.json` unchanged; it refuses when `current.json`
changed since the release was prepared. Any other apply failure leaves
`attempted.json` without a result. Rollback accepts a selected container that
exited or keeps restarting, not only a running one (`docker ps --all`).

```sh
python3 /usr/local/lib/deedkit/production.py rollback --sha <current-applied-sha>
```

Rollback is a production change: the owner confirms it like `apply` (except
the one automatic rollback of `deploy`, below). It returns
the current release's selected services to the images and configuration that
ran before it, from `<state>/<sha>/<project>.before.json`. It does not call
`promotable()` (the previous revision is no longer current `main`), does not run or
reverse migrations and does not restore data: when the release changed the
schema or data, follow the module's backup and migration runbook instead.
Like apply, it waits for active calls ("Apply and verify") before it writes
`rollback-attempted.json` and before each later project's `stop`; a refusal
before a later project leaves the rollback unfinished.

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
container". The settings digest covers Docker's whole inspected `Config`,
`HostConfig` and `Mounts`, so a Docker Engine upgrade that changes their shape
between apply and rollback also reads as drift: rollback refuses, and the manual
procedure applies. `Mounts`, `HostConfig.Links` and inherited `HostConfig.Ulimits`
are compared as sets, because Docker returns them in no stable order; the digest
is versioned (`v2:`). Records written by the earlier tool, which hashed those
lists in the order returned (installed 2026-10-18 from e0ce9f6; it completed no
apply), carry an unversioned digest: their tool rollback is not supported, and
apply and rollback refuse them with "prepare again" or "use the manual
procedure" (owner decision 2026-10-18). Upgrade: install the fixed
`production.py`, then prepare afresh against the current verified main. An
unattempted candidate directory for the same SHA is renamed (for example to
`prepared-older-tool-<sha>`), never deleted or edited, because preparation
refuses an existing directory; receipts and their digests are never rewritten.

## Image retention

```sh
python3 /usr/local/lib/deedkit/production.py images            # report only
python3 /usr/local/lib/deedkit/production.py images --remove   # remove the candidates now
```

Old release images are removed by themselves (ANYM-439): after a `deploy` that
ends `deployed` (the timer included) and after a successful manual `apply`. Not
after `--dry-run`, `rollback`, a refused or failed apply, or a deploy ending in any
other status. `images --remove` runs the same removal by hand, for example after
a disk-space pause.

Only images carrying a `deedkit-production-<target>:*` tag of the eight
CI-published targets are considered. Kept, with reasons:

- for every production state (this configuration's, the host state root
  `/var/lib/deedkit-production` and its subdirectories with a `current.json` or
  `http-current.json`, and the `state_directory` of every `/etc/deedkit/*.json`):
  the images of `current.json` and those a rollback restored, of the current
  release's `*.before.json` (the one previous release `rollback` restores), of
  every `prepared.json` without `result.json` or `http-attempted.json`, of every
  `http-prepared.json` without `http-attempted.json`, of `http-current.json` and
  its one `previous` generation (older nested generations are retired), and the
  `protected_images` of a top-level plan file such as a storage cleanup's;
- while another operation holds a state's deploy lock, the images tagged with
  the revision of each release directory there without a result (a preparation
  pulls before it writes `prepared.json`); removal then removes nothing and the
  next pass retries. Like a preparation, removal takes every state's deploy lock
  (without waiting) before the image lock and holds them until it ends, so it
  never takes the image lock from a preparation in progress and none starts
  meanwhile;
- every image ID named in a top-level JSON file of `/etc/deedkit` (configurations
  and jobs that pin an image, such as `file-lifecycle.json`) or of a non-state
  directory under the host state root;
- the image of every container, running or stopped; an image with any other
  name (for example `deedkit-rollback-retained:*`); an image of a target this
  configuration does not deploy (the backup runner pins its image by ID in its
  own configuration).

Everything else is removed under the deploy lock and the host image lock: the
image is inspected again and skipped if its ID or names changed; its other
names go first with `docker image rm --no-prune <name>`, then the image with
`docker image rm --no-prune <ID>` while one release tag still names it, so an
interrupted pass leaves it a candidate of the next one. Never `--force`, never
`docker image prune` or `docker system prune`, never volumes: Docker refuses an
image a container uses, and that image is listed under `failed` with Docker's
last error line. The summary lists `removed` and `failed` images (short ID,
tags), the `kept` count, and `interrupted`/`remaining` when an unexpected error
stopped the pass; or only `skipped` with the reason when nothing was removed (a
busy image or deploy lock; a protecting file or directory it cannot read or list;
an applied current release whose rollback snapshot is missing; a configured state directory that does not exist yet, or a state that appeared during the pass). Removal never changes the release's outcome:
`deploy` has written `deployed` before it starts, exits 0 and never pauses or
retries for it; `apply` prints the summary next to its result, and `result.json`
and `current.json` stay unchanged.

Undo: revert the change. An image published to GHCR is pulled again by its
digest (`ghcr.io/gosync-inc/deedkit-<target>@sha256:...`, the receipt's
`references`) while that package version exists, and tagged
`deedkit-production-<target>:<sha>`. A local-only build (built on the host before
CI publication started on 2026-10-17) has no such copy and cannot be restored.

Only `apply`, `rollback` and `deploy` (below) change runtime processes; `apply`
and `rollback` after review of the candidate and any required live-session
authority. The `deedkit-autodeploy` timer (below) runs `deploy` unattended.

## Continuous deployment

```sh
sudo python3 /usr/local/lib/deedkit/production.py deploy \
  --config /etc/deedkit/<plan-a>.json --http-config /etc/deedkit/http-production.json [--dry-run]
sudo touch /etc/deedkit/autodeploy.pause     # stop deploying; remove the file to resume
```

`deploy` runs as root and holds the global, plan A and HTTP host locks for the
whole run. It exits quietly (`paused`, `busy`, `current`, `waiting`, exit 0)
when the pause file exists, another operation holds a lock, `git ls-remote`
shows no new `main` commit, no newer commit has attested images yet, or, with `--auto` (the timer), the last deploy started less than 20 minutes ago.
Otherwise it:

1. reads the current plan A release (`current.json`, status `applied`) and the
   serving HTTP release (`http-current.json`, status `serving`, or
   `serving-retirement-pending` with no managed predecessor: the first managed
   generation keeps the legacy instances; a pending managed predecessor
   retirement is retried first), and pauses while any plan A attempt is
   unfinished, any HTTP attempt (`http-attempted.json`) is neither `serving`
   nor `startup-failed`, or a `serving` attempt is not the serving release or
   an ancestor of it (`promote` stopped before it published `http-current.json`);
2. fetches a blobless mirror of `main` into `<state>/deploy-source.git` (an
   existing path there must be that bare repository) and
   picks the newest first-parent commit after the current release whose
   images, every CI-published target plan A or plan B uses plus
   `deedkit-runtime`, the verifier all accepts. A commit whose image tag the
   registry reports not found (its run still runs, or failed before `publish`
   pushed) is passed over for an older one. When GHCR cannot be read, or an
   image has no attestation bundle yet (also what a failed referrers lookup
   reports), no older commit is taken: the run ends with `error`, and the third
   such run in a row pauses. Evidence that does not verify (another commit,
   signer or digest) pauses. A commit whose deployment was attempted is never
   retried, and no older commit follows it;
3. pauses with a reason when the range changes the SQL inventory (any `*.sql`,
   `services/*/migrations/**`) or a deploy tool (`scripts/production.py`,
   `scripts/seamless_release.py`, `tools/attest-verify/**`,
   `services/release-router/**`) that is not installed yet (the installed tool
   file equals the candidate's; the verifier matches the installed pin), or
   when the configuration changed since the current release (the plan A
   settings digest, each project's Compose document, the HTTP settings digest);
4. runs `prepare` + `apply` (plan A), then `seamless_release.py prepare` +
   `promote` (plan B), all at that one SHA with every guard of those steps;
   when plan A is ahead of the HTTP release, it runs plan B only;
5. writes `<state>/deploy.json` (SHA, base, start, end, outcome) and
   prints one JSON line for the journal; a run that ends `deployed` then
   removes old release images ("Image retention" below) and adds the summary
   as `image_removal`.

No step reads GitHub's REST API, so a deploy needs no API quota and no token
(ANYM-406); each check of a candidate costs one `git ls-remote`, one treeless
fetch of main and one verifier run against GHCR. That git runs without the
operator's `GIT_*` variables, global or system configuration, replacement
objects or templates, so neither a URL rewrite nor `GIT_DIR` can change which
history it reads.

On a failure it writes `/etc/deedkit/autodeploy.pause` (JSON with the reason)
and exits 1; nothing deploys until a human removes it. A refusal before any
change pauses, and so does any other error or state it cannot classify (a
malformed receipt, `deploy.json` included, a failed retirement of the previous
HTTP generation, a lock it cannot take); only a connection error, git or GHCR
failing to be read ("main could not be read", a verifier registry failure) or
a GitHub server error (5xx) before any change is retried by the next run. The
git or GHCR failures are counted in `deploy.json` (`unreachable`); the third
run in a row that ends this way pauses, and a run that gets past them resets
the count. A failed `git ls-remote` or mirror fetch (any cause, the
mirror check included) changes nothing: the run prints `fetch_failed` with
git's last error line, exits 1 and counts it in `deploy.json`
(`fetch_failures`); a successful fetch resets the count, and the third failure
in a row (about 15 minutes on the timer) pauses. When apply waited out an
active call, or could not read whether one is active, before any change
("Apply and verify"), the run ends `waiting` with that reason and keeps the
prepared release: the next run applies it while that commit is still the
candidate, or takes a newer attested one. `deploy.json` counts such runs in a
row (`call_waits`); the fourth (about an hour and a half of waiting) pauses
with the last reason, and a run that writes any other outcome there clears it. A
`--dry-run` never waits and keeps the count. An error after a release step started pauses as an unknown state; a call refusal before a later project is one. When plan A failed its health wait (`unhealthy`, above), it runs
`rollback` once (automatic deploys never change the SQL inventory) and pauses
with status `rolled-back`; a failed rollback (a call refusal there included), or an apply that stopped in any
other state, pauses with no further action. A failed plan B pauses and leaves
plan A applied (the serving HTTP pair is unchanged or awaits recovery
inspection). `--dry-run` changes no release or runtime state (it may update
the source mirror and take the locks). `status` shows `deploy.json` and the pause reason. To resume after
a pause, fix the cause (migrate, install the tools, inspect the state), then
remove the pause file.

### Timer

[`deploy/deedkit-autodeploy.service`](../../deploy/deedkit-autodeploy.service)
runs `deploy --auto` and
[`deploy/deedkit-autodeploy.timer`](../../deploy/deedkit-autodeploy.timer)
starts it about 5 minutes after the previous run ended (plus up to 30 seconds
of jitter and systemd's default one-minute accuracy) and 10 minutes after boot;
enabled later than that, the first run starts at once. `--auto` keeps at least
20 minutes between two deploy starts. The unit reads
`/etc/deedkit/production.json` and `/etc/deedkit/http-production.json` unless
the optional `/etc/deedkit/autodeploy.env` (root-owned, mode `0600`) sets
`DEEDKIT_DEPLOY_CONFIG`.

A deploy and the daily backup never overlap; the backup stops the collectors
(`personal-os-backup.timer`, 18:00 UTC) and an apply would recreate them.
Admission: the unit starts no deploy while `personal-os-backup.service` runs or
waits to retry (any state but `inactive` or `failed`) and none from 13:00 to
18:59 UTC; a failed or unexpected read of either skips the run. A run admitted
before 13:00 ends by 17:03 at the latest (the 4-hour start timeout plus the
3-minute stop timeout), so it is over before the scheduled backup starts.
Exclusion: the deploy holds the backup runner's own lock
(`/opt/personal-os-probe/backups/.runner.lock`, `DEEDKIT_BACKUP_LOCK`) for its
whole run, so a backup started by hand during a deploy refuses before it stops
anything (`backup_job_already_running`) and systemd retries it every 30 minutes,
at most three starts in 8 hours: pause the timer and let a running deploy finish
before starting a backup by hand. While a backup holds the lock, the deploy
exits 75 and the run counts as a skip. A skipped run (condition or lock) prints
no JSON line.

The unit adds no destination: `deploy` reads `main` from GitHub over HTTPS
(git only, no REST API) and GHCR (manifests, attestations, images); the
attestation verifier runs with an empty environment. The journal gets one JSON
line per run (SHAs, times, the outcome and its reason, at most git's last error
line), never resolved Compose settings.

Install it once, after a clean manual `deploy`, from the same reviewed `main`
commit as the tools:

```sh
sudo install -o root -g root -m 0644 deploy/deedkit-autodeploy.service deploy/deedkit-autodeploy.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now deedkit-autodeploy.timer
journalctl -u deedkit-autodeploy.service     # one JSON line per run that was not skipped
```

Pause with `sudo touch /etc/deedkit/autodeploy.pause`: each run then exits 0
with `paused`. A run that creates a new pause, rolls back, fails or cannot fetch
exits 1, so the unit shows as failed until the next clean run. Stop the timer
with `sudo systemctl disable --now deedkit-autodeploy.timer`; that lets a
running deploy finish. Never stop `deedkit-autodeploy.service` itself during a
run: the stop (or the 4-hour start timeout) kills `production.py` and its
children, which can leave the selected services stopped mid-apply; the next
run then finds the unfinished attempt and pauses for a human.

### Wait for a merged commit

`python3 scripts/receipt_watch.py <40-hex main sha>` (ANYM-440) waits read-only, over
`ssh deedkit-prod`, until the receipts show that commit deployed and settled, and prints one line;
run it as one background command. `LIVE` (exit 0): plan A `applied` and the HTTP release `serving`
(or `serving-retirement-pending`) both contain the commit, with no pause and no unfinished attempt.
`UNSETTLED` (1): both contain it, but a pause exists or an attempt is unfinished, so do not report it
live. `REFUSED` (1): the commit is not deployed and a pause exists. `TIMEOUT` and `UNREACHABLE` (2):
no verdict (default budget 4 hours, because the timer starts no deploy from 13:00 to 18:59 UTC). Each
poll runs one constant ssh command that sends a fixed read-only script, which prints only `sha`,
`status`, `reason` and `at` of `current.json`, `http-current.json` and `deploy.json`, the pause
reason and the names of unfinished and serving attempts. The tool reads receipts; it does not probe containers.

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
