# DeedKit data policy

<!-- From harness templates/DATA-POLICY.md (0.52.0); named in AGENTS.md Harness values. It answers owner
     item 2 (process.md section 2): a user-data question this file covers is decided by agents under the section 2
     gates; one it does not cover goes to the owner as a `user-data` ask, and the answer becomes a line here.
     DeedKit's decision log (~/deedkit-orchestrator/DECISIONS.md) cites decisions by date, so each line cites
     `owner <date>` and the owner's words; a cited decision covers only what its words say. A part marked
     `proposal` has no owner decision yet and covers nothing. First version assembled 2026-10-28 from SECURITY.md,
     BACKUP.md, BACKUP_R2.md, MEDIA_STORAGE.md, ENRICHMENT.md, SUBSCRIPTIONS.md, TELEMETRY.md and the decision log.
     After the owner's confirmation it changes only by a new owner decision. -->

Status: draft, not yet confirmed by the owner. Until the orchestrator records the owner's one confirmation
(a `user-data` ask) as a line here, this file covers nothing: every user-data question goes to the owner, and
the lines below only describe current behaviour and the decisions on record.

Data held: messages, chats, contacts and sender identities, attachments (originals, extracted text,
transcripts, descriptions), receipts, call records and calendar data from the owner's own messenger and
calendar accounts; stores: PostgreSQL, the original/media directory, the private R2 cold archive of
originals, encrypted backups on the shared backup server and its object storage, service logs. Proposal:
the owner has decided parts (below), not this inventory as a whole.

- Retention: processed, unpinned originals may leave local disk after 30 days without a request, only
  with a verified cold copy; text, titles, summaries and transcripts stay (MEDIA_STORAGE.md; owner 10-17
  cold archive «yes»; owner 10-18 BL-33 disk vs retention «давай оставим как есть если так работает»).
  Backup copies age out by the backup rotation (BACKUP.md). Messages and other kinds: proposal (no
  owner decision on a limit).
- Pre-migration database copies (ANYM-588, docs/operations/production.md "Database copies"): the
  deployer copies a database before a migration (owner 10-28 «добавление миграции автоматически с
  копией»). The copy is plaintext, root-only, on the production host's disk, never leaves the host and is
  deleted after 48 h, whatever its state (the orchestrator's reading of 2026-10-28; a later owner line may
  tighten it). Its receipts hold table names, row counts, sizes, hashes, times and failure codes only.
- Deletion: a message deleted in the messenger (for everyone, the owner's own delete-for-me when
  reported, a cleared chat after 24 h) has its content purged, keeping a content-free tombstone (owner
  10-25 «1) да 2) да 3) да»). Requested, not yet current behaviour: deleted content should not stay even
  in the cold archive, with a 30-day cold lock (owner 10-25, stories 511-516 «Ну пусть 30 дней ... То, что
  удаляется, не должно храниться даже в холодном архиве»). Today BACKUP_R2.md:444-454 says rotation never
  deletes cold copies and prescribes an indefinite bucket lock; the cold receiver can only put and get.
  Closing that gap is open implementation and configuration work (story 516). Gates: a DELETE statement
  the running service executes on rows this line or a ready card names is ordinary, PR-listed code
  (harness reviews.md, `Deletes:`); any other deletion (TRUNCATE, DROP, a script, data fix, deploy hook or
  other automation, a migration, a restore) needs an Astra-reviewed plan, a fresh verified backup and an
  undo (process.md section 2).
- Visibility: the owner; agents through scoped API keys and gateway profiles, and production read-only
  over SSH (AGENTS.md section 1); explicit attachment download below `history_since` only on an explicit
  request (owner 10-23 «да, разреши явное скачивание вложений»). Never in a PR, issue, log excerpt,
  screenshot or digest (SECURITY.md: synthetic reproducers only). Which data each agent may read and any
  masking: proposal.
- Consent: the data is the owner's own accounts and their correspondents; correspondents gave no
  consent to DeedKit. Any use beyond the owner's own assistant (another user, a public mention, a new
  purpose): proposal, a `user-data` ask.
- Data egress (current flows; each covers only its cited scope):
  - Backups, encrypted, to the shared backup server over the tailnet and its object storage; originals,
    encrypted, to the private R2 bucket (owner 10-17 cold archive «yes»; owner 10-22 «надо на всех проектах
    так сделать»).
  - The family calendar backup (ANYM-607, BACKUP.md "Family calendar"): the calendar database, its Google
    and iCloud credentials with the key that opens them, and its database passwords (the profile files),
    encrypted to the calendar recovery key (held by the backup server's root and, once the owner has stored
    it, the owner's offline copy), daily to the same shared backup server over the tailnet and its own
    prefix in the same private R2 bucket, under the same token (owner 10-29 «почему у нас не может быть
    один и тот же токен Р2 для всех бэкапов»): one leaked token exposes both prefixes' encrypted objects,
    not their recovery keys. Each verification opens the tokens in memory on that server and never writes
    them. One protected point plus room for one candidate: the rotation keeps no older point (owner 10-28
    «Ну вообще можно регулярный, ну то есть все одинаковый, короче, пока делать, да»). Agent key files are
    not in it.
  - Webhook receivers named by an agent: hint, test, endpoint, subscription, event, account, chat,
    message, sender and send IDs, the subscription version, event kinds and times, wake reasons, the
    count of omitted references, the next tool to call, the signature headers, the delivery timing, and
    what the agent itself set (the subscription's `label`, its own headers); never archive message text,
    contact names or file content (SECURITY.md, SUBSCRIPTIONS.md:558-561; owner 10-27 webhooks «... возможность прописывать
    самостоятельно агенты ...»). Under self-registration the receiver is any public host the agent names,
    with no operator host entry (ANYM-585; owner 10-28 «... дать агентам себе создавать вот этот, прописывать
    эту штуку ...»).
  - Feedback: raw feedback text reaches the model provider of the owner-run triage session and the
    autoloop's tool-less reader on the owner's workstation (owner decision D1, ANYM-264); for a principal in
    the autoloop config, a card in the card worker's own words, with no quote, name, phone number or message
    content, goes to Tiden and GitHub (owner 10-26, ANYM-568; SUBSCRIPTIONS.md:345-353).
  - Model providers for enrichment (ENRICHMENT.md: OpenAI, Anthropic, Gemini endpoints; Codex not deployed)
    and Tiden error tracking (TELEMETRY.md, off by default): proposal, which fields go where is not yet an
    owner decision.
  - Any other recipient or field is a `user-data` ask before it ships.
