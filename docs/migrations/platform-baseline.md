# DeedKit migration baseline

The migration uses a separate `codex/deedkit-platform` worktree. Existing backend
sources, tests, package names, provider sessions, data and Tiden identities stay
in place during the first gateway release. The calendar's final runtime is on
the server, with its own database, worker and encrypted credential storage.

## Reproduce a private baseline

1. Record clean/dirty state, HEAD and a SHA-256 manifest of `git ls-files` for each
   committed source. Read each source twice. Inventory untracked calls source
   separately when importing it; a parent repository SHA does not identify it.
2. Inventory consumers and the exact backend key identity (a fingerprint, never
   the credential in a report). Preserve subscription key IDs, consumer scope
   versions/positions, deployment project/volume names and current session owners.
3. Inventory calendar LaunchAgents, database schema, data/vault/key/config paths
   and backup/restore procedure. No source-relative empty state may be created
   when changing paths. Do not run another provider worker during this inventory.
4. Run `scripts/tiden_snapshot.py --repo SOURCE --config SOURCE/.tiden/config.json
   --output PRIVATE_NEW_FILE.json`. The exporter performs two complete reads,
   including every test's durable links and every spec's source anchors. A count,
   a successful gate or an unconsumed pagination cursor is not a full export.
5. Verify with `scripts/verify_tiden_preservation.py --before BEFORE --after AFTER`.
   Reviewed additions use explicit IDs. Changes need exact before/after hashes.
   Removed identities are never silently accepted.

The installed CLI omits protobuf JSON zero values: specs with no sources have no
`sourceCount`/`sources`; tests with no links can omit the count. The exporter
checks actual link readback even for zero-link tests. It never calls setup,
create, bind, update, merge, push, link or unlink.

`tests/platform/fixtures/*-contracts.json` contain synthetic schema discovery
from the separate locked backend environments. Capture with
`scripts/capture_legacy_contract.py`; `--canonical` writes the committed messaging
snapshot's format (sorted keys, ASCII escapes), so a refresh changes only real
differences. Run platform tests with
`uv run --project tools/dev --locked pytest --confcutdir=tests/platform tests/platform`;
the legacy root conftest intentionally depends on the messaging environment.
Set `DEEDKIT_{MESSAGING,CALENDAR}_{SOURCE,PYTHON}` for live schema comparisons.
Resource ACL/files/events/ACK require the existing isolated PostgreSQL tests,
not only schema snapshots. Never point tests at production archives.

## Concurrent messaging work

After R0's complete artifacts are verified, messaging may evolve in a separate
branch/worktree. R1 owns new gateway/contracts/config/launcher and platform tests.
Coordinate changes to CI, Tiden publisher/config, root packaging, public contracts
and session ownership. Before merge/cutover refresh the stable baseline and review
every legitimate addition/change. Counts alone cannot approve drift.

Messaging directory moves require a short window on the moving paths after
accepted messaging PRs are integrated. Existing tests retain their logical paths;
new platform checks must not replace the seven existing CI verification identities.
