# Issue Index — stratrad (ST- prefix)

**This file is the index and the ONLY place status lives.** Full detail for each issue is in
`tech-debt/ST-NNN.md` (frontmatter mirrors this table; if they disagree, this table is right).
Workflow, status values and gates: `CLAUDE.md` → "Change workflow".

| ST | Title | Pri | Status | Since | Area | Next action |
|---|---|---|---|---|---|---|
| [ST-001](tech-debt/ST-001.md) | Project scaffold | High | Resolved | 2026-10-08 | repo | — (prod confirmed 2026-10-11) |
| [ST-002](tech-debt/ST-002.md) | Legacy bhavcopy 2014–Jul 2024 + UDiFF gap + validated readers | High | Resolved | 2026-10-08 | data | — (prod confirmed 2026-10-11) |
| [ST-003](tech-debt/ST-003.md) | Split/bonus adjustment (corporate actions) — history 2014..2025-08-25 | High | Resolved | 2026-10-08 | data | — (prod confirmed 2026-10-11, merge `bf2f689`) |
| [ST-004](tech-debt/ST-004.md) | Point-in-time top-500 universe | High | Resolved | 2026-10-08 | data | — (prod confirmed 2026-10-14, merge `1fe1a08`) |
| [ST-005](tech-debt/ST-005.md) | Trendlyne weekly drop + importer | Med | Not started | 2026-10-08 | data | — |
| [ST-006](tech-debt/ST-006.md) | Kite Connect app + daily login | High | Resolved | 2026-10-08 | execution | — (prod confirmed 2026-10-24) |
| [ST-007](tech-debt/ST-007.md) | Static IP for Kite orders: Mumbai static-IP exit VM (Lightsail, Tailscale-only proxy) | High | In progress — rebuild drill before go-live | 2026-10-08 | execution | static IP 13.205.57.58 verified 2026-10-27; drill deferred |
| [ST-008](tech-debt/ST-008.md) | Guardrail service + guardrails.yaml | High | Not started | 2026-10-08 | execution | — |
| [ST-009](tech-debt/ST-009.md) | GTT stops, trailing, daily reconciliation | High | Not started | 2026-10-08 | execution | — |
| [ST-010](tech-debt/ST-010.md) | Baseline momentum strategy + quick backtest | High | Resolved | 2026-10-08 | strategy | — (prod confirmed 2026-10-15; verdict + card changes open in ST-010.md) |
| [ST-011](tech-debt/ST-011.md) | Telegram proposal and approval flow | High | Not started | 2026-10-08 | notify | — |
| [ST-012](tech-debt/ST-012.md) | Project database + read-only role on MyInvestIQ DB | High | Resolved | 2026-10-08 | database | — (prod confirmed 2026-10-24; CA merge moved to ST-020) |
| [ST-013](tech-debt/ST-013.md) | Symbol changes: stitch price series across renames | High | Resolved | 2026-10-08 | data | — (prod confirmed 2026-10-11, merge `c8a2c77`) |
| [ST-014](tech-debt/ST-014.md) | Workflow + docs (MyInvestIQ-style), own prod folder + X: share | High | Resolved | 2026-10-08 | repo | — (prod confirmed 2026-10-11) |
| [ST-015](tech-debt/ST-015.md) | Housekeeping: .gitattributes, git command conventions, sign-offs | Med | Resolved | 2026-10-11 | repo | — (prod confirmed 2026-10-11, merge `1a66fc2`) |
| [ST-016](tech-debt/ST-016.md) | Rename project strategy-trader -> stratrad (repo, prod folder, share, backup, docs) | High | Resolved | 2026-10-14 | repo | — (prod confirmed 2026-10-15) |
| [ST-017](tech-debt/ST-017.md) | Dashboards: stratrad performance dashboard (+ general market dashboard to decide) | Med | Not started | 2026-10-14 | reporting | backtest view after ST-010; live view after execution issues |
| [ST-018](tech-debt/ST-018.md) | Enter stratrad trades into RK_Fin_DashBoard.xlsx (OneDrive) | Med | Not started | 2026-10-14 | reporting | option a (Claude on request); after ST-006/009 |
| [ST-019](tech-debt/ST-019.md) | Demergers (spin-offs): price adjustment so a held lot keeps its value | High | Resolved | 2026-10-15 | data | — (prod confirmed 2026-10-15) |
| [ST-020](tech-debt/ST-020.md) | Merge MyInvestIQ corporate actions (after 2025-08-25) with freshness and seam checks | High | Not started | 2026-10-15 | data | blocked by MyInvestIQ TD-251 and ST-012; before the held-out run and any live signal; to be recast by ST-021 (stratrad fetches CA itself) |
| [ST-021](tech-debt/ST-021.md) | System architecture: stratrad + MyInvestIQ recast, separation of duties, roll-out plan | High | In progress — branch st/ST-021-architecture | 2026-10-27 | repo | Rakesh's review of docs/ARCHITECTURE.md |
