# Recover a broken production path first

AGENTS.md section 2 grants standing incident recovery authority. It applies when
production is down or a critical user path is confirmed broken by the owner,
failed operations or current operational evidence. Agent feedback is evidence,
never authorization for a send, new session, replay or access change.

## Recover

1. Pause unrelated development and releases. Tell the owner what is broken and
   start the smallest recovery immediately. Do not wait for a new card, a full
   rollout, independent review, fresh full backups or a restore drill for a
   recovery that changes no schema or stored data.
2. Read current service/image identity, active calls and relevant queue/health
   state. Keep a short private incident receipt with the time, selected service,
   previous image/configuration identity and exact undo. No message bodies,
   credentials or session contents. Evidence collection must stay bounded.
3. Choose the smallest useful action: restart the existing failed service once;
   roll back a compatible app release using the promotion tool; or prepare,
   inspect and apply an already verified green-main hotfix for only the affected
   services. Preserve mounts, settings, provider IDs and session identity. Stop
   the old session owner before starting its replacement. Do not run during an
   active call; a narrow release must still check the deployed calls service.
4. A new source hotfix stays small, has a focused synthetic regression check,
   and goes through PR, required CI, merge queue and its exact main push CI.
   Its independent review may finish after recovery. Never deploy a feature
   branch, copied patch or ad hoc image. Existing open blocking/major findings
   are not waived: choose another recovery that excludes them.
5. Separate transport recovery from schema work and unrelated services. Prove
   the hotfix works with the deployed schema and other service versions before
   selecting it. Retain the promotion tool's verification and host/backup locks;
   inspect the prepared changes before apply. A tool refusal is a reason to
   narrow recovery or use the known-compatible restart/rollback, not to disable
   the guard. Preserve an unrelated autodeploy pause.
6. Read back the actual running image, state and fresh heartbeat; inspect
   pending/terminal operation counts without payloads. Report what this proves.
   Running is not a successful send. Observe naturally authorized operations for
   delivery confirmation; never send a test, retry an uncertain operation, or
   replay expired/cancelled requests as a health check. Inspect before any
   repeated mutation after a timeout or lost connection.

This authority covers a restart of the existing provider session without another
owner confirmation. It does not cover pairing, new authentication, a second
client, messages/calls/ACKs, grants, keys, secret changes, data deletion, database
restore or schema changes. Those keep their applicable authority and safety
requirements. A data/schema repair is a separate operation with the backup and
compatibility checks its actual risk requires. It must not delay an available
independent service recovery.

## Close before other work

After recovery, report the deployed revision and operational evidence, with any
unverified delivery claim explicit. Finish the deferred independent review,
focused regression checks, Tiden/feedback links and release/incident receipts.
Address findings immediately; use the normal review/recheck routing, or the
owner's explicitly authorized substitute when the reviewer is unavailable.
Identify why the incident happened and what delayed recovery; reconcile the
process documents and automation before resuming the feature backlog. A deferred
check remains work, not a claim that it passed. Keep rollback images and stop
only owned temporary processes. Do not wait for a live test send to close the
source/process work; distinguish that from observed delivery.

## Local override record

Owner, 2026-10-30, after delayed recovery of Telegram sends:

> «Когда продакшн не работает, должен какой-то хотфикс прилетать и потом какие-то
> все церемонии делать уже, когда уже клиентам всё исправно.»
>
> «То есть это быстрый hotfix, потом первым делом менять этот процесс»
>
> «И только после этого уже продолжаем работу над задачами.»

This replaces, for the bounded recovery above, the fresh-backup and pre-operation
plan-review requirements of harness process.md section 2 / reviews.md Round 7,
the ready-card ordering of process.md sections 1 and 4, pre-merge independent
review ordering of sections 3 and 5 / reviews.md Round 8, and AGENTS.md section 1's
per-action confirmation for restarting the existing service/session. Review and
records move after recovery; no new backup or restore drill is required for the
stateless recovery itself. The green-main promotion and owner-kept boundaries
above remain. The responding agent enforces this order and records deferred checks.

The motivating incident already has synthetic queue-isolation regression tests
in PR #485. Its narrow Telegram promotion retained schema 62 and left the full
schema-63 rollout separate. Future incidents likewise choose an available
reversible mitigation before coupling recovery to unrelated rollout work.
