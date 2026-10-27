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
exact owner confirmation; this release has no server approval gate. See SENDING.md.
Provider authentication is done only in an owner's terminal.

Backup delivery is distinct from successful restoration. Verify a copy in an isolated
installation before pruning recovery points. A same-provider backup is not protection
against losing access to that provider account. Preserve recovery keys independently.

Self-service webhook endpoints (ANYM-519) send data to a host the agent names, inside
the operator's host policy file. A receiver host learns subscription, event, account,
chat, message and sender IDs, event kinds and times, the delivery timing and the
agent's own headers; never message text, names or file content. SSRF controls: https
on port 443, every resolved address public (no private, loopback, CGNAT, multicast,
NAT64, 6to4, Teredo or IPv4-mapped answers), resolution again before each attempt with
the address pinned, TLS hostname verification, no redirects, no proxies, and the
host policy rechecked before each attempt. The dispatcher (ANYM-534) shares a network
only with PostgreSQL and egresses over its own bridge, which host rules limit to DNS on one configured resolver and
public port 443 (no host, tailnet, private, link-local, metadata or IPv6 address). Signing secrets and header values are
write-only for the API role and never returned, logged or audited. This is privilege
separation, not encryption: database owners and anyone who can decrypt a backup can
read them. Details: [SUBSCRIPTIONS.md](SUBSCRIPTIONS.md#self-service-webhook-endpoints-schema-49).
