# Review protocol

Who reviews what is in harness `process.md` section 5; DeedKit's four questions are in
[AGENTS.md](../../AGENTS.md) section 5.
The procedure (routing, the packet, the rounds, the verdict, the Codex budget, the pull request
lines) is harness `reviews.md`; this document holds DeedKit's values for it.

## Packet

- An Astra task starts with the four fixed questions of AGENTS.md section 5 (DeedKit's filled-in
  copy of harness `process.md` section 5), verbatim.
- A code packet names the deployed versions for question 3: the production SHA, the host
  `production.py` SHA, the schema and the installed gateway (read-only `production.py status`,
  or from the orchestrator).
- `specs.md`: `scripts/spec_impact.py --body-file <body>` lists the specs with a mark and a flag
  suggestion; both only help the author choose.
- Fable product review prompt: docs/operations/prompts/fable-product-review.md.

## Report and verdict

Never downgraded as broken failure isolation: a fail-closed stop of call audio, collection or
backup is itself a failure.

## Rounds

Process text here is AGENTS.md, CLAUDE.md, `.claude/`, `docs/operations/` and TIDEN-CI.md;
AGENTS.md stays within 1,000 words.

## Codex budget

DeedKit spends at most 4 points a day: the line is `68% + 4 × days since 10-19 03:30Z` until the
10-24 21:08Z reset, then `4 × days since the last reset`. Risk paths, in addition to the harness
list: release, deploy and `production.py`, sends and live provider-session code.

## Pull request lines

- `Change path:` signal grep:

  ```sh
  git diff --no-renames --name-only origin/main... | grep -E '^\.github/workflows/|main-ruleset\.json|\.sql$|migrations/|gitleaks|compose|\.claude/settings\.json|^AGENTS\.md$|^docs/operations/feedback-autoloop\.md$'
  git diff -U5 origin/main... | grep -E -i '^[-+].*(secret|permissions|: *write|write-all|\b(GRANT|REVOKE|DROP|TRUNCATE|DELETE FROM|RENAME)\b|ALTER )'
  ```

  A host-tool change still waits for the owner to install it before autodeploy resumes
  (docs/operations/production.md).

- Autoloop protected paths (docs/operations/feedback-autoloop.md), not a change-path signal: a
  hit keeps an autoloop PR out of the controller merge. This is the one list.

  ```sh
  git diff --no-renames --name-only origin/main... | grep -E '^(\.github/|scripts/|deploy/|tools/|config/|docs/(operations|harness)/|services/release-router/)|(^|/)(AGENTS|CLAUDE)(\.override)?\.md$|^(SUBSCRIPTIONS|SECURITY|TIDEN-CI)\.md$|(^|/)\.(claude|codex)/|\.mcp\.json$|migrations/|\.sql$|(^|/)(Dockerfile[^/]*|pyproject\.toml|package\.json|go\.mod|hatch_build\.py|conftest\.py|\.dockerignore|\.gitattributes|uv\.lock|package-lock\.json|go\.sum|requirements[^/]*\.(lock|in|txt))$'
  ```
