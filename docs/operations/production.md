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
([parallel development](parallel-development.md#merging)). It was created with
`gh api --method POST repos/GoSync-Inc/deedkit/rulesets --input deploy/main-ruleset.json`.
Never update it by a PUT of the file: the live ruleset holds fields GitHub added
that the file lacks (for example `require_extra_approval_for_unattributed_changes`,
`dismissal_restriction`, `required_reviewers`), and such a PUT may reset them.
A change merges into `deploy/main-ruleset.json` first (Astra r1, process.md
section 2); an administrator then applies the same one value to the live state:
GET, change that value, PUT, read back. The example sets the merge queue's
`max_entries_to_build` (ANYM-626, PR #420); edit the `jq` filter for another value:

```sh
R=repos/GoSync-Inc/deedkit/rulesets/25274451
gh api $R > before.json                          # kept: the undo payload source
jq --argjson v 6 '{name,target,enforcement,conditions,bypass_actors,rules:[.rules[]|if .type=="merge_queue" then .parameters.max_entries_to_build=$v else . end]}' before.json > put.json
diff <(jq -S '{name,target,enforcement,conditions,bypass_actors,rules}' before.json) <(jq -S . put.json)   # only the changed value
gh api --method PUT $R --input put.json
gh api $R > after.json
diff <(jq -S . put.json) <(jq -S '{name,target,enforcement,conditions,bypass_actors,rules}' after.json)     # empty
gh api repos/GoSync-Inc/deedkit/rules/branches/main   # the effective rules
```

Stop before the PUT if the first `diff` shows more than the changed value. A
rejected PUT leaves the ruleset unchanged; `main` stays protected throughout,
because the ruleset is updated in place, never deleted. Undo: PUT
`jq '{name,target,enforcement,conditions,bypass_actors,rules}' before.json`
as the payload and read back the same way. Keep classic protection
absent: GitHub enforces it together with the ruleset, and
a pull request enters the queue only after it passes the branch protection
checks, so a strict up-to-date rule would again require updating each branch
first. `deploy/main-protection.json` remains the rollback state. To roll back
from the ruleset to classic protection (this drops the merge queue; the undo of
one update is above),
empty the queue (`mergeQueue(branch:"main"){entries{totalCount}}` is 0, or use
`dequeuePullRequest`), restore classic protection with
`gh api --method PUT repos/GoSync-Inc/deedkit/branches/main/protection --input deploy/main-protection.json`,
read it back, and only then delete the ruleset, so `main` is never unprotected.

## Host setup

Keep administration behind the existing Tailscale/OpenSSH route. CI needs no SSH
key, tailnet enrollment, provider token or production database access.

Install reviewed copies of `scripts/production.py` and
`scripts/seamless_release.py` from one `main` commit in the self-updating
layout ("Deploy tool updates" below): `/usr/local/lib/deedkit/production.py`
and `seamless_release.py` are symlinks into `current/`, `current` points at
`releases/<sha>/`, everything owned by root and writable only by root.
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
- **Staging tags (ANYM-602).** A new image is pushed under the staging tag
  `<sha>-attempt-<run_id>-<run_attempt>` in the same package, attested there, and
  gets its final tag `<sha>` only after `promote` verifies that attestation in
  GitHub and in the registry. `promote` reads the manifest by the verified digest,
  checks its SHA-256 and PUTs exactly those bytes under the final tag, so the
  final digest is the attested one; `confirm` then requires each final tag of a
  target this attempt staged to hold that digest. A push, attestation or
  verification that fails leaves only the staging tag, and a full rerun (which
  rebuilds the archives) publishes under its own staging tag. Staging tags are
  never promoted later or read by `production.py`, which resolves only `<sha>`;
  they stay in the package (the publish job never deletes package versions;
  a cleanup is separate work).
- **Write-once tags.** A final tag that already exists is never pushed again. A full
  rerun reuses an existing attested image of the same commit, without an archive.
  A final tag left with no GitHub provenance by a run before ANYM-602 is attested
  only when this attempt still holds the same tested archive; anything else fails
  the job without moving the tag, and with its archive expired the commit has no
  published images until the owner deletes that package version.
- **Provenance.** Each newly pushed digest gets a SLSA build-provenance
  attestation from `actions/attest` with `push-to-registry`, without the
  organization's artifact storage record. A final tag whose provenance exists in
  GitHub but whose registry bundle is missing is still repaired in place (`attest`). It is signed through public-good
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
   (`GOTOOLCHAIN=go1.26.10 CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath
   -buildvcs=false -ldflags=-buildid= -o attest-verify .`). The build is
   reproducible: its SHA-256 must equal `tools/attest-verify/attest-verify.linux-amd64.sha256`,
   which a CI test rebuilds and compares.
2. Copy it to the host as `/usr/local/libexec/deedkit/attest-verify`, owned by
   root, mode `0755`, not a symlink. `production.py` refuses to run any other
   file: before every call it checks that the file is regular, owned by root or
   the operator running `production.py`, not writable by group or others, and
   that its SHA-256 equals its own pinned `VERIFIER_SHA256`. A new build is
   checked before it replaces the installed one: install it under a temporary
   name in the same directory and run the self-test of the tool from the same
   commit with `--verifier <temporary name>` (the same file checks, read and
   never run; only `self-test` accepts `--verifier`), then rename it into place
   together with that tool's switch ("Deploy tool updates").
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
This tool runs no migration by hand (`deploy` runs the qualifying messaging ones itself, "Unattended
migrations" below), does not prove data compatibility, authorize external
actions, or perform provider readback. A schema change needs the module's explicit
migration procedure and the backup gate below first. Confirm that the pinned
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

### Migration releases and the backup gate

Owner 10-28: «давай, конечно, в один релиз»; «процесс с бэкапами что нужно менять давайте
менять».

1. One release. Every migration pending on `main` ships in one release: one prepared SHA, the
   pinned backup and restore runtimes updated once to it, one backup gate, one `migrate` (one
   transaction applies every pending migration in order), each migration's owner and role SQL
   steps in their order around it, then the release. A migration gets a release of its own only
   when its own procedure or a compatibility rule, wherever documented, says so: a contract
   (drop, rename, narrow) still waits for a later release (`.claude/rules/migrations.md`). When
   several procedures below are pending, follow them as one: the backup and restore runtimes of
   the prepared release (it reads the final schema) are installed before the gate; each
   procedure's steps before `migrate` run before the one `migrate`, its steps after `migrate`
   after it, in migration order; a procedure's "schema-N release" is the prepared release. A
   `migrate` retried after `LockNotAvailable` stays under the same gate and hold; item 3's age
   limits hold at each attempt.
2. After the release, no backup is required unless the release deletes data: before such a step
   (045 step 7, `purge-deleted --apply`) a fresh verified backup at the new schema. Otherwise the
   next scheduled daily backup follows as usual.
3. The pre-migration gate is met by a fresh backup, or by the latest scheduled daily backup when,
   at `migrate` (each attempt), all of these hold; otherwise take a fresh one:
   - it is the producer's `last_success_id` and its snapshot time (`last_snapshot_at`, never the
     delivery time) is at most 12 hours old: the 18:00 UTC backup serves until about 06:00 UTC;
   - its schema is the database's current schema (no migration since);
   - the receiver's independent verifier restored it: `last-success.json` names it, its proof is
     `verified` with all seven checks true at that schema, and it is the protected point;
   - the evidence above is kept privately as for a fresh backup: snapshot ID, time and schema,
     backup image revision, ciphertext size and SHA-256, receipt and the proof's run ID;
   - it is held. A hold on a reused daily backup starts after its scheduled verification: stop
     the producer's `personal-os-backup.timer` with no backup service running, then the
     receiver's verifier timer with no verifier run in flight. Until the hold ends, no capture,
     delivery, verifier run, `protect` or `rotate` runs by hand either. A read-only check then
     confirms: both timers stopped and both services inactive; no `active.json` (no pending
     attempt); `status.json` still equals `last-success.json`, which names that backup and its
     proof run (every later verifier run, idle or failed, rewrites `status.json`); the verifier
     service last started before that proof's `verified_at` (`systemctl show -P
     ExecMainStartTimestamp`); the protected file names that backup. That `held` line is at most
     30 minutes old when `migrate` starts and is read again before each retry, as for a fresh
     backup proven by the release kit. The hold lasts across retries; once `migrate` succeeds (or
     the release is abandoned), release the receiver's timer, then the producer's.

   Accepted risk (owner 10-28, quoted above): a reused backup may be 12 hours old. If `migrate`
   destroys or corrupts data, a restore returns the database to that snapshot, and everything
   written after it (messages, receipts, keys, operator changes) is lost.

### Database copies

ANYM-588. `production.py copy-database --config <plan A config> --database <name> --sha <sha>` makes an
exactly checked copy of one live database on this host without stopping its writers, proves it restores
and leaves nothing running. Automatic migrations (ANYM-449) call the same function; a hand release kit can
call the command. It runs under the plan A lock, like `prepare`.

Configuration: the plan A file's `migration` section (`deploy/production.example.json`):
`databases.<name>` = `{container, owner, database}` (the name: up to 12 lowercase letters and digits) and
`copy_timeout_s` (default 1800, the deadline of each dump and of the restore). Without the section
`copy-database` refuses and `deploy` behaves as before. The section is not part of the configuration
digest, so adding it changes no prepared release (`settings_digest`).

Steps, all through `docker exec -i -e PGAPPNAME=deedkit-copy-<id>-<step> <container>`, where `<id>` is
`<sha12>-<name>-<UTC stamp>`:

1. `<state>/migration-copies/<id>/owner.json` (0600, fsynced, directory 0700) names the scratch container
   and the session prefix before anything starts.
2. A keeper `psql` session (`idle_in_transaction_session_timeout` and `statement_timeout` = the deadline)
   opens `REPEATABLE READ READ ONLY`, exports its snapshot, reads the database size and an exact
   `count(*)` of every table (ordinary and partitioned) outside the system schemas.
3. Free space: the copy filesystem and the live database's data filesystem (the Source of the mount that
   holds the container's `PGDATA`) each need 2 × the database size + the reserve, max(10 GiB, 10 % of that
   filesystem). While a dump or the restore runs both are read every 2 s; below the reserve the step is
   cancelled.
4. At the snapshot: `pg_dump --schema-only` (the reference schema), `pg_dump -Fc` to `<database>.dump`,
   `pg_dumpall --roles-only --no-role-passwords` to `roles.sql` (files 0600, created with `O_NOFOLLOW`);
   the keeper commits.
5. A scratch PostgreSQL from the database container's own image ID: `--rm --network none --log-driver
   none --memory 1g --cpus 1 --pids-limit 256 --shm-size 256m`, label `deedkit-copy=<id>`, trust
   authentication (no network: only `docker exec` reaches it), data in `<id>/scratch`. Roles, then the
   database with the live owner, encoding and locale, then `pg_restore --exit-on-error
   --single-transaction`.
6. Every count and the schema-only dump (its random `\restrict` lines excepted) must equal the reference;
   then `copy.json` (`verified`, counts, file hashes and sizes, snapshot time, server version).

A failure is one fixed code: `dump_failed`, `restore_failed`, `count_mismatch`, `schema_mismatch`,
`disk_reserve` or `deadline`; no `copy.json` is written. No helper's output is kept or printed: stderr is
discarded, the stdout of a short helper (counts, versions) is read up to 16 MiB; receipts hold table
names, counts, sizes, hashes, times and codes only.

Cleanup, at the end of every call, pass or fail: `pg_terminate_backend` of every live session whose
application name starts with `deedkit-copy-<id>-`; `docker stop -t 10` and `docker rm` of every container
labelled `deedkit-copy=<id>`, in any state (one a crash left created but not started included); `docker
network rm` of every network with that label; then a read back (no such session in `pg_stat_activity`,
no container or network with the label) for up to 30 s; only then the scratch directory is removed and
`released.json` written. `copy.json` says nothing about resources. Only the label makes a resource the
copy's: a container or network a caller claims in `owner.json` must carry it, and one that merely has a
claimed name is never touched.

Crashes: every `deploy` (not `--dry-run`) and every `copy-database` starts by releasing each copy with an
`owner.json` and no `released.json` the same way, each on its own; the dump files stay. A labelled
container of an already released copy (a start a killed run left in flight) is removed too. A session or
container it cannot confirm gone pauses `deploy`: "a database copy could not be stopped: <id>". Inspect it
with `docker ps -a --filter label=deedkit-copy=<id>` and `pg_stat_activity`, stop what remains, then
remove the pause.

Retention (C6, docs/DATA-POLICY.md): every `deploy` run, paused or not (a paused run takes the plan A
lock only when it is free), and every `copy-database` deletes copies older than 48 hours, whatever their
state, also when another copy is stuck. Of a copy whose resources are not confirmed released only
`owner.json` stays, so a later run can still stop them. The copies are plaintext on this host's disk and
never leave it.

Undo: none needed; a copy changes no database. To remove a copy at once: `rm -r
/var/lib/deedkit-production/migration-copies/<id>` after its `released.json` exists.

### Unattended migrations

ANYM-449, owner 2026-10-28: «Да, добавление миграции автоматически с копией.» `deploy` applies a messaging
range itself only when every step below passes; anything else pauses with today's text and you migrate by
hand (above). Nothing is retried, a migration is never reversed and nothing is restored automatically (a
restore is the gated restore procedure, from the copy or the backup).

Configuration, in the plan A file's `migration` section (`deploy/production.example.json`), besides
`databases.messaging` (Database copies): `unattended` = `{network, dsn_file, runner_config}` (the
database's Docker network, the owner DSN file the migration container mounts read-only, readable by the
image user 10001, and the backup runner's `runner.json`), `receiver_schema_max` (the newest schema the
backup receiver reads; the operator raises it after updating the receiver, until ANYM-589 asks the
receiver) and `rehearsal_limit_ms` (default 1000, at most 4000). Undo: remove `unattended` (or
`databases.messaging`) and every SQL change pauses again.

What qualifies (M1): every SQL inventory path of the range is an added
`services/messaging/messaging_service/migrations/NNN_*.sql` or a modified messaging `*_role.sql`,
or a calendar path of "Calendar" below (`schema.sql`, `deploy/*.sql`, other calendar SQL, a new, deleted or
renamed role file and an edited applied migration pause); the candidate's `migration_rollout.py`, in the classifier's isolated process, names each
added file `expand` by `unattended_class` (no DROP of any kind, so the CHECK widening pair pauses) and each
role file `additive` by `role_class`; the added versions follow the base's highest without a gap.

Phases, each a pause or a wait before any production write:

1. Admission (read-only): no active or unknown call (one poll; else `waiting` with `call_waits`); nothing
   unfinished (`unresolved`); the base release's `store.py` opens its pool with
   `prepare_threshold: None` (#352; else "deploy #352 first"); `receiver_schema_max` is at least the
   target (else `waiting`); the live ledger is exactly the base's (`schema_version` and every
   `archive_migrations` checksum; else a pause).
2. Preparation: plan A `prepare` as always, then the producer check: the image `runner.json` pins has an
   `org.opencontainers.image.revision` whose `backup.py` `SCHEMAS` holds the target (else `waiting`;
   move the pin by hand until ANYM-589). Waits on the receiver or the producer count in `deploy.json`
   (`migration_waits`); the third in a row pauses.
3. Copy: ANYM-588 for `messaging`, keeping the scratch server; the copy's keeper also records the live
   catalog in its snapshot (`catalog_sha256`). A failed copy pauses "the pre-migration copy of messaging
   failed: <code>".
4. Rehearsal: a fresh `docker network create --internal deedkit-copy-<id>` and the container
   `deedkit-copy-<id>-migrate`, each named in the copy's `owner.json` before it is created; the scratch
   server leaves `none` for that network and the candidate's API image runs `migrate-expand` against it
   (trust, the owner's name, the production flags). The canonical catalog (`CATALOG` in production.py:
   schemas, relations, columns, constraints, indexes, triggers, functions, types and enum labels, rules
   and views, policies, sequences, every ACL item, default ACLs, roles, memberships, role settings and the
   row counts of extensions, publications, subscriptions, casts, operators, access methods, languages,
   foreign servers, extended statistics and text search) before and after must differ only by new tables
   with their columns, indexes, constraints, sequences, policies and ACL items, nullable or constant-default
   columns on existing tables (a literal whose stored expression holds only constants, relabels and
   built-in immutable casts; a user's cast prints as a literal too and does not count; a default whose
   printed type name is quoted, such as `'x'::"char"` or a quoted enum, is a known pause: migrate that
   release by hand) and added ACL items; `probe_metadata` only by `schema_version`;
   `archive_migrations` only by the pending versions; and no other existing table may show a write in
   `pg_stat_user_tables`. Its transaction (from `applied_at` to the first read after the container ended)
   takes at most `rehearsal_limit_ms`. Else the run pauses "not additive: <first difference>" or "too slow
   for an unattended lock". The copy's release removes and confirms every resource; a killed run leaves
   them to the next run's reconciliation.
5. Final gate: admission and the producer check again, the live catalog still the copy's (else "the
   database changed shape since its copy"), and the owner DSN file reaches that same database: the
   candidate image reads `[system identifier, database name, OID]` through it, and `docker exec` reads the
   same on the copied container (else "the owner DSN file does not reach the copied database"). The plan
   file carries that identity, and `migrate-expand` checks it again on its one connection before anything
   runs (`identity_mismatch`); the rehearsal's plan carries the scratch database's.
6. Apply: `<sha>/migration-catalog.json` (SHA-256 digests of the live catalog at the copy and the
   rehearsal's change between digests: object names, never a definition, default or setting) and then `<sha>/migration.json` `planned` are fsynced; `docker run --rm --name
   deedkit-migrate-<sha12> --log-driver none --read-only --user 10001:10001 --cap-drop ALL --security-opt
   no-new-privileges --pids-limit 64 --memory 512m -e PGAPPNAME=deedkit-migrate-<sha12>` on the database
   network runs `migrate-expand` once (30 s; past it the container is killed). Its connection takes that
   application name over any the DSN names and reads it back. The outcome is read only once that container
   is gone and no live session carries its name (else `uncertain`): `--status` shows the
   target, the live ledger the candidate's checksums and the live catalog changed exactly as in the
   rehearsal: `migrated`, and plan A and plan B follow at once. Ledger and catalog unchanged: `not-applied`
   ("did not apply"); ledger moved, catalog different: `diverged`; anything else `uncertain`; each a pause.
   A role-only range (no new version) is told apart by the catalog alone. A run that finds a `planned`
   receipt, or a `migrated` one without a plan A attempt, finishes that release first, even when a newer
   commit is attested: `migrated` continues with plan A; `planned` reads the same evidence and records
   `migrated` (continue), `not-applied`, `diverged` or `uncertain` (pause); any other status pauses.

Rollback (M5): a plan A health failure after the migration runs the usual rollback: the base images on the
target schema (expand migrations keep the base working; delivery heartbeats are read only while delivery
features are enabled). The pause reads "plan A of <sha> failed its health wait after migrating schema
<base> to <target>; rolled back to <base> on schema <target>". Receipts and the pause hold file names,
versions, fixed codes and object names, never a DSN, a row value or PostgreSQL's message (the containers
log nothing and their stderr is discarded). The copy's dumps stay for its retention (48 hours, Database
copies).

Calendar (ANYM-586): the same phases apply to the calendar database. Configuration: `databases.calendar`
and `unattended_calendar` = `{network, runtime_file}` (the calendar database's Docker network, e.g.
`deedkit-calendar_calendar-backend`, and `<CALENDAR_STATE_DIR>/service/runtime.json`, the owner DSN file the
calendar CLI reads: mode 0600, owned by the image user 10001). Undo: remove `unattended_calendar` and calendar
SQL pauses again. A calendar range qualifies when every calendar path is an added
`services/calendar/family_calendar/service/schema_vN.sql` or the modified `roles.sql`, and the candidate's
calendar `migration_rollout.py` `plan` (in the same isolated child) accepts it: versions without a gap,
every applied file unchanged, each new file `expand` in the unattended language, `roles.sql` additive (K1).
Admission reads the base release's calendar `store.py` (`"prepare_threshold": None`, K0) and the live
`fcs.migrations`, which must equal the base's files from version 1. Calendar has no backup receiver or
producer gate (its backups take any `fcs.migrations` version). The rehearsal runs the candidate's calendar
image (`family-calendar --home /data service migrate-expand`) on the copy with a rehearsal runtime.json;
`fcs.migrations` has no `applied_at`, so the rehearsal's time is the copy database's `active_time +
idle_in_transaction_time` its session added (an overestimate by the reads between). The data check allows
only the new `fcs.migrations` rows. The final gate reads the identity through the mounted runtime.json.
Apply runs `deedkit-calendar-migrate-<sha12>` once, then `db-roles` (the whole roles.sql) and then the
read-only `db-roles --check` (ANYM-601), each in the calendar image with the same flags; a non-zero exit
of either pauses "calendar migrated to <n>; roles: <code>" (a drift line never reaches a receipt), and
messaging is not applied. Evidence files are `calendar-migrate-plan.json` and
`calendar-migration-catalog.json`.

A range touching both databases copies and rehearses both before either is migrated (a failed copy or
rehearsal of either applies neither), then applies calendar (with its roles steps) and then messaging.
`migration.json` keeps the last database's summary at the top and each database's phase under `phases`
(`rehearsed`, `planned`, `migrated`, calendar's `roles`: `running`, `ok` or a code). A messaging failure
after calendar committed pauses "calendar migrated to <n>; messaging not applied: <code>"; the base code
runs on the new calendar schema (expand only). A run that finds `planned`: a database whose migrate-expand
may have run is decided from its evidence as above, and calendar's roles then run once if they had not
started; roles that were `running` pause "roles: interrupted"; a database that never started after an
earlier one committed pauses "<database> not applied: interrupted" (migrate it by hand). The calendar copy
is calendar's pre-migration copy, kept 48 hours (ANYM-588 C6); restore from it with the gated restore
procedure. It is no longer calendar's only copy: the daily calendar backup (ANYM-607, BACKUP.md "Family
calendar") keeps its last verified point on the backup server (ANYM-586 K4).

## Apply and verify

```sh
python3 /usr/local/lib/deedkit/production.py apply --sha <same-sha>
python3 /usr/local/lib/deedkit/production.py status
```

Without `--config`, `status` reads the configuration the autodeploy unit reads
(`DEEDKIT_DEPLOY_CONFIG` from the environment, else from
`/etc/deedkit/autodeploy.env`, else `/etc/deedkit/production.json`) and prints
its path as `config`; other actions default to `/etc/deedkit/production.json`.
A missing file is named in the refusal.

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
the calls state directory mounted read-only, under `--init`. The probe
interrupts its query after 0.25 seconds and dies after 1 second, so the
service's own writes wait at most that long. The probe container is created
first (`docker create`, so its ID is known) and then started with
`docker start --attach`; the host keeps at most 64 KiB of its output and takes
only the exact answer the probe prints. One poll's Docker commands share 20
seconds and no poll starts at or after the bound, so a wait lasts at most 15
minutes, one poll and the cleanup below (10 seconds per command). A probe whose
creation or start the tool gave up on is found by its `io.deedkit.calls-probe`
label token in any state (`docker ps --all`), removed by container ID and listed
again before any further probe: a removed container can never start, however
late Docker receives the start. When that cannot be confirmed, apply refuses at
once with `a calls probe container could not be confirmed stopped` (no wait, no
stop): inspect `docker ps --all --filter label=io.deedkit.calls-probe`. The host's root opens no file in the calls state
directory and makes no request to the calls service. At the bound apply refuses
with `a call is active: ...` or, when the last read failed (busy, a hot
journal, a missing mount, a probe timeout or output that is not IDs and
statuses), `whether a call is active could not be read: <category>`, or
`wait_expired` when the bound passed before that check could poll (for example
during the last CI check); it never proceeds on doubt and never stops a service for it. A refusal at the first
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

### Backup runner

The host backup runner (`/opt/personal-os-probe/ops/backup_runner.py`, the
`personal-os-backup.service` program, BACKUP.md) is neither an image nor a deploy
tool, so neither `apply` nor `deploy` installs it: the operator installs it
together with the backup image its private `runner.json` pins. Apply refuses
before any stop while that file exists and its git blob is not the release's
`services/messaging/messaging_service/backup_runner.py`; the refusal names both
blob ids, and under `deploy --auto` it pauses the deployer like any refusal
before a change. A host without the file runs no backup and is not checked.
The install is one step under the runner's own lock, so no messaging or calendar backup and no
deploy can start between the check and the hash (a backup started meanwhile refuses
`backup_job_already_running` and systemd retries it; a deploy exits 75): avoid the minutes
around 18:00 UTC. No active call. The new runner's read-only `--check` must exit 0 against
messaging's `runner.json`: exit 3 means an unfinished journal, whose recovery stays under the
existing rules (`--recover-stop` restarts the collectors it stopped), and the step stops. Any
failed command stops it (`sh -e`). From the prepared release: when the backup worker changed
in the same range since the pinned image (`org.opencontainers.image.revision` of
`runner.json`'s `image`), first set the backup image pin in `runner.json` to the release's
backup image and check it, then install the runner, both in the script below (`pin=yes`): a
newer runner can write a `worker.json` key an older worker refuses (BACKUP.md).

```sh
sha=<prepared-sha>; src=<state_directory>/$sha/source; pin=yes   # state_directory: /etc/deedkit/production.json; pin=no: the worker did not change
sudo flock --nonblock /opt/personal-os-probe/backups/.runner.lock sh -euc '
  sha=$1 src=$2 pin=$3 ops=/opt/personal-os-probe/ops cfg=/opt/personal-os-probe/backup-config/runner.json
  new=$src/services/messaging/messaging_service/backup_runner.py
  for u in personal-os-backup.service deedkit-autodeploy.service; do
    case $(systemctl show -P ActiveState $u) in inactive|failed) ;; *) echo "$u not idle"; exit 1;; esac
  done
  python3 -I "$new" --config "$cfg" --check
  if [ "$pin" = yes ]; then
    image=$(docker image inspect -f "{{.Id}}" "deedkit-production-backup:$sha")
    case $image in sha256:*) ;; *) echo "no backup image for $sha"; exit 1;; esac
    rev=$(docker image inspect -f "{{index .Config.Labels \"org.opencontainers.image.revision\"}}" "$image")
    test "$rev" = "$sha"
    rm -f "$cfg.new"
    python3 -I -c "import json,os,sys;p,i=sys.argv[1:];c=json.load(open(p));c[\"image\"]=i
fd=os.open(p+\".new\",os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.write(fd,(json.dumps(c,indent=2)+\"\n\").encode())
os.fsync(fd);os.close(fd)" "$cfg" "$image"
    python3 -I "$new" --config "$cfg.new" --check
  fi
  cp -p "$ops/backup_runner.py" "$ops/backup_runner.py.before-$sha"; cp -p "$cfg" "$cfg.before-$sha"
  if [ "$pin" = yes ]; then mv "$cfg.new" "$cfg"; fi
  install -o root -g root -m 0600 "$new" "$ops/backup_runner.py"
  test "$(git hash-object "$ops/backup_runner.py")" = "$(git hash-object "$new")"
  echo "installed $(git hash-object "$ops/backup_runner.py"), image $(python3 -I -c "import json,sys;print(json.load(open(sys.argv[1]))[\"image\"])" "$cfg")"
' sh "$sha" "$src" "$pin"
```

Every check runs before the first write: the image is resolved and its revision compared
inside the lock, and the new `runner.json` is written beside the live one and checked before
it replaces it; the script's `install` is the former separate `sudo install` step, now under
the lock. Install the new runner before you add any new `runner.json` key: the runner refuses
keys it does not know. Then remove `/etc/deedkit/autodeploy.pause` only when its reason is
this runner's refusal (`the backup runner ... is blob`); a pause for another reason stays.
The step is done when the next messaging backup reaches `state: ok` with a new
`last_success_id` (`/opt/personal-os-probe/backups/status.json`). Undo, under the same `flock`
and with the backup service idle, in this order: remove from `runner.json` every key the
`.before-<sha>` runner does not know (for example `busy_wait_seconds`), install the
`.before-<sha>` copy the same way, and only then put back the image pin it ran with
(`runner.json.before-<sha>`, `cp -p`). An older runner refuses a newer `runner.json` key, and
an older worker refuses a newer `worker.json` key the runner writes (`dump_timeout_seconds`,
ANYM-627), so the backup would fail on every run; the older runner with the newer image passes
(the worker keeps its 600 s default).

The calendar's runner (ANYM-607, BACKUP.md "Family calendar") is the same installed file with its own
config: `/opt/deedkit/calendar-backup/` (root `0700`) holds `runner.json` (from
`deploy/backup-runner-calendar.example.json`; `image` = messaging's pin, so the step above moves it too:
set the calendar `runner.json` `image` to the same ID in the same idle window and `--check` it),
`owner.dsn` (the calendar database's existing owner from `<CALENDAR_STATE_DIR>/service/postgres.env`, host
`calendar-db`), `recipient`, `delivery_ed25519` and `known_hosts`, and the job directory `backups/`. Units:
`deploy/deedkit-calendar-backup.service` and `.timer` (daily 16:00 UTC). Its read-only `--check` is
`python3 -I /opt/personal-os-probe/ops/backup_runner.py --config /opt/deedkit/calendar-backup/runner.json
--check`. Undo, in this order: `systemctl disable --now deedkit-calendar-backup.timer`; let a running
`deedkit-calendar-backup.service` end or `systemctl stop` it (its `ExecStopPost` recovers); go on only when the
service is inactive, no `deedkit-calendar-backup-*` container exists, `/opt/personal-os-probe/backups/.runner.lock.unsettled`
is absent and `--check` prints `idle`; then remove the units (`daemon-reload`) and move the directory aside
(`/opt/deedkit/calendar-backup.removed-<date>`, root `0700`) rather than deleting its journal and evidence. A
failed check stops the undo. Messaging's files are not touched.

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
Messaging migration 050 (`manual`, ANYM-534) adds `component_heartbeats`: one row each for the
API, the matcher and the dispatcher (package version, schema), each role limited to its own row by
row-level security. Additive; older runtimes write nothing there. With it the matcher and the
delivery dispatcher run in production. Nothing here runs during an active call (check read-only
first).
1. Update the pinned backup and restore runtimes to the prepared release, then the backup gate
   (fresh or a qualifying daily backup, above), then `migrate`.
2. Role SQL (reviewed with the release): `gateway_role.sql`, `subscription_role.sql` and
   `delivery_role.sql`. Logins as in step 4 of 045: `messaging_subscriptions_runtime` (`LOGIN IN
   ROLE messaging_subscriptions`) and `messaging_event_delivery_runtime` (`LOGIN IN ROLE
   messaging_event_delivery`), DSNs in `private/subscriptions.dsn` and `private/delivery.dsn`,
   mode 0600, owned like the other DSNs. Undo: stop the service, `DROP ROLE <login>`, remove the file.
3. Private files, owned by uid 10001: `private/delivery-policy.json` (mode 0600; SUBSCRIPTIONS.md,
   Host policy) and the directory `private/delivery-receivers/` (mode 0700, its files 0600) holding
   operator receiver secrets; a receiver's `secret_file` is `/run/secrets/receivers/<file>`.
4. Egress (ANYM-581). The dispatcher joins only internal networks (`delivery_db`, `delivery_proxy`):
   no DNS, no other bridge, no internet, whatever the host firewall holds. Its one way out is the
   `delivery-egress` proxy, which alone joins the egress bridge and resolves and connects only to
   public addresses of hosts allowed by `private/delivery-policy.json`; operator receiver hosts must
   be in `allow_hosts` too, never left to `self_registration`, whose kill switch would then
   cut them off at the proxy (SUBSCRIPTIONS.md, Host policy). Self-registered endpoints (ANYM-585): `deedkit admin
   delivery-endpoints list` shows each one's `admission` (`self_registration` = admitted only by the
   switch); `deedkit admin delivery-endpoints disable ID` stops one; `"self_registration": false` in the
   policy file and `reload-delivery-policy` stop all of them (SUBSCRIPTIONS.md, Self-registration). Set `DELIVERY_RESOLVER=<ip>` in the
   project's env file, `<ip>` being the host's current upstream resolver (`/etc/resolv.conf`, or
   `/run/systemd/resolve/resolv.conf` behind systemd-resolved; not a new third party); the overlay
   passes it as the proxy's `dns`. The host rules are the second layer (a host step):
   `python3 scripts/delivery_egress_check.py rules --resolver <ip>` (the same IP) prints them; as
   root, apply them before the first start (they match bridge names, which may not exist yet) and
   save them with the host's firewall persistence. They drop host-bound packets from all three
   bridges, `dk-delivery0` (egress), `dk-deliverydb0` and `dk-deliverypx0` (internal networks in gateway
   mode `isolated` have no host address; the rules are the second layer), and, from `dk-delivery0`, the tailnet (100.64.0.0/10), RFC 1918,
   link-local and metadata (169.254.0.0/16), loopback and every IPv6 packet; they allow DNS to `<ip>`
   and TCP 443 to public addresses. Undo: `rules --undo`. Writing `DELIVERY_RESOLVER` or a new
   overlay changes the project's Compose configuration, so the next autodeploy halts and writes the
   pause file ("apply the next release by hand"): a plan that changes the host's Compose files or env
   file lists step 5.5's hand apply of the next green `main` SHA S2.
5. First enrollment (owner-authorized, once; `production.py` cannot do it: a selected service must
   already run, `snapshot`, and databases are never restarted). The host is on a release with this
   code, applied by `production.py`; `I=deedkit-production-runtime:<that sha>` is its verified image
   (`docker image inspect` it; never `--build`). `F` is the project's Compose arguments (its `files`
   and env files) plus `-f compose.subscriptions.yaml -f compose.delivery.yaml`.
   1. `docker tag $I anymsg-subscriptions:enroll-<sha>` and `docker tag $I anymsg-delivery:enroll-<sha>`
      (new tags, never a production tag).
   2. `MESSAGING_VERSION=enroll-<sha> docker compose $F up --no-start --no-build --pull never --no-deps
      subscriptions delivery-egress delivery` creates the networks and containers without starting
      them.
   3. `docker network connect --alias db <project>_delivery_db <db container>`: the running database
      joins the dispatcher's network; its backend attachment and identity are unchanged.
   4. `docker compose $F start subscriptions delivery-egress delivery`.
   5. Append both overlays, in this order, to the project's `files` in `/etc/deedkit/production.json`
      and select their services (backup of the file first):
      ```json
      "files": ["...existing files...", "/srv/deedkit/deploy/compose.subscriptions.yaml",
                "/srv/deedkit/deploy/compose.delivery.yaml"],
      "services": {"api": "runtime", "subscriptions": "runtime", "delivery-egress": "runtime",
                   "delivery": "runtime"}
      ```
      The serving API is plan B, which reads its own `/etc/deedkit/http-production.json`
      (`seamless_release.py --config`, its own `prepare` snapshots): append the same two overlays,
      in the same order, to the messaging project's `files` there too (back it up first) and keep
      its `services` exactly the API and the gateway. Autodeploy pauses on these configuration
      changes; under the pause file apply the next green `main` SHA S2 by hand:
      `production.py prepare --sha S2` and `apply --sha S2` (plan A: both enrollment containers become
      release images), then `seamless_release.py prepare --sha S2` and `promote --sha S2` (plan B),
      `status`; check that the serving API container has `DELIVERY_POLICY_FILE` and its mount
      (`docker inspect`); remove the pause. Later deploys include them.
   Undo before the S2 apply, in reverse: restore both configuration backups; `docker compose $F
   stop subscriptions delivery delivery-egress` and `rm subscriptions delivery delivery-egress`;
   `docker network disconnect <project>_delivery_db <db container>`; `docker network rm
   <project>_delivery_db <project>_delivery_proxy <project>_delivery_egress`; `docker image rm`
   both enroll tags. After it, the receipts name the
   overlays and S2 can never be prepared again, so the undo is a release at a newer green `main`
   SHA: restore both configuration backups, stop and remove both services, the same hand apply
   (plan A and plan B) at that SHA, then disconnect and remove the networks. Until such a SHA
   exists, `set-delivery-features --disable` and `docker compose $F stop delivery` hold.
6. Check they run: `docker compose $F ps subscriptions delivery-egress delivery` shows them running
   with no restarts; the proxy logs `{"egress": "listening", ...}`; their logs show no repeated `subscription_match_failed`, `delivery_lane_failed` or
   `delivery_heartbeat_failed`; `SELECT component,version,schema_version,clock_timestamp()-beat_at
   FROM component_heartbeats` (operator DSN) has three rows younger than 120 s on the deployed
   version and schema 50; an agent's `get_capabilities` shows the webhook transport
   `operational: true` once a receiver or endpoint is configured.
7. Connectivity check (the receipt keeps the JSON). Blocked targets are IP literals read with
   `docker inspect`/`ip addr` first; a name, an unresolvable or malformed target fails. The matcher
   has no listener, so start a bounded synthetic one in its namespace: `docker compose $F exec -T
   subscriptions python -I - listen --port 18080 --seconds 300 < scripts/delivery_egress_check.py &`.
   `<alt>` is a public resolver other than `DELIVERY_RESOLVER` (8.8.8.8, or 9.9.9.9 if that is the
   configured one). First the positive control from the `api` container: every listener answers.
   The host listeners are the edge proxy's published 443 and 80. Port 22 is no control on a host
   whose firewall denies incoming traffic by default (ufw `deny (incoming)`, sshd allowed only from
   the admin address on the tailnet): no container reaches it, with or without the delivery rules.
   ```sh
   docker compose $F exec -T api python -I - check --open <matcher ip>:18080 --open <gateway ip>:13240 \
     --open <host public ip>:443 --open <host public ip>:80 --open <docker0 gateway ip>:443 \
     --open-dns <alt> < scripts/delivery_egress_check.py
   ```
   Then from the dispatcher; it exits 0 only when every open target connects or answers, every
   blocked one times out or has no route, the proxy opens a tunnel to `<allowed host>` (the first
   `allow_hosts` entry, such as the owner's receiver host; leave `--open-proxied` out while
   `allow_hosts` is empty) and refuses `example.com` (outside the policy). `<edge ip>` is the
   address Docker's `nat` chain `DOCKER` forwards host ports 443 and 80 to:
   ```sh
   docker compose $F exec -T delivery python -I - check --open db:5432 --proxy delivery-egress:3128 \
     --open-proxied <allowed host> --refused-proxied example.com --blocked 1.1.1.1:443 \
     --blocked 1.1.1.1:80 --blocked-dns $DELIVERY_RESOLVER --blocked <api ip>:8080 \
     --blocked <gateway ip>:13240 --blocked <matcher ip>:18080 --blocked <host public ip>:443 \
     --blocked <host public ip>:80 --blocked <docker0 gateway ip>:443 \
     --blocked <dk-delivery0 gateway ip>:443 --blocked <edge ip>:443 --blocked 169.254.169.254:80 \
     --blocked [2606:4700:4700::1111]:443 < scripts/delivery_egress_check.py
   ```
   Both internal networks use gateway mode `isolated`: they have no gateway, so their subnet `.1` is
   the first container that joined (the database on `delivery_db`, the proxy on `delivery_proxy`),
   and a `<subnet .1>` target reaches that container, not the host. The proof that their bridges hold
   no host address is `ip -4 -o addr show dk-deliverydb0` and `ip -4 -o addr show dk-deliverypx0`,
   both empty, run with every check. Then the second layer from the proxy, which has the egress
   bridge (while `allow_hosts` is empty, `--open 1.1.1.1:443` stands for `<allowed host>:443`):
   ```sh
   docker compose $F exec -T delivery-egress python -I - check --open <allowed host>:443 \
     --open-dns $DELIVERY_RESOLVER --blocked <host public ip>:443 --blocked <docker0 gateway ip>:443 \
     --blocked <dk-delivery0 gateway ip>:443 --blocked <edge ip>:443 --blocked <matcher ip>:18080 \
     --blocked 169.254.169.254:80 --blocked <alt>:53 --blocked-dns <alt> \
     --blocked [2606:4700:4700::1111]:443 < scripts/delivery_egress_check.py
   ```
   `--open-dns` and `--blocked-dns` query a resolver directly over UDP, `<alt>:53` over TCP. A host
   address's 443 or 80 is forwarded to `<edge ip>` and filtered in `FORWARD` (`DOCKER-USER`) with that
   destination, so these targets prove the forwarding layer (the proxy's RFC 1918 drop, the
   dispatcher's missing route), not the rules' `INPUT` drops. Those drops have no positive control on
   such a host: the default-deny incoming policy already keeps host services out of reach, and the
   drops stay as defence in depth. Host :22 targets may run as diagnostics, never as evidence for
   the rules. Stop the listener after.
8. `python -m messaging_service set-delivery-features --enable` (operator DSN) switches
   self-service delivery on. It refuses (exit 1, `component_heartbeat_absent|stale`,
   `component_version_mismatch`, `component_schema_mismatch` with the component) until the API,
   matcher and dispatcher all report this version on schema 50. Undo: `--disable`; queued
   endpoint hints and tests are cancelled under the delivery fence and events stay fetchable.
Rollback: `--disable`, then step 5's undo and `rules --undo`. 050 stays; a schema-49 runtime
ignores the table, but its `migrate` refuses a schema-50 database.
Messaging migration 045 (`manual`, ANYM-511) adds the deletion purge: `messages.purged`
(defaulted, a catalog change), `chats.purged_at`, the `message_purges`, `chat_tombstones`,
`ingest_receipt_links` and `purge_state` tables without foreign keys, guard triggers that keep
later writes of a purged message content-free, and the audit CHECK re-added `NOT VALID` with
`message_purge`. Running schema-44 processes keep working: their writes pass through the guards
unchanged. A schema-44 runtime refuses schema 45, so it ships with its release. Release steps, in
order, outside an active call:
0. As the owner, before `migrate`: `CREATE INDEX CONCURRENTLY IF NOT EXISTS events_message ON
   events(message);` (no lock on event inserts; about 250k rows), then check that `\d events` lists
   `events_message` without `INVALID` (a failed build: `DROP INDEX CONCURRENTLY events_message` and
   build again). 045 refuses an index that is invalid, not ready or not a btree on `events(message)`;
   without one it builds in place only on a table of at most 10k events and 8 MB, so a production
   build never runs inside the migration transaction.
1. Update the pinned backup and restore runtimes to the schema-45 release.
2. The backup gate (fresh or a qualifying daily backup, above), then `migrate`. 045 takes all its
   table locks in one attempt that never queues, retried for 2 s; `LockNotAvailable` (busy tables)
   rolls everything back: run `migrate` again. Then, as the owner, `CREATE INDEX CONCURRENTLY IF NOT EXISTS
   messages_unpurged ON messages(account,chat) WHERE NOT purged;` (needs the new column; no lock on
   message writes) and check `\d messages` lists it without `INVALID`. The chat visibility probe reads
   it; without it a long chat's probe may run out of time and is retried each pass (`hide_pending` in
   `purge-status`).
3. As the owner: `purge_role.sql` (NOLOGIN role `messaging_purge`) and `gateway_role.sql`; deploy
   the release.
4. The worker's own login (owner decision 2026-10-26, «1) а»): as the owner create
   `messaging_purge_runtime` with `LOGIN IN ROLE messaging_purge` and no other grants, with a
   generated password passed to `psql` through a variable read from a 0600 file, never on the command
   line; write its DSN (host and database as in the other private DSNs) to `private/message-purge.dsn`
   with mode 0600, owned like them. Print or log no password. Undo: stop the service,
   `DROP ROLE messaging_purge_runtime`, remove the file.
5. Start the worker: add `deploy/compose.message-purge.yaml` to the project's compose files for
   `production.py apply`, or `docker compose --profile message-purge up -d message-purge`. From here
   every new deletion is purged within a minute: the point of no return for future deletions. Its
   first passes link the receipts stored before 045; deletions wait pending until then (`pending`
   in `get_sync_status` deletions) and are reported `purge_overdue` after 15 minutes; `purge-status`
   shows `receipt_backfill` (`pending` until done).
6. Read-only: `purge-deleted --preview` (counts per account of messages deleted before 045).
7. After a fresh verified backup, `purge-deleted --apply`: the point of no return for past
   deletions. `--apply` drains every due mark and exits non-zero while marks stay pending
   (contention or failures); run it again.
Steps 5 and 7 delete data and wait for the owner's words.
The purge is irreversible: rolling back code or schema does not bring content back; only a
restore of a backup taken before it does, and only while that backup is kept (about two days of
daily verified backups). `purge-disable` stops the worker without a deploy.
Messaging migration 044 (`expand`, ANYM-495) is additive: the new `api_key_operations` table
(operation ID, request digest, key ID) without a foreign key, so it takes no lock on `api_keys`,
which every authentication reads. It is not lock-free: like every `migrate`, the same transaction
reruns `schema.sql`, whose `CREATE INDEX IF NOT EXISTS` holds a share lock on `messages`,
`deliveries` and `download_grants` until commit, so writes to them (incoming messages,
deliveries, download grants) wait for the migration. `migrate` takes those three locks together
or not at all, never queued (#283, #286): behind an open writer it retries for 2 s, then exits with
`LockNotAvailable` and rolls back, the schema stays 43, and it runs again only once that writer
ends. Only the operator CLI reads
or writes it, so it has no grant. A schema-43 runtime refuses schema 44, so it ships with its
release; update the pinned backup and restore runtimes first. Calendar migration 3 adds the same
table for agents (`principal_operations`, no lock on `principals`). Rollback: existing keys are
untouched; only keys issued with `--operation-id` have an operation row.
Calendar migrations by hand (ANYM-601): after the calendar `migrate`, run `db-roles` and then
`db-roles --check` in the same step, before the calendar services restart
(`services/calendar/docs/SERVICE.md`, Grants). A non-zero check (`db_roles_drift`, one line per
missing or extra grant, membership or role) stops the release; fix the cause, rerun both. A crash
between `migrate` and `db-roles` leaves the new schema with the old grants; rerunning `db-roles`
and `--check` repairs it. `db-roles` revokes any privilege the service roles hold on
`principal_operations` (granted by the former `ALL TABLES` line on 2026-10-28); undo of that
narrowing: `GRANT SELECT ON fcs.principal_operations TO fcs_gateway, fcs_worker`.
Messaging migration 043 (`manual`, ANYM-283) is additive: the new `person_links` table
with a SELECT grant to the gateway role. A schema-42 runtime refuses schema 43, so it ships
with its release; update the pinned backup and restore runtimes first, since earlier ones
reject schema 43. Rollback: no data outside `person_links` changes.
Messaging migration 042 (`manual`, ANYM-275) is additive: nullable columns, a wider
`chat_read_observations.basis` check and the new `owner_message_reads` table with its
collector and gateway grants. A schema-41 runtime refuses schema 42, so it ships
with its release; until a collector starts a new receiving period, freshness falls
back to the five-minute rule.
Messaging migration 041 (`manual`) keeps a schema-40 runtime working: it ignores
`reply_snoozes` and `reply_mutes`, so snoozed and muted rows show in the queue
again (nothing stays hidden longer than under schema 40), and its re-dismissal
may keep an older `reason`, which never changes visibility.
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
  release's `<project>.before.json` for each project its rollback record names (the
  one previous release `rollback` restores; other files there are ignored, and a
  missing one, or one whose `services` are not all mappings, removes nothing), of
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
   `services/release-router/**`, `deploy/deedkit-autodeploy.service` and
   `.timer`) that is not installed yet (the running tool file, or the unit in
   `/etc/systemd/system/`, equals the candidate's; a host without the unit
   installs none; the verifier: the installed binary is the running tool's
   pinned build and the candidate's `attest-verify.linux-amd64.sha256` records
   that same build, so a later tool change with an unchanged verifier installs
   itself; ANYM-618), or
   when the configuration changed since the current release (the plan A
   settings digest, each project's Compose document, the HTTP settings digest).
   An SQL pause also names the rollout class of each messaging migration the
   range adds (`041_x.sql expand`, `manual` or `no header`; ANYM-444,
   CONTRIBUTING.md): the candidate's `migration_rollout.py` classifies the
   file, both read from the mirror at the candidate (a blob fetch), in an
   isolated Python process (`-I -S -B`, an audit hook, 30 s, 512 MiB of address
   space, 4 MiB of input, 64 KiB of output, stderr discarded; the candidate is
   attested main, trusted like a tool self-update, so the hook is no sandbox);
   any failure there names the file `manual`. The
   class changes nothing else: the run pauses and you migrate by hand, unless the range qualifies
   for an unattended migration ("Unattended migrations"): then the run migrates in phases instead;
   When the only such change is the `production.py`/`seamless_release.py`
   pair, the self-updating layout installs it instead ("Deploy tool updates");
   apply also refuses before any change while the host backup runner is not
   the candidate's ("Backup runner"), so every merge that changes
   `backup_runner.py` pauses automatic deploys until the operator installs it;
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
`rollback` once (the base release also runs on a schema an unattended migration expanded; the
migration is not reversed) and pauses
with status `rolled-back`; a failed rollback (a call refusal there included), or an apply that stopped in any
other state, pauses with no further action. A failed plan B pauses and leaves
plan A applied (the serving HTTP pair is unchanged or awaits recovery
inspection). `--dry-run` changes no release or runtime state (it may update
the source mirror and take the locks). `status` shows `deploy.json` and the pause reason. To resume after
a pause, fix the cause (migrate, install the tools, inspect the state), then
remove the pause file.

### Deploy tool updates

```text
/usr/local/lib/deedkit/
  production.py -> current/production.py
  seamless_release.py -> current/seamless_release.py
  current -> releases/<sha>
  releases/<sha>/{production.py,seamless_release.py,tool.json}
```

Python takes `sys.path[0]` from the resolved script, so both modules always
come from one release directory, and every documented command and the unit
keep working. `tool.json` is `{"sha", "blobs": {"scripts/production.py": <git
blob id>, "scripts/seamless_release.py": <id>}, "installed_at"}`. A host
without `current` never updates its tool and keeps the pause of step 3. With
it, `deploy` checks the layout with `lstat` (no symlink followed): every
directory and file root-owned and not group/other-writable, `current` and the
two compatibility links the only symlinks, `current` exactly `releases/<40-hex
sha>`, the release holding only the two files, `tool.json` and an optional
`__pycache__`, and `tool.json` naming that sha and the blob ids of its files.
Anything else pauses; the reason ends "the tool does not update itself: ...".

When the only pause of the range is the tool pair, the run is the tool
`current` names, and every earlier check passed (no unfinished attempt,
configuration unchanged, candidate attested), `deploy` installs the
candidate's pair; an SQL, verifier, router or unit change in the same range
pauses as above without installing. `--dry-run` reports `would update tool
<sha>` and changes nothing; no other command updates the tool. In order:

1. Forward only: the candidate must descend from the installed `tool.json` sha
   (`merge-base --is-ancestor`); both files are read from the source mirror at
   the candidate (`git cat-file blob`, the attested commit) and each must hash
   to `git rev-parse <candidate>:<path>`. A pair `tool-rollback` rolled back is
   not installed again.
2. Stage `releases/<sha>.staging-<random>` (temporary names, fsync, rename) and
   run its self-test: `/usr/bin/python3 -B -E -s <staging>/production.py
   self-test --config <plan A> --http-config <plan B>`, PATH only, 60 seconds.
   The new code imports both modules from its directory, checks its
   `tool.json`, loads and validates both configurations, reads `current.json`,
   `http-current.json` and `deploy.json`, and checks the verifier against its
   pin ("install the verifier first" when the candidate pins another build).
   An audit hook installed before any other import refuses every connect,
   process, lock and write. It prints one line `{"self_test": "ok", "sha":
   ...}`; any other exit or output removes the staging directory and pauses
   with its last error line.
3. Rename it to `releases/<sha>` (an existing one from an interrupted run is
   reused only when its `tool.json` and blob ids are the candidate's, and its
   `__pycache__` is removed before its self-test: `-B` still reads bytecode the
   blob ids do not cover), write `<plan A state>/tool-update.json` `{from, to,
   at, status: "activating", accepted}` (fsynced; `accepted` lists `from` and
   the tool it replaced), switch `current` with one `rename(2)`, and run the same self-test
   through `/usr/local/lib/deedkit/production.py`. A pass records `active`; a
   failure switches `current` back to `from`, records `reverted` and pauses.
4. End the run `tool-updated` (exit 0; `deploy.json` keeps `sha`, `base` and
   adds `tool_update`, no `started_at`), so the next timer run, about 5 minutes
   later, deploys with the new tool. The replaced release and the one it
   replaced stay, as `tool-update.json` records them (an `active` record, never
   an install time: a reverted or abandoned candidate counts as none); other
   checked releases are removed.

Once it holds its locks, `deploy` checks the pause file again and, on a host
with a checked layout, that it runs the pair `current` names; a run that
waited while `tool-rollback` or another run switched the tool ends `paused`,
or `error` ("this run is not the tool ... names"; nothing changed, the next
run uses it). Every `deploy` then starts by settling a `tool-update.json` left
`activating`:
`current` at `to` runs the self-test (pass: `active`; fail: back to `from` and
a pause), `current` at `from` closes it `not-activated`, anything else pauses
and names the previous tool, `python3 /usr/local/lib/deedkit/releases/<from>/production.py`.

Undo: `sudo python3 /usr/local/lib/deedkit/production.py tool-rollback`
(under every lock `deploy` takes) points `current` at the `from` of the active
update, records `rolled-back` and pauses autodeploy ("tool rolled back"); by
hand it is `ln -sfn releases/<from> current.new && mv -T current.new current`
in `/usr/local/lib/deedkit`. Still installed by hand: the verifier
(`tools/attest-verify`), the release router and the systemd units. A defect
both the CI tests and the self-test miss acts as it would installed by hand;
the fail-closed pauses apply and `tool-rollback` is the undo. Nothing new
leaves the host: the files come over the existing git fetch, and the receipts
hold SHAs, blob ids, times and the self-test's error line.

Bootstrap, once, by hand, from the `main` commit the next release deploys,
with the timer paused (`<mirror>` is `<plan A state>/deploy-source.git`):

```sh
sha=<40-character main sha>; root=/usr/local/lib/deedkit; release=$root/releases/$sha
sudo install -d -o root -g root -m 0755 $root/releases $release
for f in production.py seamless_release.py; do
  sudo git -C <mirror> cat-file blob $sha:scripts/$f | sudo tee $release/$f >/dev/null
done
printf '{"sha": "%s", "blobs": {"scripts/production.py": "%s", "scripts/seamless_release.py": "%s"}, "installed_at": "%s"}\n' \
  $sha $(sudo git -C <mirror> rev-parse $sha:scripts/production.py) \
  $(sudo git -C <mirror> rev-parse $sha:scripts/seamless_release.py) $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  | sudo tee $release/tool.json >/dev/null
sudo chmod 0644 $release/*
sudo python3 -B -E -s $release/production.py self-test     # {"self_test": "ok", "sha": "<sha>"}
for f in production.py seamless_release.py; do sudo mv $root/$f $root/$f.before-$sha; done
sudo ln -s releases/$sha $root/current
for f in production.py seamless_release.py; do sudo ln -s current/$f $root/$f; done
sudo python3 -B -E -s $root/production.py self-test
```

Its undo: remove the three links and move the two `.before-<sha>` files back.

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
exits 75 and the run counts as a skip. The calendar's backup runner holds the
same lock as its `host_lock` (BACKUP.md, Family calendar), and a manual
`production.py deploy` (without `--auto`) takes it too, without waiting: while a
backup holds it the deploy returns `busy` (`backup_running`) before any change. Every
deploy, `--auto` too, also returns `busy` while the calendar runner's
`<lock>.unsettled` marker exists (its worker is not verified stopped; its runner's
`--recover-stop` or next run removes it). A missing lock directory skips the lock only on a
host without the runner and without `DEEDKIT_BACKUP_LOCK`; otherwise the deploy halts. A skipped run (condition or lock) prints
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
After `LIVE`, the hourly `feedback_sync.py auto` moves agent feedback whose card that release
delivers (ANYM-473, ANYM-536); run `python3 scripts/feedback_sync.py status` and, if it exits 1,
fix the sync (docs/operations/feedback-triage.md, Hourly sync).

## Admin CLI

`python3 scripts/deedkit.py admin key list` (ANYM-494) shows every agent key in one view: one row
per gateway principal (label, enabled, gateway expiry) with the messaging, calendar and calls
credential bound to it (public ID, expiry, `live`, `revoked`, `expired`, `missing`, or `unknown`
when a backend command fails). It reads the gateway policy and the calls registry and runs the
messaging `list-keys --all` / `key-info` and calendar `list-agents` / `agent-info` operator
commands (ACCESS.md, `services/calendar/docs/SERVICE.md`); key files reach those commands on
stdin, never in argv, environment or output. It takes no lock and writes no store or file, so it
is safe during a call. The one-shot operator containers keep their stdout (public IDs only) in
Docker's container log until `run --rm` removes them; on a timeout or an interrupt the CLI stops its
own uniquely named container (`docker stop`, 30 s bound), and the calendar reads carry a 5 s
statement timeout. Filters: `--principal ID`, `--valid` (enabled, unexpired), `--expiring` (within
the gateway's 7-day warning window or expired), `--all` (default); `--json` for machine output;
`--strict` exits 1 when there are findings (exit 2 is an unreadable input). There is no
installed `deedkit` command: `deedkit admin ...` below and in the CLI's printed rerun and undo
hints means `python3 scripts/deedkit.py admin ...` run from the host checkout.

Findings, for every gateway principal, disabled and expired ones included (re-enabling one would
expose them): `credential_missing`, `credential_revoked`, `credential_expired`,
`credential_pin_mismatch` (the key file does not match the policy's `key_sha256`, so the gateway
refuses it), `backend_expires_first` (the backend key ends before the gateway entry),
`shared_credential` (one backend key bound to two or more principals), `unbound_key` (a backend
key no principal holds, with its status) and `backend_unavailable` (a failed backend command, or
no calls registry configured while a principal holds a calls key). With `--principal`, the list
keeps that principal's findings and every `backend_unavailable` of a backend it uses.

The configuration is a private JSON file (`--config`, default
`/opt/deedkit/deploy/private/admin/config.json`; template `deploy/admin.example.json`): policy
and calls registry paths, container-to-host path mappings for key files, and the messaging and
calendar operator command prefixes. Every private file it reads must be a regular, owner-only
file (no group or other bits, no symlink). `admin key label ID TEXT` (1–80 printable characters,
an existing gateway principal) writes the label to `labels.json` beside it, mode 0600, atomically;
no service reads that file, so the enforced policy never changes.

### Write subcommands (ANYM-493 shared rules)

Running `key issue`, `key extend`, `key revoke`, `profile tools` or `undo` is the owner's decision (secrets and access); an agent runs them
only on the owner's exact words. Every write subcommand follows these rules
(tests/test_deedkit_admin_rules.py proves each):

- (a) Locks: it takes the installed host tool's `production.host_locks` (global, HTTP and plan-A
  locks, the set `deploy` holds) and then the calls-registry writer lock
  (`/var/lib/deedkit-admin/calls-registry.lock`), in that order, for at most 30 s; then it refuses
  "a deploy is running" and changes nothing. Under the locks it refuses while a release is
  unresolved (`production.unresolved`, the same check `deploy` runs: an unfinished plan-A attempt or
  rollback, an HTTP attempt neither `serving` nor `startup-failed`, a `serving` attempt
  `http-current.json` does not name) or the deploy pause file exists. It loads `production.py`
  from the installed tool (`/usr/local/lib/deedkit/current`), never a checkout, and refuses when
  that copy has no `unresolved`: the first write needs a host-tool install from a commit with it.
- (b) Active call: it reads the call state with `production.active_calls`, the probe `deploy` uses,
  under the active deploy configuration (`production.active_config`; an admin config naming
  `production_config` refuses), and also probes with `production.calls_in` every running container
  that can read the calls registry (a mount of its directory or of any ancestor directory), so a
  voice consumer outside that configuration counts too. An active call, a timeout or an unreadable
  state refuses with no store changed; so does a container that cannot be probed.
- (c) Validation: each candidate policy or registry is checked in the image of every running
  consumer, that is every container with a mount of the file's directory or of an ancestor
  (`deedkit-gateway --check` in each running gateway, `agent_api.py --check-registry` in the
  running voice image), and, for the policy, in the image `production.py` would roll back to,
  then renamed atomically into the mounted directory. Before any journal, token or backend write
  the same checks run once on a copy of the live documents, so an image without the check refuses
  with nothing written. Only a positive answer passes: the check's
  last stdout line must be the JSON `{"valid": true}`; a rejection, any other output or an image
  without the check refuses. An image without the check is updated first; a live document the
  rollback image rejects clears once a deploy makes the current image the rollback one. It refuses
  when a
  running consumer mounts the policy or registry as a single file (ANYM-424): a rename would not
  reach it.
- (d) Containers: it never starts, stops or recreates a service container; its one-shot operator
  and check containers run with `--rm` and a unique name and are removed on every exit path.
- (e) Journal, rerun and undo: every operation has an ID and a journal
  `/var/lib/deedkit-admin/ops/<op-id>.json` (directory 0700, file 0600) of public IDs, the hashes of
  the entries it created and each step's state, written before and after each step. Each backend
  credential has its own derived operation ID (`<op-id>/messaging`, `<op-id>/messaging-archive`,
  `<op-id>/calendar`), journaled before the call. On a failure or uncertain outcome it stops and
  prints two choices: rerun the same command with `--operation <op-id>` (finished steps replay by
  their operation IDs, the rest continue; nothing is issued twice) or `deedkit admin undo <op-id>`.
  Undo is compensation: it revokes only credentials whose records carry this operation's IDs,
  disables only entries whose hash still matches what the operation wrote (compare-and-swap on the
  file revision, refusing on a conflict), keeps every later change by others, and deletes the
  token files the operation created. An interrupted undo is run again. Undo refuses while a
  credential it would still revoke or disable has no readable token file of its own, or that file's
  token no longer resolves in its store to the record undo revokes (calls: to the pin of the entry
  the operation wrote), since then it cannot compare that credential with other principals'
  references; put the operation's own token file back, or revoke the public IDs in its journal per
  service as an approved manual step. Undo runs the (c) checks on copies of the documents it would
  change before its first journal change.
- (f) Exclusive credentials: undo refuses, listing the principals, when anything but this
  operation's own entry references a credential it would revoke, by the token itself (a copy in
  another file counts); a reference it cannot read (missing, unsafe mode, malformed) counts too.
- (g) Secrets: no token, key hash or key-file content appears in stdout, stderr, child argv or
  environment, logs or the journal; tokens reach operator commands on stdin.
- (h) Versions: before any write, every service command it needs runs once with `--check`;
  `schema_not_ready` or an unknown command or option refuses with the service named.

### Issue a key like an existing one (ANYM-496)

`deedkit admin key issue <new-id> --like <source-id> --days N [--label TEXT] [--dry-run]` creates
a new principal with the source's access in every store: messaging accounts, chats, permissions
and key permissions (and the calls archive key the same way); calendar calendars, scopes, member
and visibility; the calls entry (all fields except ID, hash, expiry and archive configuration) with
a new private archive configuration; the gateway profile, grants and actions. Each messaging key
gets the source key's stored ceiling as is (`issue-key --key-permissions-of`, a NULL legacy or a
wider ceiling included), so it may do exactly what the source key may. N is
1–30 for every store (the card's bound: one N must fit every store, messaging allows 365 and
calendar 183 days, and the gateway entry ends at the earliest backend expiry). `--dry-run` prints the plan (stores, public fields, policy and registry diff with hashes
masked) and writes nothing. It refuses before any write when the source is unknown, disabled or
expired, `<new-id>` or `<new-id>-calls-archive` exists in any store or names a journaled
operation (an operation reserves its ID from its first write, so an undone ID stays taken), N is
out of range, or a shared rule refuses. It never issues a key for an existing principal: the
messaging `issue-key` runs with `--new-principal`, which refuses `principal_exists` under the
principal lock.

Order: every token is first written durably (0600, synced under a staging name, then published
by a hard link that never replaces a file; the gateway token as
`/root/deedkit-admin/tokens/<new-id>.token` in a 0700 directory, backend tokens where the policy
and registry name them, owned by the consumer's UID); then each backend credential is issued with
`--token-file /dev/stdin --operation-id`; then the calls registry and archive configuration; then
the gateway policy, the activation point. The gateway `expires_at` is the earliest backend expiry
read back, so the gateway never outlives a backend. No container restarts: the gateway and voice
re-read their mounted directory on the next request. A token file is either absent or complete; a
rerun removes the staging files an interrupted write left and continues from the journal.

The output lists public IDs, expiries, the token file, the undo command and, as `next`, the single
step for the Mac: `python3 scripts/deedkit.py key install <new-id> --host <ssh-destination> --url
<gateway MCP URL>` (below; the destination stays a placeholder, since only the Mac's ssh config
names this host). `undo` disables the gateway and calls entries and revokes
the backend credentials; revoked records stay for audit, so `<new-id>` stays taken and a fresh
attempt uses another ID. Its output lists `revoked` (store and public ID), `disabled` (entries),
`deleted_files` and `next` (`python3 scripts/deedkit.py key uninstall <new-id>` for the Mac).

### Install an issued key in Claude Code on the Mac (ANYM-498)

Run from the repository checkout on the Mac (Python 3.11+, standard library only); it needs `ssh`
and `claude` on PATH and touches only the user's own files:

```sh
python3 scripts/deedkit.py key install <id> --host <ssh-destination> --url https://<host>/.../mcp
python3 scripts/deedkit.py key uninstall <id>
```

`key install` runs `ssh -T -o BatchMode=yes -- <dest> cat -- /root/deedkit-admin/tokens/<id>.token`
(30 s, at most 1 KiB, stderr never shown) and accepts exactly one gateway token. It stores it as
`~/.config/deedkit/<id>.token` (0600; the directory 0700, refused when it is a symlink, another
user's or group/other-accessible) by a synced staging file and a hard link that never replaces a
file, and copies the shipped helper `scripts/deedkit_headers.py` there as `headers.py` (0600; an
older copy is replaced by a rename). It then runs the helper exactly as Claude Code will (10 s) and
sends one `POST <url without /mcp>/v1/platform_capabilities` with its header (no proxies, no
redirects, verified TLS, 15 s): only HTTP 200 with `service: deedkit` and `principal: <id>` passes.
Only then it adds the user-scope server `deedkit-<id>` (a dot in the ID becomes `_`) with
`claude mcp add-json --scope user`, whose `headersHelper` is `<real path of the interpreter that
ran install> -I -S <path of headers.py> <id>`: the token is never in `~/.claude.json`, argv or
output. Claude Code's config (`$CLAUDE_CONFIG_DIR/.claude.json`, else `~/.claude.json`) is only
read. A rerun with the same token and entry changes nothing; a different token file or another
entry of that name refuses (`key uninstall <id>` first). A failed check adds nothing and leaves
the token file for a rerun. Install and uninstall share the lock `~/.config/deedkit/.lock`.

`key uninstall <id>` removes the user-scope entry only when it has the shape install writes (any
pinned interpreter), then the token file; each part already absent counts as done. The key stays
valid on the host until `deedkit admin key revoke <id>`.

- Rotation: `key extend` keeps the token, so the Mac needs nothing. A new key is `key install
  <new>` then `key uninstall <old>`.
- An interpreter upgrade that removes the pinned Python breaks the helper: run `key uninstall <id>`
  then `key install <id> ...` again to re-pin it.
- Entries left by ANYM-496's manual lines (a `printf` helper, local scope): `key uninstall` refuses
  them. Remove them by hand with `claude mcp remove deedkit-<id>` (add `--scope local` in the
  folder where it was added, or `--scope user`), then run `key install`; an existing
  `~/.config/deedkit/<id>.token` with the same token and mode 0600 is reused.

### Extend or revoke a key in every store (ANYM-497)

`deedkit admin key extend <id> --days N [--dry-run]` (N 1–183) extends every backend credential
of the principal: messaging `extend-key` per key UUID (the calls archive key too), calendar
`extend-agent`, then the calls registry entry to the host's start time plus N days. It reads back
each new expiry and then sets the gateway `expires_at` to the earliest of them and that bound, so
the gateway never outlives a backend whatever the step delays or clocks. Tokens and key hashes stay
the same, so no client changes. It refuses before any write when a credential (the gateway entry
included) is revoked, disabled or expired, or a store's expiry would move earlier (ACCESS.md,
`extend-key`); the output lists the previous and new expiry per store. The backends extend by
their own database clock, so these checks keep a one-hour margin: a credential ending within the
hour, or one ending less than an hour before the new bound or later, refuses (issue a new key, or
pick another N). The margin relies on both backend databases (messaging, calendar) sharing the
admin host's clock: they run as containers on that host (deploy/compose.yaml,
deploy/compose.calendar.yaml). Moving either to another host needs ANYM-567 first (each store
checks the extend by its own clock). If a store still reads back an expiry before the gateway's
current one, the gateway entry is left unchanged and the command stops. A rerun with `--operation` checks every
credential again (one revoked since refuses) and reads a step it had sent from its store instead of
sending it again; sending one again is harmless, since each store sets its expiry under the
principal lock and refuses a shorter one. `undo` of an extend is refused: expiries never move
earlier.

Both commands refuse before any write when a running gateway's `--policy` is not the configured
policy file through its directory mount, since neither the write nor the evidence would reach the
file it enforces. The journal records whether `--token-file` was given, never its path.

`deedkit admin key revoke <id> [--token-file <path>] [--dry-run]` sets the gateway entry
`enabled: false` first (access ends on the next request), then revokes each backend credential
(messaging `revoke-key`, calendar `revoke-agent`; one already revoked is skipped) and disables the
calls registry entry. Records stay for audit; deleting them is a separate owner decision.
Evidence: every running gateway's in-container policy (copied out with `docker cp`) has the
published file's digest, which disables the principal. With `--token-file`, the file's SHA-256
must equal the principal's `key_sha256` before any write, and the configuration must name
`probe_control`, the token file of another live principal; after the revoke a disposable
container in each running gateway's network namespace sends one MCP `initialize` with the control
token and then the revoked one (both on stdin, only the HTTP statuses are printed) and requires 200
then 401, so a 401 for another reason never counts. Without `--token-file` the output says the
bearer probe was not run. `undo` of a revoke enables again only the gateway and calls entries this
operation disabled (compare-and-swap on the entry it wrote) and says that the messaging and
calendar revocations are final: a working key then needs `key issue`.

### Add or remove tools in a gateway profile

`deedkit admin profile tools <profile> [--add TOOL...] [--remove TOOL...] [--dry-run] [--operation ID]` changes
only `profiles.<profile>.tools` in the gateway policy: tools in `--add` that are missing are appended, tools in
`--remove` that are listed are taken out, the order of the rest and every other field (the profile's `actions`, the
other profiles, all principals) stay. A tool is visible to a key only when the profile lists it and the principal's
own grants allow it, so a listed tool never gives a principal more than its grants; it does reach every principal
of the profile, and `--dry-run` and the result list them. Tool names are `[a-z][a-z0-9_]*`, distinct, none in both
options. Nothing to change writes no revision.

It follows the shared rules above: (a) and (b) locks, no unresolved release, no active call; (c) the candidate
policy is checked, before any journal, in the image of every running gateway and in the rollback image (`gateway
--check` refuses an unknown tool name, so an image that does not know a new tool refuses it; a rollback image that
predates the tool clears once a deploy makes the current image the rollback one); a gateway that enforces another
policy file refuses; the rename is a compare-and-swap on the bytes read under the locks and raises `revision` by one;
(e) the journal keeps the profile's tool list before the change, the added and removed names, and the step digests.
A stopped run is finished with `--operation`; the plan of the first run is kept with the digest of the profile it
was made from, and even the first write of a resumed run refuses a profile changed since.

`deedkit admin undo <op-id>` sets the profile back to the tool list it started from, only while the profile is still
exactly what this operation wrote; a later change by anyone refuses it (`changed since this operation wrote it`), and
it refuses a gateway that enforces another policy file like the change does. An interrupted undo is run again.
The undo is a new revision, never an older one. No backend store, key or token is touched.

Optional configuration keys, with the defaults above: `tool`, `journal`, `tokens`, `calls_lock`,
`validators`, `docker` (the Docker command prefix) and `probe_control` (`key revoke --token-file`;
no default).

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
