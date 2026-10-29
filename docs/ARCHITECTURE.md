# ARCHITECTURE.md — stratrad + MyInvestIQ as one system

Status: **APPROVED by Rakesh 2026-10-29** as written (ST-021, drafted 2026-10-27). A copy of the decision and the
ownership map goes to the MyInvestIQ repo. Input to the roll-out plan.

Still open in ST-021 (section 8): the MyInvestIQ brief, the Track F issues, and the roll-out plan.

## 1. Decision (Rakesh, 2026-10-27)

- **stratrad = the foundation product.** It pulls daily market data and geopolitical news, prepares the data for
  downstream use, and researches, defines and tracks strategies for Rakesh (the strategy and order work already defined).
- **MyInvestIQ (MIQ) = a stock and market dashboard product** for the user. Its existing user flow (reports, Telegram,
  email, portal) stays intact. It moves from Claude API calls to Claude Code runs on rkneo50q.
- **They work in tandem as one larger system.** `portfolio_watch` and `stratrad` databases continue, and so does the
  on-disk research data. Jobs move between the two programs over time ("eventual separation of jobs").
- **Users:** Rakesh only for now; MIQ's user flow is kept. Opening to other users is decided when the product is mature.
- **Revenue first from stratrad.** Priority: stratrad's two tracks in parallel, full steam on strategy and order roll-out
  (section 6).

## 2. Principles

1. **One writer per dataset.** Every dataset has exactly one owning program that writes it; everyone else reads it
   (files read-only, database through a read-only role). Section 5 is the register.
2. **Move a job in four steps:** build the new owner's job -> run both side by side and compare (about a week) -> switch
   the readers -> retire the old job. Never two writers at once.
3. **Every daily job has a "fresh by" time and a check that fails loudly** (n8n SSH nodes never fail on their own --
   ST-012 finding; use an If node / exit code, plus a health-check line).
4. **Research data in files, operational data in PostgreSQL** (stratrad 2026-10-11) -- applies to both programs.
5. **Each program keeps its own repo, prod folder, database, secrets and issue tracker** (stratrad `ST-`, MIQ `TD-`).
   Cross-program changes are recorded in both trackers, each from its own side.
6. **Cross-program reads are read-only and named:** `stratrad_ro` (stratrad reads `portfolio_watch`) exists; a mirror
   role for MIQ on `stratrad` and read access to stratrad's `data/` are added when MIQ first needs them.

## 3. Functional maps -- current (placeholders)

> **Placeholder.** One-paragraph maps now; each program completes its own (CODE_MAP / SYSTEM_FLOW level) as part of
> the roll-out plan.

**stratrad today** (repo `rakeshkeswani/stratrad`, prod `/home/rakeshbk/stratrad`, database `stratrad`):
- Data: NSE bhavcopy legacy 2014-Jul 2024 + UDiFF gap (ST-002, one-off), adjusted price panel with symbol-change
  stitching (ST-003/013), corporate actions from the stored NSE feed to 2025-08-25 + demergers (ST-019), point-in-time
  Nifty 500 universe (ST-004), Nifty 500 benchmark (ST-010). Built on demand, not on a daily schedule yet.
- Research: backtest engine, validator, report cards (ST-010); strategies `baseline_momentum`, `rakesh_top4`.
- Execution: database with the approved operational model (ST-012), Kite login + read-only client (ST-006).
- Not yet: daily ingestion, news, static-IP orders (ST-007), guardrails (ST-008), GTT stops (ST-009), Telegram (ST-011).

**MyInvestIQ today** (prod `/home/rakeshbk/myinvestiq`, database `portfolio_watch`; from its SYSTEM_FLOW / CODE_MAP):
- 18:00: bhavcopy archive (UDiFF) + NSE trading calendar.
- 06:00 daily pipeline: market feeds (NSE bulk deals, BSE announcements, SEBI orders) -> market-history snapshot ->
  earnings detection -> macro (yfinance, FRED, RBI/DBIE, FII/DII from NSE, geopolitical news) + Claude -> sector P/E
  from the NSE index archive -> sector analysis + Claude -> stock analysis (price, news, OHLCV) + Claude -> per-user
  reports -> email -> alerts.
