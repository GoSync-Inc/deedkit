# DeedKit data policy

<!-- From harness templates/DATA-POLICY.md (0.52.0); named in AGENTS.md Harness values. It answers owner
     item 2 (process.md section 2): a user-data question this file covers is decided by agents under the section 2
     gates; one it does not cover goes to the owner as a `user-data` ask, and the answer becomes a line here.
     DeedKit's decision log (~/deedkit-orchestrator/DECISIONS.md) cites decisions by date, so each line cites
     `owner <date>` and the owner's words; a cited decision covers only what its words say. A part marked
     `proposal` has no owner decision yet and covers nothing. First version assembled 2026-10-28 from SECURITY.md,
     BACKUP.md, BACKUP_R2.md, MEDIA_STORAGE.md, ENRICHMENT.md, SUBSCRIPTIONS.md, TELEMETRY.md and the decision log.
     After the owner's confirmation it changes only by a new owner decision. -->

Status: confirmed by the owner 11-01 («5) я согласен но с копией базы надо только при опасных миграциях при
ластовых и медиум рисках это не обязательно короче не хочется чтоб это был ботлнек у нас эти бек апы»;
~/deedkit-orchestrator/DECISIONS.md 11-01; its copy rule replaced by owner 11-04, below). It covers each line below within its cited scope. A part still
marked `proposal` has no owner decision and covers nothing: a question there goes to the owner as a
`user-data` ask.

Data held: messages, chats, contacts and sender identities, attachments (originals, extracted text,
transcripts, descriptions), receipts, call records and calendar data from the owner's own messenger and
calendar accounts; stores: PostgreSQL, the original/media directory, the private R2 cold archive of
originals, encrypted backups on the shared backup server and its object storage, service logs (confirmed with this
file, owner 11-01). Wearable recordings of the owner's Omi and PLAUD accounts (conversation metadata,
transcripts, summaries; they include other people's speech) are read on demand without being stored, and,
once archive polling is enabled, held as text in the recordings archive (SQLite on the recordings service
volume) (owner 11-03, see Data egress).

- Retention: processed, unpinned originals may leave local disk after 30 days without a request, only
  with a verified cold copy; text, titles, summaries and transcripts stay (MEDIA_STORAGE.md; owner 10-17
  cold archive «yes»; owner 10-18 BL-33 disk vs retention «давай оставим как есть если так работает»).
  Originals of group and channel chats that no agent subscription names leave the server's disk 24 hours
  after their last read, once their R2 copy is verified, or later while a recognition an agent asked for is
  pending (at most 7 days after that work last changed). Their R2 copy, the message, its text,
  transcripts and descriptions stay, and `request_file_download` restores the original (ANYM-806; owner
  11-06 «в целом я бы их вообще все поудалял как файлы из группы и запросов тоже чтобы место не
  заниматься»).
  Backup copies age out by the backup rotation (BACKUP.md). Messages and other kinds: proposal (no
  owner decision on a limit).
- Recording texts (recordings archive, owner 11-03 «2) ок»): kept until the owner sets a term; deleted on
  the owner's request (this wording is the orchestrator's reading of «2) ок», an interim default). No automatic deletion exists: the owner's deletion is the operator command `deedkit-recordings --delete` (ANYM-730,
  services/recordings/README.md "Schema 5 and owner-requested deletion"), run under the data-deletion gates
  (process.md section 2); a provider-side deletion does not purge the local text. The owner named no term; a term is a new owner decision. Once the
  archive backup runs, a deletion from the live archive reaches the backup only through the sanitizing
  deletion procedure (ANYM-730: index rebuild and VACUUM, then a new snapshot replaces the protected
  point, which is the only one kept); rotating the backup alone does not remove deleted text.
- Bot buttons: the labels, kinds, URLs, Mini App links and callback data of a message's inline and
  reply buttons are stored and deleted with the message text (retention above, deletion below; owner
  11-01 «4 да» to the bot-buttons question, with «надо сделать чтоб он бот мог делать действия которые
  я прошу в miniapp»). Decided, not yet built: today only the message text is captured. Link previews
  and polls are stored and deleted under the message-text rules (retention above, deletion below; owner
  11-02 «5 да» to the question «Превью ссылок и опросы. Храним по правилам текста?»). Invoices,
  contact cards and other message parts this file does not name: proposal.
