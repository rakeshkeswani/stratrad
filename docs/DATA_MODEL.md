# Data model — stratrad

Status: **APPROVED by Rakesh 2026-10-14.** Decided (Rakesh): research data in files,
operational data in PostgreSQL; the database (ST-012) is built after ST-010 and before any execution issue; project
and database name **stratrad** (2026-10-14). Review 2026-10-14 added: sectors, guardrails before proposing and before
ordering, the exit path, charges per order, position adjustments, instrument_token, the benchmark.

## 1. Two stores

| | Research store | Operational store |
|---|---|---|
| Holds | prices, corporate actions, renames, sectors, universe, features, backtest runs | strategies, models, signals, proposals, approvals, orders, GTT stops, positions, guardrail and audit logs, daily snapshots |
| Written by | batch jobs; everything derived can be rebuilt from raw downloads | the live system every trading day; **cannot** be rebuilt |
| Needs | fast bulk reads (years x 500 stocks) | transactions, constraints, audit trail, concurrent access |
| Home | files under `data/` (Parquet for large tables, CSV for small reviewed ones) | PostgreSQL database `stratrad` on rkneo50q (ST-012) |
| Backup | FFS Job 3 mirror + Kopia (`docs/CONFIG.md`) | `pg_dump` in the nightly 23:30 chain (set up with the homelab project in ST-012) |

The research store never reads the operational store. The operational store reads research outputs only through
versioned artifacts (a strategy config, a model version, a universe as-of date).

**Placement (ST-012):** a new database `stratrad` on the existing PostgreSQL 16 server (rkneo50q:5432) next to
MyInvestIQ's `portfolio_watch` — separate databases, so neither can touch the other's tables. Roles: `stratrad_app`
(owner, the live system), `stratrad_reader` (read-only reporting). MyInvestIQ grants a read-only role SELECT on
`corporate_actions`, `fii_daily_flow` and (since 2026-10-29, ST-008) `stock_master`; stratrad reads them over a
second connection. Server maintenance only
outside market hours (both projects share the server). Size: ~8 MB empty, < 10 MB per year of live data.

## 2. Identifiers (used in both stores)

| Name | Meaning | Source |
|---|---|---|
| `company_key` | the company's symbol **today**; constant across renames (TIDEWATER's history -> `VEEDOL`) | `core/securities.py` (ST-013) |
| `series_id` | `company_key@YYYY-MM-DD` (first date of the series); a new series starts at a trading gap > 30 days with an ISIN change (post-insolvency relistings) | ST-004 |
| `symbol` | the NSE symbol actually traded on that date (= Kite `tradingsymbol` for live orders) | bhavcopy / Kite |
| `isin` | the day's ISIN; informational (changes at splits) | bhavcopy / Kite |
| `instrument_token` | Kite's numeric instrument id (live only) | Kite instruments dump |
| `trade_date` | the IST trading date (`date`, no time) | — |

`company_key` can change when a company renames again. Research outputs are regenerated, so that is fine there.
Operational rows therefore store the **facts as traded** (`symbol`, `isin`, `instrument_token`) and resolve
`company_key` when reading, never the other way round.

## 3. Research store (files, `data/` on prod; git-ignored)

**Raw — immutable downloads, never edited:**
`bhavcopy_legacy/`, `bhavcopy_udiff/`, `corporate_actions/raw/`, `symbol_changes/symbolchange.csv`, `trendlyne/`
(ST-005); MyInvestIQ's `data/bhavcopy/` and `data/indices/` read-only — the latter includes the **Nifty 500 index**,
the benchmark for every backtest and for live performance.

**Derived — rebuildable by a script, each with a validator:**

| File | Grain | Key columns | Built by |
|---|---|---|---|
| `corporate_actions/history.csv` | action | symbol, ex_date, action_type, adjustment_factor | ST-003 |
| `panel/equity_daily/year=YYYY/part-0.parquet` | equity x trading day | trade_date, series_id, company_key, symbol, isin, series, open, high, low, close, prev_close, volume, traded_value | ST-004 |
| `universe/membership.csv` | rebalance x member | cutoff_date, effective_from, rank, series_id, company_key, symbol, median_traded_value, trading_freq, close | ST-004 |
| `reference/sectors.csv` | company x period | company_key, sector, industry, valid_from, valid_to, source | ST-005 |
| `features/<set>/<date>.parquet` | stock x day | series_id, trade_date, feature columns | not written yet (ST-010 computes indicators at load, `core/indicators.py`) |
| `backtests/<run_id>/` | one run; `<run_id>` = `<strategy>_<limits\|faithful>_<start>_<end>` | `config.json` (strategy, limits, period, data notes), `lots.csv` (one row per lot, incl. `pnl`, `pnl_pct`), `orders.csv` (fills with charges by part), `daily.csv` (cash, holdings, equity, invested_cost, tier, benchmark), `tax.csv` (per FY), `metrics.json`, `card.md` (report card) | ST-010 |
| `backtests/compare_<start>_<end>.md` | side-by-side page of the runs carded together | Markdown | ST-010 |

