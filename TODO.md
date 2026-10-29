# TODO.md — open items and resume point

Read at session start (after `TECH_DEBT.md` and the tail of `SESSION_LOG.md`). Tick/remove as done.
Last updated: 2026-10-29 (Track S prioritised; ST-021 approved; ST-008 rules built; ST-022 raised).

## Resume here (new conversation)
0. **Track S first (Rakesh 2026-10-29; docs/ARCHITECTURE.md s.6).** Track F items are taken up when Track S needs them.
   **Next: finish ST-008** -- branch `st/ST-008-guardrails` is 4 commits ahead of main and incomplete. Left on it:
   (a) the service layer (`execution/guardrail_service.py`: gather state, write one `guardrail_events` row per check)
   -- **blocked on the `stock_master` schema**, which Rakesh pastes in chat (the X: share is unreadable to Claude,
   see LEARNINGS 2026-10-29); (b) DATA_MODEL.md s.1/s.4/s.6 still say `stratrad_ro` has SELECT on `corporate_actions`
   and `fii_daily_flow` **only** -- update once the grant is confirmed from the `\dp` output; (c) the MyInvestIQ-side
   TD- issue recording the grant, and the note to homelab (role change -> backup chain).
   Then ST-009 GTT stops -> ST-011 Telegram -> verdict -> go live.
   **Gates that are not deferrable** (Track F, on the critical path): daily bhavcopy + calendar, daily corporate
   actions + demergers, daily panel + freshness check. They gate the held-out run, the verdict and go-live (G13).
   None has an ST number yet -- raising them is one of ST-021's three leftovers.
   **Go-live checklist so far:** ST-007 rebuild drill (+ KITE_ORDER_PROXY -> MagicDNS name); strategy verdict (ST-022);
   the three Track F gates above. Test 1 paper-profits question: closed 2026-10-29.
   Also open: ST-022 strategy research loop (design to be proposed).
1. **Strategy verdict (Rakesh):** review document "stratrad strategy review: ST-010 backtests 2014-2023" (Claude
   Docs artifact, numbers after ST-019) holds the cards, comparison, Claude's recommendations and the card change list.
   Decided 2026-10-29 (recorded in ST-022): Test 1 keeps open lots at their own buy price (paper profits counted in
   neither direction -- the card-change question is closed); a strategy is considered on the full 2014-2026 span while
   development stays confined to 2014-2023; `rakesh_top4, faithful` is a yardstick, not a candidate, so the candidates
   are `rakesh_top4, limits` and `baseline_momentum`.
   **Still open for Rakesh:** the verdict on those two (go live / plumbing baseline / smaller size / adjust and
   re-test) -- and per the span decision it cannot be final until the 2024-26 held-out run, which needs Track F's
   daily corporate-action fetch.
   Decided 2026-10-15: capital grows only when Rakesh allocates more (no cap-growth re-test).
2. Then ST-012 (database; before execution) → ST-006/007/008/009/011; ST-005 (sectors); ST-017/018 later.
3. Prod SSH paste glitch: `echo 'set enable-bracketed-paste off' >> ~/.inputrc` (new session), or update PuTTY.

Done 2026-10-15: ST-016 rename closed (merge `150f9da`); ST-010 resolved (merge `d6ea9f7`: engine, validator, report
card; prod = laptop results); ST-019 demergers resolved (merge `5d9e73d`: 94 factors applied, list approved).
Done 2026-10-14: ST-004 universe (merge `1fe1a08`, prod confirmed); ST-016 rename steps 0-8; data model approved.
Done 2026-10-11: ST-001/002/003/013/014/015.

## Rakesh — outside this repo
- [x] MyInvestIQ prod includes `98255d6` (prod HEAD `47c82fb`, pulled 2026-10-10)
- [ ] **MyInvestIQ TD-251 (High, raised 2026-10-11):** daily corporate-actions fetch + backfill from 2025-08-26.
      Their table today: 100 rows, ex_date 2025-08-29..2026-08-25, last fetched 2026-08-28, so **2025-08-26..28 is in
      neither source** until TD-251's backfill. Our history findings are recorded there as design inputs.
      ST-012 depends on it (merge + freshness check).
- [x] MyInvestIQ TD-247 lint: fixed 2026-10-09; lint on main 0 errors, `--no-verify` no longer needed
- [ ] Optional: `sudo apt install tmux` on rkneo50q; tablet SSH = Tailscale app + Termius/JuiceSSH to
      `100.103.189.15:22` as rakeshbk
- [ ] Optional: `git config --global core.editor notepad`

## Notes to carry into later STs
- Live system (ST-008 guardrails / news): ST-003 found 6 genuine one-day crashes 2015-2020 (UNITECH, DHFL, INFIBEAM,
  JETAIRWAYS, YESBANK, IDEA; `config/ca_exceptions.csv`), all news-driven (rumours, regulator action, insolvency).
  Rakesh 2026-10-11: the final system must handle these -- e.g. gap-down / circuit handling for GTT stops, and
  news checks before buys.
- ST-004: EASTSILK (and any symbol with a capital reduction during a suspension) has an incomparable price series
  across the gap; universe filters keep penny stocks out anyway. ST-013 found the same for post-insolvency relistings
  (INDOSOLAR->WAAREEINDO, SPENTEX->CLCIND, NTL->NEUEON): start a new series at a run gap > 30 days with an ISIN change.
  Identities come from `core/securities.py` (key = today's symbol).
- ST-005 (Trendlyne): the `[stratrad]` Samba share is read-only (Samba default), so the weekly drop needs a
  writable path — e.g. a folder on the rkfspool share (Y:) that the importer reads.
- ST-006 (Kite): keys in `.env` → exclude `.env` from the backup mirror (see backup handover).
- ST-012: read-only Postgres role on `portfolio_watch` for `corporate_actions` and `fii_daily_flow`.
- Weekend special sessions (e.g. Sunday Muhurat) may be missing from the legacy archive — accepted, negligible.
- Plan doc (Claude doc): "AI Strategy Trading System — Research & Implementation Plan".
