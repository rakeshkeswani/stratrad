-- 005_snapshot_price_source.sql -- ST-009 piece 4 (2026-11-10).
-- Applied by `python -m scripts.migrate` as stratrad_app (owner), inside one transaction.
--
-- daily_snapshots.price_source: where the day's market values came from (piece 4 decision 1). 'panel' = official NSE
-- closes from stratrad's panel (the backtest's source); 'kite' = Kite's last price, used only when the panel has not
-- reached that day; 'none' = no open lots. NULL for rows written before this migration (there are none).

ALTER TABLE daily_snapshots ADD COLUMN price_source text CHECK (price_source IN ('panel', 'kite', 'none'));
