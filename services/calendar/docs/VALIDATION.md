# Implementation check 2026-10-14

## Service v0.2 — 2026-10-15

- Full normal run: **113 passed**, no skips, with a real PostgreSQL 18 and isolated test schemas. One warning from Starlette/AnyIO about a deprecated alias; it is not a test error.
- Policy tests: grant intersection, busy-only redaction, unknown busy time, scope revocation, group isolation, duty versions and the absence of false confirmation on behalf of another person.
- Worker tests: unknown without repeating create, crash reconciliation, read-back auth failure, two workers, serialization of the write/publication of a read snapshot, access to receipts after write opt-in is turned off.
- Vault/OAuth identity, provider pagination, conditional headers, keeping the event identity on a move, floating-time degradation, REST/MCP schema and origin checks.
- Live through the official MCP SDK: one Google and two iCloud connections are read independently. In two test calendars create → update → delete: **6 verified mutations**, stable refs and read-back; the test events were deleted. Normal calendars were not changed.
- Stdio MCP from another project cwd: 10 tools, 32 allowed calendars, no provider IDs/credentials and no write opt-in. Repeating apply with the same request ID returned the existing operation.
- Slots live: configured parent+child sources complete; adding an unbound partner gives unknown, with no verified slots. Access to busy time only does not open agenda details.
- Separate DB roles cannot change grants/keys/memberships/connections/write opt-in. Both processes run as one OS user; this is not production process isolation.
- Private live receipts are stored outside Git. The first trial conditional Google insert was saved as a stopped unknown operation, was not resent; the read-back did not find the object. The successful matrix is a separate test after fixing the provider-specific precondition. The history was not rewritten into a PASS.

MCP is registered locally; already open Codex chats were not checked for hot reload. Not verified: a second independent Google account, sharing with spouses, Google recurrence write, linked family projections, incremental sync and backup/restore. The CI configuration was updated, remote CI and publication were not run. The original status of stage 1 below is kept as history.

The first stage of the local iCloud pilot is implemented. Live Gate A is **not accepted**: a real Apple Account is not connected yet, server/client compatibility is not verified.

Checked locally on Python 3.13.11 / uv 0.9.26:

- `uv run pytest tests/unit tests/contract`: **53 passed**.
- Ruff check and format check: PASS.
- `uv lock --check`: PASS.
- The source distribution and wheel were built successfully.
- Installing the wheel into an isolated uv environment and `family-calendar --help`: PASS.
- The artifacts were checked for the absence of `.local`, `outputs`, `.venv`, private keys and journals; document links were checked.
- Git ignore covers the registry, journals, outputs and the environment.

The checks include recurrence identities/occurrence sets, COUNT/UNTIL/infinite split, timezone/DST, DATE/floating fixtures, unknown properties/alarms, ETag 412, redirect credential boundaries, independent encrypted connections, no secret traceback, atomic claim, restart/idempotency, timeout after an accepted create/delete/trim, conditional compensation, cleanup refusal on a foreign edit and verification of the actual read-back evidence.

The Gate A decision test with a mock transport and an explicitly chosen test environment label checks only the logic; it is not live evidence. The CI workflow is prepared but was not run on GitHub in this pass. No package/remote repository publication or deployment was done.

Next step: connect an account locally, run discovery, choose an empty test calendar and run the matrix by the [instructions](ICLOUD_PILOT.md). Google/MCP/production backend and real family events are outside this stage.

## Series time move with exceptions: owner live check (ANYM-614)

Not run yet. The synthetic tests (`tests/service/test_schedule_series_exceptions.py`) assume that Google drops an
exception whose original start the moved rule no longer yields, as its own "All events" edit does; this check is
what confirms or refutes it. Steps, on the owner's empty test calendar only (Google, write opt-in on, no guests):

1. In Google Calendar, create a weekly series (for example Mondays 17:00-18:00, 8 occurrences, no guests).
2. By hand in Google Calendar: cancel the second occurrence ("This event" only), and move the fourth to the next day
   at 19:00 ("This event" only). Optionally retitle the fifth.
3. Ask the agent for `calendar_schedule` move of the third occurrence to 18:00 with `series: all` and
   `dry_run: true`. The preview must list the cancelled, moved (and retitled) occurrence under `exceptions` with
   fates `cancelled`, `kept` (`moves`), and `writes` = 1 + the listed writes.
4. Confirm that preview (`confirm_token`). The result must be `succeeded`.
5. Read back in Google Calendar: every plain occurrence at 18:00; the second still cancelled (not shown); the
   fourth still on the next day at 19:00 and shown once; the fifth at 18:00 with its own title.
6. Record the owner's words and the date on the card ANYM-614 (criterion 5); a `failed` result with `mismatches`
   names the occurrence to inspect, and the card stays in Review until the owner's check passes.
