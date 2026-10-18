---
paths:
  - "tests/**"
  - "services/*/tests/**"
  - "whatsapp/tests/**"
  - "packages/npm/test/**"
---
<!-- Sources: AGENTS.md, TIDEN-CI.md, docs/operations/tiden-product-workflow.md, config/tiden-test-identities.json, CONTRIBUTING.md -->
# Tests are Tiden identities
- File path + test name (+ parameter IDs) are Tiden case IDs. `config/tiden-test-identities.json` registers 1278 cases, and `tests/platform/test_test_identity_migration.py` asserts they still exist. Never rename, move or re-parametrize an existing test. If its assertions change, keep the name.
- Add new tests rather than repurposing old ones. Do not add placeholder tests or link unrelated passing tests to make Tiden coverage green.
- Fixtures are synthetic only: no real messages, phone numbers, usernames, contacts, emails, ICS, recordings, sessions or dumps. Use fake providers and mock endpoints.
- DB tests use only disposable databases (`PROBE_TEST_DSN_FILE` -> `.../messaging_probe_test`, calendar `FCS_TEST_DSN`). Never weaken the deliberate fail-without-DSN guards.
