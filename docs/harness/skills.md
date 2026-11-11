# Skills

Load the matching skill before starting the task; if several fit, say which and why. When asked
"what skills do we have for X", answer from this map. (Source: the owner's global
`~/.claude/CLAUDE.md`, which stays the loaded copy for Claude Code; change both together.)

## Before writing tests (owner 10-21)

Load the matching skill when the task fits it:

- parsers, SQL queries and transactions, state machines, encode/decode or classifiers →
  `property-based-testing` (Hypothesis);
- a web UI → `webapp-testing`;
- a new or bumped dependency → `supply-chain-risk-auditor`.

There are no reviews, so no review skills either (owner 11-11). Name the skills you used in the
DONE block.

## Map

| Task | Use |
|---|---|
| Design/redesign UI: landing, dashboard, user cabinet, settings, onboarding; UI audit or polish | `impeccable` (`init` → `craft` → `audit`/`polish`) |
| Animation, micro-interactions | `emil-design-eng`; motion review: `/review-animations` |
| Mobile web, Telegram Mini Apps | `mobile-native` |
| Stress-test UI with worst-case data | `break-ui` |
| Pre-ship UI check | `web-design-guidelines` + `agent-browser` screenshots (desktop, mobile, dark) |
| Drive/test a web app, reproduce a frontend bug | `agent-browser` (interactive); `webapp-testing` (Playwright scripts) |
| GUI step in a native app or the signed-in system browser (a service setting, a registration form) | `superset:computer` (Cua Driver; limits: worker brief, GUI steps) |
| Positioning → page copy → editing copy | `product-marketing` → `copywriting` → `copy-editing` |
| Conversion, launch, AI-search visibility | `cro`, `launch`, `ai-seo` |
| De-AI prose (posts, articles, docs) | `humanizer` (EN), `humanizer-ru` (RU; score via `uvx ru-humanizer`) |
| Build an MCP server | `mcp-builder`; test with `npx @modelcontextprotocol/inspector --cli` |
| Library/framework docs | context7 MCP — before relying on memory for versioned APIs |
| Word / PDF / slides / spreadsheets | `docx`, `pdf`, `pptx`, `xlsx` |
| Cleanup of a diff | `/simplify` |
| Security deep dive | SAST: `semgrep` / `codeql` skills; dependencies: `supply-chain-risk-auditor`; full verified scan: `claude-security` plugin (disabled by default — `claude plugin enable claude-security@claude-plugins-official`, disable after) |
| Property-based tests (Python) | `property-based-testing` (Hypothesis) |
| CI / Docker / secrets / deps linters (CLI) | `zizmor`, `actionlint`, `pinact`, `hadolint`, `gitleaks --redact`, `osv-scanner` |
| Destructive command blocked by dcg hook | Explain why it's needed; the user runs it or allowlists it — never work around the guard |
| Type errors | LSP diagnostics (pyright, typescript, clangd) appear after edits — fix before finishing |
| Language / infra conventions | Auto-loaded by file type from `~/.claude/rules/`: `frontend.md`, `python.md`, `devops.md` |

## Tiden skills

Tiden skills (`product-context`, `link-coverage`, `triage-issues`, `generate-tests`,
`start-intent`, `capture-intent`) come from the tiden CLI, not git; a new worktree may need
`tiden skill install --agent claude-code --global` (list: `tiden skill list --agent claude-code
--global`). The intent-loop skills are paused (tiden.md).