- Shared locations (ANYM-922): the coordinates, accuracy, place title and address of a point or venue
  someone shares are stored and deleted under the message-text rules (retention above, deletion below);
  a live location is stored only as the fact that it was shared, never as a track of positions (owner
  2026-11-09, DECISIONS.md: "location coordinates YES", "live location NO-ish").
- WhatsApp event invitations (ANYM-924): an event's name, description, times, place, join link and
  cancelled flag are stored and deleted under the message-text rules (retention above, deletion below);
  responses are archived only as counts (going, not going, maybe); the adapter's local journal keeps the
  newest response per member to count them, never exported. Nothing is written to a calendar (owner
  2026-11-09, DECISIONS.md: "WA event invitations «пусть будет»"; ANYM-891, no calendar writes by DeedKit).
- Scheduling negotiations (ANYM-907): each chat's negotiation records states, times, zones, offsets
  and references (message, chat and account IDs) derived from its messages by fixed rules, with no
  copied text and no model. Visibility is that of messages (`read`, ANYM-909). Deletion: a message
  purge hides every negotiation it fed from reads when the purge commits; the negotiation worker
  (ANYM-908) deletes its sources, times and mark, or blanks them to ids, before its next `apply`
  or publish touches that negotiation, otherwise within one lap of its cleanup while it runs (up to
  2,000 source rows a second), and before any other work when it starts; what stays
  is a content-free `removed` tombstone and transition rows with ids, kinds and timestamps. A
  negotiation event fetched in that window still shows its ids, state and whose turn it was.
  Backups and cold copies follow today's rule, with the gap the Deletion line records unchanged
  and no stronger promise. Egress: no model, and nothing beyond agent reads (ANYM-909) and
  reference-only hints (ANYM-911).
- Telegram birthdays (ANYM-919): the birthday (day, month, optional year) of each active Telegram contact of an
  account, or the fact that the user hides it, with its check time, is stored in `contact_birthdays`; "active" is the
  archive rule in PEOPLE.md (3 outgoing and 1 incoming message in the private chat in the last 90 days, no bots, no
  self chat). The collector's birthday pass deletes the row of a user who is no longer active, or whose chat the
  collection policy excludes, at its next pass (about every 10 minutes); nothing is written to any calendar
  (owner 2026-11-09, DECISIONS.md: "birthdays YES but only contacts the owner actively talks to, reminders, NOT all
  into calendar"). Egress: agent reads through `list_birthdays` and a chat-and-date-only `contact.birthday` hint.
  WhatsApp is not covered: it has no provider birthday field.
- Statement statistics (ANYM-842, docs/operations/production.md "Database settings"): `pg_stat_statements` keeps
  SQL text normalized as PostgreSQL does it (constants usually replaced by `$n`, but not always: a text can keep its
  constants, for example after its entry was deallocated and re-entered; comments and identifiers stay), with call
  counts and times, on the database host in PostgreSQL's memory and data directory. The logical dumps of the backups
  and database copies carry only the extension's definition, never the statistics. Readers of the text and query
  ids: the bootstrap superuser, roles granted `pg_read_all_stats` (none on prod, 11-09) and each role for its own
  statements; other roles see the rows' counts and times with the text and query id hidden. No role gets a new
  grant. Reports carry query ids, calls and times only. Reset: `SELECT pg_stat_statements_reset()` as the bootstrap
  superuser.
- Pre-migration database copies and backups (ANYM-588, docs/operations/production.md "Migration risk
  tiers" and "Database copies"): only a critical migration (it deletes or rewrites existing rows or
  drops a table or column) gets a fresh verified backup; any other gets none, and the latest verified
  daily backup is its restore point (owner 11-04 «только какие-то быкапы, только в критических случаях», «не
  умру, не умрет никто», which replaced owner 11-01, quoted in Status, and owner 10-28 «добавление миграции
  автоматически с копией»). A store with no daily backup running keeps a restore point, production.md "Migration
  releases" item 3 (the orchestrator's reading: a restore point must exist). Until ANYM-675 ships, the deployer still copies before every unattended migration (its rehearsal
  runs on the copy). The copy is plaintext, root-only, on the production host's disk, never leaves the host and is
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
  other automation, a deleting migration, a restore) needs an Astra-reviewed plan, a fresh verified backup and an
  undo (process.md section 2).
