# Bounded streaming backup (operator-only, opt-in)

`delivery_mode: "stream_v2"` avoids both a complete additional original tree
and a complete ciphertext file on the source VM. The default remains
`staged_v1`; existing format1/format2 encrypted bundles remain restorable.
This transport change has no MCP endpoint and no messenger authentication.

The opt-in [private R2 destination](BACKUP_R2.md) preserves the same upload-only
SSH/v2 producer boundary while replacing full receiver-disk staging with bounded
multipart uploads. Its setup and trusted marker are separate from the filesystem
receiver described below; installation does not switch an existing destination.
Its optional root-only server verifier performs quarantined restores and verified
rotation on the independent receiver without a daily operator-computer dependency.
Its root coordinator controls the recovery key and gives only the isolated helper
read-only access; upload stays confined.

## Capture and delivery

The isolated worker takes the same exported PostgreSQL snapshot and cold
session/settings/spool capture as the existing backup. Collectors are resumed
after cold capture/dump, before original hashing and encryption. Reference-mode
capture stores the dump, recovery objects and manifest; its `files/` directory
is empty. It is an operator workspace, **not a completed/restorable backup**.
Normal `verify_bundle` rejects such a workspace; its explicit `blob_root` option
is used only by the operator streaming code.

Originals are read from the immutable, read-only CAS. Every referenced digest
and size is verified, then each source is hashed again while normalized USTAR
reads it. The manifest itself and database dump are also checked during reads.
No source original is linked, copied, deleted or modified. With schema33 and
later, capture first opens a durable database lifecycle fence. Both staged and
reference capture retain it throughout encryption, delivery and retries. The
retention worker cannot evict an original while any such fence remains open.
Only a durable receipt matching the captured backup allows the host runner to
release that fence through its separate isolated worker. The release worker has
no provider sessions, original files, delivery key or Docker socket mounts.

When `--recover-stop` abandons a capture that published nothing (stopped during
the dump or staged encryption), it first verifies that the capture container is
stopped, records the `abandoning` phase in the journal, then ends exactly that
job's fence through the same isolated worker (`abandon_fence`: DSN and job ID
only). The result (`fence_released`, or `fence_absent` when the capture stopped
before its fence committed) is written to `status.json` as `abandoned_fence`; a
repeat reports the same result and touches no other fence. A capture that has
already published keeps its fence, journal and artifacts for the retry: a
`stream_v2` snapshot, a staged `result.json` (the journal moves to `upload`), and
`stream_upload`. If the release fails or its acknowledgement is lost, the
journal is kept and `status.json` records `error_code: backup_fence_retained`;
the next recover-stop or run reads the outcome back before any new capture.
The configured worker image must come from the same release as the runner: an
older image lacks `abandon_fence` and leaves `backup_fence_retained`.

An unknown outcome or process crash can leave a fence open; it has no automatic
expiry. A manual `snapshot` also leaves its fence open until the operator records
successful delivery or explicitly reconciles abandonment. Before releasing an
orphan, identify its exact job, stop only that owned job and establish that no
capture, encoder, delivery or retry still needs its originals. Do not infer this
from age alone. Before activating original eviction, finish or safely abandon
all older staged/reference jobs: schema32 captures have no lifecycle fence and
cannot protect themselves from eviction.

Schema33/34 manifests distinguish present originals from deliberately absent
ones. Each absence must match its captured catalog and completed eviction
journal. An unexplained missing resident original still fails verification.
Restored fences are released only after bundle/database verification and
quarantine, with collectors disabled and old API keys revoked.

Pinned binary `age` writes into a bounded pipe/FIFO. The host bridges this stream
to SSH; only the host has the forced-command delivery key. The container retains
its existing read-only mounts, public age recipient, private tmpfs PGPASSFILE and
restricted resource limits. There is no full source-side ciphertext spool.
Small plaintext dump/recovery metadata still require disk capacity and are kept
only until delivery finishes.

