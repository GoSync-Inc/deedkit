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

Confirmed production incidents follow [incident recovery](incident-recovery.md)
and AGENTS.md section 2: restore the affected path first, then finish deferred
review and records before other tasks. A compatible service-only recovery does
not wait for a schema migration, unrelated service updates, fresh full backups
or a restore drill. Main/CI/artifact verification, candidate inspection, locks,
active-call checks and session/state preservation still apply. A restart of the
existing failed service/session has standing incident authority. Routine releases
follow "Migration releases and the backup gate" below; a repair that deletes or
rewrites user data keeps its gate there.

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

`production.json` is only the default name. The production host has no
`/etc/deedkit/production.json`; its plan A file is
`/etc/deedkit/release-20261018-r7.json`, the file the autodeploy unit's
configuration names and `status` prints as `config`. Where this document writes
`/etc/deedkit/production.json`, read that file, and on the host pass
`--config /etc/deedkit/release-20261018-r7.json` to every action but `status`.

Available build targets are `runtime` (API, Telegram, subscriptions), `indexer`,
`enricher-local`, `whatsapp-native`, `gateway`, `calendar`, `recordings`, `calls` and `backup`.
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

1. `grep VOICE_TRUSTED_PEERS <file>`, where `<file>` is the project's Compose file named above,
   shows the single gateway address.