- Visibility: the owner; agents through scoped API keys and gateway profiles, and production read-only
  over SSH (AGENTS.md section 1); explicit attachment download below `history_since` only on an explicit
  request (owner 10-23 «да, разреши явное скачивание вложений»). Never in a PR, issue, log excerpt,
  screenshot or digest (SECURITY.md: synthetic reproducers only). Which data each agent may read and any
  masking: proposal.
- Consent: the data is the owner's own accounts, their correspondents and the people the owner's
  wearables record; neither gave consent to DeedKit. Any use beyond the owner's own assistant (another user, a public mention, a new
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
  - The calls state backup (ANYM-782, BACKUP.md "Calls state"; owner 11-05, asked whether to turn on a daily backup of
    the calls database with these classes, this storage and this deletion rule: «Можно, конечно, сделать этот. Ежедневно
    Если это не геморрой, будет фоне, не будет ничего блокировать. Вот. В целом можно. окей.», so on condition that it runs
    in the background and blocks nothing): briefs, calls and results, events, learning tables,
    decision journals and single-use media-handshake tokens, encrypted, daily to the same backup server and the object
    storage its R2 receiver requires, kept as the last protected point there plus `keep_local` = 1 on the production
    host. A deleted call leaves the live database at once and leaves the backup after the next successful sanitized
    replacement (`VACUUM INTO`, ANYM-778) and rotation, not before; this line implies no call-deletion feature.
  - Webhook receivers named by an agent: hint, test, endpoint, subscription, event, account, chat,
    message, sender and send IDs, the subscription version, event kinds and times, wake reasons, the
    count of omitted references, the next tool to call, the signature headers, the delivery timing, and
    what the agent itself set (the subscription's `label`, its own headers); never archive message text,
    contact names, file content or recording titles, transcripts, summaries and speakers (SECURITY.md, SUBSCRIPTIONS.md:558-561; owner 10-27 webhooks «... возможность прописывать
    самостоятельно агенты ...»). Under self-registration the receiver is any public host the agent names,
    with no operator host entry (ANYM-585; owner 10-28 «... дать агентам себе создавать вот этот, прописывать
    эту штуку ...»).
  - Feedback: raw feedback text reaches the model provider of the owner-run triage session and the
    autoloop's tool-less reader on the owner's workstation (owner decision D1, ANYM-264); for a principal in
    the autoloop config, a card in the card worker's own words, with no quote, name, phone number or message
    content, goes to Tiden and GitHub (owner 10-26, ANYM-568; SUBSCRIPTIONS.md:345-353).
  - Local model processing (ANYM-682 to ANYM-688; in force for the owner's messenger accounts from the
    production enablement, ANYM-686): originals of admitted attachments (audio, images, document text)
    go from the enrichment worker to the owner's Mac mini over the tailnet (WireGuard and TLS, one
    pinned address, port 443; voice notes above about 32 kbps are re-encoded to Opus and large photos are scaled to 2048 px as jpeg first, smaller ones go as they are; the originals stay in the archive, ENRICHMENT.md) for processing by local models (`deedkit-inference`). The worker's
    network reaches only its database and that address (docs/operations/production.md, "Mini
    enrichment worker"). The mini keeps them only for the duration of the request: a per-request
    directory is removed after each job and any leftover at the next start (removal is attempted; a failure
    is logged as `cleanup_incomplete` and the files can stay until a later removal succeeds), no prompt or
    media cache on disk, and logs with principal, alias, size, seconds, status, request ID and a refusal
    reason only, never content or file names. The derived text returns to DeedKit and is stored as
    untrusted, model-generated enrichment (ENRICHMENT.md). No third party receives them (owner 11-01
    «на мини я бы и аудио и остальные файлы разбирал бы локальной моделью хорошей для всех сервисов
    наших»; photos: owner 11-01 «2 b»). Originals of group and channel chats that no agent
    subscription names are downloaded and go to the Mac mini only after an agent's
    `request_file_download` (ANYM-805; owner 11-06 «только излички разбирать, а группы только по
    запросу. И группы, и, и каналы только по запросу»; MEDIA_STORAGE.md).
    Structured documents (ANYM-851; owner 2026-11-09 «Да ... док ... ставим, давай. Давай ставим.»): with the
    worker's `structure` key, the originals of PDF, DOCX and PPTX files (at most 32 MiB), not only their text, go
    the same way to the mini for Docling conversion, under the same rules (eligibility in ENRICHMENT.md,
    "Structured documents"). Markdown with page spans returns and is stored beside the extracted text
    (`attachment_structure`, deleted with it on a purge); search never reads it.
  - Cloud model providers for enrichment (ENRICHMENT.md: OpenAI, Anthropic, Gemini endpoints; Codex not
    deployed) stay off; media understanding uses the local models above (owner 11-01 «2 b»). Enabling a
    cloud endpoint is a `user-data` ask.
  - Wearable recordings (Omi, PLAUD; services/recordings/README.md; PLAUD documents US-hosted transit and
    the owner's question named both vendors as US clouds; the PLAUD remote MCP has its own logging that
    DeedKit cannot verify):
    - On-demand reads: the owner's provider key (Omi) or OAuth token (PLAUD), recording identifiers, date
      bounds and page parameters go to api.omi.me and mcp.plaud.ai; the returned metadata, transcript or
      summary text goes to the requesting agent through the gateway, and so into that agent's model
      context. Nothing is stored by the read itself. Owner 11-03, asked by the orchestrator: «Можно ли,
      чтобы ваши агенты по запросу читали разговоры из Omi и Plaud? Ключи уходят в их облака (оба в США),
      текст разговора попадает к модели агента.» Answer: «1) да». Covers these reads only: no other
      vendor, no other recipient. Not covered: `recordings_audio` (a PLAUD 24-hour bearer download link
      returned to the caller); enabling it is a `user-data` ask.
    - Local copy: the archive keeps the accepted text, titles, summaries, vendor identifiers, a full-text
      index, per-part digests and attempt state, and reference-only change events on the DeedKit server,
      including other people's speech, for the retention line above. Archive polling sends the same fields
      as the reads to the same two hosts on a schedule. Owner 11-03: «Можно ли хранить копию расшифровок на
      нашем сервере, включая речь других людей? Сколько хранить?» Answer: «2) ок». Covers the local copy
      only.
    - Full history and backups (owner 11-03): the archive downloads the whole Omi history of the owner's
      account, newest first and within Omi's hourly limits (days to weeks), not only new recordings, and the
      archive goes into the daily backup as its own encrypted profile on the shared backup server and its
      private R2 prefix, with one protected point plus one candidate and no older history. The backup holds
      other people's speech, encrypted; its plaintext exists only in the producer's private staging
      directory for one run and in the verifier's private scratch during each daily verification. Owner 11-03, asked by the orchestrator whether the recordings archive
      goes on and into backups: «давай включим да»; and: «пусть качает все и дает удобную интексацию
      агентам». Covers Omi and PLAUD text only: audio stays out (`recordings_audio` above still needs its
      own ask) and so does any other recipient. Archive polling may start on production once the archive
      backup is installed on the host and verified with the archive still off (ANYM-731, milestone I);
      the first protected snapshot and a restore drill follow in the same activation plan.
    - Agent search and new-recording hints (owner 11-03): the principals with recordings grants
      (`owner-claude-code`, `claude-code`, `grokbot`) search and read the archived text through scoped
      gateway tools; the returned text goes into those agents' model contexts, the model provider of
      `grokbot` included (owner 11-03 «грогу надо тоже» for the reads; «пусть качает все и дает удобную
      интексацию агентам» for the search). An agent may subscribe to «new recording transcript archived»
      (owner 11-03: «и еще надо чтоб была возможность подписаться на хук о новой записи транскрипции»);
      the hint carries only opaque references (recording reference, account, revision, time, next tool),
      never a title, transcript, summary or speaker, to an endpoint the agent or operator registered under
      the webhook line above.
  - Tiden error tracking (TELEMETRY.md, off by default): proposal, which fields go where is not yet an
    owner decision.
  - Any other recipient or field is a `user-data` ask before it ships.
