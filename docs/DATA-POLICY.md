# DeedKit data policy

<!-- From harness templates/DATA-POLICY.md (0.52.0); named in AGENTS.md Harness values. It answers owner
     item 2 (process.md section 2): a user-data question this file covers is decided by agents under the section 2
     gates; one it does not cover goes to the owner as a `user-data` ask, and the answer becomes a line here.
     DeedKit's decision log (~/deedkit-orchestrator/DECISIONS.md) cites decisions by date, so each line cites
     `owner <date>` and the owner's words. A part marked `proposal` has no owner decision yet and covers nothing:
     its questions go to the owner as `user-data` asks. First version assembled 2026-10-28 from SECURITY.md,
     BACKUP.md, MEDIA_STORAGE.md, ENRICHMENT.md, TELEMETRY.md and the decision log; the orchestrator asks the
     owner once to confirm it. After that it changes only by a new owner decision. -->

Data held: messages, chats, contacts and sender identities, attachments (originals, extracted text,
transcripts, descriptions), receipts, call records and calendar data from the owner's own messenger and
calendar accounts; stores: PostgreSQL, the original/media directory, the private R2 cold archive of
originals, encrypted backups on the shared backup server and its object storage, service logs (owner
10-17 cold archive «yes»; owner 10-22 backups over the tailnet «надо на всех проектах так сделать»).

- Retention: processed, unpinned originals may leave local disk after 30 days without a request, only
  with a verified cold copy; text, titles, summaries and transcripts stay (MEDIA_STORAGE.md; owner 10-17
  cold archive «yes»; owner 10-18 BL-33 disk vs retention «давай оставим как есть если так работает»).
  Backup copies age out by the backup rotation (BACKUP.md). Messages and other kinds: proposal (no
  owner decision on a limit).
- Deletion: a message deleted in the messenger (for everyone, the owner's own delete-for-me when
  reported, a cleared chat after 24 h) has its content purged, keeping a content-free tombstone;
  backups and cold copies age out by retention, and the cold R2 lock is 30 days, so deleted content is
  not kept even in the cold archive (owner 10-25 «1) да 2) да 3) да», stories 511-516 «Ну пусть 30
  дней ... То, что удаляется, не должно храниться даже в холодном архиве»). Gates: an Astra-reviewed
  plan, a fresh verified backup, an undo (process.md section 2).
- Visibility: the owner; agents through scoped API keys and gateway profiles, and production
  read-only over SSH (AGENTS.md section 1); explicit attachment download below `history_since` only on
  an explicit request (owner 10-23 «да, разреши явное скачивание вложений»). Never in a PR, issue, log
  excerpt, screenshot or digest (SECURITY.md: synthetic reproducers only). Masking for agents:
  proposal.
- Consent: the data is the owner's own accounts and their correspondents; correspondents gave no
  consent to DeedKit. Any use beyond the owner's own assistant (another user, a public mention, a new
  purpose): proposal, a `user-data` ask.
- Data egress: backups leave encrypted to the shared backup server and its object storage, and
  originals to the private R2 bucket (owner 10-17, 10-22); webhook receivers get IDs, event kinds and
  times, never message text, names or file content (SECURITY.md; owner 10-27 webhooks «... возможность
  прописывать самостоятельно агенты ...»); autoloop card text to Tiden and GitHub (owner 10-26 autoloop
  Q1-Q3). Model providers for enrichment (ENRICHMENT.md: OpenAI, Anthropic, Gemini endpoints; Codex
  not deployed) and Tiden error tracking (TELEMETRY.md, off by default): proposal, which fields go
  where is not yet an owner decision. Any other recipient or field is a `user-data` ask before it
  ships.