2. From the host, `/api/health` through the published port with the owner credential returns 200.
3. From the host, `/control/mcp` with the control token completes MCP `initialize` and `tools/list`
   (for example the owner's `agent_mcp.py` proxy listing the tools).
4. From another container on the same network, the same two requests with valid credentials return
   403 `Local peer required`.
5. The gateway's `calls_capabilities` succeeds with a principal key, and `/v1/capabilities` without
   one returns 401.

A 403 in step 2 or 3 means the configured address is not the host-side peer.

The disk that receives image bytes needs at least **15 GiB free** before preparation
verifies, fetches or pulls anything; the refusal names `production.py images --remove`
(below). With the containerd image store (`docker info` reports `driver-type
io.containerd.snapshotter.v1`) that is `/var/lib/containerd`, and the state directory and
Docker's root need **5 GiB**: no image byte lands there, a preparation writes about
100 MiB and the floor leaves room for a migration on a database volume. When the store
cannot be read, the state directory, Docker's root and `/var/lib/containerd` all keep the 15 GiB. Below 25 GiB on
`/var/lib/containerd` or 15 GiB on `/var/lib/docker` the deploy's `cleanup` line
(`deploy.json`, journal) and `status` carry `disk_warnings`. A refusal does not authorize pruning shared Docker objects or deleting
runtime data. Free capacity through a reviewed, targeted operation
or add storage before retrying.

### Files volume on its own disk (host, 11-05)

The current production host keeps original files on Hetzner volume `deedkit-files`
(`/mnt/HC_Volume_107370696`, tree `anymsg-files`), bind-mounted by `/etc/fstab` over
`/var/lib/docker/volumes/personal-os-probe_files/_data`. The Docker volume name, Compose
and the backup's `files_volume` do not change, and a container gets the disk when it starts. Both
fstab lines carry `nofail` and `x-systemd.before=docker.service`, an ordering and no
dependency: Docker and unrelated services start without the volume. The old tree under
the mount is `root:root` mode 000, so without the volume a file consumer gets EACCES
rather than stale data.

- **The disk floors above do not watch this disk.** `disk_warnings` reads containerd and
  Docker's root. Collectors stop background media at 19 GiB free here and live capture at 3 GiB.
- **Named volumes are `master:` mounts here, not rprivate.** A mount placed directly on
  `_data` reaches every running volume container at once: `ro` consumers become
  writable and the nested `.uploads` bind is hidden. Never mount there while containers
  run without first making the parent private (`mount --bind P P; mount --make-private P`).
- **Never `systemctl stop` that mount unit;** use a plain `umount`. A consumer moves to
  the other disk only at its next start, so an undo needs both trees kept in sync until
  every consumer has been recreated.

## CI-published images

Every `main` push run publishes the nine images its `images / build (<target>)`
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
a reviewed pull request refreshes `trusted_root.json` and an agent installs the
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
`checks / verify` job, concluded `skipped`; an absent job is refused. Beside that
skip, and only then, the push run may also skip `native / synthetic`: the queue
run must then show one successful `native / synthetic` too (through the chain
below, the pull request run must). Every other
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

A platform cutover follows [backup/migration and rollback](platform-cutover.md); a routine release follows
"Migration releases" below.
This tool runs no migration by hand (`deploy` runs the qualifying messaging ones itself, "Unattended
migrations" below), does not prove data compatibility, authorize external
actions, or perform provider readback. A schema change needs the module's explicit
migration procedure first; a critical one also the backup gate below (any other has none,
"Migration risk tiers" below). Confirm that the pinned
backup/restore runtimes support both the current database schema and the planned
target, so the daily backup keeps running after the migration. For the backup gate: resolve delivery
failures, including receiver quota/capacity failures,
before taking the fresh pre-migration snapshot; use a reviewed retention/storage change
that preserves the protected restore anchor. Confirm durable receiver delivery,
retain the snapshot ID/time/schema, image revision, ciphertext size/hash and
receipt privately, and verify an isolated quarantine restore using the module's
[backup procedure](../../BACKUP.md). Calculate freshness from the snapshot time,
not from a later delivery retry. A failed delivery, local-only snapshot or stale
receipt does not establish this gate. The deployment tool does not verify these
live backup conditions; preparation and successful source CI do not establish this
gate.

For R2 delivery, the opt-in [independent receiver verifier](../../BACKUP_R2.md#automated-independent-restore-on-the-receiver)
can establish this restore evidence on the receiver using a root-only recovery
key and isolated helper/PostgreSQL containers. It requires its own reviewed
enrollment and actual successful restore; an enabled timer, upload receipt or
synthetic CI run does not establish the live backup gate.

### Release evidence and env files

The files the tools write name each `env_file:` and hold none of its values (ANYM-773). Compose 2.40.3, the host's, lists
`config --no-env-resolution` and ignores it (measured 2026-11-05 on the host and with the upstream 2.40.3 binary; 5.5.0
honors it), so the tools do not use that flag: a `--no-interpolate` render names the references on both versions, and the
written document is the resolved document without the keys of those files. Default `config` of each written Compose file
must read back as exactly the resolved document, so an env file value using a `${VAR}` of the project's interpolation
input refuses at `prepare`: apply reads the written file without that input. No file on the host does this today.

| Writer | What it holds |
|---|---|
| `prepare`'s `<project>.json`, `<project>.before.json` | written with references |
| enrollment's `gateway.pre-run.json` | written with references |
| plan B's `http.json` | written with references |
| enrollment's resolved documents (`enroll --plan`) | digest only; in memory |
| policy copies (enrollment backup, plan B's digest of the policy) | holds no env-file value: the gateway policy and configurations name no env value |
| `verify.env` of the egress check | holds the plan's `verify_env` values only inside the private temporary directory of that plan, removed when it ends; never in release evidence |

The resolved document stays in memory: it feeds the runtime checks and every digest, so `before_digest`, `after_digest` and
the drift comparisons mean what they did. `prepared.json` gains `render: 2` and `env_files: [{path, sha256}]` (a digest of
each file; `null` for an optional file that is absent); a receipt without `render` is legacy and its inline files keep
working. `apply` refuses when an env file named by a selected service changed since `prepare`, and any change that alters
the resolved document refuses `apply`, the current-matches check and `rollback` as drift, as any changed document does.
Rollback restores images and Compose settings, not env-file values: with unchanged files a legacy `.before.json` restores its
inline values and a new one uses the env files as they are.

Refusals, which name at most a file path: `compose_ignores_env_resolution` (a synthetic project's env file was printed, or
its reference dropped; nothing is written) and `env_value_in_evidence` (a value of 8 or more characters of a named env file
occurs in the document to write, or Compose cannot read such a file to check it). The one exception is a string the source writes unchanged at that same place, under a key
that does not look secret (`PASSWORD`, `PASSWD`, `SECRET`, `TOKEN`, `*_KEY`, `CREDENTIAL`, `DSN`, `DATABASE_URL`, `PRIVATE`): that
text comes from the source. The host's db healthcheck writes `POSTGRES_USER` and `POSTGRES_DB` this way. Interpolation files
(`--env-file`, else the project's `.env`) hold paths, addresses and versions the document needs, never a secret; only a
secret-named value refuses there, except a `*_FILE` path. This is a limit: a secret under another name in an interpolation file
is not caught, and neither is a secret written by hand in `environment:` or a Compose file. The calendar project's source is
such a document until ANYM-779.

This change rewrites and deletes no existing file: release digests bind them, and the current release's `.before.json` is the rollback
target. Measured 2026-11-05 by key names: of 115 JSON files in 31 release directories, the secret-named key with a value was
`POSTGRES_PASSWORD` of `calendar-db`; the owner password was rotated that day, so older copies hold a dead value and the two
current documents the new one. After this change and ANYM-779 are live, one more rotation (ANYM-784) leaves every older file
with a dead value, so none is deleted. Old release directories leave only by the tool's own retention ("Image retention"), which keeps every rollback target.

### Migration releases and the backup gate

Owner 10-28: «давай, конечно, в один релиз»; «процесс с бэкапами что нужно менять давайте
менять».

1. One release. Every migration pending on `main` ships in one release: one prepared SHA, the
   pinned backup and restore runtimes updated once to it, one backup gate if item 3 needs one, one `migrate` (one
   transaction applies every pending migration in order), each migration's owner and role SQL
   steps in their order around it, then the release. A migration gets a release of its own only
   when its own procedure or a compatibility rule, wherever documented, says so: a contract
   (drop, rename, narrow) still waits for a later release (`.claude/rules/migrations.md`). When
   several procedures below are pending, follow them as one: the backup and restore runtimes of
   the prepared release (it reads the final schema) are installed before the gate; each
   procedure's steps before `migrate` run before the one `migrate`, its steps after `migrate`
   after it, in migration order; a procedure's "schema-N release" is the prepared release. A
   `migrate` retried after `LockNotAvailable` stays under the same gate.
2. After the release, no backup is required unless the release deletes data: before such a step
   (045 step 7, `purge-deleted --apply`, critical by "Migration risk tiers") a fresh verified backup
   at the new schema. Otherwise the next scheduled daily backup follows as usual.
3. Owner 11-04 (process-fast-release, verbatim): «Мы же уже делали, что при простых миграциях никакого
   быкапа не ждет ... мы нацелены будем на то, чтобы разработка шла быстро ... делали, только какие-то
   быкапы, только в критических случаях ... Мы быстрее катили и быстро выкатывали, чтобы без этой сложной
   канители и блоков каких-то заморозок»; «давай все делать не ждать ничего»; «не умру, не умрет никто. Это
   не какой-то там банковской системы». Before it, owner 10-29 «Я готов принять риск максимально». A
   release without a critical migration ("Migration risk tiers" below) has no backup gate and nothing to
   wait for: no fresh backup, restore proof, hold, backup-age check, `copy-database`, time window, merge
   freeze, new release kit or plan review. The latest verified daily backup is its restore point. Its only
   waits are the tool's own: an active call, and a backup that holds the runner lock. This holds for
   messaging, calendar, recordings and the calls state (`voice.sqlite3`, BACKUP.md "Calls state"): each has a
   daily verified backup as its restore point. A store with no daily backup running keeps a restore point
   anyway: a PostgreSQL database, unattended, its pre-migration copy (Database copies), by hand
   `copy-database` first. A critical release needs the backup gate above (a fresh backup and its
   restore proof, never a reused daily backup), an undo and one Astra review round of
   its plan (harness process.md section 2, reviews.md Rounds 7). The release kit or PR names each file's tier.

   Accepted risk (owner 10-29 and 11-04, quoted above): if a non-critical `migrate` destroys or
   corrupts data, a restore returns the database to the latest daily backup (about 24 hours old at most
   while the daily backup succeeds), and everything written after it (messages, receipts, keys, operator
   changes) is lost. No gate backup or copy was restored in any hand release from 10-17 to 11-04.
4. How a non-critical release runs. `deploy` applies it itself when the range qualifies ("Unattended
   migrations": every added file the classifier calls non-critical and appended role grants; the backup
   receiver's schema holds none). A range that still pauses (a critical file, a rename or another change the
   rehearsal refuses, a role file that does not only append grants) is released by the agent who sees the
   pause at once, at any hour, with the previous release's tested commands: a tested runbook,
   so no new kit, plan or review round. The order:
   1. Exclusion from the backups (a backup stops the collectors and holds snapshots; an apply recreates
      the collectors and a migration takes table locks). `personal-os-backup.service`,
      `deedkit-calendar-backup.service` and `deedkit-calendar-backup-catchup.service` read `inactive` or
      `failed`, `/opt/personal-os-probe/backups/.runner.lock.unsettled` is absent and `flock --nonblock` on
      `/opt/personal-os-probe/backups/.runner.lock` succeeds; then `systemctl stop` the timers
      `personal-os-backup.timer`, `deedkit-calendar-backup.timer` and `deedkit-calendar-backup-catchup.timer`
      (record which were active) and read the three services again. A backup that runs: wait for it to end
      (`apply` also waits for one by itself, ANYM-744). Undo and the last step: start the recorded timers again.
   2. `prepare`, then the "Backup runner" step (it takes and releases the runner lock itself).
   3. Hold the runner lock through the migration, so no backup, timer-started or by hand, runs during it: `flock --nonblock /opt/personal-os-probe/backups/.runner.lock sh -euc
      '...'`, whose script first checks again, under the lock, that the three services read `inactive` or
      `failed` and that the `.unsettled` marker is absent, then runs `migrate`. `apply` and `promote` take
      that lock themselves (ANYM-744, with a bounded wait for a running backup), so they run right after
      the wrapper, outside it (inside it they would wait for their own caller). Then remove the pause
      (`status` shows no pause and no unfinished attempt).
   4. Start the timers recorded in step 1. A daily backup the stop skipped runs at its next time; that is a
      backup follow-up, never a release hold. So is a backup receiver that does not read the new schema
      yet: a hand release does not read `receiver_schema_max`; raise it once the receiver reads the target.

   Merges never wait for a release: the candidate is one SHA ("Prepare and inspect"), so there is no merge
   freeze. No release has a time window: the waits are an active call (the tool checks it) and a running
   backup (step 1; the autodeploy unit, "Timer").
5. The lifecycle workers ("Lifecycle workers") are plan A services in both routes, the unattended one and
   item 4's: `migrate` runs while they run on the previous images, and `apply` then stops and starts them with
   the other plan A services; a rollback restores them with the others. A worker image's compatibility with the
   schema is proven by the release's own tests, not by a label.

### Migration risk tiers

Owner 11-04 (item 3 above): backups only in critical cases. Two tiers. A release takes the tier of its
highest-tier pending migration file, changed role file or step.

- Critical (the backup gate, an undo and one Astra round on the plan first): a statement that deletes
  or rewrites existing rows or drops stored data. DELETE, UPDATE, MERGE, TRUNCATE, INSERT ... ON CONFLICT
  DO UPDATE; DROP TABLE, COLUMN, SCHEMA or MATERIALIZED VIEW; a DROP ... CASCADE whose dependents include
  a table, a column or anything the author cannot list; ALTER COLUMN ... TYPE or SET EXPRESSION that
  rewrites the table or converts stored values (a binary-coercible change without a rewrite, such as
  `varchar(n)` to `text`, is not); the same inside a `DO` block or a function the file creates or replaces.
  Computed: a function or procedure whose body writes rows other than by INSERT, changes the schema or runs
  text as SQL (a word of `FUNCTION_WRITES`), in a language other than SQL or PL/pgSQL, or with its text
  outside a dollar quote; any trigger, event trigger or rule (the runner's own ledger INSERT and UPDATE fire
  one); an INSERT in a file that creates or replaces a function; a call of a function the file creates; any
  ALTER other than ALTER TABLE, and any ALTER TABLE action outside the complete forms below. Also a
  data-deleting step (item 2) and a statement whose effect on existing rows the author cannot name.
- Non-critical: only these forms. CREATE of a table, index, sequence, view, materialized view, type, domain,
  policy, schema, role, aggregate, statistics or cast; a function or procedure the rule above lets through.
  ALTER TABLE: ADD COLUMN; ADD a CHECK, UNIQUE, PRIMARY KEY, FOREIGN KEY or EXCLUDE constraint, `NOT VALID`
  included (a failing one aborts its own transaction and loses nothing); VALIDATE CONSTRAINT; DROP CONSTRAINT;
  ALTER COLUMN SET or DROP DEFAULT and SET or DROP NOT NULL; ENABLE or FORCE ROW LEVEL SECURITY. DROP without
  CASCADE of an index, function, procedure, policy, view, type, domain, aggregate, statistics, cast or role;
  a plain INSERT; comments, ANALYZE and `SET LOCAL`; grants and REVOKE; a `DO` block that only grants, revokes
  or checks a precondition.

The tier decides the backup and the plan review only. Expand, then contract
(`.claude/rules/migrations.md`) still keeps a DROP, RENAME or narrowing out of a release whose running
code uses the old shape; role SQL keeps its Astra r1 in its PR; the active-call check and the release
order stay. The tier is computed from the file (ANYM-743): `risk_class` in the candidate's
`migration_rollout.py` (the calendar copy for `schema_vN.sql`) names a statement non-critical only in
the forms listed above and everything else, text it cannot tokenize included, critical; the deploy pause
and `deploy --dry-run` print it per added file (`069_x.sql manual critical: line 12 DELETE`; a file whose
classifier failed reads `manual` with no tier: treat it as critical). It reads statements, not what an
expression calls: a DEFAULT, CHECK or `CREATE TABLE AS` may call a function that already exists. The rehearsal
sees what such a call does on the copy (a moved write counter, row count or column number); a call that first
runs after the release is seen by neither. A `-- Risk tier:` header is a note
for the reviewer and cannot lower the computed tier; to take a critical statement out of a release,
split it into its own file. Until
ANYM-675 ships, the deployer still copies before every unattended range, because its rehearsal runs on
the copy; that copy is automatic and holds no one.

An index on a large existing table goes out before the deploy, built CONCURRENTLY (the pattern of 045
step 0 under "Roll back the current release"). The rehearsal builds a migration's index on the copy's
scratch server, which has 1 CPU and 1 GiB, and an unattended run refuses one that is too slow. When that
happens, an agent builds the index as the owner on the live database: `SET lock_timeout='5s'`, then
`CREATE INDEX CONCURRENTLY IF NOT EXISTS <name> ON <table>(<columns>)`. That build does not lock writes.
It then checks `pg_index` against the migration's own precondition: valid, ready, the exact columns and
the access method. An invalid build gets `DROP INDEX CONCURRENTLY` and one rebuild. Then the agent copies
the pause to `autodeploy.pause.before-<migration>` and removes it. The migration's `CREATE INDEX IF NOT
EXISTS` then finds the index, the next rehearsal is fast, and the tool's own copy, catalog compare and
migrate release it. Nothing writes the ledger by hand. A migration that adds an index to a large table
should therefore use `IF NOT EXISTS` with a `DO` precondition on `pg_index`, as 074 does. Undo before the
deploy: `DROP INDEX CONCURRENTLY <name>`. After the deploy, the index belongs to its migration.

Release record, 2026-11-09 (074 `events_created_at`, ANYM-865): the 08:34Z run refused 074 with "too slow
for an unattended lock: the rehearsal took 2086 ms, over 1000 ms" (`events`, 437k rows). Pre-checks:
no active call, the three backup services inactive, no `.unsettled` marker, the runner lock free. The
CONCURRENTLY build ran 08:51:12Z to 08:51:14Z, and every condition of 074's `DO` block read true. The
pause was copied to `/etc/deedkit/autodeploy.pause.before-074` and removed. The 08:56Z run then copied
and rehearsed in 16 ms, migrated 73 to 74 at 09:09:12Z and deployed c68c9081 at 09:12:26Z, with no
pause left.

Release record, 2026-11-09 (075 `message_receipts_source_check`, a CHECK widened NOT VALID): the 09:23Z run
halted `migration_refused`, "the pre-migration copy of messaging failed: disk_reserve": `/` had about 18 GiB
free, below twice the 4 GiB database plus the 10 GiB reserve. The tool went from 4085de9f to d95cbcb2 by hand
first ("Deploy tool updates" steps 1-3). The backup timers were stopped at 09:33:32Z (three, recorded). Then,
under the runner lock, `python -m messaging_service migrate` ran at 09:35:06-09Z through `docker compose run
operator migrate`, with `PGAPPNAME=deedkit-hand075-migrate`: rc 0, schema 74 to 75, the constraint left NOT
VALID. `production.py apply --sha d95cbcb2` ran 09:37:05-09:39:26Z (no active call), then `seamless_release.py
prepare` and `promote --sha d95cbcb2` 09:39:50-09:40:50Z. The timers started again at 09:40:58Z and the pause
was removed at 09:41:08Z. Undo: `production.py rollback` to c68c9081 (the CHECK only widens, so c68c9081 runs on
75). Follow-ups: a range migrated by hand now releases with no second copy ("Unattended migrations",
Admission, PR #803), and `copy_root` moves the copies off `/` ("Database copies").

### Database copies

ANYM-588. A release needs a copy only for a database with no daily backup running (owner 11-04, "Migration
releases" item 3); an unattended range still gets its automatic one ("Migration risk tiers"). `production.py copy-database --config <plan A config> --database <name> --sha <sha>` makes an
exactly checked copy of one live database on this host without stopping its writers, proves it restores
and leaves nothing running. Automatic migrations (ANYM-449) call the same function; a hand release kit can
call the command. It runs under the plan A lock, like `prepare`.

Configuration: the plan A file's `migration` section (`deploy/production.example.json`):
`databases.<name>` = `{container, owner, database}` (the name: up to 12 lowercase letters and digits) and
`copy_timeout_s` (default 1800, the deadline of each dump and of the restore) and `copy_root` (optional: an
absolute, normalized directory, for example `/mnt/HC_Volume_107150701/deedkit-migration-copies` on the image volume;
default `<state>/migration-copies`). The configured root is created `0700` only when every parent exists, is a real
directory (no symlink) owned by root or the operator and writable by no one else (unless sticky, like `/tmp`), and the
last parent is on another filesystem than the state, so an unmounted volume whose mount point is still there refuses
instead of taking the copy onto `/`. Before its first copy the root is recorded in `<state>/copy-roots.json`; every run
tends (releases and expires) the configured root, every recorded one and `<state>/migration-copies`, refuses while a
recorded root is missing (`copies_unreconciled`: mount it), forgets a recorded root that holds no copy and is no longer
configured, and treats a copy ID found in two roots as stuck.
Production, from the plan A edit after this change (2026-11-09): `copy_root` on the image volume, because `/` was too small for messaging's dump and
restore (the 075 copy refused `disk_reserve`). Without the section
`copy-database` refuses and `deploy` behaves as before. The section is not part of the configuration
digest, so adding it changes no prepared release (`settings_digest`).

Steps, all through `docker exec -i -e PGAPPNAME=deedkit-copy-<id>-<step> <container>`, where `<id>` is
`<sha12>-<name>-<UTC stamp>`:

1. `<copy root>/<id>/owner.json` (0600, fsynced, directory 0700) names the scratch container
   and the session prefix before anything starts.
2. A keeper `psql` session (`idle_in_transaction_session_timeout` and `statement_timeout` = the deadline)
   opens `REPEATABLE READ READ ONLY`, exports its snapshot, reads the database size and an exact
   `count(*)` of every table (ordinary and partitioned) outside the system schemas.
3. Free space: the copy filesystem needs 2 × the database size + the reserve, max(10 GiB, 10 % of that
   filesystem); the live database's data filesystem (the Source of the mount that holds the container's
   `PGDATA`) needs the same when it is the copy's, else the reserve alone: the copy writes no file there, but its
   snapshot keeps WAL and row versions vacuum cannot reuse yet, so both filesystems are read every 2 s for as long as
   the snapshot is held, its queries included (a path that cannot be read counts as the copy's). While a dump or the restore runs both are read every 2 s; below the reserve the step is
   cancelled.
4. At the snapshot: `pg_dump --schema-only` (the reference schema), `pg_dump -Fc` to `<database>.dump`,
   `pg_dumpall --roles-only --no-role-passwords` to `roles.sql` (files 0600, created with `O_NOFOLLOW`);
   the keeper commits.
5. A scratch PostgreSQL from the database container's own image ID: `--rm --network none --log-driver
   none --memory 1g --cpus 1 --pids-limit 256 --shm-size 256m`, label `deedkit-copy=<id>`, trust
   authentication (no network: only `docker exec` reaches it), data in `<id>/scratch`. Its bootstrap
   superuser has the live server's bootstrap name (the keeper reads `pg_roles` OID 10): `pg_dumpall` writes
   `GRANTED BY <bootstrap>` on the role memberships that role granted, PostgreSQL 16+ refuses such a grant
   from any other role without ADMIN OPTION (incident 11-06, `restore_failed`), and equal names keep the
   copy's grantors equal to the live ones. Roles (`roles.sql` without the bootstrap's own `CREATE ROLE`),
   then the database with the live owner, encoding and locale, then `pg_restore --exit-on-error
   --single-transaction`.
6. Every count and the schema-only dump (its random `\restrict` lines excepted) must equal the reference;
   when the text differs, the reference schema alone is restored into an empty scratch database
   (`deedkit_copy_reference`, dropped after) and the copy's dump must equal that one's, because parsing a
   printed expression does not always print it back the same (incident 11-06: a BETWEEN inside an AND
   reads back as one flat AND); then `copy.json` (`verified`, counts, file hashes and sizes, snapshot time, server version).

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

Undo: none needed; a copy changes no database. To remove a copy at once: `rm -r <copy root>/<id>` after its
`released.json` exists. Undo of `copy_root`: remove the key from the plan A file (its backup beside it); the next
copy goes to `<state>/migration-copies` again, and `copy-roots.json` keeps the volume's root tended until its last
copy expires (48 h), then forgets it. Keep the volume mounted until then.

### Unattended migrations

ANYM-449, owner 2026-10-28: «Да, добавление миграции автоматически с копией.» `deploy` applies a messaging
range itself only when every step below passes; anything else pauses with today's text and you migrate by
hand (above). Nothing is retried, a migration is never reversed and nothing is restored automatically (a
restore is the gated restore procedure, from the copy or the backup).

Configuration, in the plan A file's `migration` section (`deploy/production.example.json`), besides
`databases.messaging` (Database copies): `unattended` = `{network, dsn_file, runner_config}` (the
database's Docker network, the owner DSN file the migration container mounts read-only, readable by the
image user 10001, and the backup runner's `runner.json`), `receiver_schema_max` (the newest schema the
backup receiver reads, kept for the operator's own record; no release reads or waits on it since owner 11-04:
a receiver behind the target is a backup follow-up) and `rehearsal_limit_ms` (default 1000, at most 4000). Undo: remove `unattended` (or
`databases.messaging`) and every SQL change pauses again.

What qualifies (M1): every SQL inventory path of the range is an added
`services/messaging/messaging_service/migrations/NNN_*.sql` or a modified messaging `*_role.sql`,
or a calendar path of "Calendar" below (`schema.sql`, `deploy/*.sql`, other calendar SQL, a new, deleted or
renamed role file and an edited applied migration pause); the candidate's `migration_rollout.py`, in the classifier's isolated process, names each
added file `expand` by `unattended_class` (ANYM-743: a rollout header, `risk_class` non-critical, no
`CONCURRENTLY`, no name over 63 bytes; the CHECK widening pair qualifies, a critical file pauses) and each
role file `additive` by `role_class`; the added versions follow the base's highest without a gap.

Phases, each a pause or a wait before any production write:

1. Admission (read-only): no active or unknown call (one poll; else `waiting` with `call_waits`); nothing
   unfinished (`unresolved`); the base release's `store.py` opens its pool with
   `prepare_threshold: None` (#352; else "deploy #352 first"); the live ledger is exactly the base's (`schema_version` and every
   `archive_migrations` checksum; else a pause).
   Migrated by hand (2026-11-09: the 075 copy refused, 075 was migrated by hand, and the next run would copy and
   pause again): a messaging-only range that changes no role file, whose live ledger is exactly the target's
   (the base's checksums and the range's own), copies, rehearses and migrates nothing. Its producer check (phase
   2) still runs, then plan A and plan B; no `migration.json` is written. A run that finds an earlier run's
   `refused` or `not-applied` `migration.json` of that release (a refusal before any write, or a migrate-expand
   whose ledger and catalog were found unchanged) reads the ledger once more and goes on the same way. Any other
   ledger, a role file change (the ledger does not show grants), a range that also touches calendar, and any
   release whose `migration.json` ended `uncertain` or `diverged` (or cannot be read) with no plan A attempt keep
   the pause: settle that release first (apply it by hand, or move its receipt aside with a note). So after "Migration releases" item 4 step 3's `migrate` of such a range, removing the pause lets the
   next `deploy` apply and promote it. `--dry-run` reads no database and still names the migration.
2. Preparation: plan A `prepare` as always, then the producer check: the image `runner.json` pins has an
   `org.opencontainers.image.revision` whose `backup.py` `SCHEMAS` holds the target. When it does not, the
   pin follows (ANYM-589 B1, under the runner lock the deploy holds): the candidate's `backup` image,
   pulled by its attested digest as `deedkit-production-backup:<sha>` (no Compose service selects it, so
   `prepare` does not; its ID and revision label are read back), must have a `backup.py` whose `SCHEMAS`
   holds the base and the target; the installed runner must be the candidate's (ANYM-488, "Backup
   runner"; else a pause before any write); `personal-os-backup.service` and
   `deedkit-calendar-backup.service` must read `inactive` or `failed`. Then, every path opened below one
   descriptor of `runner.json`'s directory, `runner.json.before-<sha>` is written (the old file's bytes,
   owner and mode), a new file with only `image` changed is written beside it, `runner.json` is read again
   (bytes, inode, size, times, owner, mode), `<sha>/producer-pin.json` records `moving` with the old and
   new image, one rename replaces `runner.json`, and the receipt records `moved` (a run that stopped
   before that records it when it finds the new pin). The check then runs again. Any condition unmet or
   unreadable: `waiting`, nothing written. A `runner.json` changed while it moves pauses (the change stays);
   a `.before-<sha>` file with other bytes pauses. Edit `runner.json` by hand only under the runner lock
   ("Backup runner"). A rollback keeps the new pin (the candidate's backup code reads the base and the
   target). Undo, only while the old image still reads the live schema, that is before the migration
   applied (`<sha>/migration.json` absent, `rehearsed`, `refused` or `not-applied`): under the runner
   lock with the backup services idle, `cp -p runner.json.before-<sha> runner.json`. After the migration
   the old pin would fail every capture (`unsupported_backup_schema`): keep the new one. The calendar
   `runner.json` keeps its own pin; move it by hand as in "Backup runner". Waits on the producer count in
   `deploy.json` (`migration_waits`); the third in a row pauses.
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
   foreign servers, extended statistics and text search) before and after may not lose stored data or
   break the running release's writes (`destroys_data`, ANYM-743): no removed table, partitioned table,
   materialized view or column (a rename reads as one and pauses for a hand release), no column whose type
   or generation expression changed, no new NOT NULL column without a default on an existing table; every
   other change (indexes, constraints, functions, triggers, views, policies, grants, defaults, roles)
   passes; `probe_metadata` only by `schema_version`;
   `archive_migrations` only by the pending versions; and no other existing table may show a write in
   `pg_stat_user_tables` or a changed exact row count (a TRUNCATE moves no write counter; ANYM-743) or column number (a column dropped and added again). Its transaction takes at most
   `rehearsal_limit_ms`: `migrate-expand` times it on a monotonic clock from before its first pending statement to
   its commit's return (every lock a migration statement takes, its wait included, until its release; the connection,
   session setup and ledger reads before it hold only ACCESS SHARE on the ledger and catalogs and do not count since
   fix-rehearsal-timer-11-09: 068's 764 ms left only 236 ms of margin on the loaded host) and prints `{"transaction_ms": N}` on the line before its answer (a reader
   of the last line alone ignores it); a rehearsal without that line pauses. The container's exit and the reads after
   it do not count: until 11-09 the window ran to the first read after the container ended, and that part was most of
   it (068's 40 ms index read 1100 to 4681 ms on the loaded host). Else the run pauses "not unattended: <first difference>" or "too slow
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
The messaging API, gateway, calendar API and recordings API define Compose liveness healthchecks
(their unauthenticated health routes, with an allowed Host, on loopback; the recordings API has no health
route, so its probe sends one unauthenticated POST and requires the 401 refusal, contacting no vendor), so
`--wait` also requires them to report healthy; the first probe runs about 30
seconds after start. Collectors, workers, indexer and enrichment expose no
container-local health signal, so `--wait` passes them as soon as they run once
and a crash-looping one can be caught running. Apply therefore settles them
(ANYM-758): once per project, after its replacements started, it waits
`APPLY_SETTLE_SECONDS` (10, a constant of the tool) and inspects each replacement
without a healthcheck again. One that is restarting, not running, or has
restarted since this apply created it fails apply exactly like a failed `--wait`:
the release is recorded `unhealthy` with rollback data and `deploy` rolls it back
once and pauses. A release adds at most 10 seconds per project; services with a
healthcheck rely on `--wait` and add none. Docker does not restart an unhealthy
container; monitor `docker ps`/`health_status` events.

### Only changed services are recreated (ANYM-895)

Builds are not reproducible, so image IDs differ on every release. `prepare` therefore computes an
input digest per selected service from what the repository and host control: the full content of the
target's Dockerfile and its context's `.dockerignore`; the stage and build arguments without
`DEEDKIT_SOURCE_SHA`/`VERSION_REF`; the git objects (with their modes) at the release commit of every
context path the target's stage chain copies (`COPY --from`, `RUN --mount=from=` and base stages
included; `<Dockerfile>.dockerignore` too); for an
image that copies `services/messaging/messaging_service`, its migrations as a part of their own; the
rendered Compose service without `image`, `build` and the revision and input labels; and sha256 digests
of its env files, bind-mounted files and the top-level secrets and configs it uses (definition and
file). A directory mounted read-only is configuration and is hashed by content (each entry's path and
mode, file content, link target; at most 2000 entries and 64 MiB, beyond that the service is always
recreated); a writable mounted directory (sessions, spools, data) counts by path only. A version constant (`INPUT_VERSION`) is part of the digest, so a
change of the rule recreates every service once. A recreated service's container carries the digest as
the label `io.deedkit.production.inputs`.

A service is **kept** only when its running container (running, not restarting or paused) carries the
candidate's digest as that label and the current applied release (`current.json`) recorded that very
container, image and `container_settings` digest with the same digest. Anything else recreates it, as
before: no label (every container created before ANYM-895, so the first release recreates everything),
another container (one created by hand with a copied label), a settings drift, a part the digest cannot
read (an `ONBUILD`, an `ADD` of a URL or an archive, a `RUN --mount=type=bind` of the context, a Dockerfile
the parser cannot read: never kept), or a `current.json` that is not an applied release (after a rollback
the next release recreates everything). Apply hashes the host files again right before the first stop and
refuses with `inputs_changed_since_prepare` when they changed or a kept container no longer runs steadily
with its label; then prepare again. Kept services are left out of both `stop` and `up`; the call wait
still runs before any stop. After the last project the tool checks that each kept service still runs the
same container with the same settings, like a recreated one. Rollback stops and restores only the services
the release recreated; what it kept keeps running, and image retention keeps its image (`kept_images` in
the rollback receipt). `status` expects a kept service on the revision its digest names. Unpinned
packages a Dockerfile installs (apt) arrive only with a service's next recreate.

Receipt fields: `prepared.json` projects carry `inputs` (`{service: {digest, parts}}`), `kept`
(`{service: {container, image, revision}}`) and `recreated` (`{service: [changed parts]}`, parts from
`dockerfile recipe code migration compose env mounts secrets`, or `unlabeled`/`unreadable`). `result.json`
adds `services` (`{project: {service: {"recreated": [...]} | {"kept": {container, revision}}}}`) and
`boot_id` (the host's boot ID, so a reboot is told from a recreate); its rollback projects add `inputs`
and `kept` (`{service: revision}`). Digests only, never a value.

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

### Session claim guards (ANYM-914)

A release that runs the session services (the collectors and pairing commands, schema 78) gets four refusals from
`production.py`; each surfaces as the existing `plan_a_refused` halt with its code in the reason:

- `session_locks_unmounted`: the release carries `session_claim.py` and a session service (a `-session` bind) in the
  host's Compose files does not bind `/run/deedkit/session-locks` or stops with a `stop_grace_period` under 20 s.
  Refused in `prepare` and again in `apply`, before anything stops. A release never edits the host's Compose files;
  install the change first (First claim release below).
- `session_locks_unsafe`: the host directory mounted at `/run/deedkit/session-locks` (`deploy/private/session-locks`)
  is created once, 0700 for uid 10001, by `prepare` and checked again by `apply`; an existing one that is a symlink, not
  a directory, group- or other-accessible or of another owner is refused, never repaired. Fix it by hand
  (`chown 10001:10001`, `chmod 700`); never copy it from another host.
- `foreign_session_opener`: before a release restarts a service with a `-session` bind, no running, paused or
  restarting container other than that directory's own Compose service may mount the session directory or a mount
  source (bind or named volume) holding a session store (`*.session`, `auth.sqlite`, a browser profile). The configured
  services' own mounts are searched two levels down; every other container's mounts four levels down, outside the
  configured services' data paths. A foreign mount with more than 20000 entries in those levels refuses too (its
  ownership cannot be established): read it, and when it holds no session store list its host path in
  `session_scan_exempt` (production settings, a list of absolute paths). Stop the one-off or foreign container first.
- `pre_claim_rollback_refused`: once a release ran claims healthy (`state/session-claims.json`), `rollback`, `deploy`
  and a by-hand `apply` to a tree without `session_claim.py` need `--pre-claim-rollback "<reason>"`, and only while one
  host runs sessions (an older tree takes no claim, so a second host could open the same session).

An unreadable container listing or claims record refuses too, so it halts deploys until the agent clears the cause.

First claim release (hand route). The first release carrying `session_claim.py` cannot run unattended: its range
changes the tool pair and adds migration 078 (`migration_by_hand`), and the host's Compose files need the lock mount.
The agent who releases it, in this order:

1. Read-only transition inventory on the production host and the Mac mini, receipt in the release record: every
   container and host process that holds or mounts a session store, and every autostart (restart policies, systemd
   units, cron, launchd) that could start one, is exactly the configured session services. PR #806 holds the first
   receipt; repeat it right before the release.
2. Install the host Compose change, with a copy of each file for undo: the bind
   `./private/session-locks:/run/deedkit/session-locks` on `telegram`, `whatsapp1` and `whatsapp2` (as
   `deploy/compose.collectors.yaml` and `deploy/compose.whatsapp.yaml`), and `stop_grace_period: 25s` on `telegram`.
   The directory itself is created by `prepare`.
3. Install the tool pair of the release SHA ("Deploy tool updates"), then release the migration by the hand route of
   "Migration releases and the backup gate", item 4. The backup receiver reads schema 78 only after its own update; that
   is a backup follow-up, never a release hold.
4. After `applied`: `session_owner` on the full sync status shows generation 1 or more for every session service.

Undo: before step 3, restore the copied Compose files. After it, a rollback to a pre-claim tree needs
`--pre-claim-rollback "<reason>"` (above); migration 078 stays (expand only).

### Session holder role (hand release, ANYM-936)

Migration 082 and `session_holder_role.sql` give the session holders (ANYM-937 WhatsApp, ANYM-938 Telegram) their
database access: a NOLOGIN group `session_holder` with SELECT, INSERT and UPDATE on `session_claims`,
SELECT(account, owner, epoch, expires_at) on `collector_leases` and SELECT(account, status) on `source_status`, and
row security on those three tables (one policy, `session_holder_scope`). A holder login sees and writes only the rows of
the account `session_holder_accounts` binds it to. An unbound login, or `SET ROLE session_holder`, sees no row. Every
other role keeps exactly its current rows, and the table owner (migrations, `pg_dump`, owner functions) bypasses row
security. Risk tier: non-critical (no row is deleted or rewritten). The new role file changes the role-file set, so
`deploy` pauses the range ("the SQL inventory changed"), and this hand release ships it:

1. Read-only: no active call, and `production.py status` shows no unfinished operation.
2. Release the migration by the hand route of "Migration releases and the backup gate", item 4 (timers stopped,
   prepare, runner lock, migrate, apply and promote, timers started). Before the next step, read back
   `schema_version` 82 and the three `session_holder_scope` policies in `pg_policies`.
3. Apply the role file as the database owner: `psql -v ON_ERROR_STOP=1 -f session_holder_role.sql` from the
   release's source, inside the database container. Read back the grants of `session_holder` with
   `\dp session_claims collector_leases source_status`, and the PUBLIC read (`=r/`) on `\dp session_holder_accounts`.
4. Only when a holder service is about to be configured (ANYM-937's cutover plan): for each holder account, create
   one login, `session_holder_<n>` (letters, digits, `_`), with `CREATE ROLE ... LOGIN NOSUPERUSER NOBYPASSRLS IN ROLE
   session_holder PASSWORD '<random 32 bytes, hex>'` and bind it in the same transaction with `INSERT INTO
   session_holder_accounts(role_name,account) VALUES(...) ON CONFLICT (account) DO UPDATE SET
   role_name=excluded.role_name, created_at=clock_timestamp()`: an account's earlier binding (a replaced login, or
   one a restore brought back) moves to the new login in one step. Write its DSN file in the host's private directory
   (`deploy/private/<service>-holder.dsn`, mode 0600, owner uid 10001). The password goes through a private operator
   connection, never a command line kept in shell history, never Git or an image. Read back: as that login, `SELECT
   account FROM session_claims` returns only its account.
5. Receipt in the release record: the schema, the policies, the grants, each login's name and binding, never a
   password.

Undo: step 4: `DELETE FROM session_holder_accounts WHERE role_name=...`, then `DROP ROLE` the login and delete its
DSN file. Step 3: `REVOKE ALL ON session_claims, collector_leases, source_status FROM session_holder`. Step 2:
migration 082 stays. Its policies pass every non-holder row, so a rollback to a schema-81 runtime is refused only by
the runtime's own `database_newer_than_runtime`, as for any newer schema. A restore recreates `session_holder` as a
fixed NOLOGIN role (`backup_restore_worker.RESTORE_ROLES`). A `pg_restore --no-acl` keeps the policies and drops
every grant, the PUBLIC read of `session_holder_accounts` they need included: until `session_holder_role.sql` is
applied again (with the other role files, before any restricted service connects), every non-owner caller of the three
tables gets `permission denied for table session_holder_accounts`. The logins are never restored, but their
bindings are (backup inventory): such a binding names a login the fresh cluster does not have and passes no row,
and step 4's binding statement moves it to the restored host's new login.

### Session holders (ANYM-896)

A session holder (`whatsapp-holder`, later `telegram-holder`: a target that copies no messaging_service code) holds a
provider connection, so recreating it is a provider reconnect. It is marked in the configuration's `services` as an
object, `"whatsapp1-holder": {"target": "whatsapp-holder", "session": true, "collector": "whatsapp1"}`; `config()`
refuses the mark on any other target (`session_target_unsupported`), unknown keys and a missing or shared collector
(`session_mark_invalid`). Global keys: `session_window` (default `03:00-05:00`) and `session_timezone` (default
`Asia/Makassar`, WITA: the owner's 2026-10-17 «Да, ночью по Бали»); neither is part of the prepared configuration
digest. Order: add a mark only once the installed tool carries this support (an older tool refuses the object
entry, `TypeError`, on every command); before a `tool-rollback` to such a tool, remove the marks first.

- Deferral. When a holder's ANYM-895 decision is a changed digest, `apply` (also inside `deploy`) recreates it only
  when the recreate begins inside the window and the holder had no planned recreate earlier in that window. Otherwise
  the running container stays as a kept service on the inputs it carries, and the receipt's `services` entry says
  `deferred (<parts>, next window <UTC>, <local> WITA)`. The decision is taken after the call waits, right before
  the first stop. Precedence: a holder that is stopped, restarting, paused, unhealthy, `unlabeled` or `unreadable` is
  recreated at once (kind `recovery` with that reason). A migration changes no holder: a holder copies none.
- Collectors are never kept back: a collector that changed goes in its release. A holder and its collector that both
  go form a pair: the collector stops first, the holder comes up and passes its check, then the collector comes up and
  must attach (the holder's `holder.attached` log event within 120 s). A collector alone needs its new image's
  protocol range (`io.deedkit.collector.protocol.{min,max}`) to include the running holder's protocol (the maximum of
  `io.deedkit.holder.protocol.{min,max}`), a holder alone the reverse; a pair, each the other's. A recreate that leaves
  no shared protocol is refused before any stop (`session_pair_incompatible`). A changed collector that cannot speak to
  the running holder takes the holder with it at once (`recovery`, `collector_requires_holder`). A failure in the pair
  records the release `unhealthy`; its rollback restores both, holder first (`deploy` does it at once).
- Night pass. A `deploy --auto` run that finds nothing new to deploy, when a deferred change is recorded, runs one
  session-only pass a night from its per-night start (inside 03:30-04:30, derived from the date) until the window
  ends. It uses the current applied release, recomputes every digest (a change a rollback undid is `nothing
  pending`) and writes `<state>/<sha>/session-<time>/attempted.json` as `started` before its call wait; a crash
  leaves that receipt without `result.json`, which every later deploy refuses as unfinished. A call wait that reaches
  the window's end stops nothing: `deferred (window closed during call wait)`. Before it stops a holder it writes
  `.planned-stop` (`{reason: session_window, planned_end}`, 0600, the collectors' owner) in the holder's socket
  directory and removes it after the holder is back, so the collector reports `provider_disconnected` with reason
  `session_window`. A holder (or pair) failing its check is restored inside the pass, holder first (no
  `guard_rollback` for the holder, which carries no migrations; the collector keeps it): `rolled_back` with the
  failed check, the change stays pending. An exception after `started` halts autodeploy with `session_pass_failed`.
- Urgent flag. `production.py deploy --session-restart-now <holder>[,<holder>...] --reason "incident:<runbook
  record>"` (the incident recovery override, AGENTS.md section 2, docs/operations/incident-recovery.md) or `--reason
  "owner:<the owner's line, quoted>"` recreates the named holders at once in a session-only pass (`--dry-run` lists
  them). Anything else, or a name that is no configured session service, is refused before any stop
  (`session_flag_invalid`). Each one is a `flagged` recreate with its reference.
- Visibility. `status` and the deploy's stdout list `session_pending`: the holder, the changed parts, the release, the
  next window in UTC and WITA, and `session_deferral_stale` once two windows passed without it. The ledger
  `<state>/session-ledger.json` keeps the recreates by kind (`planned`, `recovery`, `flagged`, `rollback`) and window,
  the pending changes and each window's pass; each pass's `result.json` has its `counts`. After a pass, `current.json`
  and the release's `result.json` name the new container, so the next release and `rollback` read what runs.

### Waiting for a running backup (ANYM-744)

`apply`, the standalone `seamless_release.py promote` and a manual `deploy` (not `--auto`, not `--dry-run`) wait up to
`backup.wait_seconds` for a running backup, then hold the runner lock (`.runner.lock`, `DEEDKIT_BACKUP_LOCK`) for
their own run, so no kit waits for a running backup by hand. Idle is positive: the lock is free, each of
`personal-os-backup.service`, `deedkit-calendar-backup.service` and `deedkit-calendar-backup-catchup.service` reads
`inactive` or `failed` (`deactivating`, `activating`, `active`, `reloading` and an unreadable state are busy) and the
`.unsettled` marker is absent, read again with the lock held. The tool polls every 15 s and holds no lock while it
sleeps; the deadline is checked after every probe and before admission, so the bound is overshot by at most one
`systemctl` probe, and a held lock refuses before any probe. The backup wait runs before the call wait, and the
lock stays held through it. `deploy` takes the lock once and its inner `apply` and `promote` neither wait nor take it;
`deploy --auto` is unchanged (the unit's `flock --nonblock` holds the lock and exits 75 at once; it has no
clock window, owner 11-04). `prepare`, `verify`, `status`, `images`, `--dry-run` and `release-plan` never wait.

`backup.wait_seconds` is an optional integer from 0 to 7200 (default 2700; 7200 is the runner's own
`busy_wait_seconds` maximum, BACKUP.md) in the plan A configuration, outside the configuration digest like `migration`;
any other value refuses the configuration before any write. 0 keeps the single non-waiting check. The standalone
`promote` reads the HTTP configuration, whose digest binds prepared releases, so its bound is the flag
`--backup-wait SECONDS` (same range and default) and that configuration stays unchanged. The wait prints
`backup check: waiting for ...` and `backup check: idle after N s` on stderr; a release that waited records
`backup_wait_seconds` in `result.json` (`apply`), `deploy.json` (`deploy`) or `http-result.json` and
`http-current.json` (standalone `promote`, which also prints it).

Past the bound the command ends with exit 75 and prints one JSON line,
`{"status":"busy","code":"backup_running","unit":"<unit, lock or marker>","waited_seconds":N,"since":"<start, if known>"}`
(a deploy adds `reason`), and nothing is written: no `attempted.json`, no `http-attempted.json`, no pause. Exit 75
`backup_running` means try again later. It never runs without the lock: a restart in the middle of a capture loses
the capture (BACKUP.md "stop recovery"). A stale `.unsettled` marker makes every release wait and then refuse naming
the marker; settle it with `--recover-stop`. A SIGTERM during the wait takes and writes nothing.

A release has no clock window (owner 11-04: «давай все делать не ждать ничего»), so it may run into a scheduled
backup start: the wait does not protect a scheduled start. A scheduled backup that starts while a release holds the lock
is refused, and what follows differs by unit (see "Continuous deployment"); a daily lost that way is a backup follow-up,
never a release hold. The autodeploy unit has no clock window either ("Timer").

### Lifecycle workers (ANYM-525)

`file-lifecycle` (target `runtime`), `cold-archive` and `cold-restore` (target `backup`) are one ordinary plan A project,
`deedkit-file-lifecycle` (`deploy/production.example.json`): its Compose file, its name and per service its target, with no
mechanism of their own. The tool loads every profile (`--profile *`) and selects exact service names, so the database stays
out. Every release and rollback stops them and starts them on the release's verified images. `production.py status` lists
each service of each configured project as `project_services` with its `state` and `revision` label; `mark` names one that
is `not running`, `differs from current.json` (not compared after a rollback, when `current.json` names the release that
was left) or `unknown` (several containers, or with `error` the read failed or the 60 seconds `status` gives all these
reads passed; `status` still answers).

What that means for them:

- A stop gives a pass Compose's 60 seconds (`stop_grace_period`), then kills it. A pass only checks for the stop between
  passes, so a long one is killed. Tests kill each worker by SIGKILL at every durable boundary (`test_a_killed_*`): the next
  pass finishes the work and repeats nothing irreversible. An eviction replays its journal, one unlink, one intent. An
  archive pass reads back the object it finds, one write under the key, and no eviction counts on it before the read-back. A
  restore registers what it published without another read. A killed restore leaves a `.pending-*` file, which a collector's
  start removes after a day (`sweep_abandoned`).
- A start runs a pass at once (the interval wait follows it), so `--limit`, `--max-bytes`, `pass_files` and `pass_bytes` bound
  a pass, now several a day with releases, not a day. `total_bytes` still bounds the archive. A pass makes more read-backs,
  never more objects than one per original.
- While `cold-restore` is stopped no worker holds its presence, so a new request goes to the provider path at once (ANYM-350).
- They have no healthcheck, so `apply` checks them as it checks other workers (`up --wait`, then the settle): one that exits
  or restarts fails the whole release's health wait and rolls it back.
- Eviction stays the owner's opt-in: `--apply` stays out of the Compose commands until the owner adds it. Plan A changes
  images and labels, never commands, mounts or credentials.

The host change is no part of a merge. It is a production operation plan (one Astra review), run outside a cold-archive pass
(`docker logs --since 1h deedkit-file-lifecycle-cold-archive-1`), as a hand release like "Enrollment is a hand release",
because the configuration digest changes and autodeploy otherwise halts with `configuration_drift`:

1. Pause autodeploy and wait until `deedkit-autodeploy.service` is not active. Take a green `main` SHA `S` holding this code,
   newer than the deployed one and not prepared yet.
2. Back up both files with `cp -p`: `/etc/deedkit/file-lifecycle.json` and the plan A configuration that
   `production.active_config()` names.
3. Bring the three workers to the serving release first, by hand. `guard_rollback` refuses to restore a messaging image with
   fewer migrations than plan B serves, and `runtime` and `backup` are both messaging images: without this step any rollback of
   the first release is refused, the automatic one included, because the images it would restore predate the serving schema.
   Set `image` of `file-lifecycle` to `current.json`'s runtime image, and of `cold-archive` and `cold-restore` to the current
   release's attested backup image (verify and pull it as in "First start by hand", step 1), then `docker compose -p
   deedkit-file-lifecycle -f /etc/deedkit/file-lifecycle.json up -d --no-build --pull never --no-deps file-lifecycle cold-archive
   cold-restore`. Read back the three images and each worker's next pass in its log.
4. Move the Compose file, byte for byte, to `/etc/deedkit/file-lifecycle/compose.json` (`sha256sum` after the move equals the
   one before it): besides step 3's images, its commands, mounts, credentials and the eviction and upload opt-ins stay exactly.
   Image retention reads only top-level JSON files of `/etc/deedkit`, so the image IDs in it stop pinning old images.
5. Append the entry of `deploy/production.example.json` to `projects`. `directory` stays the project's working directory
   (label `com.docker.compose.project.working_dir` of its containers).
6. `production.py prepare --sha S`, inspect it, then `apply --sha S` and `status`. Read back: `project_services` shows the three
   services on `S` with no `mark`. Remove the pause.

Undo of step 6: `production.py rollback --sha S`, which restores step 3's images; then put the plan A configuration back from
its copy and remove the pause. Undo of step 3: put `/etc/deedkit/file-lifecycle.json` back from its copy and run the same `up`.
Rollback's previous images stay kept as its target (`<project>.before.json`).

### Backup runner

The host backup runner (`/opt/personal-os-probe/ops/backup_runner.py`, the
`personal-os-backup.service` program, BACKUP.md) is neither an image nor a deploy
tool. `deploy` (manual and `--auto`) installs it itself (owner 11-04: «давай все делать не ждать ничего»):
after `prepare` and before any migration or apply, when the file exists and its git blob is not
the candidate's `services/messaging/messaging_service/backup_runner.py`, it does the script below
under the runner lock it already holds, for every `runner.json` that exists (messaging's, the
calendar's and the recordings', all pinning messaging's image): the backup units idle and the
candidate's attested backup image pulled (else the run waits like a migration wait; the third
wait in a row pauses), that image's revision label the candidate, the new runner's `--check` exit 0
on each `runner.json` as it will be, and, last, no active call (else a call wait); then a
`.before-<sha>` copy and one rename per file (each `runner.json`, then the runner; owner and mode
kept), the runner read back by its blob, receipt `<state>/<sha>/backup-runner.json`. A failed
check, a wait or a changed file leaves the host as it was. A stop between renames pauses with
the receipt at `installing`; every `runner.json` moves before the runner, so the old runner may
run with the new image but never the reverse, and once the pause is removed the next run finishes
from the `.before-<sha>` copies. A manual
`apply` still refuses before any stop while the file is not the release's (the refusal names both
blob ids): install it with the script below. A host without the file runs no backup and is not
checked.
A release whose `services/messaging/messaging_service/backup_profiles.py` exists (W5-9: the recordings and calls
profiles in a module beside the runner) ships that file too, as `/opt/personal-os-probe/ops/backup_profiles.py`
(root `0600`, like the runner): `deploy` stages it beside the runner, the staged runner's `--check` loads the staged
module, and it is renamed before every `runner.json` and the runner (a first install writes it new, with no
`.before-<sha>` copy: its undo removes it); `apply` refuses while it is not the release's either. The runner loads
it from its own directory, so a runner of such a release never runs without it; an older runner never reads it.
The install is one step under the runner's own lock, so no messaging or calendar backup and no
deploy can start between the check and the hash (a backup started meanwhile refuses
`backup_job_already_running`, then follows its unit's retry behaviour under "Continuous deployment"; a deploy
waits or exits 75), so it needs no clock window (owner 11-04). No active call. The new runner's read-only `--check` must exit 0 against
messaging's `runner.json`: exit 3 means an unfinished journal, whose recovery stays under the
existing rules (`--recover-stop` restarts the collectors it stopped), and the step stops. Any
failed command stops it (`sh -e`). From the prepared release: when the backup worker changed
in the same range since the pinned image (`org.opencontainers.image.revision` of
`runner.json`'s `image`), first set the backup image pin in `runner.json` to the release's
backup image and check it, then install the runner, both in the script below (`pin=yes`): a
newer runner can write a `worker.json` key an older worker refuses (BACKUP.md). An unattended migration
moves messaging's pin itself when the pinned image cannot read the target ("Unattended migrations",
phase 2); a pin it already moved reads as the release's, so the script below then runs with `pin=no`.

```sh
sha=<prepared-sha>; src=<state_directory>/$sha/source; pin=yes   # state_directory: /etc/deedkit/production.json; pin=no: the worker did not change
sudo flock --nonblock /opt/personal-os-probe/backups/.runner.lock sh -euc '
  sha=$1 src=$2 pin=$3 ops=/opt/personal-os-probe/ops cfg=/opt/personal-os-probe/backup-config/runner.json
  new=$src/services/messaging/messaging_service/backup_runner.py mod=$src/services/messaging/messaging_service/backup_profiles.py
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
  if [ -e "$mod" ]; then
    if [ -e "$ops/backup_profiles.py" ]; then cp -p "$ops/backup_profiles.py" "$ops/backup_profiles.py.before-$sha"; fi
    install -o root -g root -m 0600 "$mod" "$ops/backup_profiles.py"
    test "$(git hash-object "$ops/backup_profiles.py")" = "$(git hash-object "$mod")"
  fi
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
this runner's refusal (`the backup runner ... is blob`, from a manual run or a tool older than
the automatic install); a pause for another reason stays.
The step is done when the next messaging backup reaches `state: ok` with a new
`last_success_id` (`/opt/personal-os-probe/backups/status.json`). Undo, under the same `flock`
and with the backup service idle, in this order: remove from `runner.json` every key the
`.before-<sha>` runner does not know (for example `busy_wait_seconds`), install the
`.before-<sha>` copy the same way, then the `backup_profiles.py.before-<sha>` copy (none: remove
`backup_profiles.py`, which no older runner reads), and only then put back the image pin it ran with
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

Calendar backup freshness and same-night catch-up (ANYM-694, BACKUP.md "Daily run and freshness"): a hand install
under the section 2 gates (units and a script, no schema, no data). The script is outside every file autodeploy
compares (it compares only the runner, `BACKUP_RUNNER`), and `production.py` installs itself with the release, so
nothing here is sequenced with a release. Steps, as root: install `deploy/calendar-backup-freshness.py` as
`/opt/deedkit/calendar-backup/freshness.py` (root, `0755`); copy the four files
`deploy/deedkit-calendar-backup-fresh.service`, `-fresh.timer`, `-catchup.service` and `-catchup.timer` to
`/etc/systemd/system/` (`0644`); `systemctl daemon-reload`; `systemctl enable --now
deedkit-calendar-backup-fresh.timer deedkit-calendar-backup-catchup.timer`. `freshness.json` is mode `0644`, but
`/opt/deedkit/calendar-backup/` and its `backups/` stay root `0700` (the runner refuses a job directory with any
group or other permission bit, `private_backup_directory_required`, so never loosen them or add an ACL there). Run
`production.py status` as root, as `deploy` does; another user sees `calendar backup: freshness file unreadable
(PermissionError)`. Not during an active call. Read back: `systemctl start deedkit-calendar-backup-fresh.service` succeeds and `freshness.json`
says `fresh`; `production.py status` prints `calendar backup: fresh age=...`; `systemctl list-timers
'deedkit-calendar-backup-*'` lists both new timers; the first 19:15 fire is skipped by its condition (journal) and
no second snapshot exists that day. Undo: `systemctl disable --now deedkit-calendar-backup-catchup.timer
deedkit-calendar-backup-fresh.timer`, remove the four unit files and `freshness.py`, `daemon-reload`; the daily
service and timer are never touched.

Backup alerts (ANYM-807, BACKUP.md "Backup alerts"): a hand install on both hosts under the section 2 gates (units, a
script and a configuration; no schema, no data, no backup unit or runner configuration changes). Steps, as root, per
host: install `services/messaging/messaging_service/backup_health.py` from the main commit you name as
`/opt/deedkit/backup-health/backup_health.py` (root, `0755`); write `/etc/deedkit-backup-health/config.json` (root
`0600`) from `deploy/backup-health-production.example.json` on the production host or
`deploy/backup-health-backup1.example.json` on backup-1, with `release_sha` set to that commit; copy
`deploy/deedkit-backup-health.service` and `.timer` to `/etc/systemd/system/` (`0644`); `systemctl daemon-reload`;
`systemctl enable --now deedkit-backup-health.timer`. Production's `dsn_file` is the ingest DSN the API's telemetry
overlay already mounts, `/opt/personal-os-probe/private/tiden-runtime.dsn` (the `source` in the personal-os-probe
stack's resolved Compose file; no copy). On backup-1 the same DSN is copied to `/etc/deedkit-backup-health/tiden.dsn` (root
`0600`) over the tailnet without printing it; read back by comparing the sha256 of both files. The agents'
secret-read hook refuses that copy (11-08), so the owner runs it. Read back:
`systemctl start deedkit-backup-health.service` exits 0, `production.py status` prints the five `backup` lines,
`--print` on backup-1 prints four. The copy does not follow later releases: reinstall it when `backup_health.py`
changes (ci-deploy-speed 11-09: the production copy also raises `deploy_halted.<code>` for an autodeploy halt, so an
agent sees a pause within the hour; reinstall on the production host only, then read back with one
`systemctl start deedkit-backup-health.service` and its `due` line; undo: the previous copy, kept beside it as
`backup_health.py.before-<sha>`). Undo per host: `systemctl disable --now deedkit-backup-health.timer`; remove the two unit files,
`/opt/deedkit/backup-health`, `/etc/deedkit-backup-health` (on backup-1 the DSN copy with it) and
`/var/lib/deedkit-backup-health`; `daemon-reload`. No backup is touched, so nothing else is undone.

### Module event router (ANYM-575)

The router (`deploy/compose.event-router.yaml`, SUBSCRIPTIONS.md "Module event router") is not
enrolled by any release: nothing runs until these steps. It needs schema 58 and a producer
endpoint (calls ANYM-563, calendar ANYM-565) deployed. Access steps: the two module read
tokens (secrets and access) and the link grants (ANYM-564). The agent runs each step under AGENTS.md section 2, the
gateway profile switch (step 5) under the `profile tools` rule ("Write subcommands"):
1. Module read tokens: one per module, issued by the module for the router's audience, stored
   as `private/event-router-tokens/calls` and `.../calendar` (directory 0700, files 0600, uid
   10001). The repository and the config name only the paths, never a value.
2. Role and login: install `module_router_role.sql` as owner (schema 58 grants included), create
   `messaging_module_router_runtime` (`LOGIN IN ROLE messaging_module_router`), DSN in
   `private/event-router.dsn` (0600). Undo: stop the service, `DROP ROLE <login>`, remove the file.
3. Config: `private/event-router.json` from `deploy/event-router.example.json` (0600); list only
   modules whose endpoint is deployed. Optional: `python -m messaging_service set-module-cursor
   --source <s> --after <n>` before the first start to read from an earlier seq than the head.
4. Enrollment as step 5 of the subscription enrollment above, with `compose.event-router.yaml`
   appended to the project's `files` and `"event-router": "runtime"` selected. The router has no
   healthcheck: it is checked as running; its state is `module-router-status` and
   `module_source_health`.
5. Link grants (ANYM-564) and the gateway profile switch, as above; without a
   link no subscription has a module source and published events wake nobody.
6. Recordings source (ANYM-732; ships disabled, planned here and read back in ANYM-653's evidence). It needs schema 67 (hand
   release 067: SUBSCRIPTIONS.md "Recordings source"; `receiver_schema_max` and the backup and restore runtimes at 67 first) and
   the recordings feed (ANYM-726: `event_router` in the recordings configuration, its SHA-256 and expiry). Steps, none during an
   active call:
   a. Token: a random value in `private/event-router-tokens/recordings` (0600, uid 10001); its SHA-256 goes into
      `event_router.key_sha256` of the recordings configuration (an atomic replace inside the private directory; the service
      re-reads it). Never the value into Git, a log or a chat.
   b. Network, once, before the recordings container is deployed from `compose.recordings.yaml` of this change or the router is
      started with the new overlay: `docker network create --internal -o com.docker.network.bridge.name=dk-recrouter0 -o com.docker.network.bridge.gateway_mode_ipv4=isolated deedkit-recordings-router`.
      Internal, no host address, no route out, no IPv6; only `event-router` and `recordings-api` join it, and
      `recordings-egress` stays the container's one way out. Host firewall: ANYM-712's rules (`recordings_egress_check.py`)
      do not name this bridge yet (a card candidate); until they do, the isolation rests on Docker's internal network with the
      isolated gateway mode, and the router has no listener to reach.
   c. Enrollment: the recordings project gets the same compose file with the new network (a compatible service update, undo
      below); `recordings` is added to `private/event-router.json` (`http://recordings-api:13234`, token file
      `/run/secrets/module-tokens/recordings`) and the router restarted.
   d. Connectivity test (read-only): `docker compose $F exec -T event-router python -c "import urllib.request as u; print(u.urlopen(u.Request('http://recordings-api:13234/v1/events/head', headers={'Authorization': 'Bearer ' + open('/run/secrets/module-tokens/recordings').read().strip()}), timeout=5).status)"`
      prints 200. `docker network inspect deedkit-recordings-router` lists exactly the two containers, `Internal: true` and no
      gateway; from `recordings-api` a connection to the router's address on that network is refused (it has no listener).
      `module-router-status` shows the
      `deedkit:recordings` cursor at the feed's head after the first pass.
   Undo: remove `recordings` from `private/event-router.json` and restart the router. The network may stay (it joins nothing else).
   To remove it, first redeploy the recordings project and the router from files without the `recordings-router` entries (both
   compose files require it while they name it), then `docker network disconnect deedkit-recordings-router <container>` for any
   container still on it, then `docker network rm deedkit-recordings-router`.
   Rotate or revoke the token: replace the digest in the recordings configuration and the token file together (two digests allow a rotation).
Read-back: a synthetic module event published by a test producer appears in `module-router-status`
(cursor moved) and in `module_events`, only after the module read tokens exist. Undo: `docker
compose $F stop event-router`, remove it from `files`; cursors and gaps stay (schema 58 is
additive) and a later start resumes from the stored cursor.

### Negotiation worker (ANYM-908)

The negotiation worker (`negotiations` in `deploy/compose.subscriptions.yaml`, `python -m
messaging_service.negotiation_worker`) is the only writer of the ANYM-907 tables. It reads the journal and the
archive, never sends, calls a model or opens a provider, and no collector, send or hint path imports it. No
release starts it: it runs only once the steps below select it. A new role file and migration
`081_negotiation_heartbeat.sql` (manual: the `component_heartbeats` CHECK gains `negotiations`, and an RLS
policy limits the role to that row) pause autodeploy; release them by hand at once ("Migration releases" item
4). Neither deletes or rewrites user data (non-critical tier). None of it runs during an active call (check
read-only first).
1. Install the role file as owner: `negotiation_role.sql` (grants only, on existing objects; the role comes
   with 076). Undo: `REVOKE` each grant it lists (the file names them), or `DROP OWNED BY messaging_negotiations`
   after the worker is stopped (076's own table grants then need `store.migrate`'s re-apply).
2. Create the LOGIN and its DSN file: `CREATE ROLE messaging_negotiations_runtime LOGIN PASSWORD '<random>' IN ROLE
   messaging_negotiations` (member of that role only), DSN in `private/negotiations.dsn` (0600, owned by uid
   10001 like the other DSNs). Never the value into Git, a log or a chat. Undo: stop the service, `DROP ROLE
   messaging_negotiations_runtime`, remove the file.
3. Apply the heartbeat migration: the item 4 route (`prepare`, the runner lock wrapper, `migrate`), together with
   any other pending migration. Undo: none needed for a compatible older runtime (it writes no `negotiations`
   row); the CHECK and the policy stay.
4. Start the service, under the pause file and both tool locks (the global `/var/lib/deedkit-production/deploy.lock`
   and the state directory's `deploy.lock`) for each host edit. Prerequisite: the project's env file holds
   `ANYMSG_OWNER_TIMEZONE=<IANA zone>` (the owner's zone; the api reads the same line, compose.yaml). Check it
   first with the worker's own read-only `docker compose $F run --rm --no-deps -T negotiations python -m
   messaging_service.negotiation_worker --status`; `UTC_fallback` stops here. The production `files` are host
   copies (compose.subscriptions-live.yaml holds `subscriptions` only), so:
   1. Overlay: copy the `negotiations` block of `deploy/compose.subscriptions.yaml` at the current release into
      `/opt/personal-os-probe/compose.negotiations.yaml` (0600 root), its `image` pinned to that release's
      `runtime` image id, and append it to the messaging project's `files` in the active configuration (backup
      first). `docker compose $F config --hash '*'` of every other service reads the same before and after.
   2. First start by hand: `docker compose $F up -d --no-build --pull never --no-deps negotiations` (`$F`: the
      project's files and env files as `production.py` passes them). `prepare` refuses a selected service
      without exactly one running container, so the start comes before step 3.
   3. Add `"negotiations": "runtime"` to the project's `services` (backup first).
   4. Apply the next green `main` SHA by hand under the pause (`prepare`, `apply`, then `seamless_release.py
      prepare` and `promote`): steps 1, 3 and the env line change the recorded configuration digests, so
      autodeploy halts with `configuration_drift` until a release is applied by hand. A range that changes
      `backup_runner.py` needs `deploy` (a manual `apply` refuses it), so let autodeploy carry such a SHA before
      the pause. Then remove the pause; `deploy --dry-run` reads current.
   The worker's start fence exits `not_ready` and writes nothing while a grant of step 1 or a table of 076 is
   missing. Its first start backfills the journal's events of the last 14 days. Undo: restore the configuration
   backups (`services` first), `docker compose $F stop negotiations`, remove the container and the overlay; the
   tables keep what it wrote (a later start resumes from its cursor).
5. Check: `production.py status` prints `negotiation worker: heartbeat <version> at <time>, owner zone <label>`.
   The label must not be `UTC_fallback`: `ANYMSG_OWNER_TIMEZONE` comes from the project's env file. The start
   log line `negotiation_worker_started owner_zone=<label>` says the same. Skipped events log
   `negotiation_event_skipped message=<id> error=<class>`; every 10 minutes
   `negotiation_worker_totals processed=<n> skipped=<n>` counts them (criterion 7 reads these lines and the
   tables, read-only).

Host steps that touch the firewall (the delivery and inference rules) read the boot receipt first: `python3
scripts/inference_egress_check.py verify --receipt-check` and the same for `delivery_egress_check.py` print
`ok` and `receipt ok` only when the rules agree and `/var/lib/deedkit/firewall-verify.json` is ok for the current boot
ID; any other line (`receipt unknown`, `receipt not_ok`, a differing pair) stops the step ("Host firewall
persistence" below).

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

After an A+B promotion (plan B, `seamless_release.py`, serves its own runtime
generation) a plan A rollback is guarded (ANYM-642), standalone and as
`deploy`'s automatic one. It resolves the HTTP configuration (`--http-config`,
default `/etc/deedkit/http-production.json`; `deploy` passes its own) and holds
the locks `seamless_release.py` holds for the whole rollback. It first checks
plan B's state the way `deploy` does (`unresolved`: an unfinished apply or
rollback, an unresolved HTTP attempt, an interrupted publication, read from the
local source mirror `<state>/deploy-source.git`, never fetched), then that
`http-current.json` names a serving generation whose attempt serves and whose
records agree (its runtime image, its API container's image, the release's
`http-result.json`, and its recorded schema with the image's inventory).
Unknown, malformed or inconsistent HTTP state refuses. Only an absent default
HTTP configuration with no HTTP state (`http/`, `http-current.json`,
`*/http-attempted.json`) in `<state>`, the host state root, its subdirectories
with a receipt or any `/etc/deedkit` configuration's state, and no `deploy.json`
recording a plan B promotion, means no plan B and skips the guard (under the
global lock, where the host has one); otherwise pass `--http-config`. The guard then
reads the schema inventory (checksums of `schema.sql` and `migrations/*.sql`,
`docker run --network none --read-only`) of the serving generation's runtime
image and of each messaging image (runtime, indexer, enricher-local,
whatsapp-native, backup) the rollback would restore. Equal inventories, or a
serving inventory that is a strict prefix of the restored one (plan B older),
pass. Migrations plan B serves that a restored image lacks, divergent
inventories (a changed checksum, or neither a prefix) and an unreadable
inventory refuse before `rollback-attempted.json`, any stop or `up`:

```text
promotion refused: plan B serves generation <generation> of <sha> with migrations <project>/<service>'s restored image <image> lacks: migrations/061_<name>.sql; a plan A rollback alone would leave the serving API newer than its workers. Roll forward instead: `production.py deploy` of a newer green main SHA (a fix or a revert that keeps every applied migration; production.md, "Roll back the current release")
```

There is no override. The way out is roll-forward: `production.py deploy` of a
newer green `main` SHA (a fix or a revert pull request that keeps every applied
migration) applies plan A and then promotes plan B in order (`check_schema`).
Attempt markers are per SHA (`<state>/<sha>/attempted.json`), so the serving
release's marker does not block the newer one, and the migrated database stays
as it is. Rolling plan A back alone while plan B is newer is never a recovery:
it recreates the incompatible pair. The guard covers migration-dependent
compatibility only: a code-only protocol change between the serving API and
the workers with equal inventories passes it.

Rollback is a production change an agent runs under AGENTS.md section 2 (`deploy`
also has one automatic rollback, below). It returns
the current release's selected services to the images and configuration that
ran before it, from `<state>/<sha>/<project>.before.json`. It does not call
`promotable()` (the previous revision is no longer current `main`), does not run or
reverse migrations and does not restore data: when the release changed the
schema or data, follow the module's backup and migration runbook instead.
After an A+B promotion (`apply`, then `seamless_release.py promote` of the serving API),
roll back plan B before plan A: this rollback does not check plan B, and a new serving API
next to plan A's previous workers can ask them for what they lack. Messaging schema 61
(ANYM-520): a paused endpoint's resume then refuses on every try (`endpoint_test_failed`,
`access_or_configuration_changed`), the endpoint stays paused and nothing is lost or sent
twice, until both plans run the same release again. The guard is ANYM-642.
Messaging migration 050 (`manual`, ANYM-534) adds `component_heartbeats`: one row each for the
API, the matcher and the dispatcher (package version, schema), each role limited to its own row by
row-level security. Additive; older runtimes write nothing there. With it the matcher and the
delivery dispatcher run in production. Nothing here runs during an active call (check read-only
first).
1. Update the pinned backup and restore runtimes to the prepared release, then the backup gate
   if item 3 above needs one, then `migrate`.
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
   persist them as the `deedkit-delivery` block of `/etc/ufw/before.rules` and `before6.rules`
   (`rules --resolver <ip> --format ufw --family v4|v6`; "Host firewall persistence" below). They drop host-bound packets from all three
   bridges, `dk-delivery0` (egress), `dk-deliverydb0` and `dk-deliverypx0` (internal networks in gateway
   mode `isolated` have no host address; the rules are the second layer), and, from `dk-delivery0`, the tailnet (100.64.0.0/10), RFC 1918,
   link-local and metadata (169.254.0.0/16), loopback and every IPv6 packet; they allow DNS to `<ip>`
   and TCP 443 to public addresses. Undo: `rules --undo`. Writing `DELIVERY_RESOLVER` or a new
   overlay changes the project's Compose configuration, so the next autodeploy [halts](#halts) and writes the
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
2. The backup gate if item 3 above needs one, then `migrate`. 045 takes all its
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
Step 7 takes the deletion gate of harness process.md section 2 (a fresh verified backup, an undo, one Astra
review); step 5's worker runs the DELETEs docs/DATA-POLICY.md names.
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

## Database settings (ANYM-842)

The messaging `db` service runs with statement statistics and a memory set inside its `mem_limit` (2 GiB):
`shared_preload_libraries=pg_stat_statements`, `pg_stat_statements.max=5000`, `track_io_timing=on`,
`shared_buffers=128MB`, `work_mem=4MB`, `effective_cache_size=1536MB` (a planner estimate, no memory),
`random_page_cost=1.1`, `shm_size: 256m`; `jit` stays on (the JIT-heavy statements set `jit=off` themselves).
`deploy/compose.yaml` carries them for new installs; the production host runs a larger set ("Production memory split
(2026-11-09)" below). Each messaging process names its sessions after its service
(`application_name`: `api`, `http-api`, `collector-telegram`, `collector-whatsapp`, `matcher`, `delivery`,
`enricher-<profiles>`, `indexer`, `file-lifecycle`, `cold-archive`, `message-purge`, `file-understanding`, `backup`;
the event router keeps `module-router`). Statement totals come from `pg_stat_statements` by database, user and query
id: services that share a role and a statement share an entry, so a perf pass reads it with `pg_stat_activity`
samples. The view keeps normalized SQL text on the database host (SECURITY.md, docs/DATA-POLICY.md); a report
carries query ids, calls and times only.

Memory budget (measured on prod 2026-11-09, read-only). `max_connections` 100, `max_worker_processes` 8,
`max_parallel_workers` 8 (2 per gather), `autovacuum_max_workers` 3, `maintenance_work_mem` 64MB,
`hash_mem_multiplier` 2. A 4-minute `pg_stat_activity` sample every 0.5 s: at most 35 client sessions, 14 active,
2 parallel workers, 1 autovacuum worker. The cgroup held 736 MB anonymous memory; the backends' `RssAnon` summed to
781 MB for 31 sessions (15-52 MB each, idle collector sessions about 35 MB: their caches stay), and shared memory
162 MB (128 MB of it shared_buffers). The budget, with idle sessions counted, not only active ones:

| Part | MB |
|---|---|
| shared_buffers + other shared memory (34 MB measured, pg_stat_statements about 10 MB) | sb + 44 |
| 40 sessions (35 observed + 5) × 25 MB measured private memory | 1,000 |
| 14 active × 2 × `hash_mem_multiplier` 2 × `work_mem` 4 MB | 224 |
| 3 autovacuum workers × `maintenance_work_mem` 64 MB | 192 |
| 8 parallel workers × (10 MB + 2 × 2 × 4 MB) | 208 |

At sb = 128 MB that is 1,796 MB, above 75% of 2 GiB (1,536 MB); the other parts alone are 1,668 MB, so no
shared_buffers value gives 25% headroom under this limit and today's sessions. The measured view agrees: a synthetic
peak run (2026-11-09, disposable PostgreSQL 18 under the same 2 GiB limit and these settings; 600k messages, 150k
attachments, 2.4 GB; 35 sessions, 14 running the media claim, the enrichment claim, sync status, a selective search
and a parallel join for 180 s, up to 7 parallel workers; the broad searches ran into the 5 s statement timeout, as
they would on prod) rose 593 MiB above its idle 205 MiB to 798 MiB anonymous plus shared memory, with no OOM kill
(the OrbStack cgroup reports no kernel memory; `memory.current` minus page cache, 585 MiB, agrees). Prod's long-lived
sessions already hold 954 MB (anonymous 736, shared 162, kernel 56), so the same burst there reaches about 1,550 MB
at 128 MB and about 1,930 MB at 512 MB. Decision (ANYM-842 criterion 3 as revised on its Tiden branch): shared_buffers
stays at 128 MB, the value prod already runs, so this change adds only pg_stat_statements' shared memory (about
10 MB) to today's footprint; more shared_buffers needs a larger `mem_limit` or fewer idle sessions (smaller pools),
each its own card.

The change is not `production.py`'s: the tool never promotes `db` (and runs Compose with `--no-deps`, so no deploy
recreates it). It is one reviewed production operation, none of it during an active call:

All commands run in `/opt/personal-os-probe` with the project's files in place (they reference `./private/...`
relative to the first file, so never run Compose on copies). `F` is exactly `production.py`'s own Compose arguments
for the project (`compose(project)`, scripts/production.py:490): `--profile '*' --project-name personal-os-probe
--project-directory /opt/personal-os-probe`, `-f` for each of the project's `files` and `--env-file` for each of its
`env_files`, in the order of the plan A configuration (`production.py`'s `active_config()`). Never the container's
`com.docker.compose.project.config_files` label: on 2026-11-09 it named 4 of the 21 files, and a recreation from
those 4 would drop `delivery_db` and `inference_db` (declared in `compose.delivery.yaml` and
`compose.enrichment-mini.yaml`) and cut the dispatcher and the mini worker off the database.

1. Read-only pre-check and undo record. No active call: `python3 -I -c 'import runpy;
   p=runpy.run_path("/usr/local/lib/deedkit/production.py"); print(p["active_calls"](p["config"](p["active_config"]())))'`
   prints an empty list. Record in a dated root-only directory: the sha256 of each file in `F`, the member list of each
   `personal-os-probe_*` network (`docker network inspect -f '{{range .Containers}}{{.Name}} {{end}}'`) and `SHOW` of
   every setting above. Prove that `F` renders the running container, without printing either side (Compose resolves
   env files into the render, the database password included): a host-side `python3 -I` script compares the
   running container with `docker compose F config --format json` for db, key by key (env per key, resources,
   command, mounts, networks, labels, image, user, healthcheck), values by sha256 in memory, and prints only the
   names of keys that differ: none, apart from the volume's name form (`database` vs `personal-os-probe_database`).
   Then `docker compose F config --hash db` is the undo baseline (the container's own hash label may not be
   reproducible: Compose's hash of the same service changes with the file set). Write the overlay
   `/opt/personal-os-probe/compose.db-settings.yaml` (0600) with only `services.db.command` and
   `services.db.shm_size` as in `deploy/compose.yaml`; the same comparison with `F` plus the overlay names only `cmd`
   and `shm_size`; record the overlay's `--hash db`.
2. Pause autodeploy (`/etc/deedkit/autodeploy.pause` with the reason, after the unit is not active). Then run the
   change under the backup runner lock, undo included: `flock --nonblock /opt/personal-os-probe/backups/.runner.lock
   <script>`. The script first checks again, under the lock (as `production.py` does, ANYM-744), that
   `personal-os-backup.service`, `deedkit-calendar-backup.service` and `deedkit-calendar-backup-catchup.service` read
   `inactive` or `failed`, that `/opt/personal-os-probe/backups/.runner.lock.unsettled` is absent and that no call
   is active. Any other answer: stop, remove the pause, try later.
3. `docker compose F -f compose.db-settings.yaml up -d --no-build --pull never --no-deps db`: the `database` volume
   is kept and the database comes back in every network `F` declares for it, each with its `db` alias. Wait for
   healthy (`docker inspect --format '{{.State.Health.Status}}'`). Not healthy within 2 minutes (a preload or
   setting refused at start): the undo below at once, in the same script.
4. Every `personal-os-probe_*` member list equals the record, apart from one-off `compose run` containers that ended
   meanwhile (`--rm`).
5. As the bootstrap superuser: `CREATE EXTENSION IF NOT EXISTS pg_stat_statements` in `messaging_probe`; no role gets
   a grant.
6. Verify: `SHOW` returns the new values; `SELECT count(*) FROM pg_stat_statements` works; the API answers; the
   collectors' leases renew (`collector_leases.expires_at` moves ahead, `collector_health.receiving_heartbeat_at`
   stays fresh); `subscription_outbox` rows are created and finished after the restart; `pg_stat_activity` shows
   the service names; client errors stop after the restart instant (delivery lanes log `AdminShutdown` once each).
   Then remove the pause.
7. Watch 24 h: no OOM kill (`memory.events` `oom_kill` of the container and `docker events --filter event=oom`),
   hit ratio and temp bytes per hour before and after, and a top-20 by total time (query ids, calls, times).

Applied 2026-11-09 04:27:53-04:28:02Z (healthy after 8 s; pause 04:27:45-04:30:45Z), record and scripts in
`/root/anym842-20261109T0422Z` on the host: undo baseline `d6a514a8…`, overlay `e26841ea…`; no client container
restarted.

Undo, under the runner lock as in step 2: `docker compose F up -d --no-build --pull never --no-deps db` without the
overlay (on 11-09: `/root/anym842-20261109T0422Z/db842.sh undo`); the hash equals the undo baseline. Step 4 again, then
remove the overlay file. The
extension may stay (without the preload its view only refuses reads) or `DROP EXTENSION pg_stat_statements`. The
data stays on the volume.

### Production memory split (2026-11-09)

The production host gives the database more memory than the 2 GiB default (owner 11-09, «свободный 4-7 гигабайта ...
надо как-то распределить, чтобы мы не упирались точно нигде»). Before: the `db` cgroup sat at its 2 GiB limit
(`memory.events` max 4,710, no OOM kill) with 264 MB anonymous, 160 MB shared and 1.78 GB page cache, and 741k
file refaults: its own cache was being evicted. The host had 7,751 MB RAM, no swap, 5.1 GB available. Every other
container used at most 90 MB anonymous memory and 36% of its limit, with no OOM kill; `file-lifecycle` and
`cold-archive` touched their 256 MB limit with page cache only (33 MB anonymous) and keep it.

The overlay `/opt/personal-os-probe/compose.db-settings.yaml` now sets `mem_limit: 4g`, `shm_size: 1g`,
`shared_buffers=1GB`, `effective_cache_size=3GB`, `work_mem=8MB`, `maintenance_work_mem=256MB`,
`autovacuum_work_mem=128MB` (3 workers × 128 MB, not 3 × 256 MB), and keeps the statement statistics,
`random_page_cost=1.1` and `jit`. `deploy/compose.yaml` keeps the 2 GiB set for other installs. Budget inside 4 GiB with
the method above: shared memory 1,068 + 40 sessions × 25 = 1,000 + 14 active × 2 × 2 × 8 MB = 448 + autovacuum 384 +
parallel 8 × (10 + 2 × 2 × 8) = 336, so 3,236 MB (79%) when every part peaks together. Measured: about 1.85 GB idle
plus the 600 MB synthetic burst, about 2.45 GB (60%). Host: other services use about 2.36 GB, about 3.2 GB with a
call and a backup at once, so about 2.9 GB stays free in the expected case and 1.3 GB when everything peaks together.

Applied 2026-11-09 06:56:19-06:56:28Z by the route above (healthy after 8 s, no active call, under the runner lock;
autodeploy was already halted for the 072 hand release and is not this step's pause). Record, overlays and script are
in `/root/prodmem-20261109` on the host: hashes `e26841ea…` (before) and `f7a4c4a1…` (after). The comparison named only
`cmd`, `mem_limit` and `shm_size`, the networks equal the record, and no client container restarted. Client errors
appear only between 06:56:21 and 06:56:27Z; collector leases, heartbeats, ingest, `subscription_outbox` and the
listeners resumed. One hour later (07:57Z): no OOM kill (`memory.events` all 0, no `docker events` oom, no kernel
line), cgroup peak 3.5 GB (shared 1.14 GB, anonymous 196 MB, the rest page cache), host 3.96 GB available. The hit
ratio over the hour was 99.73% (257.3M hits, 0.71M reads). From 04:35 to 06:56Z it was 94.5% (823.9M hits, 48.0M reads),
so block reads per minute fell about 29×. Temp use was 2 files and 3 MB in the hour, against 6 files and 85 MB in the
2 h 21 min before. All 25 `subscription_outbox` rows created after the restart were finished, and 213 messages
were ingested.

Undo, under the runner lock: `flock --nonblock /opt/personal-os-probe/backups/.runner.lock
/root/prodmem-20261109/dbmem.sh undo` puts the previous overlay back (`compose.db-settings.before.yaml`, the
ANYM-842 set) and recreates `db`, so the hash is again `e26841ea…`. Then run step 4 again.

## Host firewall persistence (ANYM-693)

What is persisted and where (ufw owns `DOCKER-USER`, so a second restore file would flush or duplicate a hook):

| part | where | loaded by |
|---|---|---|
| delivery filter chain, `DOCKER-USER` hook, host-bound drops, IPv6 drops | `deedkit-delivery` block in `/etc/ufw/before.rules` and `before6.rules` (the first block; it declares `:DOCKER-USER`) | ufw, at boot and at every `ufw reload` |
| inference filter chain, `DOCKER-USER` hook, host-bound drops (`dk-inference0`, `dk-inferencedb0`), IPv6 drops | `deedkit-inference` block in the same two files, after the delivery block (it declares only its own chain) | ufw |
| inference raw chain and the `raw` `PREROUTING` jumps (IPv4 chain, IPv4 and IPv6 jump) | `/etc/ufw/after.init` (mode 0755, root), `rules --format after-init` | ufw, with `start`, after it has loaded its rules |
| recordings filter chain, `DOCKER-USER` hook, host-bound drops (`dk-recordings0`, `dk-recbackend0`), IPv6 drops (ANYM-712) | `deedkit-recordings` block in the same two files, after the delivery block (it declares only its own chain) | ufw |
| recordings raw chain and the `raw` `PREROUTING` jumps | a function of the same `/etc/ufw/after.init`, which is then generated from both families (`rules --format after-init` of either script) | ufw, with `start` |
| comparison and boot receipt | `verify` of both scripts; `deedkit-firewall-verify.service`, which writes `/var/lib/deedkit/firewall-verify.json` | each boot, after `ufw.service` and `docker.service` |

The raw table is not in ufw's files because restoring them would flush Docker's own raw rules. `after.init` replaces
the raw chain in one `iptables-restore --noflush -T raw` (one kernel commit: a failure or a kill leaves the previous
complete chain, never an empty one), adds the jump only after the chain is complete, bounds every command to 30 s
(a command that ignores TERM is killed 2 s later), runs the whole hook, log and record included, under an outer bound of
60 s (its failure record is written by a bounded child with no terminal and no pipe of the caller; the supervising
shell's own diagnostics go to /dev/null, and the caller's stderr is restored only inside the supervised child, so no shell,
dash included, can block on it), logs and records a failure in `/var/lib/deedkit/raw-step.json` and still exits 0, so ufw and Docker start. The failure
shows in `verify` (`kernel_vs_repo table_differs:raw`), in the boot receipt and in the worker's own exit 78. The IPv6
raw rule is the single `PREROUTING -i dk-inference0 -j DROP` of `rules()` (there is no IPv6 chain), added if absent.

Facts to know: a `ufw reload`, `ufw disable/enable` or a ufw restart loads `before.rules` with `iptables-restore
--noflush`, and its `:DOCKER-USER` declaration empties that chain; before ANYM-693 this removed the inference hook from
`DOCKER-USER`. After a reboot Docker leaves `enricher-mini` stopped (restart policy `no`) and `deedkit-inference-worker.service` starts it behind its gates ("Stopped worker"); a promotion-tool `up` before the
rules exist starts it into exit 78. With the rules persisted they are there first (`ufw.service` starts before
`docker.service`).

`verify` (root, read-only, stdlib) compares three texts for each chain: the repository (the authority; the mini address
from `INFERENCE_MINI_IP` and the resolver from `DELIVERY_RESOLVER` in `/opt/personal-os-probe/.env`), the persisted
files and the kernel (`iptables-save`, `ip6tables-save`). It prints `ok`, or one line per differing pair
(`file_vs_repo`, `kernel_vs_repo`, `file_vs_kernel`) with the first difference: `missing:<rule>`, `extra:<rule>`,
`hook_absent:<bridge>`, `mini_differs`, `table_differs:<filter|raw|v6>` or `missing::<chain>` (a file that uses
`DOCKER-USER` or its chain without declaring it, which would stop ufw from loading); exit 1 on any, 2 when an input or
the kernel cannot be read. `--persisted-only` reads no kernel (the check before loading); `--receipt-check` also reads
the boot receipt. A file and a kernel that agree on an allowance the repository does not have still fail: the
repository is the authority. The host-bound drops count as one fact whether the kernel holds them in `INPUT` (the
command form) or in ufw's chain of the same family (`ufw-before-input`, `ufw6-before-input`; in the kernel only when `INPUT`
jumps to it), but a rule present in both is a duplicate and fails as `extra:`. A rule that accepts or returns the bridge's
traffic before a managed hook in `DOCKER-USER` or raw `PREROUTING` (no `-i`, a wildcard or a negated `-i`; also through a
`-j` or `-g` into a chain that accepts; a goto that can fall off the chain's end, an empty chain included, leaves the
base chain too) fails as `extra:<rule>`, since the hook would never see those
packets; the same check covers the path through `INPUT` to ufw's chain, whose jump must cover the managed bridge and carry no
other condition (a `-p tcp` jump carries only part of the traffic). A rules file that declares `:DOCKER-USER` or the managed
chain twice, or after rules that use it, fails as `table_differs:filter` or `table_differs:v6` (restoring the late
declaration empties the chain).

Safe order for any firewall change (as root; the plan is Astra-reviewed, none runs during an active call):
1. Back up the files that change: copy `/etc/ufw/before.rules`, `before6.rules` and `after.init` (if it exists) with
   `cp -a` to a dated directory under `/root`, and write `iptables-save` and `ip6tables-save` next to them.
2. Build the new files without touching the old ones: `python3 scripts/inference_egress_check.py rules --mini-ip
   $INFERENCE_MINI_IP --format ufw --family v4 --source /etc/ufw/before.rules` writes the whole new file to a new path
   (v6 with `before6.rules`). It replaces a block of the same label, inserts after the delivery block (after its end marker when it has one, never inside it) and refuses a file
   with no delivery block or no `:DOCKER-USER` declared before the insertion point. `--format after-init` writes the hook.
3. `iptables-restore --test --noflush` on the new v4 file and `ip6tables-restore --test --noflush` on the v6 file: syntax
   only. A syntax error in a ufw rules file stops ufw from loading, so nothing is installed before this passes.
4. Install the two rules files (mode 0640 as they are now, root) and the hook (mode 0755, root), then `verify
   --persisted-only` for both scripts.
5. Load: `ufw reload` (the reload empties and refills `DOCKER-USER` and loads the block), then `/etc/ufw/after.init start`
   (idempotent; ufw's reload may not run it). The first time only, delete the hand-added copies of the host-bound drops,
   which ufw's chain now holds too: `iptables -D INPUT -i dk-inference0 -j DROP` and the same for `dk-inferencedb0`, and
   for `ip6tables` both (delete only what `iptables -S INPUT` and `ip6tables -S INPUT` show).
6. Full `verify` for both scripts, then `systemctl start deedkit-firewall-verify.service` and `verify --receipt-check`.
   Stop on any line other than `ok` and `receipt ok`.

Rollback: copy the saved rules files and `after.init` back, `ufw reload`, `verify`. Undo of the whole card: the same
copy-back (remove `/etc/ufw/after.init` if it did not exist before), `systemctl disable --now
deedkit-firewall-verify.service`, remove the unit and `firewall_persist.py`, `ufw reload`; the kernel rules of the command
form (`rules --undo`) are removed only by an explicit decision.

Install of the scripts and the unit (once per tool update): copy `scripts/firewall_persist.py`,
`scripts/inference_egress_check.py` and `scripts/delivery_egress_check.py` to `/usr/local/lib/deedkit/` (mode 0755,
root) and `deploy/deedkit-firewall-verify.service` to `/etc/systemd/system/`; `systemd-analyze verify` it, `systemctl
daemon-reload`, `systemctl enable` it. It is wanted by `docker.service`, not by a target: nothing waits for it, nothing is
ordered after it, `TimeoutStartSec=60`, and a failure fails only itself.

The boot receipt (`/var/lib/deedkit/firewall-verify.json`, written at each boot) holds the boot ID, the result of both
`verify` commands, `ufw_rules_loaded_monotonic` (ufw.service's `ExecMainExitTimestampMonotonic`), `raw_step_monotonic`
(the step's own `/proc/uptime` on success and on failure), `docker_start_monotonic`, `first_container_monotonic` (the start of the earliest running container's main process this boot, from
`/proc/PID/stat`, never from Docker's wall-clock times; a container that restarted shows its latest start; unknown when a
container cannot be read, and whenever a stopped container that ever ran exists, since without a reliable boot-wide
clock mapping its start cannot be placed on this boot's clock) and
`rules_before_first_container` = `yes`, `no` or `unknown` (unknown when a time is missing or a step failed). The times are
seconds on one monotonic clock (the host does not suspend, so `/proc/uptime` equals it). A boot with no receipt for the
current boot ID reads as `receipt unknown` in `verify --receipt-check`, never as ok. The first reboot that happens for
another reason is read through the receipt and recorded here as supplementary evidence; nothing is rebooted for it.

## Mini enrichment worker: network confinement (ANYM-688)

`deploy/compose.enrichment.yaml` holds `enricher-mini` (profile `enrichment-mini`, the published
`enricher-local` image). It joins two networks and no other: `inference_db` (internal, bridge
`dk-inferencedb0`, gateway mode `isolated`, so no host address; the overlay also attaches the `db`
service) and `inference_egress` (bridge `dk-inference0`, this worker only). It never joins `backend` or
`frontend`. The host rules narrow `dk-inference0` to TCP 443 to the mini's tailnet address. Select this
profile alone: never with `enrichment-local` or `enrichment-cloud` (ENRICHMENT.md). This section is the
hand apply; the production apply itself is ANYM-686, under its reviewed plan. Nothing here runs during
an active call (check read-only first).

Inputs: `INFERENCE_MINI_NAME` (the mini's `.ts.net` name, exactly the host of `base_url`) and
`INFERENCE_MINI_IP` (its tailnet address, 100.64.0.0/10) in the project's env file; the overlay pins the
name with `extra_hosts` and sets the same address as the worker's `dns`, so any other lookup goes where
the rules drop it. `private/enrichment-mini.json` (from `deploy/enrichment-mini.example.json`, only
`deedkit_inference` profiles, mode 0600, owned by uid 10001) and the directory `private/inference/`
(mode 0700, `inference.key` mode 0600 inside, from ANYM-684). The worker mounts the directory, not the
file: a bind-mounted file keeps the retired inode after an atomic rotation. The DSN is the
`messaging_enricher` one (`private/enrichment-local.dsn`).

Prerequisite: the host's current release contains ANYM-685 (the `deedkit_inference` profile). A release
before it rejects the profile (`invalid_model_profile`), so the enabled worker exits before it can be
promoted: apply that release through the normal route first (`production.py`), and only then enroll.
Apply, in this order (a failed step stops the apply; the worker is selected for promotion last).
`F` is the project's Compose arguments plus `-f compose.enrichment.yaml`; `E` is the verified
`enricher-local` image of the host's current release (`docker image inspect` it; never `--build`),
tagged `docker tag $E anymsg-enricher-local:enroll-<sha>` (a new tag, never a production tag), and the
Compose commands below run with `MESSAGING_VERSION=enroll-<sha>`:
1. Rules (host, as root, before the networks exist; they match bridge names):
   `python3 scripts/inference_egress_check.py rules --mini-ip $INFERENCE_MINI_IP` prints them; load them
   and persist them as "Host firewall persistence" below says (the `deedkit-inference` block in ufw's two
   rules files for the filter table and the host-bound drops, `/etc/ufw/after.init` for the raw table), then
   `verify`. The same chain hangs in two places:
   `raw` PREROUTING (before conntrack and the filter `FORWARD` chain, so Tailscale's `ts-forward`
   accept, which sits at position 1 and is reinstalled when `tailscaled` restarts, cannot pass it) and
   `DOCKER-USER`. From `dk-inference0` they allow only TCP 443 to the mini and drop DNS to any address,
   RFC 1918, link-local and metadata (169.254.0.0/16), the rest of the tailnet, loopback and all IPv6;
   host-bound packets from `dk-inference0` and `dk-inferencedb0` are dropped in `INPUT`.
2. Networks: `docker compose $F --profile enrichment-mini up --no-start --no-build --pull never --no-deps
   enricher-mini` creates both networks and the stopped worker.
3. Database: a Compose change that adds a network to `db` would recreate the container, and databases
   are never restarted by the tool. Attach it live instead: `docker network connect --alias db
   <project>_inference_db <db container>`. Its `backend` attachment, identity and sessions are
   unchanged; no connection drops. A later recreate of `db` (a Postgres image bump) takes the network
   from the overlay.
4. Check from a probe container. The worker with `"enabled": false` exits (`enrichment_disabled`) and
   restarts in a loop, so it cannot host the check. `run` starts a bounded one-off container from the same
   service definition (same networks, `extra_hosts`, `dns` and limits) with the interpreter as entrypoint:
   `docker compose $F --profile enrichment-mini run --rm -T --no-deps --entrypoint python enricher-mini -I
   - check ... < scripts/inference_egress_check.py` (below). Fix the rules on any failure.
5. Start: with `private/enrichment-mini.json` enabled, `docker compose $F --profile enrichment-mini start
   enricher-mini`; it runs, with no restarts.
6. Only now select the worker for promotion (the host tool's `snapshot` needs a running container):
   append `compose.enrichment.yaml` to the project's `files` in `/etc/deedkit/production.json` (and the
   messaging project's `files` in `/etc/deedkit/http-production.json`) when it is not there yet, and map
   `"enricher-mini": "enricher-local"` in `services` (back both files up first). Autodeploy pauses on a
   Compose change ("apply the next release by hand"): under the pause file apply the next green `main`
   SHA by hand as item 5 of the delivery enrollment above does, then remove the pause.

Check (the receipt keeps the JSON). `listen` is a bounded synthetic listener; the positive control keeps
"blocked" from meaning "nothing listens". Blocked targets are IP literals read with `docker inspect` and
`ip addr`, each with a known listener; `<api ip>` is the API container's address on `backend`,
`<mini ip>:<port>` a port the mini listens on other than 443 (such as 22 while Remote Login is on) and
`<tailnet node ip>:<port>` another tailnet node's listener. The two tailnet targets prove the raw hook
beats `ts-forward`:
```sh
docker compose $F exec -T api python -I - listen --port 18081 --seconds 300 < scripts/inference_egress_check.py &
docker compose $F exec -T api python -I - check --control <api ip>:8080 --control <api ip>:18081 \
  --control <mini ip>:<port> --control <tailnet node ip>:<port> < scripts/inference_egress_check.py
docker compose $F --profile enrichment-mini run --rm -T --no-deps --entrypoint python enricher-mini -I - check \
  --open db:5432 --open $INFERENCE_MINI_IP:443 --blocked <mini ip>:<port> --blocked <tailnet node ip>:<port> \
  --blocked <api ip>:8080 --blocked <api ip>:18081 --blocked-name api:8080 --blocked <host public ip>:443 \
  --blocked <dk-inference0 gateway ip>:443 --blocked 1.1.1.1:443 --blocked 1.1.1.1:53 --blocked-dns 1.1.1.1 \
  --blocked-name example.com:443 < scripts/inference_egress_check.py
```
(The `--control` runs come from a container that may reach those addresses; the tailnet ones from the host
itself: `python3 scripts/inference_egress_check.py check --control ...`.) It exits 0 only when the database
and the mini connect, every blocked address times out or has no route, and every blocked name does not
resolve. A refusal means the address answered, which a DROP never does; a name that resolves fails even if
the connection is then dropped. A failed lookup does not prove that no query left, so watch the host during
the check with a unique probe name: use `--blocked-name probe-<random>.example.com:443` instead of
`example.com:443`, and capture the whole host: `tcpdump -ni any -l 'port 53' | tee dns.txt`. Docker
masquerading rewrites the source of a packet that leaves the host, so do not filter by the container
subnet; search for the unique label instead. The bridge capture must show the label on `dk-inference0`
(the check did generate the query), and no other interface (`tailscale0`, the uplink) may show it. First
the positive control: from the host, `getent hosts probe-control-<random>.example.com` must show its label
on the uplink in the same capture, or the capture proves nothing. `dns.txt` holds every name the host
resolved: keep it private on the host (mode 0600, removed after the review), and give the receipt only the
labels, the interfaces that showed them and the verdict.

Self-check of the worker (ANYM-688). The rules above can be missing when the worker starts or later: after a reboot, a
`ufw reload`, a partial load, or a start that skips this section (`docker start`, the promotion tool's `up`). So
`enricher-mini` proves its own confinement by trying to leave: before it publishes a profile or claims a job, before
every claim, and every 15 seconds in the background while a job runs. It tries TCP to public IPv4 and IPv6 addresses
on 443, to 1.1.1.1 on 53 and to the metadata address 169.254.169.254, a UDP DNS query with a random label to 1.1.1.1
and to every resolver in `/etc/resolv.conf` (plus TCP 53 to the configured ones), the container's default gateway
(the host) on 22, 53, 80 and 443, and ports 22 and 80 of the mini. Only a dropped attempt (timeout, no route) counts as
confined. A connection, a refusal, a reply, an ICMP error, an unexpected local error, a local fact it cannot read and a
check that does not finish in time all count as NOT confined. Then it exits with code 78, prints one JSON line with the
failed check names to stderr (`{"error": "confinement_not_effective", "checks": [...]}`), and claims nothing (a job in
flight is given back). The attempts carry no data. A worker that restarts with 78 means the rules or networks are wrong:
fix them, never disable the check. The same probe is a command, for the probe container or any container on the bridge:
`docker compose $F --profile enrichment-mini run --rm -T --no-deps --entrypoint python enricher-mini -m
messaging_service.inference_confinement <mini .ts.net name>` prints `{"confined": true, "failed": []}` and exits 0 on the
confined bridge, and a list of failed checks with exit 78 anywhere else.

On a custom Docker network `/etc/resolv.conf` names Docker's embedded resolver (a loopback address): it always listens, so
it is judged by its answer to a query for an external name, not by listening. NOERROR or NXDOMAIN means an upstream
answered (not confined); SERVFAIL, REFUSED or silence is what it says when its upstream (the mini's address, dropped by
the rules) is unreachable. Add the known-listening addresses that must stay unreachable to the worker config as
`"confinement_blocked": ["100.x.y.z:22"]` (up to 16 `ip:port`, IPv6 as `[ip]:port`), for instance another tailnet node's
SSH, the target the egress `check` above uses as `--blocked <tailnet node ip>:<port>`: without it a bypass that opens
only an unprobed tailnet destination (a `ts-forward` accept) is not seen.

What it is and is not. It is a guard against the operational loss of the rules for the worker as shipped (a start path
that skips the operator's checks, a reload, a partial load, a shadowed hook that opens one of the probed exits): a claim
is never started on an unproved network, and a rule loss during a job is noticed within about 15 seconds plus one probe.
It is not a defence against a compromised worker (code that skips the check) and not a proof of the whole allowlist: it
samples the exits that matter. The host rules (and their persistence in ufw) remain the security boundary. An
independently enforced destination restriction (a network without a gateway plus a fixed-destination relay) is the
stronger design: until it exists, do not read this check as making `restart: unless-stopped` risk-free. What the worker
sends to the mini goes only to the pinned name of its profile, so a window of lost rules does not change where a benign
worker sends a file.

Undo (reverse): `docker compose $F --profile enrichment-mini stop enricher-mini` and `rm enricher-mini`;
`docker network disconnect <project>_inference_db <db container>`; `docker network rm
<project>_inference_db <project>_inference_egress`; restore both configuration backups (a promoted release
needs the same newer-SHA hand apply as the delivery undo above); `docker image rm` the enroll tag; as root
`python3 scripts/inference_egress_check.py rules --undo` prints the mirror of the rules (hooks first, then
both chains); with the rules persisted, also restore the saved copies of the rules files and `after.init` and
`ufw reload` ("Host firewall persistence", Rollback), or the next reload or reboot puts the rules back. The worker's jobs requeue on SIGTERM; nothing in the database changes.

### Stopped worker: the unit and manual recovery (ANYM-709, ANYM-794)

After a host reboot or a Docker restart Docker leaves `enricher-mini` stopped: its restart policy stays `no`, since a policy
cannot leave out exit 78. `deedkit-inference-worker.service`, a root oneshot wanted by `docker.service` with no `Restart=`
and no timer, runs `/usr/local/lib/deedkit/inference_worker_start.py` once per Docker start. Nothing requires it and Docker
does not wait for it. A stopped selected worker makes `prepare` refuse ("each selected service must already have exactly
one running container") and autodeploy pauses on that refusal ("Continuous deployment"); the unit never removes
`/etc/deedkit/autodeploy.pause`.

Its steps, in order; a failure records its code, runs no later step and starts nothing. First: an installed tool without a
function it uses is `tool_too_old`, an unreadable configuration `config_unreadable`.
a. Container running: outcome `already_running`, nothing else (`docker_unreadable` if `docker ps` fails).
b. Backup runner's lock, then `deploy.lock`, not waiting, held to the end of k: `backup_running`, `release_running`.
c. `unresolved` is clean: `release_unresolved`.
d. One container whose ID, image and settings digest equal `current.json`'s: `no_container`, `identity_mismatch`,
   `identity_unrecorded` (none or a legacy one, a rolled-back release included).
e. `/etc/deedkit/inference-worker.hold` is absent: `operator_hold`.
f. Last exit code is not 78: `previous_exit_78`.
g. Database healthy within 300 s: `database_not_ready`.
h. No active call, one 120 s deadline shared with j: `active_call`, `call_probe_failed`.
i. `inference_egress_check.py verify` prints `ok`: `egress_rules_not_verified`.
j. Again: the hold file, the identity, the calls.
k. `docker compose $F --profile enrichment-mini start enricher-mini`, `start` only: `start_failed`. It reads the container
   for 60 s: an exit records `confinement_not_proved` (78) or `exited_<code>`; it never starts it again. A later 78 stops
   the next run at f.

Receipt: `/var/lib/deedkit/inference-worker.json` (`checked_at`, `outcome` `started|already_running|refused`, `reason`), and
`production.py status` prints `inference worker: <outcome> <reason> (checked <time>)` on stderr. Exit 0 for the first two
outcomes, 1 for `refused`. No retry: a person or the next Docker start decides.

Stop it so that it stays stopped: `touch /etc/deedkit/inference-worker.hold` first, then the `stop` of the Undo below.

Install, by hand, like the firewall unit, only after checking that the deployed release holds ANYM-786 (`git merge-base
--is-ancestor 4017891b <deployed sha>`: until a lost confinement always exits 78, a masked exit reads as ordinary): copy
`scripts/inference_worker_start.py` to `/usr/local/lib/deedkit/` and `deploy/deedkit-inference-worker.service` to
`/etc/systemd/system/`, `systemctl daemon-reload`, `systemctl enable deedkit-inference-worker.service`. Correct the comment
at lines 48-49 of the host overlay `compose.enrichment-mini.yaml` in the same step (comment only, with a copy and a
read-back): it names a unit path and a timer that do not exist. Uninstall: `systemctl disable` the unit; nothing else changes.

Who reads its liveness: whoever reads the day's production status (the watch below) and the operator of every release.
A stopped worker raises the Tiden event `enrichment_no_worker` (triage: "Mini inference: watch and enable record"). Read `docker ps --filter name=enricher-mini` (one container, `Up`)
and `SELECT account, kind, enabled, pause_reason, expires_at FROM processing_profiles` (operator DSN, no write).

The manual path stays for a refused start. Read the reason and fix it first; then start it, as root on the host, in this order; a failed step stops:
1. The active-call gate (it prints `no active call`; a call or a traceback means stop). This host's configuration is
   the one `active_config()` names, so the `p.CONFIG` form of inference-enable.md fails here: `python3 -I -c 'import sys;
   sys.path.insert(0, "/usr/local/lib/deedkit"); import production as p; found = p.active_calls(p.config(p.active_config()));
   print("active calls:" if found else "no active call", found); sys.exit(1 if found else 0)'`.
2. The rules: `python3 /usr/local/lib/deedkit/inference_egress_check.py verify` must print `ok` ("Host firewall
   persistence" for any other line).
3. `docker compose $F --profile enrichment-mini start enricher-mini`: `start`, never `up`, so the container keeps the
   identity the release receipts recorded. `F` is the project's `files` and `env_files` from that configuration.

Check: running, no restarts; a restart with exit 78 means the rules or networks are wrong (self-check above; never
disable it). The `processing_profiles` rows are enabled again (the worker publishes them at start). If
`/etc/deedkit/autodeploy.pause` exists, read its reason and remove it only when that reason is this refusal. Undo:
`touch /etc/deedkit/inference-worker.hold`, then `docker compose $F --profile enrichment-mini stop enricher-mini`; its jobs
requeue on SIGTERM. Remove the hold file before the worker should start again.

### Working space and audio limit: run record (ANYM-709)

Applied on 2026-11-03 by hand release N8 (`045f37b7`, under the pause, with migration 066; host edit 12:09:58Z, plan A
12:21:35Z): `tmpfs: ["/tmp:size=320m,noexec,nosuid"]` on `enricher-mini` in the host's `compose.enrichment-mini.yaml`,
and the audio `max_input_bytes` 94371840 -> 209715200 in `private/enrichment-mini.json` (profile hash `f1cd04ef` ->
`f9b561c1`). Backups: both files with the suffix `.bak-709-20261103T120954Z`. `profile_max_jobs_per_day.audio` stayed
500, which covered the inventory plus the day's fresh notes.

Read on 2026-11-05 on release `1003e409`, after later autodeploys kept both edits: the worker runs the release's
`enricher-local` image, one container, 0 restarts, `Tmpfs` 320m and `df -h /tmp` 320M, the mounted profile shows
209715200, all nine `processing_profiles` rows are enabled, no audio job is leased, egress `verify` prints `ok`.

Inventory outcome. The two owner notes (43.8 and 47.9 MiB) keep their transcripts, same length and hash. The five old
rows (14.9, 24.1, 25.8, 28.3 and 39.9 MiB) were **not retried**: they still read `failed` with their codes of 10-14
(`audio_duration_limit` once, `original_size_limit` four times), attempts 3. Their originals are evicted, and a claim
skips an evicted original (`PROCESSABLE_ORIGINAL`), so a changed profile hash does not bring such a row back;
`request_file_download` would (MEDIA_STORAGE.md: cold copy first, then the provider queue), and it was not run. No audio
over 90 MiB has reached the worker since, so the 200 MiB limit is read back, not yet exercised by a real file.

Undo, with the active-call gate before each step: pause; restore both backups; apply by hand a newer green `main` SHA
that was never prepared (never a hand `up`); read back `Tmpfs` 128m and 94371840; remove the pause. Restore the profile
before any older image is applied (an older image refuses a profile above 32 MiB). Completed inference and spent budget
are not undone.

## Mini inference: watch and enable record (ANYM-686)

The plan to enable it is [inference-enable.md](inference-enable.md). Whoever reads the production status of a
day also does this read-only watch (operator DSN, `psql` through the host's documented database access; no write).
One query lists the scopes (account, kind, profile) that have queued work:

```sql
SELECT m.account, e.kind, e.profile_hash,
       count(*) FILTER (WHERE e.state = 'queued' AND e.next_due <= clock_timestamp()) AS due,
       count(*) FILTER (WHERE e.state = 'queued' AND e.error_code = 'model_backpressure') AS held,
       round(extract(epoch FROM clock_timestamp() - max(e.updated_at)
             FILTER (WHERE e.state = 'ready' AND e.provider = 'deedkit_inference')) / 3600, 1) AS hours_since_ready
  FROM attachment_enrichment e JOIN attachments a ON a.id = e.attachment JOIN messages m ON m.id = a.message
 WHERE e.kind IS NOT NULL AND e.profile_hash IS NOT NULL GROUP BY m.account, e.kind, e.profile_hash
HAVING count(*) FILTER (WHERE e.state = 'queued') > 0;
```
Timestamps of rows cannot give continuous waiting (a deferral refreshes `next_due`, `updated_at` and the
pause), so the reader keeps a ledger, a text file on the host (`private/inference-watch.tsv`, mode 0600, no
content) with one line per scope: `account kind profile_hash first_seen_utc`. At each reading: a scope the query
lists and the ledger does not is added with the reading's time; a scope the ledger has and the query no longer
lists (the queue drained) is removed. Report, separately: (a) every scope whose ledger time is more than 24
hours old (elapsed continuous waiting) together with its `hours_since_ready`; (b) a stall when that elapsed
waiting is above 24 hours **and** `hours_since_ready` is above 24 or NULL (never a success from the mini; compare
with the enable date). A long wait with fresh successes is a backlog draining at its caps, not a stall. Another
scope's success does not hide a stalled one: the groups are separate.

The API raises Tiden events on shorter limits than this 24 hour by-hand rule (ANYM-785): `enrichment_stalled` (a live
worker, queued rows and no `ready` for 6 h), `enrichment_starved` (50 or more queued rows in a scope sent 6-48 h ago),
`enrichment_no_worker` (a mini row not enabled or expired, last published 30 min to 7 days ago). `get_sync_status` shows
`enrichment_health` per account. Triage, read-only: the mini's `/healthz` and `busy.json`, the `refusal=` counts in the
service log, the impact log.

The key and the service: `python3 scripts/inference_probe.py --url https://<mini>.<tailnet>.ts.net --key-file
private/inference/inference.key` prints one line and exits 0 whatever it finds; report any line that is not
`reachable=yes status=ok` with a `key_expires=` value (`status=http_502`, `reachable=no` and a rejected key all
count), and `key_days_left` of 30 or fewer (the rotation of inference-mini-setup.md section 12 is due).

The enable record on the ANYM-686 card keeps: the date and `S`; the three attachment ids of the end-to-end run (not
their content); the egress `check` JSON and the `tcpdump` evidence; the offline and admission drill times; the key's
expiry date and its rotation due date (30 days before); never the key, a DSN or a file name.

## Mini inference: slow uploads (ANYM-692)

Use when an upload of a note or photo to the mini is slow (drill 5 of ANYM-686: 6.4 MB in 579 s of the 600 s
`upload_seconds`; the failed attempts end as `model_http_error` for the mini's 408, `model_request_failed` for the
worker's own timeout, then `retry_exhausted`). Everything here is read-only except the probe's one refused request
and the requeue at the end. The probe posts random bytes that the mini refuses with 422 `audio_unreadable` before
any engine runs (not counted against the daily quota); it is not a listed live action. Never during an active call
(read-only check first). Its `refusal=audio_unreadable` lines in the mini's log are probe traffic: the daily watch
above (and ANYM-686 criterion 8) must not read them as failures.

Three candidate causes, told apart before anyone works on one of them: the link, the worker's container path, the
mini's receive side. The measurement is a hypothesis test with an `inconclusive` outcome, never a verdict.

1. Host, read-only: `tailscale status` (direct or relay), `tailscale ping <mini>`, `ip link show tailscale0` (MTU 1280
   expected), `sysctl net.ipv4.tcp_congestion_control net.ipv4.ip_no_pmtu_disc`.
2. Container, read-only: `docker compose $F exec -T enricher-mini sh -c 'for f in /sys/class/net/*/mtu; do echo $f
   $(cat $f); done'`. On the host: `iptables -t mangle -S` and `nft list ruleset | grep -i maxseg` for a TCPMSS clamp.
   A bridge `dk-inference0` at MTU 1500 over a route through `tailscale0` at 1280 with no clamp is the suspect.
3. The probe, once at 1 MiB from the host and once from the worker's container, in the same hour (more bytes only while
   the last run took under a third of `upload_seconds`; at 11 KB/s even 8 MiB cannot finish in 600 s):

   ```sh
   python3 scripts/inference_probe.py --url https://<mini>.<tailnet>.ts.net --key-file private/inference/inference.key --upload-bytes 1048576
   docker compose $F exec -T enricher-mini python3 -I - --url https://<mini>.<tailnet>.ts.net \
     --key-file /run/secrets/inference/inference.key --upload-bytes 1048576 < scripts/inference_probe.py
   ```
   Each prints one line `inference upload bytes=N seconds=S kbytes_per_s=R status=<code> refusal=<code|-> valid=yes|no`
   and exits 0. Only `valid=yes` (422 `audio_unreadable`: the whole body was read, then refused) is a rate; anything
   else (408 from the mini's `upload_seconds`, 429, 403, 503 `upload_capacity` or `draining`, 502 from Serve, a probe
   `timeout`, an early 422) is `valid=no` with its status. The seconds start after the TCP and TLS connect and end with
   the reply, so the rate (1 KB = 1000 bytes) is the endpoint's effective throughput including the mini's body read, not
   raw link capacity. The probe's default timeout is 300 s (`--timeout`).
4. How to read the two rates: the container below a third of the host points at the container path; both slow points at
   the link or at the mini's receive side (both probes end at the same receiver) and leaves bytes (ANYM-696) as the only
   certain lever; both fast while real uploads crawl points at scheduling or inference on the mini (its busy state,
   `Between`, the access log `seconds=`; a real request is also scheduled and inferred), which the probe cannot show.
5. Before the container path is called the cause, record in the same hour, read-only and header-only, for 60 s during the
   container probe: `tcpdump -nn -s 96 -i tailscale0 'host <mini ip> and (icmp or tcp[tcpflags] & tcp-syn != 0)'` (the
   SYN's MSS, any ICMP fragmentation-needed, no payload) and the `TcpRetransSegs` delta from `nstat`. Without it the
   outcome is `inconclusive`. Put the two rates, the evidence, the date and the hour on the card once.
6. Only a confirmed container path is fixed: `com.docker.network.driver.mtu: "1280"` on `inference_egress` in
   `deploy/compose.enrichment.yaml` (bridge name unchanged), applied by stopping the worker with the normal `stop`,
   recreating that network and starting it, under the section 2 gates with an Astra-reviewed plan; undo is the previous
   compose file and a recreate. Repeat the container probe in the same hour: at least 5 times the earlier rate, else
   revert and record `inconclusive`.

Requeue after a timeout storm (the agent that releases it, under the section 2 gates; run it once the fix of the bytes
or the link is live, or the notes fail again). Read, through the host's documented database access:

```sql
SELECT count(*), error_code FROM attachment_enrichment
 WHERE state = 'failed' AND provider = 'deedkit_inference' GROUP BY error_code;
```

The error codes alone do not say which rows timed out: `model_http_error` is every non-2xx answer of the mini (its 408
`upload_timeout`, but also a 422 for an unreadable file) and `retry_exhausted` is a catch-all. So the cohort is frozen
first, as a positive list, and only that list is requeued.

1. Name the storm window (first and last failure, from the mini's access log and the worker log) and freeze the
   candidates, with their pre-update state, into the operation record (a file, no content):

   ```sql
   SELECT e.attachment, e.state, e.error_code, e.attempts, e.updated_at, a.mime, a.bytes
     FROM attachment_enrichment e JOIN attachments a ON a.id = e.attachment
    WHERE e.state = 'failed' AND e.provider = 'deedkit_inference'
      AND e.error_code IN ('model_http_error', 'model_request_failed', 'audio_transcode_size_limit', 'retry_exhausted')
      AND e.updated_at BETWEEN :storm_start AND :storm_end
    ORDER BY e.updated_at;
   ```
2. Keep a row only with per-row evidence that its last attempt was an upload or transcode failure. Only
   `audio_transcode_size_limit` qualifies by its code (the transcode produced a file over the limit; nothing was sent).
   `model_request_failed` is also the code of a malformed JSON answer and of any transport error unrelated to the
   upload, so it is ambiguous like `model_http_error` (every non-2xx answer) and `retry_exhausted` (a catch-all): each
   of those qualifies only when the worker log shows an upload timeout for that attachment's last attempt, or the
   mini's access log (read-only) shows a 408 `upload_timeout` for that request in the window. A size or rate estimate
   (the bytes against what the measured rate could carry in `upload_seconds`) is a hint for where to look, never
   evidence. Every other row (an unreadable file, an auth refusal, an engine failure, an unattributed row) is left out
   and listed with its reason. The kept rows are the frozen cohort: their ids and their `updated_at` values from step 1.
3. Requeue exactly that cohort, in one transaction, guarded by the state the freeze saw:

   ```sql
   BEGIN;
   UPDATE attachment_enrichment e SET state = 'queued', attempts = 0, next_due = clock_timestamp()
     FROM unnest(:ids::text[], :stamps::timestamptz[]) AS frozen(attachment, updated_at)
    WHERE e.attachment = frozen.attachment AND e.updated_at = frozen.updated_at
      AND e.state = 'failed' AND e.provider = 'deedkit_inference'
   RETURNING e.attachment;
   COMMIT;
   ```
   The guard makes it repeat-safe: a row that has failed again, or was requeued and processed meanwhile, has a newer
   `updated_at` and is not matched, so its attempts are never reset twice. Compare the returned ids with the frozen
   cohort and record both.

Undo: a row that fails again returns to `failed` by itself; nothing else changes. The profile's daily job budget still
applies, so a large cohort can take several days. Track each returned id until it has a stored transcript or
description or a recorded disposition (failed again with its code, or still queued with the budget spent); the failed
count falls the moment the rows are queued and proves nothing. A queued or leased row is open evidence.

## Recordings (ANYM-711)

`deploy/compose.recordings.yaml` runs the optional recordings backend (Omi and PLAUD reads,
[services/recordings/README.md](../../services/recordings/README.md)) as `recordings-api`. Merging and promoting the
image enable nothing: the service needs the private configuration directory, a gateway policy grant, a first
start by hand and an enrollment release, all of them under the section 2 gates and none of them during an active
call. The container joins no `integration` network. The gateway reaches it over the internal network
`deedkit-recordings-backend` (bridge `dk-recbackend0`, gateway mode `isolated`, no host address), and its one
way out is the filtered bridge `dk-recordings0` (ANYM-712; the host rules come first). Archive polling, audio
links and the archive backup are separate gates (docs/DATA-POLICY.md).

**Private directory** (`RECORDINGS_STATE_DIR`, outside Git, mounted read-only as a DIRECTORY at
`/run/deedkit/recordings`, so an atomic replace inside it reaches the running service): mode 0700, owner
10001, holding `config.json`, `cursor.key`, `omi.key`, `plaud.key` and the per-agent backend key files, every
file a regular file of mode 0600 owned by 10001. Paths inside `config.json` are the in-container ones;
`deploy/recordings.example.json` is the synthetic structure (token files under `/run/deedkit/recordings/`);
`deploy/recordings-archive.example.json` adds the archive (under `/data/archive`, never a `/private/...` path of the
README example) and each account's `archive_namespace`, and is used only after the archive gates pass. The owner
writes the provider keys by hand; the agent only checks modes and that a file is not empty.

**Omi backfill sizing (ANYM-724).** The archive example sets `max_bytes` to 4 GiB (about 1.33 GiB of SQLite pages: all pages
count toward the one-third page cap) and the collector's `omi_budget` to 18 transcripts and 45 reads per hour (agents keep the
rest of Omi's 25/60). Activation sets the real `max_bytes` from the first discovery count: read `archive_status`
(`traversal.discovered_total`, `projected_db_bytes`, `capacity_warning`, `usage`) after the first hour and size
`max_bytes = 3 x projected_db_bytes`, plus `free_reserve_bytes` of free space. Schema 3 is additive and applies when the
single writer starts; the backup tooling must accept schema 3 first (ANYM-714). Archive polling stays off until the ANYM-731
activation milestones.

**Network confinement (ANYM-712).** `recordings-api` joins only `recordings-backend` (internal) and `recordings-egress`
(bridge `dk-recordings0`, no IPv6); no other service joins the egress bridge. `scripts/recordings_egress_check.py` prints the
host rules (`rules --resolver IP`, with `--undo` for the exact inverse): the chain `DEEDKIT-RECORDINGS` allows UDP and TCP 53
to the host resolver, drops `0.0.0.0/8`, `10/8`, `100.64/10` (tailnet), `127/8`, `169.254/16` (metadata), `172.16/12`,
`192.0.0/24`, `192.168/16`, `198.18/15`, multicast and reserved, allows TCP 443 and drops everything else. It is hooked twice,
in `DOCKER-USER` and in raw `PREROUTING` (Tailscale's `ts-forward` accept can precede `DOCKER-USER`; the raw hook runs before
conntrack and cannot be passed over by a reorder), plus `INPUT` drops for `dk-recordings0` and `dk-recbackend0` (no route to the
host itself) and IPv6 drops in all three places. It never edits the inference or delivery rules.

The internal backend bridge `dk-recbackend0` also needs a rule: peers on it reach each other, and the gateway listens there on
0.0.0.0, so without one the recordings container could open a connection to the gateway. The filter chain therefore starts
with three rules for traffic entering from `dk-recbackend0` (a third `DOCKER-USER` hook): replies (conntrack
`RELATED,ESTABLISHED`) pass, a NEW connection to the service port 13234 passes (the gateway's requests), everything else is
dropped. The raw chain does not hold them (no conntrack before raw). These iptables rules see same-bridge traffic only with `br_netfilter`,
which is not loaded on this host (kernel 6.8, Docker 29.1.3; read 11-05), so their counters stay 0 and the isolation that works here is the bridge-family nft table `deedkit_recbackend` (unit `deedkit-recbackend-nft.service`, ANYM-713). `verify` compares rule text and does not see that table. Read-back: `check --blocked GATEWAY_BACKEND_IP:13240` from the container is
blocked, while a gateway request to recordings-api still answers.

Persistence follows "Host firewall persistence (ANYM-693)": the filter blocks go into the `deedkit-recordings` block of
`/etc/ufw/before.rules` and `before6.rules` (`rules --resolver $RECORDINGS_RESOLVER --format ufw --family v4|v6 --source FILE`
merges it after the delivery block), the raw half is a function of the ONE generated `/etc/ufw/after.init`
(`rules --resolver $RECORDINGS_RESOLVER --mini-ip $INFERENCE_MINI_IP --format after-init`; the inference script prints the same
text with `--recordings-resolver`). `RECORDINGS_RESOLVER` (the host's DNS resolver the container uses, IPv4) is a new line of
`/opt/personal-os-probe/.env`; with it set, both `verify` commands expect the composed hook, without it the hook is the
inference-only text of before. `recordings_egress_check.py verify` prints `ok` or `<pair> <code>`; the boot receipt gets a
`recordings` key (`not_installed` until the resolver is configured, then it decides `ok`).

Order for the root activation (the plan is Astra-reviewed, none runs during an active call): install the updated
`firewall_persist.py`, `inference_egress_check.py`, `recordings_egress_check.py` to `/usr/local/lib/deedkit/` FIRST (the old
inference verifier would read the composed hook as `table_differs:raw`); then the ANYM-693 "Safe order" steps with the new
block and hook; `verify` of all three scripts must print `ok`; only then start `recordings-api`. To remove: stop
`recordings-api` first, then run the rules' `undo` (it removes only the recordings rules and the composed hook goes back to
the inference-only text, with `RECORDINGS_RESOLVER` removed from the env file).

**DNS.** The overlay sets `dns: [$RECORDINGS_RESOLVER]` (required), the same address the host rules allow: Docker's embedded
resolver then queries that server from the container's namespace, where the bridge rules apply; without `dns` it would
use the host's inherited resolvers from the host namespace, outside them. Read-back: `getent hosts api.omi.me` works in the
container and a query to any other resolver (`check --blocked-dns IP`) gets no answer.

**Boot order and budget.** The recordings half of `after.init` runs LAST, after the inference half has loaded and just before
the record is written, so it cannot delay, shorten or break the inference half, which runs exactly as before under the
hook's unchanged 60 s bound. When inference fails or is slow, the recordings rules are not loaded in that boot: the failure
is in the raw-step record, in `verify` and in the receipt, and `recordings-api` refuses to serve without them (admission,
fail closed). The recordings half is a child of the hook under its own bound (`DEEDKIT_RECORDINGS_TIMEOUT`, 1-10 s, default
8 s, plus a 2 s kill grace). GNU `timeout` puts each command in its own process group, so the child gets the same value as its
per-command limit and `/dev/null` for stdin, stdout and stderr, and reports by exit status (11 restore, 12 and 13 the hook),
never by captured output. An overrun records `hook_recordings_124` (or `restore_recordings_v4` when the command's own limit,
the same value, ends first), a restore or hook failure `restore_recordings_v4` or `hook_recordings_v4|v6`, in the same
raw-step record. The hook still exits 0.

**Egress admission.** The overlay sets `RECORDINGS_EGRESS_ADMISSION=required` and requires `RECORDINGS_EGRESS_BLOCKED` and
`RECORDINGS_EGRESS_OPEN` (comma-separated, in the project's env file; Compose refuses a missing one). Before the CLI binds its
port it probes them from inside the container: every blocked `IP:PORT` (a known host listener, a tailnet address with a known
listener, the gateway's listener; `dns:IP` for a DNS query) must get no answer (timeout or no route; a refusal fails, since a
DROP never sends one), every open target (`api.omi.me:443`, `dns:<RESOLVER_IP>`) must answer. Otherwise it exits 78 and
`restart: unless-stopped` loops only this service; the gateway returns its recordings tool error, nothing else depends on
it. This covers boot, promotion and any restart. Source runs and tests leave the variable unset and skip it. The probes send
a TCP handshake and one DNS query, never a token.

**The canary proves the raw layer.** `RECORDINGS_EGRESS_BLOCKED` must name `192.0.2.254:9` (TEST-NET-1, RFC 5737: never
routable, no listener anywhere); admission refuses to start without it. The raw chain drops that destination first (the probe
times out), the filter chain REJECTs it with a reset. In a boot where inference failed or was slow, ufw has loaded the filter
rules but the recordings raw rules were skipped: the canary then gets a refusal instead of a timeout and the start is refused,
so `recordings-api` cannot serve without the raw layer (the one that survives a tailscaled `ts-forward` reorder) in that boot.
Example: `RECORDINGS_EGRESS_BLOCKED=192.0.2.254:9,<host listener>,<tailnet listener>,<gateway listener>` and
`RECORDINGS_EGRESS_OPEN=api.omi.me:443,dns:<resolver>`.

The production read-back runs `check` from the real two-network container (`docker compose $F exec -T recordings-api python -I -
check --open api.omi.me:443 --open-dns RESOLVER --blocked HOST_BRIDGE_IP:PORT --blocked TAILNET_IP:PORT --blocked
GATEWAY_IP:PORT < scripts/recordings_egress_check.py`; every blocked target needs a known listener, or "blocked" could mean
that nothing listens; `listen` in a sibling container is the positive control).

*Residual, documented:* the bridge does not pin hostnames. Any public HTTPS host is reachable by the container; host pinning
rests on the adapters' fixed origins (`api.omi.me`, `mcp.plaud.ai`) and refused redirects and proxies (`trust_env=False` in
`omi.py` and `plaud.py`, with their tests) until a proxy-capable adapter exists. Follow-up card candidate: a hostname-allowlisting
egress (a proxy or DNS-level allowlist) for the recordings bridge.

**First publication (owner action, once).** GHCR creates the new package `deedkit-recordings` private. The host has
no GitHub token and `production.py` reads the registry anonymously, so before the first start an organization owner
sets the package public in its package settings (as for the other packages, "CI-published images"), and an
anonymous check passes: `docker logout ghcr.io`, then `docker manifest inspect ghcr.io/gosync-inc/deedkit-recordings:<sha>`
answers. Until then promotion cannot resolve the image; changing visibility is the owner's.

**First start by hand** (`production.py` cannot start a service that is not enrolled: `snapshot` needs one
running container). `F` is `-p deedkit-recordings -f /opt/deedkit/deploy/compose.recordings.yaml` with
`RECORDINGS_STATE_DIR` and `DEEDKIT_VERSION` set. With `<sha>` a green `main` SHA whose `publish` job finished:
1. `gh attestation verify oci://ghcr.io/gosync-inc/deedkit-recordings:<sha> --repo GoSync-Inc/deedkit
   --signer-workflow GoSync-Inc/deedkit/.github/workflows/ci.yml --source-ref refs/heads/main --source-digest <sha>
   --deny-self-hosted-runners` (see "CI-published images"), `docker pull` that digest and
   `docker tag <digest ref> deedkit-recordings:enroll-<sha>` (a new tag, never `--build`).
2. Once: `docker network create --internal -o com.docker.network.bridge.name=dk-recbackend0 -o
   com.docker.network.bridge.gateway_mode_ipv4=isolated deedkit-recordings-backend`.
3. `DEEDKIT_VERSION=enroll-<sha> docker compose $F up -d --no-build --pull never` and read `docker ps` health
   (healthy after about 30 seconds).

**Gateway.** Upgrade the gateway to a release that knows the module before the policy names it (an old gateway
rejects the module name). The policy gets, in one atomic replace with a higher `revision` and a backup first:
`deployment.modules` + `recordings`, `deployment.backends.recordings.url` = `http://recordings-api:13234`, the
profile's `recordings_capabilities` / `recordings_list` / `recordings_read` tools, per principal
`credentials.recordings` = `{key_file: /run/deedkit/keys/recordings-<agent>.key, key_sha256}` (the key file is
0600, owned by 10001, in the gateway keys directory) and `grants.recordings` = `recordings:read` +
`recordings:content` (no `recordings:audio`). A backend URL change needs a gateway restart.

**Enrollment is a hand release**, modelled on the delivery enrollment (step 5 of "Apply and verify"). The host
lists `deploy/compose.recordings.yaml` as a new project `deedkit-recordings` (the Compose project name of the first
start; `snapshot()` finds the running container by that label) with `"services": {"recordings-api":
"recordings"}` in `/etc/deedkit/production.json` ONLY (plan B accepts exactly two services, the API and the
gateway), and `deploy/compose.gateway-recordings.yaml` after `compose.gateway.yaml` in the gateway project's
`files` in BOTH `/etc/deedkit/production.json` and `/etc/deedkit/http-production.json`, so the promoted HTTP gateway
generation joins `deedkit-recordings-backend`. The change makes the recorded configuration digests differ, so
autodeploy [halts](#halts) at "apply the next release by hand". Sequence: create the pause file; back up and edit both
configuration files; `production.py prepare --sha S` and `apply --sha S` (plan A); `seamless_release.py prepare
--sha S` and `promote --sha S` (plan B); `status`; remove the pause; `production.py deploy --dry-run` must report
the new state current (a dry run with the pause file present returns `paused` before it checks drift). Plan A apply
restarts the selected services of the enrolled projects as in any release.

**Enrollment (ANYM-745).** The hand release above is declared by a reviewed manifest, `deploy/modules/<module>.json`
(first one: `recordings.json`), and checked by one read-only command that prints the exact edits:

```sh
sudo python3 /usr/local/lib/deedkit/production.py enroll --module recordings --sha <green main sha> --plan
```

The tool reads the manifest from the verified main commit in its source mirror, never from a working tree. It writes no
production state (its scratch copies live in private temporary directories) and prints no secret: env files, private
configurations and key files are read for presence, mode, owner and digests, never printed. Exit 0 prints the plan; exit 2
prints `{"status": "refused", "refusals": [{"code", "detail"}]}` with one entry per failed precondition. Applying and
undoing a plan is `--apply` and `--undo` (ANYM-746, below); the hand release above stays the fallback.

*Manifest.* Exactly these keys, nothing else: `name` (the file name); `project` (`name`, `files` as repository paths under
`deploy/`, `env_file` as an absolute host path, `env_required` variable names, `services` as service to build target,
optional `networks`: the internal Compose networks the first start creates once, each `name`, `bridge` and, for a network
the gateway joins, `gateway_port`, the port the isolation probe checks on every serving gateway; a module whose
networks exist already declares none, and a new manifest key reaches a host only after the tool that reads it is
installed, because an older tool reads an unknown key as a malformed manifest and `deploy` pauses);
`gateway` (`overlay` a repository path or null, and `policy`: `module`, `backend_url`, `tools`, `principals` with their
`grants`, `key_dir` as an absolute host directory, `key_file_pattern` with `{principal}`, `key_uid`); `egress`
(`outside` false, or true with `admission_env` (`blocked` and `open` variable names), `canary`, `verify` (a command whose
first word is a repository script `scripts/*_egress_check.py`), `verify_env`, `open` targets, `blocked` (the address
classes `host` and `tailnet` that need a blocked target with a known listener) and `controls` (`dns_env`, the variable
whose resolver must be an open `dns:` target: the positive control)); `private_config` (`mount` the container directory
the Compose file mounts, `file` its configuration file, `forbidden_keys` the key paths that must be absent at first start,
`credential_files` the key paths whose values are provider credential files under that mount); `owner_steps` (strings the plan prints); `live_actions` (always empty). Secrets never
live in a manifest.

*Refusal codes.* Manifest (one cause each): `manifest_missing`, `manifest_invalid`, `manifest_unknown_key`,
`manifest_repo_path` (an absolute, `..` or non-`deploy/` repository path), `manifest_relative_path` (a host path that is
not absolute), `manifest_forbidden_service` (`db`, `calendar-db`, `database`, `postgres`, `operator`),
`manifest_grant_wildcard` (a grant containing `*`), `manifest_unknown_principal` (a principal the live policy does not
define), `manifest_egress_incomplete` (`outside` true without admission settings), `manifest_verify_script` (a verify
script outside `scripts/` or not ending in `_egress_check.py`), `manifest_live_actions` (a non-empty list). Plan:
`sha_not_verified` (not a green main commit, `promotable`), `sha_already_prepared` (a candidate directory of that SHA
exists), `module_enrolled` (the project is already in the configuration), `policy_module_present` (the live policy already
names the module), `no_current_release`, `gateway_project_missing`, `source_unavailable` (a file is not in the commit or
the gateway project's first file cannot be read), `range_sql_inventory` (the range from the current release changes SQL
or migrations), `range_tool_pending` (a deploy tool not installed yet), `range_backup_changed` (the backup runner or a
backup unit changed), `env_file_missing`, `env_file_mode` (not a regular file of mode 0600 owned by root),
`env_variable_unset` (a required variable is missing or empty), `compose_config_failed` (`docker compose config` of the
project, or of the gateway project with the overlay, with exactly the files and env file the tool will record),
`call_active`, `calls_unknown`, `paused` (the autodeploy pause file exists), `egress_env_missing` (the canary or an open
target is not in the env file), `egress_verify_failed` (the verify script did not print `ok`), `key_file_invalid` (a
gateway key file is missing, not a regular file, not mode 0600, not owned by `key_uid`, or empty), `policy_unreadable`,
`policy_check_failed` (the candidate is not additions only, or `deedkit-gateway --check` fails in any serving gateway
image: every distinct image of a running `gateway` or `http-gateway-*` container), `private_config_unreadable` and
`private_config_forbidden_key`, `credential_file_invalid` (a file named at a `credential_files` path is missing, outside
the mount, not a regular file, readable by group or other, not owned by the configuration's owner, or empty: mode, owner
and size only, never a byte), `egress_target_invalid` (a blocked target that is not `IP:PORT`),
`egress_blocked_incomplete` (no blocked target in a class the manifest requires), `egress_control_missing` (the open
targets lack `dns:<resolver>`) and `command_timeout` (a command outside the probes exceeded its time limit; a probe that
times out refuses with its own code). The verify script gets a private `--env-file` holding exactly the `verify_env`
values this plan checked, never the process environment. A malformed manifest the range does not change is not parsed by a
deploy, and a deploy gates the range of the release it selects, not the tip of main. The `private_config_forbidden_key` check reads the private configuration the container will actually mount (resolved
from the Compose document, so a changed `RECORDINGS_STATE_DIR` selects another file) and refuses when a forbidden key,
for recordings `archive` or `accounts[*].archive_namespace`, is present: a first enrollment is on-demand reads only
(docs/DATA-POLICY.md). Only the presence of a key is read, never a value.

*Plan.* The overlays to install (repository path, host path in the gateway project's directory, sha256, git blob id, owner,
group and mode copied from the gateway project's first file); the project object appended to `production.json` only (plan B
accepts exactly two services); the gateway project's `files` with the overlay in both `production.json` and
`http-production.json`; the `env_files` entry; the candidate policy (revision plus one, additions only, listed); the owner
steps. The output ends with `plan_digest`, the sha256 of the SHA, the manifest blob id, both configuration digests, the live
policy digest, every overlay digest, the grants, the verify and canary values, the content digests of the env file and of the
mounted private configuration, the digest of the candidate policy (so a replaced key changes it) and the digests of the resolved Compose documents (the
project, the plan A gateway and the HTTP gateway, each in its own directory with its own files). Recomputing the plan gives the same digest
only while none of them changed; `--apply` refuses a different `--plan-digest` (`plan_changed`).

*Apply (ANYM-746).* `enroll --module <module> --sha <sha> --apply --plan-digest <plan_digest>` performs exactly the plan a
reviewer approved, as root, through the release machinery `deploy` uses. Refusals (exit 2, nothing written) are the plan's
codes plus `plan_changed` (the recomputed digest, or the manifest, differs), `enrollment_unfinished` (an
`enrollment.json` that is not undone, or an unfinished apply or HTTP attempt, exists), `configuration_changed` (the
configuration drifts from the current release before the run) and `active_call`. An external network the manifest does not
declare, with or without `name:`, or a declared network that exists and is not internal is refused as `network_unsafe` before the pause, the receipt or any file is written (`--plan` reports the undeclared one too). Under the host locks and the backup wait
(ANYM-744: exit 75 after the bound, nothing written) the order is: the plan is recomputed and compared; the tool writes the
pause (its own marker); copies `production.json`, `http-production.json` and the live policy into a root-only directory
`enrollment-<module>-<sha12>` of the state directory (sha256 in the receipt `enrollment.json`, mode 0600: digests,
revisions and step names only); installs the overlays by atomic rename from `git show <sha>:<path>` with read-back
(`overlay_conflict` when the host path holds other content); creates a missing network of the manifest once (`--internal`,
its bridge, isolated); tags the attested image `deedkit-<target>:enroll-<sha>` (never `--build`); re-reads the private configuration immediately before `up`
(`private_config_forbidden_key`) and requires the resolved admission to be `required` (`egress_admission_not_required`);
`up -d --no-build --pull never`, health, and for outside egress the confinement check from inside the container with a host
positive control per blocked target but the canary (`confinement_check_failed`); edits both configuration files and the
policy atomically (`deedkit-gateway --check` first); plan A through `deploy_plan_a` (its call wait, health wait and automatic
`rollback`); plan B `prepare` and `promote`; `status`; the isolation probe of every serving gateway generation on a
gateway-joined network (`isolation_check_failed`; the addresses are probed, never written); and last the real
`configuration_drift` and the release checks. Only if they report the new release current and the pause is still the
marker this run wrote, the tool removes it. The calls are checked immediately before the first start, before the
configuration and policy writes and before plan A (`active_call`: the run stops there, the pause stays). It never retries an
uncertain step. The capabilities read-back through the gateway needs a principal key and stays the owner-confirmed first read
of the module's card.

*Failure.* A failure before plan B's promotion rolls back plan A through `rollback`, restores the configuration files byte
for byte and the policy with a revision above anything a gateway may have loaded (never a lower number), stops only the new
module (`docker compose stop`, never `down`, never a database), keeps the pause and writes the failing step to
`enrollment.json` (`status: stopped`). A recovery that fails says `production state is unknown, inspect it` and changes
nothing more. Plan A's own snapshots are prepared from the edited configuration, so after `rollback` the tool recreates the
gateway from the resolved configuration and image it ran before the run (kept in `gateway.pre-run.json` beside the copies,
after a call wait) and requires every existing project to run its pre-run image, else the state is unknown. Plan B has no rollback of its own: a failure at or after promotion changes nothing more
(any failure of the isolation step, a negative probe or an error reading Docker, still stops the module), keeps the pause, records the failing step and the HTTP status it read in `enrollment.json`
(`status: http-manual`) and says `HTTP recovery is manual, inspect it` (seamless-release.md); automatic recovery of the
previous HTTP generation is a separate card. A crash leaves the pause and the receipt: the next `--apply` refuses
(`enrollment_unfinished`) until `--undo` or an inspection clears them. One receipt serves one enrollment at a time.

*Undo.* `enroll --module <module> --undo` (under the host locks and the backup wait, as the module's backup runs inside its
container; after the call check; `enrollment_not_applied` unless the receipt says
`applied`; `configuration_changed` or `policy_changed` when a file differs from what the apply wrote, which is then undone
by hand) restores both configuration files from the copies byte for byte (sha256 equal), installs the backed-up policy with
a higher revision (it removes the module), stops the module's services and removes nothing else (overlays, networks and
the enroll tag stay). Autodeploy stays paused: a SHA prepared with the module can never be prepared again, so deploys
resume at the next newer green main SHA once the pause is removed.

*Review sequence.* The manifest pull request is reviewed as code like any change under `deploy/` (the review-protocol
signal grep already matches `deploy/modules/`; Astra r1 when it adds grants or outside egress). After it merged, the plan
output for the real host is a production operation plan and gets its own Astra r1 (harness reviews.md, Rounds 7); the apply
step is bound to the approved digest.

*Autodeploy.* When a deploy finds a manifest in its range for a module the host does not run, or a configuration that
differs from the recorded digests only by an enrollment, it pauses with `enroll_required: <module>: production.py enroll
--module <module> --sha <sha> --plan`. It never enrolls by itself: restarts, firewall and access are not for a timer. A
changed manifest that does not parse, of a module whose manifest at the range's base names a running project, pauses with
`manifest_invalid: <module>: fix deploy/modules/<module>.json; enroll is not needed` (`manifest_code` in the pause file is the fixed reason).

*What stays with a person.* The owner writes provider tokens by hand and makes the registry package public after its first
publication; backend and gateway key files are an agent step under the section 2 gates, only checked here; root firewall
activation stays its own gated step (the plan runs the read-only verify and refuses unless it prints `ok`); the first real
read through a provider is the owner-confirmed live read of the module's card. Sends, calls, ACKs, pairing, provider-session
changes and allowlist changes are never part of a manifest.

**Undo by hand** (the tool's `--undo` above does the same): remove the module, tools, grants and credential from the policy (new revision); stop
`recordings-api` only (`docker compose $F stop recordings-api`) and leave autodeploy paused, which is the
holding state; at a NEWER green `main` SHA (a docs-only pull request creates one) restore both configuration backups
and repeat the hand release sequence above (a SHA prepared with recordings can never be prepared again); `deploy
--dry-run` must report current without a drift halt; then `docker compose $F down`, remove the
network `deedkit-recordings-backend` and the enroll tag; the owner revokes the provider keys in Omi and PLAUD.

**Operations on the running service.**
- *Add an account:* add it to `config.json` (atomic replace inside the directory) with its token file; a new
  provider identity gets a new `archive_namespace` when it is archived.
- *Rotate a token:* replace the token file (atomically, 0600); the service re-reads it on every provider call, so no
  restart is needed; keep the archive namespace.
- *Replace an account:* a different provider identity under a reused local account ID needs a new
  `archive_namespace`; remove the old account first.
- *Read archive health:* `docker exec` into `recordings-api` and send `POST /v1/archive_status` for an account with an
  operator backend key; it returns counts, last success and fixed error codes and contacts no vendor.

**Recordings archive backup (ANYM-727).** `deedkit-recordings-backup.timer` runs the daily job at 21:30 UTC and
`deedkit-recordings-backup-catchup.timer` the catch-up at 23:15 UTC (from 23:00, only when the last good bundle is older
than 22 hours); the units and `deploy/backup-runner-recordings.example.json` are installed by ANYM-731, not by a deploy.
Staging: create `/opt/deedkit/recordings-backup/staging` by hand, owner 10001:10001, mode 0700 (the runner refuses
`recordings_staging_unsafe`), and mount it at `/run/deedkit/backup-staging` in the recordings container
(`deploy/compose.recordings.yaml`, `RECORDINGS_BACKUP_STAGING`). Admission: the runner starts only when its worst case
ends 15 minutes before the next start of another profile (worst case plus the unit's two stop budgets, read from their
timer files; a start already due within its randomized delay plus 120 s is a veto); else `recordings_skipped_window`
(also `_schedule_unknown`, `_yielded`, `_lock_busy`, `_disk`, `_container_down`, `_archive_disabled`; all are status
`skipped`, exit 0, with nothing left in staging). The expected archive size (`expected_archive_bytes`, default 1 GiB) plus
`min_free_bytes` must be free; the status file records the real `archive_bytes`. Archive polling and this timer are
independent: with the archive off the job skips `recordings_skipped_archive_disabled`. Read-back after installation:
`systemctl list-timers deedkit-recordings-backup.timer`, the status file, and an empty staging directory. Undo:
`systemctl disable --now deedkit-recordings-backup.timer deedkit-recordings-backup-catchup.timer`. BACKUP.md "Staging,
slot and admission" has the full rule. The ordered installation (runner blob and pins, receiver instance, units, first skip), the bounded archive activation, the first protected snapshot and the restore drill are [recordings-archive-activation.md](recordings-archive-activation.md) (ANYM-731).

### Omi webhook receiver (ANYM-760)

**Status (11-08): the public route is not enabled yet; polling alone collects.** The 11-05 attempt ran steps 1-6 of an earlier form of this plan (receiver and backend on a shared bridge `dk-rechint0`, isolation by a host bridge table) and stopped before the route: a review found two blocking gaps that came from the shared networks themselves (with the table gone the receiver could reach the backend's main listener and the proxy's 80 and 443; binding the main listener around the hint address made polling depend on name resolution). The fix (11-08, `Spec: ANYM-760`) removes the shared networks: the receiver has no network at all, and both of its paths are Unix socket files. "From the 11-05 state" below is the way from what runs on this host now.

The ingress plan, reviewed with the code. Merging and deploying change nothing: without `archive.omi_hint` and `RECORDINGS_HINT_LISTEN`
no listener exists, and the receiver and relay containers exist only in `deploy/compose.recordings-hint.yaml`. Every step below is an agent step
under process.md section 2 (backup of the affected files, read-back, undo) after merge and deploy, none during an active call, except
the Omi app step, which is the owner's. Polling stays the source of truth throughout: every state of this plan leaves collection as it is.

**Route.** One public route, `POST /omi/memory/<token>`, on the existing proxy's existing host. The proxy forwards one prefix and nothing
else of recordings, and has no secret in the proxy configuration (the token is the path suffix, checked by the receiver against its stored
digests). The Caddy form inside the site block, with the body cap the receiver enforces again:

```
handle /omi/memory/* {
    request_body {
        max_size 4MiB
    }
    reverse_proxy recordings-omi-hint:13236
}
```

The proxy's log carries no path: `deploy/Caddyfile.example` lines 2-13 delete the request fields. That is the example, not the running proxy,
and a running container proves neither its log filter nor a restart path (the ingress note under "Apply and verify"). So before enabling read
the live configuration back, and run the **proxy-log canary**: one `POST` to `/omi/memory/<43 random characters that are no token>` answers
404, and `docker logs --since 5m <proxy> 2>&1 | grep -c <those characters>` prints 0. A missing filter or a nonzero count stops the plan.

**Topology.** Omi → proxy → (`deedkit-recordings-hook`, bridge `dk-rechook0`) → relay `recordings-omi-relay`, alias `recordings-omi-hint:13236` → Unix socket
`receiver.sock` (tmpfs volume `recordings-hint-hook`) → receiver `recordings-omi-hint` → Unix socket `channel.sock` (tmpfs volume `recordings-hint-channel`) →
the backend's second listener, `POST /omi-hint`. Containment is the topology, not a filter:
- The receiver, the process that parses what the internet sends, runs with `network_mode: none` (loopback only). It cannot open a TCP connection to the backend,
  the proxy, the gateway or the host whatever the host's filters, kernel modules or resolvers do: it has no route. It reaches the channel by connecting to a socket file
  on a volume it mounts read-only (it can neither plant a file there nor replace the socket), and it serves on a socket file only the relay reads.
- The relay is the only part on a network the proxy shares. It copies bytes between one TCP connection and the receiver's socket: no configuration, no secret,
  nothing parsed, at most 64 connections, each at most 5 seconds and 4 MiB + 64 KiB in, 16 KiB out (`hint.serve_relay`). Its only mount is the hook volume, read-only.
- The backend joins no new network: it gains the channel volume and `RECORDINGS_HINT_LISTEN=/run/deedkit/omi-hint-channel/channel.sock`. Its main
  listener stays where it was, and nothing about it depends on a name, a filter or the hint path: a channel socket that cannot be opened leaves the backend and
  polling running (`listener_error` in the status).
- Both volumes are tmpfs, owner 10001, mode 0700, 64 KiB; the sockets are 0600. The paths are constants in `hint.py`, because the promotion tool refuses a
  mounted path in a command.
The receiver has no volume but its own private directory and the two socket volumes, no provider token, no archive, read-only root, non-root, all capabilities dropped.
The hook network is internal, gateway mode `isolated`, no host address, no IPv6; only the proxy and the relay join it.

**Host rules and bridge table (the hook network only).** The receiver's containment needs neither: they are a second layer on `dk-rechook0`, where the relay
and the proxy meet. `scripts/recordings_egress_check.py hint-rules` prints the chain `DEEDKIT-RECORDINGS-HINT` (a NEW connection on `dk-rechook0` only to 13236), persisted
by `hint-rules --format ufw --family v4|v6 --source FILE` into `/etc/ufw/before.rules` and `before6.rules` ("Host firewall persistence (ANYM-693)", Safe order);
`hint-verify` prints `ok`, and `RECORDINGS_HINT_RULES=1` adds the pair to `deedkit-firewall-verify`'s boot receipt as `recordings_hint`. Without `br_netfilter` (this
host) that chain never sees same-bridge traffic: the bridge table `/etc/deedkit/rechint.nft` (table `bridge deedkit_rechint`, unit `deedkit-rechint-nft.service`) does,
with chain `to_receiver` on `dk-rechook0` passing ARP, TCP to 13236 and non-SYN segments. Its `dk-rechint0` half guarded `deedkit-recordings-hint`, which nothing joins
after this change. If the table is gone, the relay could open a connection to the proxy's 80 and 443, as anyone on the internet can; the receiver still could not.

**Secrets.** `--omi-hint-issue` makes the 256-bit path token and the channel credential, writes each to its own new 0600 file in a 0700 directory
it refuses to widen, and prints only their SHA-256 digests. Neither value is an argument, an output, a log line, a PR, a chat message or an orchestrator
record: only file paths are passed on. It needs no configuration and no network:

```sh
docker run --rm --network none --read-only --cap-drop ALL -u 10001:10001 -v /opt/deedkit/recordings-hint:/out/receiver \
  -v /opt/deedkit/recordings-hint-handoff:/out/handoff ghcr.io/gosync-inc/deedkit-recordings:<sha> --omi-hint-issue \
  --public-origin https://<proxy host> --url-file /out/handoff/omi-url --channel-key-file /out/receiver/channel.key
```

**Order.** Back up the affected files first; each read-back must pass before the next step. On a host that has none of it yet, steps 1-4 of the 11-05 form
come first (the issue command, both private directories, `receiver.json` from `deploy/recordings-omi-hint-receiver.example.json`, `archive.omi_hint` by an atomic replace,
the hook network with `docker network create --internal -o com.docker.network.bridge.name=dk-rechook0 -o com.docker.network.bridge.gateway_mode_ipv4=isolated deedkit-recordings-hook`,
the host rules and the bridge table above); this host has them.

*From the 11-05 state* (receiver and backend on `deedkit-recordings-hint`, `receiver.json` with `channel_url`, the overlay of that day in plan A's `files`, `recordings-omi-hint` in `services`):
1. Deploy a green `main` SHA holding this code (autodeploy). Under the old overlay the backend's channel stays on `recordings-hint-channel:13235` (the older form is kept for this),
   and the receiver answers every request 404, because `receiver.json` has no `channel_socket`: harmless with no route, and its healthcheck stays green.
2. Edit `receiver.json` by an atomic replace: `channel_url` becomes `"channel_socket": "/run/deedkit/omi-hint-channel/channel.sock"`. Nothing else changes; the
   path token and channel credential issued 11-05 stay (their files never left the host).
3. Hand release. The receiver and the relay are the same published image with another command, so `production.py` needs no change (should the tool ever need to
   change for this, that is a separate release-path PR). `snapshot` needs a running container of every selected service, so the relay's **first start by hand** comes
   before `prepare`, as in "Enrollment is a hand release". Let `P` be the plan A configuration, the file `production.active_config()` names (`/etc/deedkit/autodeploy.env`;
   on this host `release-20261018-r7.json`, there is no `production.json`), and
   `F` = `-p deedkit-recordings -f /opt/deedkit/deploy/compose.recordings.yaml -f /opt/deedkit/deploy/compose.recordings-hint.yaml --env-file <the env file P names for the project>`.
   In this order, each step read back before the next:
   a. Pause autodeploy first, and wait until `deedkit-autodeploy.service` is not active and `production.py status --config P` reports the deployed
      release with no candidate in progress. Only then may its inputs change: an apply already past its first checks would otherwise meet the edits below.
   b. Pick `S`: a green `main` SHA newer than the deployed one and not prepared yet (`prepare` refuses an existing candidate directory), with a clean range: pin `S`
      and check deployed..`S`. `range_changes` returns no SQL and no pending tool, and no path matches `BACKUP_FILES`; a hit means remove the pause, let autodeploy
      take that range and pick a later `S`.
   c. Verify and pull `S`'s attested recordings image and tag it `deedkit-recordings:enroll-<S>` ("First start by hand", step 1).
   d. Back up `/opt/deedkit/deploy/compose.recordings-hint.yaml` and `P`. Install `git show <S>:deploy/compose.recordings-hint.yaml` over the first (the project's `files`
      are absolute host paths; the overlay is already in them), and add `"recordings-omi-relay": "recordings"` to the project's `services` in `P` by an atomic replace.
   e. Under `flock -n` on the global, plan A and HTTP deploy locks and the backup lock, with no `.unsettled` marker:
      `DEEDKIT_VERSION=enroll-<S> docker compose $F up -d --no-build --pull never --no-deps recordings-omi-hint recordings-omi-relay`. It creates both volumes,
      recreates the receiver without a network and starts the relay. Never recreate `recordings-api` by hand: that can kill a running recordings backup.
      Release the locks, because `prepare` and `apply` take them.
   f. `production.py prepare --sha S --config P`, then `production.py apply --sha S --config P` (restarts the backend once: it gains the volume, opens
      `channel.sock` and leaves `deedkit-recordings-hint`), `production.py status --config P`.
   g. Remove the pause. Read `configuration_drift` directly and run `production.py deploy --dry-run --config P`: a dry run that answers `current` returns before
      the drift check, and without `--config` it fails on the missing `production.json`.
4. Read-backs. The receiver has only loopback: `nsenter -t <receiver pid> -n ip -o link` lists `lo` alone. The backend left the hint network: `docker network inspect
   deedkit-recordings-hint --format '{{len .Containers}}'` prints 0. The socket: `docker compose $F exec -T recordings-omi-hint ls -ln /run/deedkit/omi-hint-channel` shows
   one socket, owner 10001, mode 0600, and `touch` there fails (read-only). The channel answers through the file without a credential: from the receiver,
   a `POST /omi-hint` with no `Authorization` over that socket answers 401. Both healthchecks are healthy (the relay's goes through the receiver), and
   `archive_status.omi_hint.listening` is true. The hook side, before the proxy joins: `docker compose $F exec -T recordings-omi-relay python -I - check --blocked 192.0.2.254:9 < scripts/recordings_egress_check.py` times out.
5. Proxy: keep the reviewed manifest of "Apply and verify". Add the route to a candidate and validate it with the same image, no listener (`caddy validate`, `--network none`). The Caddyfile is a single-file bind mount, so write it in place (`cat candidate > Caddyfile`, same inode): a rename leaves the container on the old file.
   **A reload closes every WebSocket of the old configuration, and a live call's media socket goes through this proxy.** A call gate alone leaves a gap, so run the reload inside an admission exclusion, in the voice container's network namespace: a watchdog that removes the rule after 45 s; `nsenter -t <voice pid> -n iptables -I INPUT -i eth0 -p tcp --syn --dport 13842 -j REJECT --reject-with tcp-reset`; wait at most 10 s until no connection on 13842 sent or received data in the last 5 s (idle upstream keep-alives pass; media flows every 20 ms); the active-call gate; `caddy reload`; remove the rule and prove it gone. Anything still active, or a call: remove the rule and stop. New voice requests fail for those 5-10 s; MCP streams on `/deedkit/*` drop and reconnect. The rule has no host chain, so the firewall verifier does not see it. Telegram call audio uses a Unix socket, not the proxy.
   Before the proxy joins, the proxy-log canary on the upstream error path: a `POST` answers 502 and the log count is 0. Then join the proxy to `deedkit-recordings-hook` (`docker network connect`, and the same line in that manifest); no second reload, Caddy resolves at request time. Read-backs: the default route unchanged, the log filter, the proxy-log canary, a wrong-token request answers the same 404 as an unknown one, a right-token request with a wrong `uid` answers 400 (no provider is called by either; the URL reaches curl on stdin, not in argv). From the proxy's namespace, with host Python: `nsenter -t <proxy pid> -n python3 -I - check --blocked RELAY_HOOK_IP:9 --control RELAY_HOOK_IP:13236 < scripts/recordings_egress_check.py`; from the relay, `--blocked PROXY_HOOK_IP:443` and `--blocked PROXY_HOOK_IP:2019` (the bridge table's `to_receiver` drop counter moves).
6. The owner's hands (below). Then remove `/opt/deedkit/recordings-hint-handoff/omi-url`.
7. Afterwards, nothing joins `deedkit-recordings-hint`: `docker network rm deedkit-recordings-hint` and the table's `dk-rechint0` half may go (no hurry; they guard nothing).

**The owner's step in the Omi app.** Entering the URL needs the owner's device and Omi login (a Firebase user token; our `omi_dev_` key cannot do it); no agent tries. In the
Omi app (source read at the pinned commit, `app/lib/pages/settings/developer.dart:240-247`, `profile.dart:68-73`): Settings → Developer Mode → Developer Settings → Webhooks →
Conversation Events ("New conversation created"), switch it on, paste the content of the handoff file (the owner reads it in their own SSH session), Save. The agent gets the owner's `uid`
for step 4 from the owner: Settings → Account → User ID (tapping the row copies it). That place is read in source, not yet seen on the owner's device: if the owner's app does not show it, stop before step 4; per ANYM-760 criterion 8 dropping the uid factor is Astra's decision, recorded on the PR, and needs a code change. A `uid` is no secret and no factor by itself; the path token is the factor.

**What Omi sends.** `memory_created` at Omi commit `cf601889` (`backend/utils/webhooks.py:309-345`, `utils/conversations/render.py:181-228`) POSTs JSON to `<url>?uid=<uid>`, with an
`Idempotency-Key` header, unsigned; 4 attempts at 1, 5 and 30 s, no retry on a 4xx other than 408 and 429. The body is `Conversation.model_dump()` of `backend/models/conversation.py:464` with
`geolocation`, `capture_coverage` and `client_processing` removed:
`sync_content_revision`, `sync_relevance`, `id`, `created_at`, `updated_at`, `started_at`, `finished_at`, `recorded_start`, `recorded_end`, `arrival_at`, `clock`, `episode_id`, `source`, `language`,
`uses_custom_stt`, `record_audio_partial`, `structured`, `processing_state`, `summary_retryable`, `transcript_segments`, `transcript_segments_compressed`, `photos`, `audio_files`,
`conversation_audio`, `private_cloud_sync_enabled`, `audio_timeline`, `speaker_resolution`, `screenshot_sharing_enabled`, `apps_results`, `suggested_summarization_apps`, `plugins_results`,
`user_corrections`, `user_overview`, `external_data`, `app_id`, `capture_group`, `content_kind`, `discarded`, `imported`, `visibility`, `starred`, `processing_memory_id`,
`processing_conversation_id`, `status`, `is_locked`, `deferred`, `processing_deferred`, `data_protection_level`, `folder_id`, `folder_name`, `is_call`, `call_platform`, `calendar_event_id`, `geolocation_scope`,
`other_participants_location`, `call_id`, `calendar_event`, `client_device_id`, `client_platform`, `meeting_treatment_eligible`, `meeting_treatment_reason`, `meeting_duration_s`, `meeting_dedup_speech_s`.
`structured` holds `title`, `overview`, `emoji`, `category`, `sections`, `action_items`, `events`, `meeting_type`, `participants`, `insights`, `salience`, `note_claims`, `title_provenance`. Each of `transcript_segments` holds
`id`, `text`, `original_text`, `speaker`, `speaker_name`, `speaker_id`, `is_user`, `person_id`, `speaker_label_source`, `start`, `end`, `translations`, `speech_profile_processed`, `stt_provider`, `speaker_match_source`, `speaker_id_scope`, `speaker_identity_status`.
The receiver parses this in memory for the length of the request (4 MiB at most), takes `id` and drops the rest: nothing else is stored, logged, answered or forwarded, and nothing goes to a third party (docs/DATA-POLICY.md is unchanged on purpose). The only datum leaving the receiver is the validated `id`, held by the backend in memory.

**Deferred inserts.** A forced list whose page holds no known row while more pages follow inserts nothing (`archive_status.omi_hint.deferred` counts it): the regular head, which walks on, inserts them all, up to one poll later. So a hint never ends a head walk early.

**Worst cases.** A leaked URL brings forward at most 6 head lists an hour, and cannot create a record, read a transcript or put in text; these lists count in `omi_budget`, in the head share, so while a backfill holds the rest of the hour they can use the slots a regular head list would, and the forced list then does the regular list's discovery; rotate. A compromised receiver can read the conversation bodies Omi sends while it is compromised, holds the digests and one channel credential, has no network: it starts no connection but the channel socket, and cannot reach the archive, the main listener, the proxy or a provider whatever the host filters do. On the channel it holds at most 16 connections, each for at most 5 seconds (bound at the transport, before any HTTP parsing).

*Rotate:* run the issue command with new file names. Put the new path digest beside the old one in `receiver.json` and `archive.omi_hint.path_token_sha256`, the new channel digest beside the old one in `channel_key_sha256`, then atomically replace `channel.key` with the new file. The owner updates the URL in the Omi app. Remove both old digests and the old files.
*Revoke:* remove the digests: the path digests from `receiver.json` (every request answers the same 404) and the whole `archive.omi_hint` object from `config.json` (the channel answers 404). Polling goes on.
*Undo:* in this order, because `snapshot` refuses a release while a selected service is not running. Revoke (above): the receiver stays up and answers 404. Remove the proxy route (the reload as in step 5), take the proxy off the hook network and out of its manifest. By the same hand release take `recordings-omi-hint` and `recordings-omi-relay` out of `services` and the overlay out of the project's `files` (the backend loses the volume and its channel). Only then stop and remove the receiver and the relay; their volumes go with `docker volume rm` of those two names only. The collector is unaffected. Then, if wanted, run `hint-rules --undo`, remove the ufw block, the bridge table and its unit, `RECORDINGS_HINT_RULES` and `RECORDINGS_HINT_STATE_DIR`, and `docker network rm` the hook network. To go back from the socket form to the 11-05 form instead (route not yet live, or revoked first): if `deedkit-recordings-hint` was removed, recreate it with `docker network create --internal -o com.docker.network.bridge.name=dk-rechint0 -o com.docker.network.bridge.gateway_mode_ipv4=isolated deedkit-recordings-hint` and load the bridge table with its `dk-rechint0` half (`nft -c -f`, then load) before anything joins it; then step 3 a-g with the older overlay (`git show <the 11-05 SHA>:deploy/compose.recordings-hint.yaml`), `recordings-omi-relay` taken out of `services` in step d, and in step e `up -d --no-build --pull never --no-deps recordings-omi-hint` only, after `channel_url` is back in `receiver.json`. After `apply`, stop and remove the relay (it is no longer selected) and the two volumes by name.
*Receiver or relay failure:* both are selected services, so a stopped or crash-looping one stops releases (the halt names it; that halt is the alarm). `restart: unless-stopped` covers one crash. Stopped: `docker start` it once (incident recovery). Crash loop or unhealthy: revoke, then the hand release of *Undo*, which frees the release path and also closes the backend listener; polling goes on. A receiver that refuses its socket (`socket_unavailable`: the hook volume is not 0700 and its own) exits 1 and restarts; nothing else changes.
Rolling a package back: remove `archive.omi_hint` before a package downgrade (an older package refuses the unknown field, which would disable the archive consumers while on-demand reads go on).

By this plan no existing recordings isolation is weakened: `compose.recordings.yaml`, its internal networks, egress admission, the ANYM-712 rules and the no-host-address bridges stay as they are, and `recordings-egress` stays the backend's only way out. Closing evidence after the owner's step: one real new Omi conversation in the archive within about a minute of Omi finishing it, with the receiver's counters (`docker logs`: `omi_hint_receiver`, counts only), `archive_status.omi_hint` and the date on the card.

## Rotate the calendar database passwords

`scripts/calendar_rotate.py` (ANYM-784; stdlib only, as root, from the release's verified source directory
`<state_directory>/<sha>/source`) rotates `fcs_owner` (`apply owner`) or `fcs_gateway` and `fcs_worker` together
(`apply roles`). Use it after an exposure, after a restore (`--mode isolated`, before any service can reach the
restored database) or on a schedule you start by hand. It is not autonomous and has no timer. It prints
fingerprints (12 hex of sha256) only, never a value, a DSN or a file content; the same lines go to
`rotation.log` beside the journal, so a dropped ssh loses nothing. Exit 0 done, 1 refused or failed (the
line's `code` says why, `next` says what to run), 75 `busy` (nothing changed by that attempt, run it again later).
Not in scope: Google or CalDAV credentials, the gateway keys, the messaging database.

The sequence, one role set at a time:

```sh
cd <state_directory>/<sha>/source
sudo python3 scripts/calendar_rotate.py plan        # `plan`: read-only; `ready` and `issues` say whether apply would pass
sudo python3 scripts/calendar_rotate.py apply owner # or `apply roles`: stage, ALTER, files, stop and start of api and worker
sudo python3 scripts/calendar_rotate.py verify      # `verify`: database, files, services, then one real calendar backup
sudo python3 scripts/calendar_rotate.py finish      # `finish`: needs the verified state and that backup; old copies go
sudo python3 scripts/calendar_rotate.py undo-note   # `undo-note`: what undo means
```

What each step does:

- `apply` needs the runner and the installed `production.py` to read the marker (`admission_not_installed`
  otherwise: a deploy installs both), no active call (the call gate of `production.py`, read-only, before the
  step, before the restart and after the step), the runner's `.runner.lock`, messaging's `host_lock` and the
  deploy locks free (`busy`), and every file of the set agreeing on one value the database accepts.
- Files: `fcs_owner`: `postgres.env`, `runtime.json`, the backup `owner.dsn`. `fcs_gateway`: `gateway.json`,
  `fcs_worker`: `worker.json`, where only the password in `dsn` changes (`bind`, `allowed_hosts` and
  `allowed_origins` stay). Each keeps its owner and mode and is staged, parsed, renamed and read back.
- The verifier is computed on the host (SCRAM-SHA-256), so the plaintext reaches no server log, argv,
  environment or `psql` history. The `ALTER` goes over the database container's local socket, with deadlines the
  server enforces and a fence on the verifier it replaces. A login probe runs
  in a throwaway container of the database's image on its internal network, scram only, with the value on stdin.
- `verify`: the replaced value is refused and the new one accepted on fresh connections, the files hold the new
  value, `db-roles --check` is ok through `runtime.json`, `/health` is 200, the saved container identities are the
  same, then a real calendar backup through the new `owner.dsn`. `verify --no-backup` makes `finish` refuse.
- Retired list: `apply owner` adds the replaced password's prefix to
  `/opt/deedkit/calendar-backup/retired-passwords` (0600 root, one per line, replaced by one rename) once the `ALTER`
  settled; entries are never removed. The restore card (ANYM-780) reads it.

Admission: `/opt/deedkit/calendar-backup/rotation/rotation.unsettled` exists from the first byte of phase `staged`
until `finished`, and it outlives the process. While it exists `production.py deploy` (also `--auto` and
`--dry-run`) returns `busy` with code `rotation_running`, and a calendar backup run refuses with
`rotation_in_progress` (the catch-up timer starts it again later; a `--recover-stop` still runs). In phase
`verifying` only the tool's own backup passes, by the token in the marker. Other `production.py` commands (`apply`,
`rollback`) do not read the marker: run none during a rotation. The tool holds the locks only for the instants it
writes, and they are free while its backup runs. The marker has no expiry: while it stays, every calendar backup
refuses (`status.json` reads `rotation_in_progress`, the freshness alert fires once the newest snapshot is too old)
and deploys answer `busy`, so a stopped rotation is finished with `recover` the same day.

Phases in `journal.json` (0600, directory 0700, fsync and atomic rename): `staged` (copies of the old files on
disk, the new value staged 0600), `altered`, `files`, `verifying`, `finished`. HUP, INT, TERM, QUIT and TSTP are
ignored from the start of `apply`, `recover` and `finish`. A `plan` during an open rotation names the phase.

After any interruption run `recover`. It is forward only: once an `ALTER` may have committed it asks the database
which value it accepts and completes with the staged one; it never enables the replaced value again.

| Interrupted | State | `recover` |
|---|---|---|
| before the `ALTER` was sent (`staged`) | nothing changed in the database or the files | aborts, removes the staged files and the marker; run `apply` again |
| after the `ALTER`, before the first file | the database has the new value (or the `ALTER`'s result is uncertain, which a probe settles before any file changes), the files the old one | completes with the staged value: sends it again if needed, retired list, files, restart, `verifying`. Every `ALTER` commits only while the role still has the stored verifier it read, so one that outlived its caller and starts late changes nothing |
| between files | some files old, some new | rewrites the rest from what each holds now |
| a short write or a killed write | the file itself is whole (rename); a `.tmp` beside it | removes the `.tmp`, writes the file |
| staged value lost | the journal's fingerprint no longer matches a file | generates a fresh value, sends it, rewrites every file |
| before `verify` | files new, services restarted or not | restarts again and returns to `verifying` |
| a failed `verify` | `verify_failed` with the failed check names | fix the cause, `recover`, `verify` |
| after `finish` | none | `nothing_to_recover`; a kill inside `finish` is completed |

Usable operator credentials exist throughout: the database container's local socket needs no password, and the old
copies and the staged value stay on disk (`rotation/staged/<id>/`, 0600) until `finish`. An active call or a held
lock makes `recover` answer `busy`: the marker stays and it is run again. So does a calendar backup worker a dead
runner left behind (`backup_unsettled`): `recover` first runs the runner's `--recover-stop` and changes nothing while
`<host_lock>.unsettled` exists. A throwaway container an earlier run stranded is removed by its label. A marker with an unreadable journal
(`journal_unreadable`) never happens through the tool: stop and compare the database and the files by hand.

Restore (ANYM-780): `--mode isolated --container <name> --profile-dir <dir> [--retired-list <file>]` on every
subcommand rotates the roles in that container and rewrites only the profile files in that directory (the journal
goes to `<dir>.rotation`, on the resolved path). It refuses when the container's project has a `calendar-api` or
`calendar-worker`, when the container is on a non-internal network or shares one with any other container, or when
the directory or the list is under production state. A link inside the journal's directory refuses in either mode. It restarts nothing,
reads no production credential, takes no production admission and verifies at the database level only.

Undo: undo is another rotation (`apply` the same set again); an exposed value is never restored. Until ANYM-779
moves the calendar project off its rendered Compose document, that document keeps the previous `POSTGRES_PASSWORD`
(read only at initdb of an empty directory) and a changed `postgres.env` can read as drift (ANYM-783): those two
cards come before the first production use.

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

Containers of retired HTTP generations (ANYM-689). `retire` only stops a drained
predecessor, so each release left two exited containers, and every image "used by
container" is kept: after a few weeks 200 stopped containers pinned 200 images and the
15 GiB floor halted autodeploy (2026-11-02). The tool therefore removes them too, by the
exact container IDs of the receipts, never by a label alone: a generation qualifies only
when a successor's `http-result.json` records its retirement as complete (status
`serving`, `retired_roles` gateway and api), it is neither the serving generation nor its
one `previous`, its own attempt was published (`serving`) and its receipt names the same
containers as the successor's copy. Each container is inspected immediately before its
removal (ID, image, project, service and release labels, state `exited`) and removed with
a plain `docker rm <id>`: no force, no volumes, no prune. It runs (a) after `retire` of a
serving generation and (b) at the start of a real `deploy`, before the preparation and its
disk check, followed there by a time-bounded image pass with the same keep rules, because
the gate counts the bytes of images, not of containers (order: container cleanup, image
removal, disk check, preparation; the pass after a deployed release stays). Both are
best effort: 120 seconds and 30 per docker command for the containers, no lock waited
for, every failure only a line of the summary, an unknown removal outcome never repeated
in the same run. `deploy.json` carries the summary (`preflight`, and `cleanup`:
containers removed, generations, images removed, disk free after) whether the deploy
ends or halts at the floor; a halt shows `kept_reasons` of the images that stay.
`seamless_release.py clean --config <http config>` lists the same set (report only by
default); `clean --remove --plan-a-config <plan A config>` removes it and runs the image
pass. What is lost: a removed container's metadata and json-file logs; an image comes back
only from `prepared.json` registry references while the package version exists (a
local-only build cannot be restored). Unresolved generations (no receipt, no recorded
retirement, an interrupted promotion) are counted, never removed by the tool.

A release generation whose candidate fails to start (ANYM-757). When `promote` fails after
it started the candidate (readiness, activation or a guard before traffic), it records
`startup-failed` in `http-attempted.json`, then stops the candidate with `docker compose
--project-name deedkit-http-<generation> -f <its http.json> stop http-api-<generation>
http-gateway-<generation>`: its own project and services only, never the serving generation,
the router or another project. The containers are stopped, never removed, so their logs and
state stay inspectable: `docker ps -a --filter label=ai.deedkit.http-release=<generation>`.
Without the stop, the candidate keeps the live service's restart policy and crash-loops
unrouted (10-28: 14 hours, 886 error events). The stop is best effort with the tool's
command timeout; its outcome is recorded as `candidate_stopped: true|false` beside
`startup-failed`, and its failure never replaces the promotion's own error. A failure
before any container starts (prepared validation, schema check) stops nothing.
`production.py status` lists such attempts as `http_failed_attempts` (sha and
`candidate_stopped`; absent on attempts before ANYM-757). A `startup-failed` attempt does
not block the next release, which prepares a new generation; removing the stopped
containers stays manual (`clean` counts them as unresolved).

Release directories. The same preflight, between the container and the image pass, removes
old `<state>/<sha>` directories of the plan A and HTTP states: a whole directory, receipts and
checkout. Selected are those
on main before the current release (`git rev-list --first-parent`), beyond the newest 5 of that
state. Never selected: the current or serving release and its HTTP `previous`, a release a
container mounts, a release a backup runner configuration or its `.before-*` copy names (one that
cannot be read removes nothing: `preflight.releases.skipped`), an unfinished apply, rollback or HTTP attempt, a failed apply not rolled back,
a preparation made since the current release was applied, an unsettled migration, the
receipt recording the retirement of a generation whose containers still exist, an unreadable
receipt, anything newer than the current release or not on main. A state whose release is not
`applied` or `serving` is left alone. A never-applied preparation made before the current
release was applied goes too (the containers it is bound to are gone, so `apply` refuses it), and
the image pass can then remove its images. Each directory is renamed into `<state>/.pruned/` first, because a partly removed one
would read as an unfinished apply; the next pass finishes an interrupted removal. Best effort,
at most 120 seconds of the 300 of the preflight, deletion included:
`deploy.json` `preflight.releases` lists it. Undo: none needed; `prepare` of that SHA recreates a
directory. Note a removed release no longer refuses a new `prepare` of its old SHA.

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
- the image each backup runner pins (ANYM-525), read before any removal: `image` of the
  messaging `runner.json` the configuration names (`migration.unattended.runner_config`)
  and of every `runner.json` of "Backup runner" that exists, and of every
  `runner.json.before-*` beside each, the undo of a move. Every copy counts, because `cp -p`
  keeps an old time and so no time tells the last move; delete an old copy to let its image
  go. A pin must be read: a `runner.json` or copy that cannot be read or names no image,
  a pin by tag whose lookup fails (for a `runner.json`, also one Docker answers with no such
  image; a copy in that state has no image left to keep), a named `runner_config` that is
  missing, or an installed runner with no `runner.json` at all refuses, and nothing is
  removed. A release only reports that refusal as its removal's `skipped`;
- the image of every container, running or stopped; an image with any other
  name (for example `deedkit-rollback-retained:*`); an image of a target this
  configuration does not deploy. Once the lifecycle workers deploy `backup`, that
  rule no longer holds backup images: older ones go, and the runner's pin above stays.

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
shows no new `main` commit, no newer commit has attested images yet, or, with `--auto` (the timer), the last deploy started less than 5 minutes ago or a git or GHCR read failed less than 4 minutes ago.
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
   class changes nothing else: the run pauses and you migrate by hand at once ("Migration releases" item 4), unless the range qualifies
   for an unattended migration ("Unattended migrations"): then the run migrates in phases instead;
   When the only such change is the `production.py`/`seamless_release.py`
   pair, the self-updating layout installs it instead ("Deploy tool updates");
   when the host backup runner is not the candidate's, `deploy` installs it with
   the candidate's backup image pin after `prepare` and before any migration or
   apply ("Backup runner"), so a merge that changes `backup_runner.py` no longer
   pauses automatic deploys;
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
in a row pauses. An `--auto` run retries a failed git or GHCR read only 4
minutes (`RETRY_SPACING`) after the last counted failure and ends `waiting`
before that, so on the one-minute timer the pause still comes after about 10
minutes of failures; a manual run always reads and counts. When apply waited out an
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

### Halts

A halt's pause file is one JSON line, mode 0600: `status`, `reason`, `at`, any fields of the halt, and `code`, `actor`
(`agent` or `owner`) and `next` (one command or one anchor; the `reason` names the SHA and module its placeholders stand
for) from the table below. No code is `owner` today: production changes are agents' under AGENTS.md section 2. A code
the table lacks writes `agent` and this section. `production.py status` keeps `autodeploy_pause` and adds
`autodeploy_halt` with `reason`, `code`, `actor`, `next` and `waiting` (`waiting for <h>h<m>m`, from `at`; there is no time
estimate: a halt waits for whoever acts). Any other pause file (an older tool's without a route, hand-written or empty,
a missing, unparsable or future `at`) prints `actor: unknown` and no `waiting`. `tool-rollback` and `enroll` write their
own pause, in that older shape. A dry run reports `would_pause` and writes nothing.

| Code | Actor | Next |
|---|---|---|
| `attempt_unresolved` | agent | `docs/operations/production.md#continuous-deployment` |
| `call_wait_exhausted` | agent | `docs/operations/production.md#continuous-deployment` |
| `configuration_drift` | agent | `docs/operations/production.md#apply-and-verify` |
| `copies_unreconciled` | agent | `docs/operations/production.md#database-copies` |
| `deploy_receipt_malformed` | agent | `docs/operations/production.md#continuous-deployment` |
| `deploy_refused` | agent | `docs/operations/production.md#continuous-deployment` |
| `deploy_unknown_state` | agent | `production.py status` |
| `enroll_required` | agent | `production.py enroll --module <module> --sha <sha> --plan` |
| `host_locks_failed` | agent | `docs/operations/production.md#continuous-deployment` |
| `http_release_unsettled` | agent | `docs/operations/production.md#continuous-deployment` |
| `http_retirement_failed` | agent | `seamless_release.py status --sha <sha>` |
| `main_unreadable` | agent | `docs/operations/production.md#continuous-deployment` |
| `manifest_invalid` | agent | `docs/operations/production.md#recordings-anym-711` |
| `migration_by_hand` | agent | `docs/operations/production.md#migration-releases-and-the-backup-gate` |
| `migration_refused` | agent | `docs/operations/production.md#unattended-migrations` |
| `migration_unsettled` | agent | `docs/operations/production.md#unattended-migrations` |
| `migration_wait_exhausted` | agent | `docs/operations/production.md#unattended-migrations` |
| `plan_a_preparation_unfinished` | agent | `docs/operations/production.md#prepare-and-inspect` |
| `plan_a_refused` | agent | `docs/operations/production.md#prepare-and-inspect` |
| `plan_a_rolled_back` | agent | `docs/operations/production.md#roll-back-the-current-release` |
| `plan_a_unknown_state` | agent | `production.py status` |
| `plan_b_preparation_unfinished` | agent | `docs/operations/production.md#continuous-deployment` |
| `plan_b_stopped` | agent | `seamless_release.py status --sha <sha>` |
| `registry_unreachable` | agent | `docs/operations/production.md#ci-published-images` |
| `release_not_applied` | agent | `docs/operations/production.md#apply-and-verify` |
| `release_not_on_main` | agent | `docs/operations/production.md#continuous-deployment` |
| `rollback_failed` | agent | `docs/operations/production.md#roll-back-the-current-release` |
| `session_pass_failed` | agent | `docs/operations/production.md#session-holders-anym-896` |
| `tool_blob_mismatch` | agent | `docs/operations/production.md#deploy-tool-updates` |
| `tool_by_hand` | agent | `docs/operations/production.md#deploy-tool-updates` |
| `tool_not_descended` | agent | `docs/operations/production.md#deploy-tool-updates` |
| `tool_release_mismatch` | agent | `docs/operations/production.md#deploy-tool-updates` |
| `tool_rolled_back` | agent | `docs/operations/production.md#deploy-tool-updates` |
| `tool_self_test_failed` | agent | `docs/operations/production.md#deploy-tool-updates` |
| `tool_switch_failed` | agent | `docs/operations/production.md#deploy-tool-updates` |
| `tool_update_stopped` | agent | `docs/operations/production.md#deploy-tool-updates` |
| `tool_update_unresolved` | agent | `docs/operations/production.md#deploy-tool-updates` |

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
   adds `tool_update`, no `started_at`), so the next timer run, about a minute
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
starts it about a minute after the previous run ended (plus up to 10 seconds
of jitter and a 5-second accuracy) and 10 minutes after boot; enabled later
than that, the first run starts at once. A run with nothing new reads only
`git ls-remote`. `--auto` keeps at least 5 minutes, about one deploy's
duration, between two deploy starts; deploys never overlap anyway (the host
locks and the oneshot unit). The 20 minutes it kept before guarded the GitHub
API quota, which promotion no longer reads (ANYM-406; ci-deploy-speed 11-09). The unit reads
`/etc/deedkit/production.json` and `/etc/deedkit/http-production.json` unless
the optional `/etc/deedkit/autodeploy.env` (root-owned, mode `0600`) sets
`DEEDKIT_DEPLOY_CONFIG`.

A deploy and the daily backup never overlap; the backup stops the collectors
(`personal-os-backup.timer`, 18:00 UTC) and an apply would recreate them.
Admission: the unit starts no deploy while `personal-os-backup.service` runs or
waits to retry (any state but `inactive` or `failed`); a failed or unexpected
read skips the run. There is no clock window (owner 11-04: «давай все делать не
ждать ничего»): a deploy may still run at 18:00, and then the scheduled backup
refuses on the runner lock below and systemd retries it (30 minutes apart, at
most three starts in 8 hours). A daily backup lost that way is a backup
follow-up, never a release hold (owner 11-04, accepted).
Exclusion: the deploy holds the backup runner's own lock
(`/opt/personal-os-probe/backups/.runner.lock`, `DEEDKIT_BACKUP_LOCK`) for its
whole run, so a backup started by hand during a deploy refuses before it stops
anything (`backup_job_already_running`). What follows is per unit: messaging's
unit retries every 30 minutes, at most three starts in 8 hours, so a hold across
those starts loses the day; the calendar's unit waits up to 5400 s for the lock and
then fails for the day, with no `Restart=`. Pause the timer and let a running
deploy finish before starting a backup by hand. While a backup holds the lock, the
`--auto` deploy exits 75 and the run counts as a skip. The calendar's backup runner holds the
same lock as its `host_lock` (BACKUP.md, Family calendar), and a manual
`production.py deploy` (without `--auto`) takes it too, after waiting up to
`backup.wait_seconds` for a running backup ("Waiting for a running backup"): past the bound the deploy
returns `busy` (`backup_running`, exit 75) before any change. Every
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
no verdict (default budget 4 hours). Each
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
when a backend command fails). A recordings credential has the same line (ANYM-772): public ID, expiry and `live`,
`expired` or `missing`, matched by its key hash in the recordings private configuration (the expiry prints in canonical ISO form). It reads the gateway policy
and the calls registry and runs the
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
no calls registry configured while a principal holds a calls key). While a principal holds a recordings key, an
unconfigured or unusable recordings configuration is a `backend_unavailable` finding with `backend: recordings` and
the row's fixed `note` (`recordings_config_not_configured`, `_unreadable`, `_invalid` or `_duplicate_key`), so
`--strict` exits 1 and the other rows still print. With `--principal`, the list
keeps that principal's findings and every `backend_unavailable` of a backend it uses.

The configuration is a private JSON file (`--config`, default
`/opt/deedkit/deploy/private/admin/config.json`; template `deploy/admin.example.json`): policy
and calls registry paths, container-to-host path mappings for key files, and the messaging and
calendar operator command prefixes. The optional `recordings_config` is the absolute host path of the recordings
private `config.json`, read only, with the rule below and no owner check (its owner is UID 10001). Every private file it reads must be a regular, owner-only
file (no group or other bits, no symlink). `admin key label ID TEXT` (1–80 printable characters,
an existing gateway principal) writes the label to `labels.json` beside it, mode 0600, atomically;
no service reads that file, so the enforced policy never changes.

### Write subcommands (ANYM-493 shared rules)

An agent runs `key issue`, `key extend`, `key revoke`, `profile tools` and their `undo` under AGENTS.md section 2 and
the harness process.md section 2 access gate. A profile's tool list never gives a principal more than its grants, so a
tool within the grants of the owner's own agents on that profile is an own-agent grant (the journal's before-state,
read-back, `undo`); a profile with any other principal takes Astra r1. A tool that sends, calls, ACKs, pairs, starts or
stops a provider session or changes an allowlist is added only on the owner's exact words (AGENTS.md section 1). Every write subcommand follows these rules
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
- Entries left by ANYM-496's manual lines (a `printf` helper, added in the local scope of the
  folder it ran in): remove each by hand in its own scope, `claude mcp remove --scope local
  deedkit-<id>` in that folder (`key install` names such folders as `shadowed in:`), or `--scope
  user` for a user-scope copy (`key uninstall` refuses a user-scope entry install did not write).
  The old `mkdir -p` usually left `~/.config/deedkit` at 0755, which `key install` refuses: check
  that it is your own directory (`ls -ld ~/.config/deedkit`) and run `chmod 700 ~/.config/deedkit`
  (and `chmod 600` on the token file), then `key install`; an existing `<id>.token` with the same
  token and mode 0600 is reused.

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
calls registry entry. Records stay for audit; deleting them is a separate step under AGENTS.md section 2.
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

### Tool call durations (ANYM-843)

Every MCP or REST tool call that reaches the gateway's tool handler writes one line to the gateway container's
stderr: `{"tool":"<catalog name or unknown>","outcome":"ok|<gateway error code>|error","duration_ms":<int>}`.
Nothing else is on the line: no arguments, chat or message IDs, principal or key names, or results. `error` is a
call that raised (the exception text is not logged); a name outside the catalog is `unknown`. Requests the boundary
refuses before the handler (no key, wrong origin, body too large) write no line. Read-only, per tool for the last
day, calls and nearest-rank p50/p90 in milliseconds (the container: `docker ps --format '{{.Names}}' | grep gateway`):

```sh
docker logs --since 24h <gateway container> 2>&1 | python3 -c 'import sys,re,json;P=re.compile(r"\{\"tool\":\"[a-z0-9_]+\",\"outcome\":\"[a-z0-9_]+\",\"duration_ms\":[0-9]+\}");D={};[D.setdefault(r["tool"],[]).append(r["duration_ms"]) for r in (json.loads(x) for x in map(str.strip,sys.stdin) if P.fullmatch(x))];[print(t,"calls=%d"%len(v),"p50_ms=%d"%sorted(v)[(len(v)-1)//2],"p90_ms=%d"%sorted(v)[(9*len(v)-1)//10]) for t,v in sorted(D.items())]'
```

Only lines of exactly that shape count; any other output, also one that starts like it, is skipped.
Docker's log rotation for the container bounds how far back `--since` reaches.
services/gateway/tests/test_call_duration_log.py runs this program on captured lines.

`deedkit admin wake-targets list [--principal P]` (ANYM-848) prints every agent's chat wake targets (principal, key ID, kind, thread, label, state, hourly budget, subscription IDs, whether a challenge code came back) with counts by state and `unbound`; it runs the messaging `wake-targets-list` operator command. `deedkit admin wake-targets disable ID` and `enable ID` run `disable-wake-target --id` / `enable-wake-target --id`; `enable --all [--principal P]` (`enable-wake-target --all`) lifts every `operator_disabled` target, the step after a restore (SUBSCRIPTIONS.md "Wake targets").

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