Receiver v2 first reserves a conservative **maximum** ciphertext size against
its per-object limit, quota and free-space reserve. It accepts frames of at most
1 MiB and checks actual size/free reserve throughout delivery. After FIFO EOF,
the host requires the independent encoder result (source integrity, successful
age exit, ciphertext length/hash) before sending the final frame. A worker that
exits or publishes its result before writing to the FIFO ends this wait promptly:
while no writer is attached, the host checks the owned container at most once per
second instead of waiting for the whole stream deadline. An interrupted
or corrupt source, failed encoder, oversized/truncated stream, bad footer or
receiver error cannot publish a backup. Receipt means ciphertext integrity;
it does not replace decryption and isolated restore verification.

The receiver commits a private manifest-digest binding before atomically linking
the complete encrypted object. A crash can leave an orphan binding or incomplete
temporary file; a retry discards only unpublished data. A committed ID is never
replaced. V2 accepts a retry with the same ID and manifest SHA256 and returns its
existing size/hash receipt without sending ciphertext back. This resolves loss
of the final receipt despite age encryption being randomized. A different
manifest under the same ID is rejected.

## Retry and crash recovery

`stream_upload` in the host journal keeps the exact original DB/recovery capture
when transmission fails. `--recover-stop` stops only the labelled owned worker,
resumes only saved previously running collector IDs, and retains the capture.
The next job re-encrypts that same capture without stopping collectors or taking
a different DB/session snapshot. Changed or absent immutable sources cause a
safe failure; they are never silently excluded. A durable final delivery receipt
allows completion of interrupted local cleanup without another live capture.

V1 still retains/retries the same staged ciphertext object. Its behavior and
manual `encrypt_snapshot` preservation are unchanged. A receiver upgrade supports
both versions; a sender switch requires the new matching worker and host runner.

## Operator rollout gate

1. Run `tests/test_backup*.py` with an isolated PostgreSQL test database. Run an
   actual pinned-age encrypt/decrypt and pg_dump/pg_restore drill with sessions,
   spool and settings fixtures in quarantine. Never send or activate restored
   collectors. Current supported backup schema range is 2..34.
2. Install the new root-owned `backup_receiver.py` on the existing backup host
   without changing the restricted key's shell/read/delete privileges. The forced
   command stays the same receiver invocation; it accepts v1/v2 from stdin and
   never executes `SSH_ORIGINAL_COMMAND`. The v2 caller's `backup-put-v2` string
   is an informational selector, not an executable server command.
3. Set receiver `--max-bytes` and `--quota-bytes` from the measured archive and
   conservative wire bound. Default 20 GiB/object and 30 GiB/total are **not**
   sufficient for a 29 GiB archive plus retained copies. Free/quota admission
   includes old copies and the complete new object before any retention runs.
   Preserve the existing independently verified restore anchor.
4. Build/pin the matching backup image and install the host runner. In the private
   existing runner JSON add `"delivery_mode": "stream_v2"`; mounts/recovery sources,
   SSH key, known_hosts and recipient stay operator-owned. Do not copy secrets
   into the image or examples. This mode is not activated by a library upgrade.
5. Deliver and independently restore one new backup before declaring the change
   operational. Verify manifest/tables/original hashes and cold recovery objects,
   then inspect source/receiver status, collector resume and staging disk peaks.
   Live restore/decryption limit must be raised explicitly to fit measured data.

## Retention

Use receiver-host maintenance `--prune --protected-file <root-owned-marker>
--max-copies 3` to keep at most three committed copies **including** every verified
anchor, filling remaining slots with the newest copies. If anchors exceed the
selected cap, or a protected ciphertext fails verification, deletion is refused.
The old daily/weekly presets remain available when `--max-copies` is omitted.

For **one independently verified recovery point**, use local root maintenance
`--rotate-anchor --protected-file <marker> --restore-proof <proof>
--expected-protected-sha256 <previous-marker-sha256>`. Keep the existing anchor
until its replacement has been decrypted and restored in an isolated quarantine.
One retained point still needs temporary capacity for the old point **and** the
new candidate; a quota failure never authorizes deleting the only verified point.