Rules: prices in the panel are **raw**; split/bonus adjustment is applied at read time from `history.csv` +
`config/ca_overrides.csv`, and demerger adjustment (prices only, not volume) from `config/demergers.csv` (ST-019), so a
corrected factor never needs a panel rebuild. Point-in-time: a row dated D uses only
data available at D's close; sectors are dated (`valid_from`) because classifications change. Parquet (pyarrow),
snappy compression, partitioned by year if a file grows past ~1 GB.

**Curated, in git (`config/`):** `ca_overrides.csv`, `ca_exceptions.csv`, `symbol_changes_extra.csv`,
`symbol_exceptions.csv`, `demergers.csv`, `guardrails.toml` (ST-008) and `strategies/*.toml`. Every row has a reason.
TOML rather than YAML for the two config files (ST-008, 2026-10-29): `tomllib` is in the standard library and has no
writer, so "code and ML never write guardrails" is enforced by the parser, not only by convention.

## 4. Operational store (PostgreSQL `stratrad`, ST-012)

Conventions: `timestamptz` stored in UTC, shown in IST; money `numeric(14,2)` INR; prices `numeric(12,2)`; quantities
`integer`; every table has `id bigserial`, `created_at`; status changes are new rows in an events table, not silent
updates; raw broker payloads kept as `jsonb`; schema changes only through numbered SQL migrations in
`database/migrations/`.

