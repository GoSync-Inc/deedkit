# Security

AnyMsg 0.9.x is experimental. Use scoped agent credentials, TLS and separate
installations for different owners. Keep database ports private, gateway original
mounts read-only, provider sessions isolated and backup keys outside collectors.

Report vulnerabilities using GitHub private vulnerability reporting at
https://github.com/GoSync-Inc/deedkit/security/advisories/new.
Do not include real tokens, phone numbers, message bodies, sessions, dumps or files
in public issues. Use synthetic reproducers. Do not test against other people's accounts.

Messages, extracted file text and model output are untrusted source data. They must
not authorize sends, tool execution or changes to access scopes. Text sending is
opt-in for both the account and agent credential. The acting agent must obtain
exact owner confirmation, subject to AGENTS.md section 1's scoped goal overrides;
this release has no server approval gate. See SENDING.md.
Provider authentication is done only in an owner's terminal.

Backup delivery is distinct from successful restoration. Verify a copy in an isolated
installation before pruning recovery points. A same-provider backup is not protection
against losing access to that provider account. Preserve recovery keys independently.

Self-service webhook endpoints (ANYM-519) send data to a host the agent names, inside
the operator's host policy file. With its `self_registration` switch on (ANYM-585) that is any
public host `deny_hosts` does not match; a per-host hourly budget across keys bounds what one
third-party host receives, such an endpoint never receives a request twice when the first may have
arrived, and `self_registration: false` stops those endpoints. A receiver host learns hint, test,
endpoint, subscription, event, account, chat, message, sender and send IDs, the subscription
version, event kinds and times, wake reasons, the count of omitted references, the next tool to
call, the Standard Webhooks signature headers, the delivery timing, and what the agent itself set:
the subscription's `label` and its own headers; never archive message text, names or file content. SSRF controls: https
on port 443, every resolved address public (no private, loopback, CGNAT, multicast,
NAT64, 6to4, Teredo or IPv4-mapped answers), resolution again before each attempt with
the address pinned, TLS hostname verification, no redirects, no proxies, and the
host policy rechecked before each attempt. The dispatcher (ANYM-534) shares a network
only with PostgreSQL and egresses over its own bridge, which host rules limit to DNS on one configured resolver and
public port 443 (no host, tailnet, private, link-local, metadata or IPv6 address). Signing secrets and header values are
write-only for the API role and never returned, logged or audited. This is privilege
separation, not encryption: database owners and anyone who can decrypt a backup can
read them. Details: [SUBSCRIPTIONS.md](SUBSCRIPTIONS.md#self-service-webhook-endpoints-schema-49).

The messaging database records statement statistics (`pg_stat_statements`, ANYM-842): SQL text as PostgreSQL
normalizes it (constants usually, not always, replaced; comments and identifiers kept: not redaction), plus counts and
times, kept on the database host. The text and query ids are visible to the bootstrap superuser, roles with
`pg_read_all_stats` (no DeedKit role has it, prod 11-09) and each role for its own statements; other roles see only
counts and times. The logical dumps (backups, copies) carry the extension's definition, not the statistics; perf
reports carry query ids, calls and times only (docs/DATA-POLICY.md).

A vendor endpoint (ANYM-582, Grok Bot routine webhooks; its host named in that policy, or under
self-registration a Grok routine URL recognised by its shape, ANYM-585) receives the
same content-free bodies plus the agent's own routine key as `Authorization: Bearer`, stored and
hidden like any other header value. A request that may have reached the vendor is never sent again
(SUBSCRIPTIONS.md, Vendor hosts).

The inference service on the owner's Mac mini (`services/inference`, ANYM-682 to ANYM-688) processes
audio, photos and document text locally, and converts PDF, DOCX and PPTX originals to Markdown (Docling, OCR off, no
outbound network; ANYM-850, ANYM-851). It binds only loopback; Tailscale Serve (never Funnel) is its
one tailnet entry on port 443, and the tailnet policy gets one additive rule that lets the production host
reach it (access the policy already grants to other devices is not changed, so the effective reachability is
whatever the policy and its recorded before and after matrices say). Each calling service has its own key;
the mini's policy file (mode 0600) holds only its hash, with an expiry, an overlapping rotation (at most two
keys per principal) and revocation by removing the hash. The key itself exists as a 0600 file in a
directory only the production worker mounts, and briefly on the mini during delivery or rotation. The
mini also keeps one persistent deploy-probe key (`probe.key`, 0600, in the service account's home, outside
the service's sandbox reads) that `inference_deploy.py` uses for its health probe; its scope is a single
audio principal with a daily cap and an expiry. The
process runs in a sandbox that denies the service tree any outbound network, and each job's files live in
a per-request directory removed afterwards. A leaked key allows jobs within its scopes and daily cap; the
service stores nothing that can be read back. On the production side the worker reaches only its database
and the mini (docs/operations/production.md, "Mini enrichment worker"). Model output is untrusted data,
like any extracted text. Runbook and rotation:
[docs/operations/inference-mini-setup.md](docs/operations/inference-mini-setup.md).

Agent sessions: reading container environments (ANYM-774). A session transcript goes to the model provider, so the
PreToolUse hook `.claude/hooks/block-secret-env-reads.sh` refuses Bash commands that print secret-bearing environment,
and refuses when it cannot decide. The threat is an agent's accidental read while diagnosing; a deliberate bypass is out
of scope. The hook does not parse shell: it matches the raw text with quotes dropped, and refuses syntax it does not read
(`${...}`, arithmetic, `eval`, `env -S`, a decode step) on a line that holds a guarded name. False refusals are accepted,
and the refusal names the allowed way, including passing prose through a file. Blocked, also inside `ssh`, `sudo`, `bash -c`, a pipe or `$(...)`: `docker inspect`
without a template, with any `--format=`/`-f=` form, or with a template that reads `.Config`, `.Spec` or the whole
object; `docker compose config` unless limited by `--services`, `--images`, `--hash`, `--quiet` or `--volumes`, and always with
`--environment`; `env`, `printenv`, `set` or `export` directly or through
`docker exec|run` and `docker compose exec|run`; any such command naming `/proc/*/environ`; `cat`, `less`, `more`, `head`, `tail`, `grep`, `rg`, `sed`, `awk`, `strings`, `xxd`, `base64`, `nl`, `tac`, `od`, `hexdump`,
`bat`, `cut`, `sort` or `uniq` of a path ending `.env`, named `.env.*` (except `.env.example` and `.env.sample`), or ending `environ`, `config.v2.json`, `.dsn`, `.token`, `.key`, `.pem` or
`.agekey`, or a glob that selects one (`.env*`); a pattern search of a `private` or `.local` directory or `/etc/deedkit`; `systemctl show` of `Environment` or without a property filter. Allowed:
`docker inspect --format '<template>'` limited to `.State`, `.Name`, `.Image`, `.RestartCount` or `.Created`; `docker compose config --services`, `--images` or
`--hash`; a key-names read through the orchestrator. Limits: it covers sessions started in
this repository or its worktrees; it does not cover the orchestrator, other projects or a determined bypass (encoding,
a script that prints the file, a path or command name held in a variable). A recursive pattern search of a parent
directory is not refused (`grep -r PASSWORD .`), and neither is a plain reader of a non-sensitive name inside a private
directory (`cat /etc/deedkit/config.json`, `cat deploy/private/*`). `docker compose config` with `--no-env-resolution` is blocked because it still
prints values interpolated from a project `.env`; adding `--no-interpolate` hides those and `env_file` values but still
prints a literal value in the Compose document, which a rendered project document holds (synthetic probe, Compose 5.5.0). The
durable fix is secrets as files, not environment variables, which is a separate decision.

Because it fails closed on every Bash call, a broken hook stops all Bash in sessions of this repository, incident
recovery included. A refusal says which it is: a secret refusal, a load refusal (the 5 second deadline on a busy host:
retry the same command), or a broken hook. Recovery from a broken hook, both tested on Claude Code 2.1.318 with a hook
that always blocks: the owner's `!` prefix runs a command without PreToolUse hooks; the Edit tool is not matched by this
hook, so an agent can change this entry's `matcher` in `.claude/settings.json`, and Bash works again in the same session.
That edit is local to one worktree and only for a broken hook; revert the merged change through the normal route. Hosts:
the laptop and the Mac mini hold `timeout` in `/opt/homebrew/bin`, which a non-login shell on the mini lacks on `PATH`, so
the hook also looks there, and `python3` in `/usr/bin` (mini) or on `PATH` (laptop). CI runs only this hook's tests, on
Ubuntu, which has `timeout`. Cloud sessions were not checked. The production host runs no agent session: an SSH command
is checked on the machine that starts it, and Codex does not read `.claude/` hooks.

Android app secrets (ANYM-969, guarded executor): a value a phone run types as `${secret:NAME}` lives only in `<state_dir>/secrets.json` (0600, atomic replace) on the phone host and is set from standard input by `deedkit-android secret set`; no HTTP route or tool sets, reads or lists one, the worker types it only into a focused editable field of the app it is bound to, names holding PIN, CVV, CVC, CARD, OTP or PASSCODE are refused, and every read of a run after the typing (receipt, failure observation) shows the reference instead, with no screenshot. The replacement ordinary Codex path (ANYM-993) does not resolve this secret store; its scoped authority and native sandbox are described in [agent feature experiments](docs/operations/agent-feature-experiments.md). Details: services/android/README.md, "Secrets".