Before using this policy, disable the receiver's scheduled count/daily/weekly
prune job and replace it with the independent verification/rotation procedure.
**Do not schedule `--prune --max-copies 1`: it retains the old protected anchor
and can delete a new upload before verification.** The upload path and the old
retention defaults remain unchanged. Upload completion alone never invokes
rotation. Serialize verification/rotation with the next delivery; unresolved
or failed candidates require an explicit operator decision before another full
candidate is admitted. Rotation leaves unrelated unverified objects untouched;
it is not a general age-based cleanup operation.

The proof is a private (0600), single-link, root-owned regular JSON file outside
the receiver archive, in a root-owned directory without group/other write access.
The protected marker has the same operator ownership/privacy requirements.
Every ancestor up to `/` must also be root-owned, without group/other write
access or symlinks, for both the operator paths and the receiver archive.
Sticky shared temporary directories are not suitable. Only the private archive
leaf may belong to the restricted upload user. Rotation walks the directory
chain from `/` with `openat` and retains descriptors; evidence reads, lock,
atomic writes, fsync and exact object deletion use those anchored descriptors.
If the archive or marker directory identity changes before cleanup, deletion is
refused. These requirements apply to rotation; existing upload/retention
invocations retain their established contracts.
Proof input is bounded to 16 KiB. A separate verifier/operator creates this record
**after** actual decryption, manifest and original/recovery hash verification,
PostgreSQL restore/table verification, and collector quarantine. The receiver
validates the attestation and its exact ciphertext/manifest identity; it does not
perform or independently establish those restore checks itself. For example,
the shape is:

```json
{
  "v": 1,
  "type": "independent_restore_verification",
  "status": "verified",
  "backup": {
    "id": "20261016T091703Z-fedcba9876543210",
    "bytes": 123456789,
    "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "snapshot_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "verification": {
    "id": "0123456789abcdef0123456789abcdef",
    "verified_at": "2026-10-16T10:00:00+00:00",
    "schema_version": 32,
    "manifest_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "checks": {
      "decryption": true,
      "bundle_integrity": true,
      "database_restore": true,
      "table_inventory": true,
      "original_hashes": true,
      "recovery_objects": true,
      "collectors_quarantined": true
    }
  }
}
```

The version is the actually restored schema, and all seven checks must be true.
The 32-character hex verification ID identifies that independent restore run;
`manifest_sha256` must equal the candidate's v2 `snapshot_sha256`. The candidate
must have its matching committed v2 receipt and ciphertext. Verification time
must include a timezone, follow receipt creation and not be in the future
(five-minute clock tolerance). An upload receipt is not an acceptable proof.

Under the same receiver lock as upload/prune, rotation verifies the old marker's
exact SHA256 and every currently protected ciphertext, verifies the candidate,
and fsyncs a root-owned `<marker>.rotation.json` journal. It atomically replaces
and fsyncs the marker before deleting only the explicitly superseded verified
objects and their receipts. The result protects one point. On error, keep the
proof and journal and retry with the **same proof bytes and original expected
marker SHA256**; this reconciles interruption before/after marker replacement
without unprotecting the replacement. Finish an interrupted rotation before
starting another one. Preserve the journal after success for idempotent retry.
The restricted SSH forced command remains upload-only: sender stdin and
`SSH_ORIGINAL_COMMAND` cannot select local maintenance or a proof path.

Size transfer deadlines from measured **end-to-end** throughput. A streamed
upload includes source integrity checks and encryption before/during delivery;
the receiver's alarm begins when the SSH command starts, even before its first
ciphertext frame. Fifteen minutes is insufficient for a 30 GiB archive at
20 MiB/s. Set explicit receiver `--timeout-seconds`, runner `timeout_seconds`
and systemd `TimeoutStartSec` together. Keep a finite whole-job deadline and
verify stop/retry preserves the committed capture without stopping collectors
again. A larger CPU cap alone may not improve a transport-limited upload.
Only root maintenance can prune; the upload key cannot choose this operation.
Associated v2 receipt bindings are removed with an authorized expired object.
Do not prune existing verified copies to make admission appear successful.

Examples/configuration are generic. Production activation, a real encrypted
delivery and isolated restore are separately recorded evidence, not implied by
unit transport stubs or this document.