| Table | One row per | Main columns | First used by |
|---|---|---|---|
| `strategies` | strategy version | name, version, config (jsonb), status, approved_by, approved_at | ST-010/011 |
| `models` | model version | kind (regime / allocator / ranker), version, artifact path, metrics (jsonb), gate, approved_at | ML phase |
| `signals` | stock x run | run_date, strategy_id, model_id, symbol, isin, sector, score, rank, rationale (jsonb) | ST-011 |
| `proposals` | proposed buy | signal_id, symbol, sector, qty, limit_price, stop_price, amount, status (pending / approved / rejected / expired / blocked), decided_at, telegram ids | ST-011 |
| `orders` | Kite order | proposal_id or position_id, kite_order_id, symbol, instrument_token, side, qty, price, type, reason (entry / stop / manual / exit), status, filled_qty, **avg_price** (Kite's average fill price), raw (jsonb) | ST-006/007 |
| `order_charges` | executed order | order_id, brokerage, stt, exchange_txn, sebi, stamp_duty, gst, total, source (virtual_contract_note / contract_note), reconciled_at | ST-006/009 |
| `gtt_orders` | Kite GTT | position_id, kite_gtt_id, trigger_price, limit_price (NULL for a MARKET leg, migration 003), status | ST-009 |
| `positions` | holding lot (stratrad strategies and `manual`) | symbol, isin, instrument_token, sector, strategy_id, entry date/price/qty, exit date/price/qty, exit_reason (stop / trail / time / manual / rule; `time` migration 003), status, realised_pnl (net of charges) | ST-009 |
| `position_adjustments` | corporate action on a held lot | position_id, ex_date, action_type, factor, qty before/after, entry price before/after, stop before/after | ST-009 |
| `order_events` | status change | order_id / gtt_id / proposal_id, from_status, to_status, at, detail (jsonb) | ST-006+ |
| `guardrail_events` | guardrail check | stage (before_proposal / before_order; before_sell / before_gtt from migration 003, ST-009), rule (comma-separated rule ids behind the outcome, `OK` on a clean allow), decision (allow / ask / block, migration 002), approval_id (Rakesh's approval in `audit_log`, required on an over-limit allow), limits snapshot (jsonb), context (jsonb: request, state, findings, flags, what was read) | ST-008 |
| `daily_snapshots` | trading day | invested_at_cost, market_value, realised / unrealised pnl (net of charges), rolling 12-month loss %, tier (ok / alert / no-buys / halt), exposure by strategy and sector (jsonb) | ST-008/009 |
| `reconciliations` | day x difference | Kite holdings / GTTs vs `positions` / `gtt_orders`; monthly: `order_charges` vs the official contract note | ST-009 |
| `audit_log` | action | actor (system / Rakesh), action, details (jsonb); append-only | all |

Charges: each executed order gets its charges the same day from Kite's virtual contract note (`POST /charges/orders`;
responses come back in request order — match by position, not by order id). They are reconciled monthly against
the official contract note / Console; the difference is recorded in `reconciliations`. P&L and the rolling 12-month
loss are net of charges.

**Lots, strategies and the shared account (Rakesh, 2026-10-14):**
- A position is a **lot**: each weekly buy is its own lot with its own entry price, its own stop and its own GTT (never
  one stop on an averaged entry). Each lot belongs to one strategy; one lot = one order (separate orders per strategy,
  even for the same stock on the same day), so charges belong to exactly one lot.
- A stock may be held by several strategies at once; each keeps its own lots and its own stop rule (from its config).
- stratrad trades in Rakesh's **existing Zerodha account**. His own holdings and trades are lots of the strategy
  **`manual`**, imported from Kite holdings/trades. Shares are interchangeable: a stratrad stop sells that lot's
  quantity and closes that lot; `manual` lots keep their quantity.
- Daily reconciliation: all lots of a stock (every strategy + `manual`) = Kite's holding; stratrad's GTT quantities +
  any of Rakesh's own GTTs never exceed the holding.
- Performance: per strategy from its own lots only (returns, drawdown, hit rate, equity curve); portfolio = all
  stratrad lots, net of charges, vs the Nifty 500 — the 12-month 20% loss limit applies here. `manual` is reported
  separately and never counts against stratrad's caps.
- Tax: the broker applies FIFO across the whole account (a stratrad sale may count as Rakesh's oldest shares). Not
  modelled per lot; taken at portfolio level from Kite's tax P&L. Holds are mostly 1-6 months and Rakesh's gains exceed
  the exemption anyway, so the STCG/LTCG split makes no practical difference (Rakesh).

Corporate actions on held lots: Kite adjusts the holding quantity; stratrad records a `position_adjustments` row
(quantity, entry price and stop scaled by the factor) and re-places the GTT. Reconciliation flags any mismatch.

Read-only from MyInvestIQ (`portfolio_watch`): `corporate_actions`, `fii_daily_flow` (ST-012); `stock_master` for the
sector of a new symbol (ST-008, interim until ST-005 -- a deviation from s.6's "sectors from files", on purpose).

## 5. Flow

```
NSE / Kite / Trendlyne --> raw files --> derived files (panel, universe, sectors, features) --> backtests
                                                   |
                                     approved strategy / model version
                                                   v
ENTRY: daily run --> signals --> guardrails #1 --> proposals --(Rakesh approves)--> guardrails #2 --> order
          (blocked -> logged, never proposed)                (blocked -> proposal 'blocked')          |
                                                                                                      v
                                                                         position + GTT stop (+ charges)
EXIT:  GTT triggered / trailing rule / manual sell in Kite --> order --> position closed (+ charges)
DAILY: Kite holdings, GTTs, corporate actions --> reconciliations, position_adjustments --> daily_snapshots
       daily_snapshots --> loss tier (ok / alert / no-buys / halt) --> guardrails
```

Guardrails run twice: **#1 before proposing**, so Rakesh is never asked to approve something that would be blocked;
**#2 just before placing the order**, because limits can move between approval and order (other fills, a loss
tier change).

## 6. Settled in ST-012 (Rakesh, 2026-10-15)
- Column types, constraints and indexes: `database/migrations/001_initial_schema.sql` (all 14 tables at once).
  Beyond the conventions: Kite's `avg_price` and per-order charge components keep 4 decimals, adjustment factors 8;
  `audit_log` is append-only by trigger (owner included).
- Grants: `stratrad_app` owns everything; `stratrad_reader` SELECT on all tables (default privileges cover later
  tables); `stratrad_ro` SELECT on `portfolio_watch.corporate_actions` and `fii_daily_flow` (ST-012), and
  `stock_master` (2026-10-29, ST-008).
- Raw broker payloads (`jsonb`) are kept indefinitely (< 10 MB a year).
- Research outputs the live run needs (universe, sectors, features) are read from files, never loaded into the
  database.
