# CODE_MAP.md — modules and what calls what

Present tense. Update when a module, entry point or call chain changes.

## Entry points

| Command | Purpose | Writes |
|---|---|---|
| `python -m ingestion.fetch_legacy_bhavcopy [--start --end --dates --retry-404 --force]` | one-time legacy bhavcopy download | `LEGACY_BHAVCOPY_DIR` (only writer), `LOG_DIR` |
| `python -m scripts.validate_legacy_bhavcopy [--skip-overlap --skip-calendar]` | ST-002 acceptance checks | `LOG_DIR` |
| `python -m scripts.relocate_udiff_gap [--go]` | historical one-off (2026-10-08) — do not re-run | — |
| `python -m ingestion.fetch_symbol_changes` | snapshot of NSE's symbol-change list (run before the CA fetch) | `SYMBOL_CHANGES_DIR` |
| `python -m ingestion.fetch_corporate_actions_history [--years Y.. --force]` | one-time CA history 2014..2025-08-25; reuses `raw/` | `CORPORATE_ACTIONS_DIR`, `LOG_DIR` |
| `python -m scripts.validate_corporate_actions [--start --end --skip-spot]` | ST-003 acceptance checks (jump scan over prices) | `LOG_DIR` |
| `python -m scripts.validate_symbol_changes [--start --end]` | ST-013 acceptance checks (renames proved by price runs, ISIN continuity) | `LOG_DIR` |
| `python -m ingestion.build_price_panel [--full] [--start --end]` | price panel: `--full` rebuilds, plain run appends new days | `PANEL_DIR`, `LOG_DIR` |
| `python -m ingestion.build_universe` | all Nifty 500-aligned reviews -> `membership.csv` (seconds) | `PANEL_DIR`, `UNIVERSE_DIR` |
| `python -m scripts.validate_universe [--skip-coverage]` | ST-004 acceptance: coverage, no lookahead, survivorship, fidelity vs NSE list | `LOG_DIR` |
| `python -m scripts.build_demergers [--check]` | ST-019: demerger factors from the stored CA feed + raw panel; keeps `REVIEWED` notes; checks moves around each ex-date | `config/demergers.csv`, `LOG_DIR` |
| `python -m backtest.run --strategy NAME [--start --end --faithful]` | one backtest (ST-010); refuses to write to a share | `BACKTESTS_DIR/<run_id>/`, `LOG_DIR` |
| `python -m scripts.validate_backtest RUN_ID [--rerun]` | ST-010 acceptance checks 1-9 on one run folder (`--rerun`: repeatable + no lookahead, 4 re-runs) | `LOG_DIR` (issues .csv + summary .json) |
| `python -m backtest.report_card RUN_ID [RUN_ID ...]` | plain-language report card per run; 2+ runs also a comparison page | `<run folder>/card.md`, `BACKTESTS_DIR/compare_<start>_<end>.md` |
| `python -m scripts.migrate [--status]` | ST-012: apply pending `database/migrations/NNN_*.sql` once each (checksum-guarded) | database `stratrad`, `LOG_DIR` |
| `python -m scripts.check_database` | ST-012 acceptance: tables, UTC, every role's allowed/refused actions tried for real | `LOG_DIR` |
| `python -m execution.kite_callback` | ST-006: the Kite login callback (systemd `stratrad-kite-callback` on prod; 127.0.0.1:8765) | `KITE_SESSION_FILE`, `audit_log`, `LOG_DIR/kite_callback.log` |
| `python -m scripts.kite_ip_check` | ST-007: one order that cannot execute (qty 0, BUY limit Rs 1), sent through the static-IP proxy and directly: proxy -> order refusal, direct -> IP refusal | stdout |
| `sudo bash deploy/kite-exit/setup.sh` (on the VM) | ST-007: Tailscale (skipped if joined), tinyproxy (rkneo50q only -> api.kite.trade only), ufw, unattended upgrades | the VM |
| `python -m scripts.kite_check` | ST-006: read-only check of today's Kite session (masked user, funds, holdings count) | stdout |
| `python -m scripts.guardrail_check --symbol S --strategy N --qty Q --price P --stop X (--live \| --empty-book ...)` | ST-008: dry-run one buy; ALLOW / ASK (every breach) / BLOCK; exit 0 / 3 / 1. `--live` reads the real book and data | nothing (stdout) |
| `python -m pytest -q tests` | unit tests (no network) | — |

## Modules

