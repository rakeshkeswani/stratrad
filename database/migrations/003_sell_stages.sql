-- 003_sell_stages.sql -- ST-009 (2026-11-10): the sell side.
-- Applied by `python -m scripts.migrate` as stratrad_app (owner), inside one transaction.
--
-- 1. guardrail_events.stage gains 'before_sell' (a sell order: time exit, backstop) and 'before_gtt' (a GTT placed,
--    modified or deleted). Every Kite write needs a clearance, and a clearance is minted only after its event row is
--    written (execution/guardrail_service.py), so these stages carry every sell-side write.
-- 2. gtt_orders.limit_price becomes optional: ST-009's GTT leg is a MARKET sell with market protection (decision 3,
--    "ensure the sale when the GTT hits"), which has no limit price. The CHECK (> 0) still applies when one is set.
-- 3. positions.exit_reason gains 'time' for the 12-month time exit (folded into ST-009).
--
-- As in 002, constraint names are PostgreSQL's defaults for migration 001's inline CHECKs; if one differs its DROP
-- fails and the whole migration rolls back.

ALTER TABLE guardrail_events DROP CONSTRAINT guardrail_events_stage_check;
ALTER TABLE guardrail_events ADD CONSTRAINT guardrail_events_stage_check
    CHECK (stage IN ('before_proposal', 'before_order', 'before_sell', 'before_gtt'));

ALTER TABLE gtt_orders ALTER COLUMN limit_price DROP NOT NULL;

ALTER TABLE positions DROP CONSTRAINT positions_exit_reason_check;
ALTER TABLE positions ADD CONSTRAINT positions_exit_reason_check
    CHECK (exit_reason IN ('stop', 'trail', 'time', 'manual', 'rule'));

COMMENT ON COLUMN guardrail_events.stage IS
    'before_proposal / before_order (buys, ST-008); before_sell / before_gtt (sells and GTT actions, ST-009).';
COMMENT ON COLUMN gtt_orders.limit_price IS 'NULL for a MARKET leg (ST-009 default); set only for a LIMIT leg.';
