-- 002_guardrail_ask.sql -- ST-008 (2026-10-30): limits are soft, subject to Rakesh's approval.
-- Applied by `python -m scripts.migrate` as stratrad_app (owner), inside one transaction.
--
-- 1. guardrail_events.decision gains 'ask': every hard rule passed but one or more limits would be exceeded, so the
--    lot goes to Rakesh as a question instead of being blocked (tech-debt/ST-008.md, "Decided 2026-10-30").
-- 2. guardrail_events.approval_id points to Rakesh's approval in audit_log. An 'allow' whose limits snapshot shows a
--    limit exceeded must carry one; without it the ST-008 falsifying clause is violated. Checkable with one query.
--
-- The constraint name below is PostgreSQL's default for the inline CHECK in migration 001. If it differs, the DROP
-- fails and the whole migration rolls back -- by design, rather than leaving the old check in place silently.

ALTER TABLE guardrail_events DROP CONSTRAINT guardrail_events_decision_check;
ALTER TABLE guardrail_events ADD CONSTRAINT guardrail_events_decision_check
    CHECK (decision IN ('allow', 'ask', 'block'));

ALTER TABLE guardrail_events ADD COLUMN approval_id bigint REFERENCES audit_log (id);
CREATE INDEX guardrail_events_approval ON guardrail_events (approval_id) WHERE approval_id IS NOT NULL;

COMMENT ON COLUMN guardrail_events.decision IS
    'allow / ask / block. ask = a soft limit would be exceeded; Rakesh may approve (ST-008, 2026-10-30).';
COMMENT ON COLUMN guardrail_events.approval_id IS
    'audit_log row recording Rakesh''s approval of the limits this check exceeded; required on an over-limit allow.';
