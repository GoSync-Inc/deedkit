# Private R2 backup destination (operator-only, opt-in)

This receiver sends the existing age-encrypted `stream_v2` transport to a
dedicated Cloudflare R2 Standard bucket. The source keeps its upload-only SSH
key; it receives no R2 credential or list/delete interface, and its backup key
cannot read. A separate cold-archive key can only store and read encrypted
originals by digest under `cold/` ([Cold archive](#cold-archive-of-originals)).
R2 credentials stay on the separate receiver. An optional root-only verifier on that receiver
holds the age recovery key and performs independent restores and verified-point
rotation. Normal scheduled operation requires no Mac or other operator computer.
The producer never receives that recovery key.

The [filesystem receiver](BACKUP_STREAMING.md) remains available. Installing the
module does not change an SSH forced command, switch a producer, enable a timer
or authorize production changes. Enable the server verifier explicitly after its
synthetic and operational enrollment checks below. Follow
[verified promotion](docs/operations/production.md) for an exact current main SHA
with successful required CI. Never install feature-branch source, a copied source
patch or an ad hoc application image overlay on a host.

## Account and bucket preparation

1. The owner signs into Cloudflare and activates R2 through **Storage & databases
   → R2 → Overview**, accepting the checkout and payment terms. This is
   usage-based billing; a domain transfer or paid website plan is not part of
   this setup. [Cloudflare activation instructions](https://developers.cloudflare.com/r2/get-started/).
2. Create one dedicated **Standard** bucket. Keep public access, the public
   development URL and custom domains disabled. Do not add CORS rules: this
   server-to-server backup route needs none. Record its jurisdiction and matching
   S3 endpoint privately.
3. In the R2 API token controls, create **Object Read & Write** access restricted
   to that bucket. Do not grant account administration or all-bucket access.
   Save its Access Key ID and Secret Access Key directly in a private receiver
   credential file. The secret is shown only once; do not put it in chat, shell
   arguments, Git, examples or CI artifacts. A jurisdictional bucket requires its
   corresponding endpoint, such as `https://<ACCOUNT_ID>.eu.r2.cloudflarestorage.com`
   for EU. [Cloudflare R2 authentication](https://developers.cloudflare.com/r2/api/tokens/).
4. Do not add lifecycle expiration or storage-class transition rules for committed
   backups, receipts or `cold/` copies. Cleanup of **incomplete multipart uploads** is allowed;
   it does not replace root-controlled verified-point rotation. Check that no
   existing broad bucket policy expires the protected object.

The bucket-scoped credential can access objects within that bucket. The
upload-only boundary is enforced by the receiver's restricted SSH account and
fixed command, not by giving the producer this credential. Root on the receiver
remains trusted. Keep receiver credentials out of producer recovery settings.

## Cost and capacity

Prices checked on 2026-10-16: Standard storage is **$0.015/GB-month**, with
10 GB-month free per account, shared with its other Standard use. The request
allowances are one million Class A and ten million Class B operations monthly;
excess requests are billed. Direct R2 egress is free. Storage billing averages
each day's peak, so temporary old-plus-new overlap counts even when brief.
For a generic 35 GB daily replacement, budget about 70 GB at the daily peak:
`(70 - 10) × $0.015 ≈ $0.90/month` for storage if the allowance is available.
Taxes, growth, request charges and other account use can increase the bill.
This is an estimate, not a spending cap. Infrequent Access has a 30-day minimum
and retrieval fees, making it a poor fit for daily replacement.
[Cloudflare pricing and billing units](https://developers.cloudflare.com/r2/pricing/).

One retained verified point still requires room in R2 for the previous point
and one candidate. Configure a conservative maximum wire size and a quota that
admits both. The receiver counts committed and unresolved physical objects,
allows at most two full objects, and refuses accumulation after an unresolved
attempt. A quota refusal never authorizes deletion of the only recovery point.
Application admission limits do not cap the Cloudflare account's bill or other
clients' writes.

## Receiver installation and private configuration

Use a reviewed wheel built from the exact CI-verified current main revision and
the [receiver-only SDK lock](services/messaging/requirements-backup-r2.lock) from
that same revision. Install into a dedicated Python 3.13 environment. Set
`REVIEWED_WHEEL` to the inspected wheel and `REVIEWED_SOURCE` to the verified
clean revision; check their provenance before these generic installation steps:

```sh
python3.13 -m venv /opt/deedkit-backup-r2/venv
/opt/deedkit-backup-r2/venv/bin/python -m pip install --no-deps "$REVIEWED_WHEEL"
/opt/deedkit-backup-r2/venv/bin/python -m pip install --require-hashes \
  -r "$REVIEWED_SOURCE/services/messaging/requirements-backup-r2.lock"
```

The wheel's `--no-deps` installation deliberately avoids installing messaging
provider dependencies on this receiver. The module needs its sibling
`messaging_service.backup_receiver` helpers; copying one Python file is not an
installation. The SDK is optional and is not added to ordinary application
dependencies or images. Importing the application does not resolve AWS
credentials or connect to R2.

The upload receive process has no need for a messaging DSN, provider session,
original file tree, Docker socket or age private key. A host Python installation
and the receiver's explicit SDK dependencies suffice for delivery. The separate
root verifier below uses Docker and a root-only recovery key; the restricted
upload process cannot access them. Do not activate the application or collectors
to install either tool. Inspect wheel/sdist contents and test the installed wheel
outside the checkout before host enrollment.

Copy [the generic configuration](deploy/backup-r2.example.json) to a private
operator path and replace every deployment-specific value. The endpoint must be
the account's explicit HTTPS R2 S3 endpoint; the client uses region `auto`,
signature v4 and explicit credentials. It does not fall back to ambient AWS
profiles or instance metadata. The credential JSON contains only these keys:

```json
{
  "access_key_id": "REPLACE_WITH_BUCKET_SCOPED_ACCESS_KEY_ID",
  "secret_access_key": "REPLACE_WITH_SECRET_ACCESS_KEY"
}
```

This is a shape example, not a usable credential. Files must be private, regular,
single-link and owned by their process user or root; symlink paths are refused.
Use mode `0600` for files and `0700` for private state directories. The receive
process uses its own private config and credential file, owned by the restricted
upload user. Root maintenance uses separate **root-owned** config and credential
copies: root does not accept an upload-owned configuration or credential. The
operator config must select the same endpoint, bucket, prefix and shared upload
state directory; only its `credentials_file` path changes to the root-owned copy.
The private upload-state leaf may belong to the upload user. Keep its ancestor
directories, root control and proof paths root-owned without group/other write
access. Control and proof paths must be outside upload state. Shared temporary
directories are unsuitable. Do not weaken these checks to fit a deployment.

Before root maintenance, let the first receive initialize `.receiver.lock` in
the shared upload-state directory, or provision that regular single-link file
as the upload user with mode `0600`. Root maintenance refuses a missing lock in
upload-owned state (`uploader_lock_initialization_required`) and never creates
a root-owned lock that would prevent later uploads. Preserve the existing lock;
replacing it while a process holds it would break shared exclusion.

The example uses 40 GiB/object, 80 GiB/total, two objects and 64 MiB parts. These
are explicit example limits, not measured deployment settings. Size the receiver
deadline, producer `timeout_seconds` and scheduler deadline together from
end-to-end capture, encryption, hashing, upload and full cloud readback time.
A receive deadline begins before the first data frame. Preserve a finite timeout
and enough RAM for the bounded part buffer; the receiver does not stage a
complete ciphertext locally.
Do not mount R2 as a POSIX filesystem: upload durability uses the object API,
receipts and journal, not filesystem rename assumptions.

Configuration version is `v: 1`; unsupported/duplicate fields fail closed.
Optional defaults are 64 GiB `max_bytes`, 128 GiB `quota_bytes`, two
`max_objects`, 64 MiB `part_bytes` and 7200 `timeout_seconds`. `max_objects` must
be exactly two. Part sizes may be 5–64 MiB; the object bound must fit both the
10,000-part limit and the backend's maximum. Deadlines may be 60–21600 seconds.
Set explicit values appropriate to the measured deployment rather than relying
on defaults. This implementation accepts the default and EU R2 endpoints.

## SSH boundary and producer enrollment

Use a dedicated upload key with a root-owned SSH home and `authorized_keys`.
Pin the receiver host key through an already trusted administration route. The
fixed forced command invokes only `receive`, for example:

```text
restrict,from="SOURCE_ADDRESS",command="/opt/deedkit-backup-r2/venv/bin/python -m messaging_service.backup_r2_receiver --config /etc/deedkit-backup-r2/receive.json receive" ssh-ed25519 REPLACE_WITH_UPLOAD_PUBLIC_KEY
```

The addresses and key above are placeholders. Preserve restrictions against
shell, SFTP, forwarding and agent access. The receiver never executes or selects
maintenance from `SSH_ORIGINAL_COMMAND`; stdin cannot choose a control/proof path.
Only the configured module invocation is run. Inspect the actual key restrictions
and file ownership before changing the existing upload route.

The producer keeps its existing host/user/key/known-hosts transfer configuration
and uses `delivery_mode: "stream_v2"`; this backend does not accept `staged_v1`.
No R2 key or endpoint is added to the producer. Do not switch an in-flight job
without reconciling its journal and exact prior destination: retrying a stale
capture is not taking a fresh pre-migration snapshot.

Before enrollment, use a separate synthetic prefix and configuration to deliver
an age-encrypted synthetic fixture through the real forced SSH route, download
it, compare its complete SHA256/size and decrypt/restore it in quarantine.
Exercise duplicate-ID delivery without producing a second logical backup. Use
no real accounts, sessions or production database in this synthetic probe.
Remove only its exact owned test objects/state after inspection; never run a
bucket-wide delete. Keep its evidence separate from actual backup acceptance.

## Commit, download and independent restore

The receiver accepts the same bounded v2 framing as the existing runner. It
uploads bounded multipart parts, but completes the multipart object only after
validating age framing, input EOF, size/hash and the producer's final encoder
confirmation. R2 supports multipart uploads for large objects; parts except the
last must satisfy the service's minimum size.
[Cloudflare upload documentation](https://developers.cloudflare.com/r2/objects/upload-objects/).

Each attempt has a random immutable physical key under
`<prefix>/objects/<backup-id>-<token>.tar.age`. A logical receipt at
`<prefix>/receipts/<backup-id>.json` binds that key to ciphertext size/SHA256 and
snapshot manifest SHA256. Its conditional `If-None-Match: *` publication cannot
replace a committed ID. A matching logical retry returns the committed receipt;
a conflicting ID fails. Multipart ETags are never treated as full-file SHA256.
Atomic conditional publication is an R2 API contract;
negative reads do not establish that an earlier unknown write cannot still
finish. [Cloudflare S3 compatibility](https://developers.cloudflare.com/r2/api/s3/api/).

For a manual drill or one-time offline bootstrap, download by logical ID into a
**new** private output file. Scheduled server verification uses the direct
streaming path below instead of a complete ciphertext spool:

```sh
/opt/deedkit-backup-r2/venv/bin/python -m messaging_service.backup_r2_receiver \
  --config /etc/deedkit-backup-r2/operator.json download \
  --id BACKUP_ID --output /PRIVATE_NEW_DIRECTORY/candidate.tar.age
```

Download verifies the entire ciphertext's size and SHA256 before atomic output
publication. For an offline drill, securely transfer that ciphertext to the
isolated restore machine and verify the complete hash there again, or use
separately authorized private operator download configuration there. This manual
path remains available for recovery; it is not a daily dependency of the server
verifier. Never give the producer download authority or assume that HEAD/ETag or
upload success proves recoverability.

Follow the [backup restore drill](BACKUP.md#restore-drill) with an explicitly
sufficient decryption bound, matching PostgreSQL tools/runtime and enough private
disk. Verify authenticated decryption, bundle integrity, restored database/table
inventory, original hashes and cold recovery objects. Quarantine restored
collectors, sessions, keys and outbound work; do not start a provider client.
Record the actual schema, backup ID/size/hash/manifest hash, verification ID and
timezone-aware verification time in the private proof described in
[the independent proof contract](BACKUP_STREAMING.md#retention). All seven checks
must reflect the actual independent restore. An upload receipt, successful unit
test or browser subscription checkout is not this proof.

## Automated independent restore on the receiver

`messaging_service.backup_r2_verifier` is an opt-in root coordinator on the
independent receiver. It uses the root operator R2 configuration and a recovery
key stored there as a root-owned, single-link regular `0600` file outside the
upload subtree. Keep its private configuration, working directory, proofs,
status and protected marker under trusted root-owned ancestry. The producer
retains only its existing forced-command upload key. A backup upload cannot
select the verifier's command, recovery key, helper image or proof path.

This deliberately changes the earlier manual/offline-key operation: the receiver
can now decrypt backups without an operator computer. Compromising root on that
receiver can expose the recovery key and backup plaintext, and root already
holds its bucket credentials. The restricted upload account receives none of
this authority. Keep an independent offline recovery-key copy for loss of the
receiver; the daily job does not depend on that copy or a Mac being online.

Copy [the verifier configuration](deploy/backup-r2-verifier.example.json) to a
private root-owned `0600` file. Its `receiver_config_file` references the root
operator config, not the upload user's config. `directory` is separate from
upload state and root proof/control paths are separate from the helper's writable
payload. `identity_file` points to the root-only age key. `helper_image` must be
an immutable image ID built from reviewed exact current main with successful CI;
`helper_revision` is that exact source revision. `postgres_image` is the official
PostgreSQL 18 digest pinned by the reviewed messaging Dockerfile. Replace the
example helper ID/revision; the placeholders are not runnable images. Build and
inspect these candidates before installing or enabling the service.

Run one coordinator invocation using the private config:

```sh
/opt/deedkit-backup-r2/venv/bin/python -m messaging_service.backup_r2_verifier \
  --config /etc/deedkit-backup-r2/verifier.json
```

The coordinator skips an already protected current ID. With no new committed
candidate it is idle; exactly one unprotected committed candidate may be
selected. Orphan objects, multipart uploads, unresolved attempts or ambiguous
candidates block new work. It streams the full cloud GET directly into an
isolated `backup_restore_worker` helper and hashes/counts the exact consumed
ciphertext, without staging a complete encrypted file on receiver disk.
A body read that breaks mid-stream (a connection reset or read timeout on a
multi-gigabyte object) resumes at the next byte with a ranged GET of the same
ETag, at most five times per object with 2-30 s backoff; the full SHA256 still
covers every byte. After that the run fails with `storage_unavailable`. A
deadline, a service stop or a failed write to the helper is never retried:
the signal handlers record the deadline or stop, and a first-priority botocore
`needs-retry` hook and a check after every cloud call re-raise it, so an SDK
request retry cannot swallow it and continue the run.

The helper receives the recovery key read-only and plaintext workspace access,
with no R2 credentials, producer DSN, delivery key, shared application volumes or
Docker socket. PostgreSQL and helper containers use an exact owned internal
network with no egress and no published host ports. Their images and resource
limits are explicit: the helper has 512 MiB and PostgreSQL 1 GiB, with one restore
job. These container bounds still require host headroom; this is not application
startup or permission to disturb existing services on a 4 GiB receiver.

Successful verification requires authenticated age EOF and exact full cloud
ciphertext size/SHA256, bundle/manifest integrity, actual PostgreSQL restore,
table inventory and hashes, original file hashes, cold recovery objects and
quarantine. Database verification precedes quarantine changes. Record the actual
restored schema without running application initialization or migrations. Only
after all seven proof checks are true may the coordinator call initial `protect`
or exact-marker `rotate`. It never substitutes upload/HEAD success for restore.

Plaintext originals, dump and restored database still need disk space. When work
and Docker share a filesystem, preflight reserves the selected ciphertext's
wire size plus PostgreSQL headroom and the free-space reserve. With a candidate
up to the example's 40 GiB bound, 8 GiB headroom and 2 GiB reserve, plan for up to
**50 GiB free**. Separate filesystems must each pass their respective work or
database requirement with reserve. These are planning bounds, not a filesystem
quota or a guarantee about future database growth.
Measure available disk and RAM alongside the host's other projects. On a small
receiver, leave memory for the host and existing applications; do not increase
helper/PostgreSQL limits or delete shared data to make admission pass.

The coordinator keeps root-owned `active.json`, `status.json`, `last-success.json`
and proof evidence outside the helper's writable payload. A failed run's
`status.json` (and `last-failure.json`, which later busy, idle or verified runs
keep) adds `diagnostic`: the exception class chain (cause/context, at most four)
with code frames as `[file, line, function]`, never messages, arguments or
locals. The service prints the same code and diagnostic as one JSON line to
stderr (the journal). A failed `pg_restore` also adds `detail`: its first
`pg_restore: error:` line as one fixed phrase (the kind of a missing or existing schema
object, a COPY failure, a resource category) or `unclassified pg_restore error`,
never a name, message text or row value. The helper writes
it to `failure-detail.json` beside the unchanged `failure.json`. Before the restore
the helper creates the fixed roles the schema names (`RESTORE_ROLES`) as NOLOGIN in
the isolated cluster only;
`--no-acl` skips grants but not row-level security policies. A failed download,
decryption, restore, quarantine, promotion or cleanup remains a failed job.
Verification failure never authorizes discarding the previous point. If a
failure follows marker promotion, the newly verified point may already be
protected; inspect the durable phase before retrying. Preserve candidate, receipt,
proof, prior marker digest and journal for retry. After interruption, reconcile
the owned attempt before another candidate. Stop/remove only the exact labelled
helper, PostgreSQL container, network and volumes created by that attempt;
never prune shared Docker objects or another project's data. Private plaintext
and restore credentials must not remain in logs, Git or packaged artifacts.

## Scheduling and enrollment

The generic [oneshot service](deploy/deedkit-backup-r2-verifier.service) and
[timer](deploy/deedkit-backup-r2-verifier.timer) run the receiver coordinator as
root. They do not schedule source captures or deploy services. Install them only
after adapting paths and reviewing configuration and image provenance. The
timer waits 15 minutes after an invocation ends, plus its configured random delay
and timer accuracy, so a delayed daily upload or transient verification failure
can be picked up without an operator Mac.
Systemd does not start a second copy of the same oneshot service while it runs;
the coordinator's durable state/locking remains necessary for manual invocations
and crash recovery. The 21900-second service start limit gives the configured
21600-second job deadline a cleanup margin. Match these values if changing the
deadline; do not shorten the service timeout below the actual restore budget.
See the [systemd timer contract](https://github.com/systemd/systemd/blob/main/man/systemd.timer.xml)
for monotonic timing and active-unit behavior.

Before enabling the timer, run a server-native synthetic drill with a dedicated
synthetic prefix/config, real age encryption, the pinned helper/PostgreSQL images
and no real accounts or production data. Verify all seven checks, protect/rotate,
repeat invocation, failure status/retry and exact owned cleanup. Inspect network
isolation, mounts, memory/disk peaks and private file ownership. This drill proves
the synthetic server path; an actual compatible snapshot and its independent
restore remain separate enrollment/release evidence.

For an initial capacity-constrained migration, an operator computer may perform
the **one-time** restore of the selected cloud bootstrap point. Verify the full
R2 ciphertext and independent restore proof before cloud protection; only then
remove exact obsolete local archives under a separate reviewed operation. This
frees local capacity for future server restores. Preserve the old protected copy
until cloud acceptance, and preserve its marker/proof/journal. Normal subsequent
captures, server verification and rotation do not require that computer. An old
bootstrap point does not satisfy the fresh pre-migration snapshot gate.

After synthetic acceptance and actual enrollment, install the reviewed examples
under `/etc/systemd/system/`, then explicitly enable the timer:

```sh
systemctl daemon-reload
systemctl enable --now deedkit-backup-r2-verifier.timer
systemctl list-timers deedkit-backup-r2-verifier.timer
systemctl status deedkit-backup-r2-verifier.service
```

Inspect private coordinator status and the actual protected point after the
first scheduled run. Enabling a timer or seeing an active unit is not evidence
of a completed restore. Source snapshot freshness, delivery and verifier failure
status need operational monitoring; these unit examples do not send alerts or
establish a live RPO/RTO. Disable only this timer if enrollment fails; retain its
private evidence and reconcile its exact owned resources.

## First protected point and one-point rotation

Run maintenance locally as root with trusted configuration and credentials.
Keep the proof and protected marker in separate private root-controlled paths
outside uploader state. Their files must be regular, private and single-link;
every ancestor must be root-owned, nonsymlinked and not group/other writable.
Never accept an uploader-authored proof or marker. The R2 marker uses a
`v`/`destination`/`backups` envelope binding it to this configured destination;
do not copy the filesystem receiver's bare-list marker into it.

Initial enrollment requires an absent marker and an independently restored
committed cloud point. It deletes nothing:

```sh
/opt/deedkit-backup-r2/venv/bin/python -m messaging_service.backup_r2_receiver \
  --config /etc/deedkit-backup-r2/operator.json protect \
  --protected-file /etc/deedkit-backup-r2/control/protected.json \
  --restore-proof /etc/deedkit-backup-r2/proofs/initial-restore.json
```

If enrollment is interrupted, preserve its root-owned enrollment journal and
retry the same proof bytes. An existing marker is accepted only as a matching
journal-backed replay; do not remove or reseed it to force initial enrollment.

For each replacement, keep the previous cloud point while delivering and
independently restoring the candidate. Record the **exact existing marker
bytes' SHA256** privately before rotation. Then pass that digest, for example
using `PREVIOUS_MARKER_SHA256` supplied by the operator:

```sh
/opt/deedkit-backup-r2/venv/bin/python -m messaging_service.backup_r2_receiver \
  --config /etc/deedkit-backup-r2/operator.json rotate \
  --protected-file /etc/deedkit-backup-r2/control/protected.json \
  --restore-proof /etc/deedkit-backup-r2/proofs/replacement-restore.json \
  --expected-protected-sha256 "$PREVIOUS_MARKER_SHA256"
```

Rotation holds the shared receiver lock, checks destination, exact marker digest,
receipt/proof identities and object hashes, then fsyncs its root-owned pending
journal. It switches and fsyncs the marker **before** deleting only the exact
superseded ciphertext and logical receipt. It does not remove unrelated
candidates. Preserve the successful journal, proof and marker for audit/retry.
Serialize this procedure with new deliveries. Two occupied slots require
candidate reconciliation/rotation before another full upload, not higher limits
or automatic deletion to hide the failure.

Do not schedule `--prune --max-copies 1` on the old receiver or apply age/count
pruning to cloud objects. Disable the old scheduled count/daily/weekly prune
before enrollment of this policy. The upload receiver itself does not schedule
restores. The separately enabled root verifier and timer above establish the
independent proof before promotion; upload completion alone never rotates.

## Cold archive of originals

The same bucket also holds encrypted cold copies of CAS originals under
`<prefix>/cold/<sha256>.age`, written by the producer's cold archiver before an
original may be evicted ([MEDIA_STORAGE.md](MEDIA_STORAGE.md#cold-archive-of-originals-schema-35)).
The producer still receives no R2 credential. Add a **separate** key to the
restricted upload account, with its own fixed command:

```text
restrict,from="SOURCE_ADDRESS",command="/opt/deedkit-backup-r2/venv/bin/python -m messaging_service.backup_r2_receiver --config /etc/deedkit-backup-r2/receive.json cold" ssh-ed25519 REPLACE_WITH_COLD_PUBLIC_KEY
```

The `cold` mode reads one request line and accepts only `put` or `get` of a
64-hex digest. It derives the key itself; a request cannot name a path, prefix
or other verb. It never lists, deletes or overwrites: a `put` for an existing
key reports `exists` without reading the new ciphertext, and a single-request
write is conditional (`If-None-Match: *`). Cold state lives in a private
`cold/` subdirectory of upload state: a separate `.cold.lock`, so a long backup
upload does not block cold writes, and one `<digest>.json` journal per
multipart write. The next attempt for that key settles the journal first: an
upload whose completion was never sent is aborted; a sent completion is
settled only by its object (`exists`) or by aborting the still-open upload.
Otherwise the key answers `completion_outcome_unknown` and its original stays
resident, so an earlier completion can never replace a later object. An
operator resolves such a journal only after confirming, as root with the
receiver credential, that the key is still absent and no multipart upload for
it is open; then the journal file may be removed and the next pass retries. Backup admission and the verifier ignore the `cold/` directory.
Objects must be age ciphertext; the optional `cold_max_bytes` (default 5 GiB)
bounds each one and must exceed the archiver's `max_object_bytes` plus age
overhead. Larger objects use the configured multipart part size.

## Schema capabilities key

An automatic migration (ANYM-587) needs to know whether this receiver can verify
a backup at the new schema before production moves to it. A **third** key on
the restricted upload account answers that and nothing else:

```text
restrict,from="SOURCE_ADDRESS",command="/opt/deedkit-backup-r2/venv/bin/python -m messaging_service.backup_r2_receiver --config /etc/deedkit-backup-r2/receive.json capabilities" ssh-ed25519 REPLACE_WITH_CAPABILITIES_PUBLIC_KEY
```

The `capabilities` mode takes no argument (argparse refuses one; the forced
command fixes the argv anyway) and no input: a byte already waiting on stdin is
refused with `capabilities_takes_no_input`, and an open, silent stdin is never
read. It reads no archive or receipt and never contacts the object store. It prints one
line:

```json
{"v":1,"verifier_schemas":[2,50],"helper_schemas":[2,50],"helper_revision":"<40-hex>"}
```

`verifier_schemas` is the range the installed verifier accepts from its helper
(`backup_r2_verifier.SCHEMAS`). `helper_schemas` and `helper_revision` describe
the pinned helper image, which only root may inspect. They are read from a
root-published file at a path fixed in code, never configured:
`/var/lib/deedkit-backup-r2/published/capabilities.json`. The mode is enabled by
`verifier_config_file` in the receiver configuration: the absolute path of the
installed service's verifier configuration, outside `state_directory` (set the
same value in the root operator copy).

The publisher of that file is not part of this release; it comes with the
receiver's update follow (ANYM-597). Until it exists, `capabilities` answers
`capabilities_unavailable` and the deployer waits, as it does for every answer
it cannot prove. The reader accepts a file only when it is root-owned,
single-link, world-readable and not group/other-writable, under root-owned
ancestors that are not group/other writable, and exactly
`{"v":1,"verifier_config":{…},"helper_revision":"<40-hex>","helper_schemas":[first,last]}`.
`verifier_config` must name `verifier_config_file` and its current path, device,
inode, size, modification and change times. `capabilities` compares them with a
`stat` of that file, which needs only search permission on its directories,
never read access to the private file. Any edit, replacement, removal or symlink
of the verifier configuration after the publication makes it stale. A missing,
unreadable, malformed or stale file answers `capabilities_unavailable`. Keep the
verifier configuration's directories searchable by the upload account (as for
`receive.json`), or the answer is always `capabilities_unavailable`.

Undo: remove the capabilities key line from `authorized_keys` and
`verifier_config_file` from the receiver configuration; the upload and cold keys
and `receive` are unchanged by this key.

Backup quota inventory, verified-point rotation and the verifier only address
`<prefix>/objects/` and `<prefix>/receipts/`: cold copies never occupy a backup
slot and rotation never deletes them. Cleanup of incomplete multipart uploads
remains allowed for the whole bucket.

**Bucket lock (owner action).** Add an indefinite lock rule on the cold prefix
so that R2 itself refuses deletion and overwrite of archived objects, for
example `npx wrangler r2 bucket lock add <BUCKET> --name cold-originals --prefix <PREFIX>/cold/ --retention-indefinite`
([Cloudflare bucket locks](https://developers.cloudflare.com/r2/buckets/bucket-locks/)).
Do not add it to `objects/` or `receipts/`, which rotation must delete.
Retention of `cold/` after message deletion is a separate, future decision.

**Keys.** Every cold object is encrypted to two recipients: the backup recovery
recipient (the root-only verifier key and its offline copy can always restore
it) and a dedicated cold recipient created for the producer. Generate the cold
identity with `age-keygen` on the producer host, keep it outside Git, images
and logs as a private (`0600`), single-link regular file owned by the
archiver's runtime user (UID 10001), and keep an offline copy with the backup
recovery key.
Losing the cold identity does not lose data; the backup recovery key still
decrypts every object.

Before enabling the archiver, run a synthetic drill through the real forced
command: write a synthetic object, read it back, decrypt it and compare its
SHA-256, then confirm with the root operator credential that deleting it is
refused by the bucket lock. Under the locked prefix that small synthetic object
stays; record its digest with the drill evidence. Use no real originals.

**Rehydration and restore drill (ANYM-350).** The producer's `cold-restore`
service reads objects back through the same `cold get` and decrypts them with
the cold identity; it writes nothing to R2. A backup point names each evicted
original's object in `absent_blobs[].cold_object_key`. Drill: restore the point
into an isolated database and files directory, start `cold_archive --restore
--interval-seconds 10` against the isolated DSN and files, request one evicted
original there (a request without a running worker goes to the provider), and
compare the restored file's SHA-256 with its `absent_blobs` digest
([MEDIA_STORAGE.md](MEDIA_STORAGE.md#cold-first-rehydration-anym-350)). With
the cold identity lost, decrypt with the backup recovery identity instead.

## Unknown outcomes and migration from local archives

On an error or timeout, preserve private state, receipts, proof and journal.
Reconcile the same logical ID, attempt, snapshot and destination. A multipart
completion with an unknown result retains its exact attempt evidence and blocks
a new replacing attempt; a single negative HEAD is insufficient to discard it.
Conditional receipt publication can retry only its exact body. Do not erase a
journal, invent another backup ID or broadly abort/delete objects to get past an
uncertainty. Failed parts may be aborted only when their unpublished status and
ownership are established.

For an interrupted rotation, retry with the **same proof bytes and original
expected marker SHA256**. Keep the pending journal, including after the marker
has switched; the operator rechecks the new protected point before any remaining
exact deletion. Finish or explicitly reconcile that operation before another
rotation. A storage permission/timeout error is not evidence of absence.

When moving from local archives, retain both the old protected local anchor and
any existing candidate until the selected cloud copy has its complete ciphertext
hash verified and has been independently decrypted/restored from R2. Protect the
cloud point only after that proof. Preserve the old marker, proof and journals;
only then may a separate reviewed operation remove exact obsolete local archive
IDs and their receipts. Do not delete the sole anchor to fit the first transfer,
prune shared Docker data, touch another project's volumes, or treat a cloud copy
of an old snapshot as a fresh pre-migration backup.

Record these gates separately: exact-main source/CI and package checks; bucket
privacy and scoped credential enrollment; synthetic live-bucket transport drill;
actual fresh compatible snapshot delivery; independent quarantined restore;
protected-point enrollment/rotation; and approved application release. This
document and synthetic CI do not claim live acceptance for any deployment.
