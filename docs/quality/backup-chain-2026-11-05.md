# Daily backup chain: baseline and R2 checksum probe (ANYM-641)

Research note for ANYM-641, read 2026-11-05 before the first edit. Sources are read-only: the producer's and receiver's
journals, the proof files and `verified_at` in them, the mtime of each attempt journal's last write (its `committed` phase,
right after the receipt was published), `status.json` and `last-success.json`.

## Baseline (criterion 1)

Chain time is `last_snapshot_at` to the post-promotion `last-success.json` `verified_at`. Both baseline chains succeeded;
neither was retried.

| Backup ID | Ciphertext | Snapshot | Receipt | Proof `verified_at` | Promoted | Upload | To proof | Promotion | Chain |
|---|---|---|---|---|---|---|---|---|---|
| `20261029T180022Z-7f59dc9c33f1b0b9` | 28.62 GB | 18:00:22 | 18:35:56 | 19:02:40 | 19:12:42 | 35:34 | 26:44 | 10:02 | 72:20 |
| `20261030T180022Z-9afd84d3a1c0b55a` | 30.21 GB | 18:00:22 | 18:41:32 | 19:06:42 | 19:16:35 | 41:10 | 25:10 | 9:53 | **76:13** |

**Baseline: 76 min 13 s** (10-30, the slower). Deployed then: 10-29 producer 9037a0d4, receiver and helper cbb89410; 10-30
all c1098ab3.

Three substitutes, since these chains are a week old:
- `status.json` and `last-success.json` hold only the latest run. The snapshot column is the backup ID's time. On 11-04 the ID
  said 18:01:01 and `last_snapshot_at` said 18:01:09, so the two chains read about 8 s long.
- "Promoted" is the journal's `verified` line on 10-30. The receiver on 10-29 did not print that line yet, so that row uses
  the unit's `Finished` time, which follows `last-success.json` by moments.
- "Receipt" is the attempt journal's mtime, not the receipt's own time: the old objects and receipts have been rotated away.

The chain has grown since, with the data:

| Backup ID | Ciphertext | Snapshot | Receipt | Proof | Promoted | Upload | To proof | Promotion | Chain |
|---|---|---|---|---|---|---|---|---|---|
| `20261103T180022Z-73d1330aa955e373` | 37.21 GB | 18:00:22 | 18:48:06 | 19:21:18 | 19:31:22 | 47:44 | 33:12 | 10:04 | 91:00 |
| `20261104T180101Z-71aedf6eb1cf052c` | 38.29 GB | 18:01:09 | 18:52:16 | 19:23:30 | 19:35:44 | 51:07 | 31:14 | 12:14 | 94:35 |

Criterion 7 compares with the baseline above, so this growth counts against the target: ciphertext grew about 1.6 GB a day
over 10-29..11-04.

## Capability probe (criterion 2)

Run 2026-11-05 on the receiver with its installed SDK (boto3 1.43.114) and its own client settings (`make_client`), on a
synthetic prefix (`anym-641-probe/<random>/objects/`) with random bytes, in the bucket the receiver's credential is scoped to.
Printed: labels, booleans, error codes and values derived from the synthetic bytes. Each run removed exactly its own objects
and uploads and read back that none were left. Nothing was written to the receiver's disk.

**As the card wrote it,** `UploadPart` with the part's `ChecksumSHA256` is refused:

```text
create: accepts SHA256 + COMPOSITE                         PASS answer algorithm=None type=None
botocore.exceptions.ClientError: An error occurred (NotImplemented) when calling the UploadPart operation:
  Header 'x-amz-checksum-sha256' with value '<digest of the synthetic part>' not implemented
```

**The other request form.** With `ChecksumAlgorithm=SHA256` the SDK sends the digest as an HTTP trailer, which R2 accepts:

