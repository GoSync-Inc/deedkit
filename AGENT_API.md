# Agent tool rulebook v1

One contract for every agent tool of every module (ANYM-544). The types, the validators, the status maps and the
shared conformance matrix live in `packages/platform-contracts/src/platform_contracts/agent.py` and
`agent_conformance.py`; this document is their prose. A new tool follows these rules; an older tool keeps its shape
until its own story moves it, and the module's contract tests say which tools comply.

## Result: `AgentResult`

| Key | Rule |
|---|---|
| `state` | One of `previewed`, `pending`, `running`, `succeeded`, `failed`, `unknown`, `cancelled`, `expired`. |
| `detail_state` | The module's own status, unchanged (for example `dispatching`, `verified`, `ringing`). |
| `request_id` | The caller's key of this effect, when there is one. |
| `operation` | `{module, id}`: present whenever `state` is not final or is `unknown`; the status tool reads it. |
| `next` | `{tool, arguments}` or `null`: the one call that continues this result. |
| `resumable` | Only on `failed`: `true` when every failed step definitely had no effect (see Parent lifecycle). |

Final states: `previewed`, `succeeded`, `failed`, `cancelled`, `expired`. `previewed` means nothing was enqueued; it
carries `preview` and `confirm_token` and never an `operation`. `unknown` is not final: the outcome is not known yet and
the caller follows `operation`, never repeats the effect.

## Error: `AgentError`

Today's messaging envelope keys stay: `error` (a snake_case code), `request_id`, `retryable`, `field_errors`, `hint`.
Rulebook v1 adds:

- `next: {tool, arguments}`: the call that fixes or continues the request, when one exists.
- `candidates: [{id, label, kind, network, account, last_activity, next}]`: at most 10. When there are more, the error
  carries `more: true` and a top-level `next` naming a read-only tool that lists them.
- `selection_required` (ambiguity, also with a single fuzzy candidate) never has a top-level mutating `next`; a mutating
  continuation exists only on a candidate. `validate_error(value, read_only=...)` rejects a violating error; each story's
  tests run it.

`hint`, `next` and candidates never carry message text, file names or phone numbers taken from a request or a backend
error; the ANYM-295 privacy allowlist is unchanged.

## `request_id`

- 8-128 characters of `[a-zA-Z0-9_.:-]`.
- Required for a live effect unless a `confirm_token` is given; then the token is the key.
- The same ID with the same payload returns the first result. A different payload is `idempotency_conflict`. A request
  still in flight is `in_progress`.
- `dry_run` neither needs nor consumes it.
- Missing on a live effect: `invalid_arguments` with `field_errors: [{field: "request_id", reason: "required"}]` and a
  `next` that repeats the call with `request_id: "REPLACE_WITH_YOUR_ID"`. Every tool refuses that placeholder (the
  gateway before any backend sees it), so the `next` can never run verbatim. Every tool description says the field is
  required for live effects. This `next` intentionally copies the caller's own arguments back to the same caller: it
  is the one gateway `next` that carries payload, and it never comes from a backend.

## `dry_run` and `confirm_token`

- `dry_run: true` runs the same validation and authorization as the real call and returns `state: previewed`, the
  preview and a `confirm_token`.
- The token is an opaque random reference to a durable preview record. The record binds the principal, the tool, the
  resolved target IDs, the canonical payload and file digests, and an expiry of at most 10 minutes.
- Execution consumes the token atomically and binds it to one durable intent. Lookup order:
  1. A token already bound to an intent returns that intent's saved result, also after expiry or drift.
  2. Only an unbound token is checked for freshness and re-resolved; on any difference it is refused with
     `preview_stale`, `changed: [<fields>]` and a fresh preview.
  3. A token with a `request_id` that is already bound to another intent is `idempotency_conflict`.
