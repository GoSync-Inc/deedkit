# Family Calendar Service

Shared backlog, priorities and statuses: **[DeedKit BACKLOG](../../BACKLOG.md)**.

A separate local service for agent management of several **Google Calendar and iCloud CalDAV** accounts. Version 0.2.0 includes PostgreSQL 18, family roles, explicit grants, duties, a shared agenda/slots and a unified MCP/REST. Creating, moving and deleting individual events has been verified live on the test calendars of both providers.

Connection, startup, roles, agent keys and examples: **[docs/SERVICE.md](docs/SERVICE.md)**. Recurring events are read; one occurrence of a series can be updated or cancelled and all-day events can be written (see SERVICE.md). Series-wide changes, this-and-following, invitations, sharing and linked family projections are not yet enabled in MCP. The original iCloud harness below is kept independent of the service.

## Starting the service

Needs `uv`, Python 3.13 and a running Docker. From the repository root:

```sh
uv sync --locked
uv run family-calendar-local bootstrap
uv run family-calendar service init-family
# Permanent start on macOS:
uv run family-calendar-local install
uv run family-calendar-local status
```

The gateway listens on `127.0.0.1:13232` (`/health`, `/mcp`, `/v1/{method}`). Credentials and settings are in the ignored `.local/`; do not add them to Git. The Docker daemon must be running; starting it after login is configured separately.

## Connecting

Create a separate empty calendar **Family Calendar Pilot** in iCloud and an app password for this service. Then, from the repository root:

```sh
uv sync --locked
uv run family-calendar connection add-icloud --id pilot-a
uv run family-calendar pilot icloud discover --connection pilot-a
```

The first connection command asks for the Apple Account email and a hidden-input **app-specific password**. The second only reads the list of calendars. Writing is enabled separately for a specific `calendar-ref`.

Full step-by-step instructions, running the matrix, the visual check and cleanup: [docs/ICLOUD_PILOT.md](docs/ICLOUD_PILOT.md).

## What is implemented

- Independent connection IDs and encrypted credentials; a separate HTTP client per account.
- HTTPS CalDAV discovery, privileges, window reading, a ban on sending credentials to foreign hosts.
- Create/update/delete with `If-None-Match` / strong `If-Match`, read-back and saving the actual response.
- A SQLite journal before sending, stable UID/href, reconciliation instead of repeating an unknown write.
- Single move/cancel, series shift, future split of a simple weekly series with COUNT/UNTIL/no end.
- Preserving exceptions, EXDATE/RDATE, VALARM, VTIMEZONE, X-properties and Unicode.
- A private report with library versions, a capability matrix and verifiable receipts.
- Limited cleanup of only own, unchanged test objects.

These are capabilities of the separate legacy harness. Its recurrence/future operations do not automatically extend to the service MCP.

## Development

Needs `uv`; Python 3.13 is selected by the project environment. Versions are pinned in `uv.lock`.

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

Normal tests work on fictional ICS, mock providers and isolated PostgreSQL schemas, without real accounts or an external network. DB tests need a local bootstrap or `FCS_TEST_DSN`; without it they are skipped, and such a run cannot be counted as a full check. CI provides PostgreSQL 18. Live writing requires an explicitly allowed test calendar.

Documents: [contract](CONTRACTS.md), [recurrences and limits](docs/RECURRENCE.md), [transport choice](docs/ADR-001-CALDAV.md), [stages](docs/ROADMAP.md), [dependency licenses](THIRD_PARTY.md). The code is distributed under the [MIT](LICENSE) license.
