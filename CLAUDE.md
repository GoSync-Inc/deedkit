@AGENTS.md

# DeedKit: Claude Code notes

AGENTS.md (imported above) and harness `process.md` (SessionStart hook) are the rulebook; this
file adds verified project facts.
Path rules live in `.claude/rules/`. `ANYMSG_*` environment names keep their
compatibility meaning too. Every service keeps its own lockfile, environment,
process and state.

## Layout
- `services/messaging`: AnyMsg server (Python 3.13, PostgreSQL 18, schema 61)
- `services/gateway`: common MCP gateway (HTTP only, imports no provider code)
- `services/calendar`: family calendar (Google API/CalDAV), has its own AGENTS.md
- `services/calls`: voice calls; `services/calls/frontend`: Vite UI
- `whatsapp/whatsmeow`: default Go adapter; `whatsapp/*.mjs`: whatsapp-web.js fallback (Node 24)
- `packages/npm`: JS client/CLI; `packages/platform-contracts`: shared contracts
- `deploy/`: Compose overlays and `*.example.json`; `scripts/production.py`: promotion tool
- `tools/dev`: pinned dev tools (ruff 0.15.7, load-gate deps)
- `config/tiden-test-identities.json`: registry of Tiden test IDs

## Verified commands (mirrors `.github/workflows/checks.yml`)
```sh
uv sync --group dev                                   # CI: --frozen
PROBE_TEST_DSN_FILE=<file> uv run pytest tests        # DSN must end in /messaging_probe_test
(cd services/gateway && uv sync --locked --group dev && uv run pytest -q)
(cd services/gateway && uv sync --locked --group dev) && (cd services/calls && uv sync --locked --group dev && uv run pytest -q)
(cd services/gateway && uv sync --locked --group dev) && (cd services/calendar && uv sync --locked --group dev && FCS_TEST_DSN=<dsn> uv run pytest -m 'not live')
(cd services/calls/frontend && npm ci --ignore-scripts && npm test && npm run build)
node --test whatsapp/tests/*.test.mjs
npm test --prefix packages/npm
(cd whatsapp/whatsmeow && go vet ./... && go test -race ./...)
python scripts/build.py                               # wheel + sdist into dist/
uv run python scripts/check_release.py [version]      # also needs dist/anymsg-<v>.tgz from npm pack
uv run python scripts/smoke.py --dsn-file <file>      # synthetic MCP/HTTP drill
```
- Root tests that use the `database` fixture fail on purpose without
  `PROBE_TEST_DSN_FILE`; calendar service tests likewise fail without `FCS_TEST_DSN`.
  Point both only at disposable databases.
- OCR/audio tests need `pdftotext`, Tesseract (eng+rus) and ffmpeg; the backup drill needs `age`.
- Lint: `uv run --project tools/dev --locked ruff check <path>`. The code style uses
  compact one-line statements; do not mass-reformat to satisfy default ruff rules.

## Root docs map
- Start: README, ARCHITECTURE (logical components = Tiden components), CONTRIBUTING,
  SECURITY, ROADMAP, `docs/platform-runtime.md`
- Tiden/product: BACKLOG (navigation only), TIDEN-CI (CI reference), TELEMETRY,
  `docs/operations/tiden-product-workflow.md`
- Setup/operations: SETUP, COLLECTORS, COLLECTOR_STATES, HISTORY_POLICY, BACKUP,
  BACKUP_STREAMING, RELEASING, `docs/operations/{production,platform-cutover}.md`
- Agent read API: ACCESS, SEARCH, PEOPLE, CONVERSATION, REVIEWS, REVIEW_FOLLOWUP,
  MESSAGE_ACTIONS, SUBSCRIPTIONS
- Sending/operations: SENDING, MESSENGER_OPERATIONS, MESSENGER_CAPABILITY_AUDIT,
  FORMATTING, RECEIPTS
- Files/media/AI: FILE_INDEX, MEDIA_STORAGE, ENRICHMENT, CODEX_ENRICHMENT
- WhatsApp: WHATSAPP_IDENTITIES, `docs/whatsapp-native.md`, `docs/quality/whatsmeow-parity.md`
- Legal: LICENSE, THIRD_PARTY_NOTICES; other dirs: `docs/{migrations,quality,plans}/`

## Project facts
- Required checks: `CI gate` and `checks / verify`; `main` uses the ruleset
  `deploy/main-ruleset.json` with a merge queue. `checks.yml` step ids
  `build release python collector npm smoke distributions` are the seven
  `anymsg-ci::*` Tiden checks.
- Provider auth happens only in the owner's terminal.
- Python and npm versions must equal `messaging_service.__version__` (check_release.py).
- A new whatsmeow source file must be added to `NATIVE_SOURCES` in
  `services/messaging/hatch_build.py` (sdist allowlist).
- Tiden skills and the skill map: harness `skills.md`.