```text
create {'ChecksumAlgorithm': 'SHA256', 'ChecksumType': 'COMPOSITE'} ACCEPTED {}
  UploadPart ChecksumSHA256 (explicit header)                      REFUSED NotImplemented/501
  UploadPart ChecksumAlgorithm=SHA256 (SDK computes)               ACCEPTED {'ChecksumSHA256': 'Brb55vOW...'}
create {'ChecksumAlgorithm': 'SHA256'}                             ACCEPTED {}
  UploadPart ChecksumSHA256 (explicit header)                      REFUSED NotImplemented/501
  UploadPart ChecksumAlgorithm=SHA256 (SDK computes)               ACCEPTED {'ChecksumSHA256': 'Brb55vOW...'}
create no checksum                                                 ACCEPTED {}
  UploadPart ChecksumSHA256 (explicit header)                      REFUSED NotImplemented/501
  UploadPart ChecksumAlgorithm=SHA256 (SDK computes)               ACCEPTED {'ChecksumSHA256': 'Brb55vOW...'}
PutObject ChecksumSHA256 (single object, for comparison)           ACCEPTED {'ChecksumSHA256': 'JOnIXITx...'}
```

**(a)-(d) in that form.** (c) changes the request on the wire, so that only R2's own check can catch it:

```text
client: receiver make_client settings; parts 5 MiB, 5 MiB, 1 MiB; bytearray bodies
wire form: x-amz-checksum-sha256 header=False, x-amz-trailer=b'x-amz-checksum-sha256', x-amz-content-sha256=b'STREAMING-UNSIGNED-PAYLOAD-TRAILER'
(a) each UploadPart returns the part SHA-256                   PASS
(b) completion returns the composite = b64(sha256(digests))-N  FAIL got=None expected=Xqe/J16f63Cf7iGxp8iO3ORnbbwG4NVCTQooiT4w2z4=-3 type=None
    stored bytes equal the parts                               PASS
(c) UploadPart with a wrong trailer digest is rejected         PASS BadDigest/400
(c) UploadPart with one changed body bit is rejected           PASS BadDigest/400
(d) completion with a wrong part checksum is rejected          FAIL accepted; answered composite=None expected=Xqe/J16f63Cf7iGxp8iO3ORnbbwG4NVCTQooiT4w2z4=-3
(d) completion with reordered part checksum is rejected        FAIL accepted; answered composite=None expected=Xqe/J16f63Cf7iGxp8iO3ORnbbwG4NVCTQooiT4w2z4=-3
(d) completion with one missing part checksum is rejected      FAIL accepted; answered composite=None expected=Xqe/J16f63Cf7iGxp8iO3ORnbbwG4NVCTQooiT4w2z4=-3
(d) completion with all missing part checksum is rejected      FAIL accepted; answered composite=None expected=Xqe/J16f63Cf7iGxp8iO3ORnbbwG4NVCTQooiT4w2z4=-3
cleanup: objects left=0 open uploads left=0
RESULT SOME FAILED
```

**Result: the gate failed on (b) and (d), and on (a) in the card's own request form.** R2 checks each part as it arrives, but
it does not bind the completed object to those checksums. By criterion 2, criterion 3 and criterion 5's upload half are dropped
and the post-upload GET stays. Criterion 4 ships. Not probed: whether HEAD with `ChecksumMode=ENABLED` shows a composite after
completion.

**What criterion 4 relies on,** probed on a plain multipart object, as the receiver uploads today:

```text
binding: stored bytes equal the parts           True
binding: ETag completion == HEAD == GET         True
binding: ContentLength HEAD == GET == size      True
binding: ranged IfMatch GET answers the same ETag True
binding: ranged GET with another IfMatch        REFUSED PreconditionFailed/412
binding: a replacement changes the ETag         True
cleanup: objects left=0 open uploads left=0
```

## What this leaves for criterion 7 (estimate, not a measurement)

The sidecar removes the candidate's re-hash from promotion, which is most of the "Promotion" column above. At 10-30's size that
gives about 67 min. At 11-04's it gives about 83 min, above the 65 min target and above the 76 min baseline, and the data is
still growing. Only the measured chains after the receiver is updated decide criterion 7.
