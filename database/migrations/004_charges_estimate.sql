-- 004_charges_estimate.sql -- ST-009 piece 3 (2026-11-10).
-- Applied by `python -m scripts.migrate` as stratrad_app (owner), inside one transaction.
--
-- order_charges.source gains 'estimate': charges computed from Zerodha's published rates (backtest/costs.py, the same
-- functions the backtest uses), Rakesh's decision 2026-11-10 ("estimates are fine"). The monthly contract-note
-- reconciliation (piece 4) adds a 'contract_note' row beside it; UNIQUE (order_id, source) keeps one of each.

ALTER TABLE order_charges DROP CONSTRAINT order_charges_source_check;
ALTER TABLE order_charges ADD CONSTRAINT order_charges_source_check
    CHECK (source IN ('estimate', 'virtual_contract_note', 'contract_note'));