- Records are kept 7 days for replay and are readable only by their principal.
- Token storage lands with each intent story (ANYM-546, ANYM-552, ANYM-551). Each runs the shared conformance matrix
  (`agent_conformance.CASES`: tampered token, other principal, other tool, expiry, drift, revocation between preview and
  execute, concurrent consumption, crash after binding, replay with another `request_id`, lost response then replay)
  against its own storage. The package runs it against an in-memory reference.

## Composite steps

- A step's key is `child_key(principal, tool, parent_key, step, attempt)`: a SHA-256 hex digest, 64 characters, so
  every key also fits the 128-character backend limit, including under a 128-character parent ID.
- Child keys live in an internal key domain: each intent story adds a key-origin column to its backend's uniqueness
  constraint (messaging `022_outgoing.sql`, `025_operations.sql`). A caller ID sent through the old API can then never
  equal or pre-occupy a child key; old inputs are unchanged.

## Parent lifecycle

- A terminal result is immutable. A repeat with the same key or token returns it without any effect.
- A `failed` parent whose failed steps definitely had no effect (for example `account_offline`, a provider rejection
  before acceptance) carries `resumable: true`. The caller resumes it with the same `request_id` and `resume: true`.
  A resume starts a new durable attempt, never repeats a succeeded step and never resumes an uncertain one.
- A consumed token never resumes; resume goes by the parent `request_id`.

## `wait_s`

- 0-25 seconds. The call returns the final result, or `state`, `operation` and a `next` that validates against the
  status tool's input schema.
- The gateway admits every catalog tool that takes `wait_s` through a dedicated bounded wait pool, chosen from the
  catalog (`http_clients.WAIT_ADMISSION`, `wait_tools`), never the metadata pool. `wait_s: 0` is a plain call. A wait
  never holds a backend database connection between polls.

## Status mapping

Every current module status maps onto exactly one `state` (`agent.STATUS_STATES`); the module's tests fail on a status
without a mapping.

| Module status | `state` |
|---|---|
| send: `queued` / `dispatching` / `sent` / `failed` / `expired` / `cancelled` / `unknown` | pending / running / succeeded / failed / expired / cancelled / unknown |
| operation: `queued` / `dispatching` / `succeeded` / `failed` / `expired` / `cancelled` / `unknown` | pending / running / succeeded / failed / expired / cancelled / unknown |
| calendar operation: `queued` / `running` / `verified` / `conflict` / `failed` | pending / running / succeeded / failed / failed |
| calendar operation: `unknown` / `needs_attention` / `partial` | unknown (reconcile, do not create again) |
| call: `waiting_for_browser` | pending |
| call: `dialing` / `ringing` / `connecting` / `active` | running |
| call: `finished` / `failed` / `interrupted` | succeeded / failed / failed |
| call with `dial_state: unknown`, any status | unknown |

An unknown provider outcome maps to `unknown`, never to `failed`.

## Gateway errors

The gateway's own errors keep their keys and add `next` when one exists:

- Unknown or forbidden tool: `next` is `platform_capabilities` when the key may call it, the same for every name asked.
- Tool of an unselected module: `next` is `platform_select_modules` with the current selection plus that module, when
  the session may widen it.
- Missing `request_id`: the placeholder `next` above.
- Expired or revoked key: no `next`.

A backend error is relayed through these filters; each drops a field and never repairs it:

- A `next` (top level or on a candidate) must run as given for this caller on this route: a tool of the caller's
  effective list (module selection included), arguments the target's input schema accepts, and a target in the failing
  tool's module or the gateway itself, with its scopes, backend implementation and messenger operation in place.
- A `hint`, `next` or candidate that carries the caller's payload is dropped: a string the caller sent outside an ID or
  reference field (message text, file names, queries), or a word of it of 6 or more characters. A `hint` or `next`
  argument with a phone-shaped number is dropped too.
- `selection_required` keeps a top-level `next` only when it is read-only; a mutating one stays on candidates only.
- Candidates must pass the shared shape and are cut at 10; `more: true` stays only with a read-only top-level `next`.