- 07:00 earnings analysis (filings, AI passes, delivery); 08:00 QA check; weekly stock/sector master refresh.
- Users, subscriptions (FREE/PAID), Telegram bot, portal, email delivery.
- One-off corporate-actions fetch 2026-08-28 (`corporate_actions`; TD-251 open: not fetched since).

## 4. Separation of duties -- current and end state

`Track` = which stratrad track takes it over (section 6); `--` = stays where it is.

| Function | Current owner | End-state owner | Move / note | Track |
|---|---|---|---|---|
| NSE bhavcopy daily download + archive | MIQ (18:00) | stratrad | stratrad already built the panel partly from MIQ's archive | F |
| NSE trading calendar / holidays | MIQ | stratrad | with the bhavcopy job | F |
| Adjusted price panel, series stitching | stratrad (on demand) | stratrad | make it a daily job with a freshness check | F |
| Corporate actions: splits, bonuses | stratrad history to 2025-08-25; MIQ one-off 2026-08-28 (TD-251 open) | stratrad (daily) | replaces ST-020's dependency on MIQ; TD-251 closes as "moved" | F |
| Demergers | stratrad (stored feed to 2025-08) | stratrad (daily) | same fetch as above | F |
| Symbol changes | stratrad | stratrad | daily with the panel | F |
| Index data (NSE index archive), sector P/E inputs | MIQ | stratrad (data); MIQ computes its sector P/E from it | | F |
| Point-in-time universe (Nifty 500) | stratrad | stratrad | semi-annual | -- |
| FII/DII daily flows | MIQ (`fii_daily_flow`) | stratrad | MIQ reads stratrad's copy | F |
| Macro market data (indices, VIX, INR, crude, RBI rate) | MIQ | stratrad (data); MIQ (display, AI synthesis) | | F |
| Market feeds: bulk deals, announcements, SEBI orders | MIQ | stratrad | Rakesh 2026-10-27; earnings detection depends on announcements | F (later) |
| Earnings filings: detect + PDF pulls (data) | MIQ | stratrad | Rakesh 2026-10-27: stratrad = data incl. pulls | F (later) |
| Earnings: AI analysis + delivery | MIQ | MIQ | Rakesh 2026-10-27: MIQ = analysis | -- |
| News and geopolitical | MIQ (NewsAPI, feedparser, macro) | stratrad | new stratrad scope; headlines, links, own summaries | F |
| Stock master: identifiers, names, official BSE classification (weekly) | MIQ (`stock_master`) | stratrad | Rakesh 2026-10-27; stratrad needs sectors for ST-008's per-sector caps | F |
| Sector P/E from the NSE index archive (daily) | MIQ (`sector_master.avg_pe_ratio`) | stratrad (data) | with the index-data takeover | F |
| 13-sector scheme, P/E flag thresholds, Haiku sector metrics | MIQ (`sector_master`) | MIQ (analysis) | Rakesh 2026-10-27: data -> stratrad, analysis -> MIQ | -- |
| AI analysis (macro, sector, stock) | MIQ via Claude API | MIQ via Claude Code on rkneo50q | personal use only for now (section 7) | -- |
| Reports, email, Telegram bot, portal, subscriptions | MIQ | MIQ | user flow stays intact | -- |
| Stock and market dashboards | -- | MIQ | the recast; reads stratrad data | -- |
| Strategy research, backtests, report cards | stratrad | stratrad | | S |
| Signals, proposals, approvals, orders, GTT stops, guardrails, Kite | stratrad (partly built) | stratrad | ST-007/008/009/011 | S |
| stratrad dashboard (strategy status, costs) | -- (ST-017 idea) | MIQ (dashboard reading stratrad's data) | Rakesh 2026-10-27; ST-017 recast accordingly | -- |
| Approval channel (Telegram) | -- | stratrad's own bot (recommended) | separate from MIQ's bots | S |
| QA / freshness checks | MIQ for its pipeline | each owner checks its own datasets; **one combined health view** for both | Rakesh 2026-10-27 | F |
| Scheduling | n8n (both) | n8n (both) | jobs grouped per program | -- |
| Backups | homelab: Job 2 (MIQ), Job 3 + dump (stratrad), Kopia | homelab | tell homelab on any DB/role change | -- |

## 5. Data ownership map (end state; "today" where different)

| Dataset | Owner (writes) | Location | Readers | Fresh by | Backed up |
|---|---|---|---|---|---|
| Bhavcopy archives (legacy, UDiFF) | stratrad (today: MIQ for daily UDiFF) | `stratrad/data/bhavcopy_*` | stratrad, MIQ | 19:00 IST trading days | Job 3 + Kopia |
| Trading calendar | stratrad (today: MIQ) | `stratrad/data/calendar` (TBD) | both | yearly + changes | Job 3 + Kopia |
| Price panel (raw) + adjustment inputs | stratrad | `stratrad/data/panel`, `corporate_actions`, `config/` | stratrad, MIQ | 19:30 IST | Job 3 + Kopia |
| Universe, benchmark | stratrad | `stratrad/data/universe`, `benchmark` | stratrad, MIQ | semi-annual / daily | Job 3 + Kopia |
| FII/DII flows | stratrad (today: MIQ `fii_daily_flow`) | TBD | both | 19:30 IST | Job 3 / Job 2 |
| Macro market data | stratrad (today: MIQ) | TBD | both | 06:00 IST | |
| News and geopolitical | stratrad (today: MIQ, inside the pipeline) | TBD | both | 06:00 IST | |
| Market feeds, announcements, earnings filings (PDFs) | stratrad (today: MIQ) | `portfolio_watch` today; TBD | both | 06:00 IST | Job 2 today |
| Stock master (reference) | stratrad (today: MIQ `stock_master`) | TBD | both | weekly | Job 2 today |
| AI analyses, reports, users, subscriptions | MIQ | `portfolio_watch` | MIQ | 06:30 IST | Job 2 |
| Backtests, report cards | stratrad | `stratrad/data/backtests` | stratrad (MIQ dashboards later) | on demand | Job 3 + Kopia |
| Strategies, signals, orders, positions, audit | stratrad | database `stratrad` | stratrad (MIQ read-only later) | live | nightly dump + Kopia |

## 6. stratrad's two parallel tracks (Rakesh: both full steam)

**Track S -- strategy to orders (revenue first):** ST-007 static IP for orders -> ST-008 guardrails -> ST-009 GTT stops
+ reconciliation -> ST-011 Telegram approvals -> strategy verdict (open in ST-010) -> go live within the limits.

**Track F -- foundation (daily ingestion):** daily bhavcopy + calendar -> daily corporate actions + demergers (unblocks
ST-020 and the 2024-26 held-out run) -> daily panel with a freshness check -> FII/DII -> macro -> news and geopolitical
-> read access for MIQ. Each takeover follows principle 2.

Track S needs from Track F: an up-to-date panel and corporate actions before the first live signal.

## 7. Open questions

1. **Claude Code instead of API calls in MIQ:** fine while the output is for Rakesh only; check Anthropic's terms before
   MIQ serves other users (a subscription is licensed for the subscriber's own use).
2. **Exchange data on dashboards for other users** may need an NSE data licence; not needed while Rakesh is the only
   user.
3. ~~Owners of market feeds, earnings, stock/sector master; stratrad dashboard; health view~~ -- decided by Rakesh
   2026-10-27 (section 4): feeds, earnings data and stock master -> stratrad; earnings analysis -> MIQ; dashboards ->
   MIQ reading stratrad; one combined health view; sector master split (P/E data -> stratrad; 13-sector scheme,
   thresholds, Haiku metrics -> MIQ).

## 8. Next steps

1. Rakesh reviews this draft; TBD rows decided or parked.
2. Brief to the MyInvestIQ project: the decision, principles, sections 4-5, and its own items (recast, Claude Code move,
   reading from stratrad, retiring fetchers as stratrad takes them over, TD-251 -> moved).
3. stratrad issues for Track F (one per takeover) and the roll-out plan with both tracks.