| Module | Role |
|---|---|
| `core/config.py` | loads `.env`; all paths (`LEGACY_BHAVCOPY_DIR`, `BHAVCOPY_ARCHIVE_DIR`, `UDIFF_GAP_DIR`, `LOG_DIR`, `CORPORATE_ACTIONS_DIR`, `SYMBOL_CHANGES_DIR`) |
| `core/legacy_bhavcopy_reader.py` | parse/read legacy zips; `BhavcopyDateMissingError`, `BhavcopyFormatError` |
| `core/udiff_bhavcopy_reader.py` | parse/read UDiFF zips (copied from MyInvestIQ); MyInvestIQ archive first, then gap dir |
| `core/prices.py` | `read_ohlcv_for_date()` — routes by date |
| `ingestion/fetch_legacy_bhavcopy.py` | downloader; `validate_payload()` gates every write |
| `scripts/validate_legacy_bhavcopy.py` | overlap + calendar checks |
| `core/symbol_changes.py` | `load_changes()` (NSE snapshot + `config/symbol_changes_extra.csv`), `symbol_on(changes, symbol, date)` |
| `core/corporate_actions.py` | `parse_subject()`, `classify_records()` (feed → rows under the symbol in use on ex_date), `write_history()`, `load_history()` (+ `config/ca_overrides.csv`), `cumulative_factor()`, `adjust_price()`, `adjust_volume()` |
| `ingestion/fetch_symbol_changes.py` | downloads `symbolchange.csv`; keeps the previous snapshot |
| `core/securities.py` | `Securities.load()`; `key_on(symbol, date)` (company key = today's symbol), `symbols_for(key)`, `symbol_for_key_on(key, date)`, `read_stitched_ohlcv(key, start, end)`; `is_equity()` shared with the ST-003 validator |
| `scripts/validate_symbol_changes.py` | price runs per symbol; confirms listed renames at the stitch; finds same-ISIN handovers missing from the list |
| `core/panel.py` | panel schema, `assign_series()` (new series at a > 30-day gap with an ISIN change), `write_year()`, `read_panel(start, end, columns)`, `last_rows()` |
| `ingestion/build_price_panel.py` | reads every day via `core.prices`, equities only, tags `company_key` + `series_id`, writes `data/panel/equity_daily/year=YYYY/` |
| `core/universe.py` | `review_calendar()`, `build_review()` (point-in-time rule), `load_membership()`, `members_on(membership, date)`, `rebalance_dates()` |
| `ingestion/build_universe.py` | builds every review from the panel; logs eligibility counts and turnover |
| `scripts/validate_universe.py` | panel vs raw files, review rebuild with data cut at cut-off, survivorship, overlap with NSE's Nifty 500 list |
| `ingestion/fetch_corporate_actions_history.py` | one call per year, raw JSON kept, `history.csv` written; fails on unclassified subjects |
| `scripts/validate_corporate_actions.py` | feed re-parse, jump scan (equities), PREVCLOSE probe, yfinance spot check; reads `config/ca_exceptions.csv` |
| `core/kite.py` | ST-006: `exchange_token()`, `save_session()` / `load_session()` (refuses a session issued before the last 06:00 IST: `LoginNeeded`), read-only `Client` (profile, margins, holdings; no order endpoints), `login_url()`, `mask()` |
| `execution/kite_callback.py` | ST-006: `handle()` (path, Cloudflare Access header, status, exchange, save, audit) behind a 127.0.0.1-only `http.server`; access log without the query string |
| `core/db.py` | `connect('app' / 'reader' / 'myinvestiq')`: psycopg 3, session timezone UTC, URLs from `.env` |
| `core/trading_calendar.py` | ST-008: `TradingCalendar` over MyInvestIQ's holiday archive (`CALENDAR_ARCHIVE_DIR`, read-only): `is_trading_day()`, `sessions_between(after, before)` (a data age), `previous_trading_day()`; an uncovered year is None, never a guess |
| `execution/guardrails.py` | ST-008: pure rules, no I/O. `load_limits()` (`config/guardrails.toml`, raises if absent), `evaluate()` -> `Decision` ALLOW / ASK / BLOCK with every `Finding`; hard rules G01/G02/G06a/G11 halt/G12 unknown/G13/G14 block, soft limits ask |
| `execution/guardrail_service.py` | ST-008: the only door to an order. `check_before_proposal(request, run_picks)`, `check_before_order(proposal_id, approval_id)` (G00 = proposal not approved); `gather_state()` (committed money, tier, universe, sector, data ages); `approval_covers()` / `approval_details()` (the `audit_log` approval format ST-011 writes); `DbSources` (stratrad_app + stratrad_ro + panel + universe); one `guardrail_events` row per check |
| `database/migrations/002_guardrail_ask.sql` | ST-008: `guardrail_events.decision` gains `ask`; `approval_id` -> `audit_log` |
| `execution/guardrails.py` (sell side) | ST-009: `SellRequest`, `LotState`, `evaluate_sell()` -> ALLOW / BLOCK only; S01 open stratrad lot (never `manual`), S02 quantity vs lot and Kite holding, S03 trigger below LTP by Kite's gap / one active GTT / stops only rise, S04 CNC + configured order type, S05 no delete of an open lot's active GTT, S06 dry-run probe (qty 1, far below LTP, never a lot's GTT) |
| `execution/guardrail_service.py` (clearances) | ST-009: `check_before_sell(req, sources, market)` (stage `before_sell` / `before_gtt`) and `authorise_buy(proposal_id, approval_id)` return `(Decision, Clearance)`; a `Clearance` is minted only on ALLOW after its event row, is one-shot, valid 60 s, bound to one action; `MarketView` protocol (holding, LTP, GTT status) |
| `execution/broker.py` | ST-009: the ONLY Kite writer. Reads direct (holdings, positions, orders, trades, GTTs, LTP; implements `MarketView`); writes via `KITE_ORDER_PROXY` only, built from a redeemed clearance: `place_buy` (LIMIT), `place_sell` (MARKET + market protection), `place_gtt` / `modify_gtt` / `delete_gtt` (single-leg MARKET sell). Orders tagged `st<event_id>` |
| `scripts/gtt_probe.py` | ST-009 piece 2, prod only: `python -m scripts.gtt_probe [--symbol RELIANCE]` -- dry run of the sell path on Kite: qty-1 GTT at ~45% of LTP placed, read back, raised, a lower refused by S06, deleted; checks this run's `guardrail_events` rows; exit 0 / 1 / 2; log `logs/gtt_probe_YYYYMMDD.log` |
| `database/migrations/003_sell_stages.sql` | ST-009: stages `before_sell` / `before_gtt`; `gtt_orders.limit_price` nullable (MARKET leg); `exit_reason` gains `time` |
| `database/migrations/001_initial_schema.sql` | the 14 tables of docs/DATA_MODEL.md s.4; `audit_log` append-only trigger; strategy `manual` seeded; SELECT for `stratrad_reader` |
| `database/setup_roles.sql` | superuser, once: the three roles, database `stratrad`, `stratrad_ro` grants on `portfolio_watch` |
| `core/demergers.py` | `feed_records()` (EQ demerger/scheme records), `candidates()` (factor = prev close / ex-day close; falls > 5% applied), `merge_reviewed()`, `load_demergers()` (applied rows as actions, kind `demerger`) |
| `core/adjusted.py` | `actions_frame()`, `series_factors()` (split + demerger factors per series; demergers price-only), `adjust()` |
| `backtest/run.py` | `load_market(start, end, panel_hook)`, `run_backtest()` (no files), `write_outputs()`, `headline()` (Test 1 = worst rolling 12-month rupee loss at cost / stake), `pnl_at_cost()`, `lot_pnl()` |
| `backtest/report_card.py` | reads a run folder + the latest validator summary; `fy_table()`, `rolling_ahead()`, `bootstrap_lead()` (circular 6-month blocks), `underwater()`, `card()`, `compare()` |
| `scripts/validate_backtest.py` | checks 1-9; charges and tax re-derived from rates/rules written in the script (not `backtest/costs.py`); scrambles future raw prices via `panel_hook` for the lookahead check; reads `config/ca_exceptions.csv` |

## Curated config files (in git, every row has a reason)

| File | Read by | Purpose |
|---|---|---|
| `config/ca_overrides.csv` | `load_history()` | VOID a feed action that did not change the equity scale / ADD a missing factor; a stale VOID raises |
| `config/symbol_changes_extra.csv` | `load_changes()` | renames missing from NSE's list (same 4-column format) |
| `config/demergers.csv` | `load_demergers()` | demerger factors (ST-019): every feed record with prices, factor, applied Y/N, note; `REVIEWED` notes survive a rebuild; Rakesh approves |
| `config/ca_exceptions.csv` | validator only | explains genuine crashes (`drop`) and records capital-reduction decisions (`review`) |
| `config/symbol_exceptions.csv` | ST-013 validator | optional; `not_rename` (a same-ISIN handover that is not a rename) / `rename_ok` (reviewed stitch). None needed 2026-10-11 |

## Call chain

```
core.prices.read_ohlcv_for_date(d)
  ├─ d < 2024-01-01 → core.legacy_bhavcopy_reader.read_ohlcv_for_date → parse_legacy_csv
  └─ else           → core.udiff_bhavcopy_reader.read_ohlcv_for_date → _find_file (MyInvestIQ, then gap) → parse_udiff_csv

fetch_corporate_actions_history: raw/<YYYY>.json → classify_records(records, as_of, load_changes())
  → parse_subject (per "/"-part) → symbol_on (feed symbol → symbol on ex_date) → write_history(history.csv)

adjusting a price: load_history() [history.csv + ca_overrides.csv] → cumulative_factor(table, symbol, from, to)
  → adjust_price = price / factor ; adjust_volume = volume * factor   (price dated before ex_date is divided)
```
