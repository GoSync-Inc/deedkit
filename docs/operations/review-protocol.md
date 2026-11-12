# Review protocol

No review on any path (AGENTS.md section 5, owner 11-11): no Astra, Fable, Codex, plan or card
review and no Diff read. A pull request merges on green required checks with a failing-first test and
the line `Review record: none (owner 11-11: no reviews)`; the merge keeper and the autoloop controller
enqueue it with no approval. This document holds DeedKit's pull request lines: the change-path grep,
the host-format rule and the autoloop protected paths.

Agent-facing feature PRs include the baseline, implementation decision and integrated same-task
comparison from [agent feature experiments](agent-feature-experiments.md) in the existing author
checklist. Functional checks remain failing-first; no review or extra approval is added.

## Codex budget

The line is printed by `~/deedkit-orchestrator/bin/codex-meter.sh` from the orchestrator's config.sh
(`CODEX_DAILY_POINTS`, `CODEX_LINE_OFFSET`); risk paths, in addition to the harness list: release,
deploy and `production.py`, sends and live provider-session code.

## Pull request lines

- `Change path:` signal grep:

  ```sh
  git diff --no-renames --name-only origin/main... | grep -E -i '^\.github/workflows/|main-ruleset\.json|\.sql$|migrations/|gitleaks|compose|^deploy/modules/|\.claude/settings\.json|^AGENTS\.md$|^docs/operations/feedback-autoloop\.md$'
  git diff -U5 origin/main... | grep -E -i '^[-+].*(secret|permissions|: *write|write-all|\b(GRANT|REVOKE|DROP|TRUNCATE|DELETE FROM|RENAME)\b|ALTER )'
  ```

  A `DELETE FROM` hit in service code (not a migration, not a script or hook that deletes production data) is a
  `Deletes:` PR line item (harness reviews.md), not an OWNER signal. A `TRUNCATE` or `DROP` hit, in service code
  as in a migration, is a gated change (process.md section 2), never a `Deletes:` item; only a service-run DELETE of
  rows a card or docs/DATA-POLICY.md names is one.

  A host-tool change reaches the host through the tool's own update; where it cannot, autodeploy
  pauses until an agent installs it (docs/operations/production.md, "Deploy tool updates").

- A format the installed host tool reads (manifest keys, configuration shape, receipt fields) never gains a required
  part in the PR that first uses it: the part is optional, or the tool that reads it merges and installs first and the
  format follows in a second PR. An installed tool reads an unknown key as a malformed file, and the enrollment gate runs
  before the tool updates itself. Incident 11-04: #632 added a required manifest key and every deploy paused with
  `enroll_required` for the running recordings module until #638. Enforcer: `test_the_recordings_manifest_uses_only_keys_the_installed_host_tool_reads_and_the_networks_key_is_optional`
  (tests/test_module_enroll.py), which pins the manifest keys the installed tool reads.

- Autoloop protected paths (docs/operations/feedback-autoloop.md), not a change-path signal: a
  hit keeps an autoloop PR out of the controller merge. This is the one list; it covers the
  checks' own verifiers (load budgets, test config, release contracts, import shadowing). In a
  test directory (`test/` or `tests/` anywhere) an autoloop PR may only add test files
  (`test_*.py`, `*.test.*`, `*_test.go`) and fixtures: a modified or deleted file there, or an
  added code file that is not a test (a helper), is a hit. Added bytecode and native libraries
  (`*.pyc`, `__pycache__/`, `*.so`, `*.pyd`, `*.dylib`) are hits anywhere. The tests a loop PR
  adds never run in the required suites: every test job deletes them first, and `checks / added`
  runs them alone inside `CI gate` (ANYM-578, TIDEN-CI.md).
  One exception, for the controller only (ANYM-590 L1, docs/operations/feedback-autoloop.md
  "Additive migrations"): when all of a loop PR's SQL is added `expand` migrations and appended
  `additive` role grants on the tables and columns they create or add, those files are no path hit, and on their
  changed lines the text signal does not count `ALTER` (migrations) or `GRANT` (role files); any
  other word still hits. Otherwise each SQL path is a "waits for the owner: migration" or "grant"
  hit. The classifier (`migration_rollout.py`) is itself a protected path.
  Both path lists match in any case (`-i`: `migrations/X.SQL`, `prod.dockerfile`, `AGENTS.MD` on a
  case-insensitive checkout); only the test-name exemption (`grep -v -E`) stays exact.

  ```sh
  git diff --no-renames --name-only origin/main... | grep -E -i '^(\.github/|scripts/|deploy/|tools/|config/|docs/(operations|harness)/|services/release-router/|packages/platform-contracts/|tests/platform/benchmarks/)|^[^/]+\.py$|^[^/]+/__init__\.py$|(^|/)(sitecustomize|usercustomize)\.py$|\.pth$|(^|/)(pytest\.ini|setup\.cfg|tox\.ini)$|(^|/)(AGENTS|CLAUDE)(\.override)?\.md$|^(SUBSCRIPTIONS|SECURITY|TIDEN-CI)\.md$|(^|/)\.(claude|codex)/|\.mcp\.json$|migrations/|\.sql$|(^|/)(([^/]*[.-])?Dockerfile[^/]*|pyproject\.toml|package\.json|go\.mod|hatch_build\.py|conftest\.py|migration_rollout\.py|\.dockerignore|\.gitattributes|uv\.lock|package-lock\.json|go\.sum|requirements[^/]*\.(lock|in|txt))$|\.(pyc|so|pyd|dylib)$|(^|/)__pycache__/'
  git diff --no-renames --diff-filter=D --name-only origin/main...   # any deleted file
  git diff --no-renames --diff-filter=M --name-only origin/main... | grep -E -i '(^|/)tests?/'   # a changed test-directory file
  git diff --no-renames --diff-filter=A --name-only origin/main... | grep -E -i '(^|/)tests?/' \
    | grep -E -i '\.(py|pyi|js|mjs|cjs|ts|tsx|go|sh)$' | grep -v -E '(^|/)test_[^/]*\.py$|\.test\.[^/]+$|_test\.go$'   # an added helper
  ```
