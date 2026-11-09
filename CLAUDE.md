# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project Overview

**AI Strategy Trading System** — trades Rakesh's own Zerodha Kite account using rule-based
stock-selection strategies (momentum, RSI pullback, breakout, low-volatility quality, sector
strength) on the Nifty 500. ML decides which strategies get capital and tunes their settings
within fixed bounds. Rakesh approves every buy; the system manages exits through GTT stop-losses.

- Plan (canonical): https://claude.ai/code/artifact/81d40d11-41d6-4d01-9ee2-fec2ac5a846e
- Technical context: `PROJECT_CONTEXT.md` — Issue tracker: `TECH_DEBT.md` (ST- prefix)
- Personal use only. Not investment advice.

## Objective (overrides everything below it)

Minimise losses and maximise profits over a rolling 12-month forward window, applied in order:
1. Worst rolling 12-month loss <= 20% (Rs 20,000 on the Rs 1,00,000 cap) — hard reject otherwise
2. Then highest median rolling 12-month return after costs and tax, walk-forward, out-of-sample
3. Tie-break: better return-to-drawdown ratio
4. Must beat Nifty 500 buy-and-hold over the same windows

ML and code may never change this objective.

## Working Agreements (follow every session)

1. **Propose the approach and get approval BEFORE writing any code**
2. **Git: Claude provides commands only — Rakesh executes them**
3. **Ubuntu (rkneo50q) is strictly PROD — test on Windows dev first**
4. **One issue at a time** — finish and test before moving on
5. **Always check the actual DB schema before writing queries** — never assume column names
6. **ST items marked Resolved ONLY after Rakesh gives a prod sign-off date**
7. **`.env` is never committed**
8. **`TECH_DEBT.md` is the canonical issue tracker**
9. **Understand current design before enhancing** — ask "what exists now?" first
10. **Review actual changed files, not summaries**
11. **Every command block starts with WHERE (laptop or prod) and a `cd` to the full path** — never a bare command block (Rakesh, 2026-10-11)

## Trading Safety Rules (non-negotiable)

- Every order goes through the guardrail service. No other module may call Kite order APIs.
- `config/guardrails.toml` is edited only by Rakesh. Code and ML read it; they never write it.
- Limit orders only, CNC equity only. No F&O, MTF/margin, SME or ASM/GSM stocks.
- Every holding must have a live GTT stop-loss. Stops only move up.
- No new ML model or strategy setting goes live without Rakesh's approval.
- Never log or print Kite access tokens, API secrets or Telegram tokens.
- Any change touching `execution/` needs a dry-run test against Kite before prod.

## Agreed Limits (mirror of the plan; `config/guardrails.toml` is the enforced copy)

| Limit | Value |
|---|---|
| Capital cap | Rs 1,00,000 at cost of open positions; revolving (a GTT exit returns that lot's cost). Profits never re-enter the kitty; it rises only when Rakesh raises it (2026-10-29) |
| New buys | Rs 10,000 per week |
| Position size | ~Rs 10,000, max Rs 12,000 |
| Risk per trade | <= Rs 1,000 entry-to-stop |
| Per strategy / per sector | <= Rs 40,000 / <= Rs 25,000 |
| Per stock | <= 8% of the capital cap, across all stratrad strategies (no cap on the number of positions, 2026-10-29) |
| Loss tiers (trailing 12m) | 10% alert, 15% no new buys, 20% full halt |
| Universe | Nifty 500 |
| Holding period | Positional, 1-6 months |

## Data Rules

- MyInvestIQ's archives (UDiFF bhavcopy from 2024-01-01, index closes) are **read-only**,
  via `BHAVCOPY_ARCHIVE_DIR` / `INDEX_ARCHIVE_DIR`. Never write to them.
- Legacy bhavcopy (2014 to Jul 2024, `nsearchives.nseindia.com/content/historical/EQUITIES/`)
  is downloaded once by this project (ST-002) into `/home/rakeshbk/stratrad/data/bhavcopy_legacy/`
  (`LEGACY_BHAVCOPY_DIR`). This project is its only writer. Daily files are never downloaded here;
  they come from MyInvestIQ's own 18:00 job.
- Join prices by symbol, never ISIN (see MyInvestIQ `core/bhavcopy_reader.py`).
- Returns are price return (no dividends) unless a decision changes this.
- Backtests: walk-forward only; 2024-2026 held out until a strategy's final check.
- News/LLM scores are never back-filled for backtests (look-ahead leakage).

## Code Standards

Follow MyInvestIQ's standards: specific exception types (no bare `except`), `logging` not
`print`, docstrings and type hints on new functions, parameterized SQL only, context managers
for DB connections, None-checks on fetch results.

## Environments

See `docs/CONFIG.md` (machines, folder layout, shares X:/Z:, env vars, one-off operations).
Prod repo: `/home/rakeshbk/stratrad` (venv: `source venv/bin/activate`). Windows: `C:\RKOneDrive\OneDrive\Work\RKInvesting`.
**Each project's prod folder is its own** — stratrad never writes under `/home/rakeshbk/myinvestiq/`.

---

## Change workflow (adopted from MyInvestIQ, 2026-10-08)

1. **Discuss** the issue; Claude proposes the design. Nothing is coded before Rakesh approves.
2. **Done when** is written into `tech-debt/ST-NNN.md` before the fix exists — observable behaviour,
   **plus a falsifying clause** (the state that must NOT exist). Assume a count can lie.
3. **Branch per ST:** Rakesh runs `git branch --list "st/ST-NNN*"` first; if nothing, `git switch -c st/ST-NNN-short-name`,
   else `git switch` to it. Never commit to `main` directly. Every commit's output must show the expected branch name.
4. Claude writes code on the branch in the Windows working copy and runs tests.
5. Claude gives commit commands with **explicit file names** — never `git add .` — then
   `git status` before and `git show HEAD --name-only` after, to confirm exactly what was committed.
6. Rakesh reviews the changed files (not the summary), merges to `main`, pushes, `git pull` on prod.
7. Prod verification against Done-when. **Resolved only when Rakesh confirms** and gives a prod_confirmed date.

**Git command conventions:** `git --no-pager` for every `show`, `log` and `diff` (the PowerShell pager cut
`git show --name-only` output on 2026-10-08). Merges are always
`git merge --no-ff st/ST-NNN-short-name -m "Merge ST-NNN: <summary>"` so no editor opens. Line endings are LF
everywhere (`.gitattributes`).

### Status values (index only — `TECH_DEBT.md` is the single place status lives)

| Status | Meaning |
|---|---|
| `Not started` | logged, nothing done |
| `Design proposed` | design sent, awaiting Rakesh's approval |
| `In progress — branch st/…` | code being written |
| `Code complete — awaiting review` | tests pass, commit given to Rakesh |
| `Tested in Dev — awaiting merge` | Rakesh approved |
| `Merged to main — awaiting prod deploy` | merged and pushed |
| `Moved to Prod — pending verification` | pulled/run on prod |
| `Resolved` | Done-when verified on prod, Rakesh confirmed, docs updated |

Claude never advances a status on assumption — it waits for Rakesh's words ("approve", "on prod", "prod confirmed").

### Issue standard
- Next free ST number; one row in `TECH_DEBT.md`; full detail in `tech-debt/ST-NNN.md` with frontmatter
  (`id, title, status, priority, area, since, prod_confirmed, docs, docs_updated`). Frontmatter mirrors the index;
  if they disagree the index is right.
- **Doc gate:** no ST reaches `Resolved` until every doc it impacts is updated and `docs:` / `docs_updated:` are filled
  (`none` allowed, but must be written).

### Which doc for which change
paths, env vars, shares, servers, schedules, one-off ops → `docs/CONFIG.md` | data source, format, trap →
`docs/DATA_SOURCES.md` | module, entry point, call chain → `docs/CODE_MAP.md` | DB schema → `docs/DATABASE.md`
(when it exists) | a decision or a corrected understanding → append to `vault/LEARNINGS.md` (never rewrite).

---

## Standing rules carried over from MyInvestIQ

- **SQL is always a runnable shell command:** `psql -h localhost -U portfolio_rk -d portfolio_watch -c "…"`;
  multi-statement → heredoc with output to `/home/rakeshbk/stratrad/logs/<name>_<date>.txt 2>&1`
  (readable on Windows at `X:\logs\`). Never a bare SQL or Python block. Never guess column names.
- **Docstrings are updated whenever a function is touched.** When code and docstring disagree, the docstring is a bug.
- **A written instruction is evidence of what was true when written, not of what is true now** — check before acting.
- Debug/scratch scripts are not committed.

---

## Session rituals

**Start:** read `TECH_DEBT.md` (fresh), the tail of `SESSION_LOG.md` and `TODO.md`; `git status` + current branch on Windows;
on prod, `ls ~/stratrad/logs | tail` for the latest run; state current priorities.

**Close:**
1. Update `TECH_DEBT.md` statuses (only on Rakesh's confirmation) and `tech-debt/ST-NNN.md` details.
2. Append the session to `SESSION_LOG.md` (STs touched, commits, prod actions, status at close).
3. Doc check for every ST touched (table above); append any decision/correction to `vault/LEARNINGS.md`.
4. Update `PROJECT_CONTEXT.md` if status/phase changed.
5. Give explicit-file commit commands.

## File reading rules

| File | Rule |
|---|---|
| `TECH_DEBT.md` | always read fresh — never from memory |
| `SESSION_LOG.md` | read the tail at session start |
| `TODO.md` | read at session start — resume point and open items |
| `docs/CONFIG.md` | read before giving any path, env var or prod command |
| `docs/DATA_SOURCES.md` | read before touching ingestion or readers |
| MyInvestIQ repo | read-only reference; changes there go through MyInvestIQ's own process |
