"""ST-009 piece 4: the evening check -- reconciliation, `manual` lots, the daily snapshot.

  EveningCheck.run(on)
    1. lifecycle.sync()                    Kite's GTT and order states into our rows (piece 3)
    2. repair                              an open lot with no active GTT: re-placed at its last stop, never lower
                                           (decision 3) -- unless its last GTT was DISABLED, which Kite does on a
                                           split or bonus: then alert and leave it to `scripts.adjust_lot` (decision 2)
    3. reconcile -> `reconciliations`      GTTs (missing / inactive / quantity / trigger / not stratrad's / near Kite's
                                           500 cap) and holdings (Kite holds fewer than stratrad's lots)
    4. `manual` lots                       per stock: Kite holding - stratrad's lots, one lot, Kite's average price;
                                           closed when the remainder reaches 0. Database only -- never a Kite write.
    5. `daily_snapshots` (trading days)    stratrad lots: cost, value, realised / unrealised, the rolling 12-month result
                                           = the backtest's Test 1 measure (backtest/run.py pnl_at_cost), tier from
                                           guardrails.toml [tiers] on the stake (capital.cap). This lifts ST-008's G11.

Decisions 2026-11-10 (Rakesh): (1) market value from official panel closes, Kite's last price only when the panel
has not reached the day (recorded in `price_source`); (2) splits / bonuses: detect + alert, factor applied by Rakesh
with scripts.adjust_lot; (3) a missing GTT is re-placed automatically; (4) the contract-note check is a later item;
(5) `manual` = one lot per stock (his `zdh` rows; stratrad's are `STR` in his tracker).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from backtest.costs import buy_charges, sell_charges
from execution.lifecycle import DbStore, Report, floor_tick

logger = logging.getLogger(__name__)

GTT_SOFT_CAP = 450               # Kite allows 500 pending GTTs; alert well before
WINDOW_DAYS = 365                # Test 1's rolling window (backtest/run.py pnl_at_cost)


@dataclass(frozen=True)
class Diff:
    kind: str                    # reconciliations.kind: 'gtt' | 'holdings'
    what: str                    # missing_gtt, extra_gtt_rows, gtt_inactive, gtt_qty, gtt_trigger, gtt_symbol,
    symbol: str | None           # gtt_not_ours, gtt_cap, holding_short
    expected: dict
    actual: dict

    def text(self) -> str:
        return f'{self.kind}/{self.what} {self.symbol or ""}: expected {self.expected}, actual {self.actual}'


# ---- Kite's book (pure) ---------------------------------------------------------------------------------------------

def kite_totals(holdings: list[dict], day_positions: list[dict]) -> dict[str, int]:
    """Shares per symbol: demat + T1 holdings (any exchange) + today's net CNC buys."""
    out: dict[str, int] = defaultdict(int)
    for h in holdings:
        out[h['tradingsymbol']] += int(h.get('quantity') or 0) + int(h.get('t1_quantity') or 0)
    for p in day_positions:
        if p.get('product') == 'CNC':
            out[p['tradingsymbol']] += int(p.get('quantity') or 0)
    return dict(out)


def kite_prices(holdings: list[dict]) -> dict[str, tuple[float, float]]:
    """symbol -> (Kite's average price, last price)."""
    return {h['tradingsymbol']: (float(h.get('average_price') or 0), float(h.get('last_price') or 0)) for h in holdings}


# ---- reconciliation (pure) ------------------------------------------------------------------------------------------

def gtt_differences(open_lots: list[dict], our_active: list[dict], our_ids: set, kite_gtts: list[dict]) -> list[Diff]:
    by_id = {int(g['id']): g for g in kite_gtts}
    diffs: list[Diff] = []
    for lot in open_lots:
        mine = [g for g in our_active if g['position_id'] == lot['id']]
        if not mine:
            diffs.append(Diff('gtt', 'missing_gtt', lot['symbol'], {'lot': lot['id'], 'gtts': 1}, {'gtts': 0}))
            continue
        if len(mine) > 1:
            diffs.append(Diff('gtt', 'extra_gtt_rows', lot['symbol'], {'lot': lot['id'], 'gtts': 1},
                              {'gtts': len(mine), 'ids': [g['kite_gtt_id'] for g in mine]}))
        g = mine[-1]
        k = by_id.get(int(g['kite_gtt_id']))
        if k is None or k.get('status') != 'active':
            diffs.append(Diff('gtt', 'gtt_inactive', lot['symbol'], {'lot': lot['id'], 'gtt': g['kite_gtt_id'],
                                                                     'status': 'active'},
                              {'status': None if k is None else k.get('status')}))
            continue
        cond, leg = k.get('condition') or {}, (k.get('orders') or [{}])[0]
        if int(leg.get('quantity') or 0) != int(lot['qty']):
            diffs.append(Diff('gtt', 'gtt_qty', lot['symbol'], {'lot': lot['id'], 'qty': int(lot['qty'])},
                              {'gtt': g['kite_gtt_id'], 'qty': leg.get('quantity')}))
        kite_trigger = float((cond.get('trigger_values') or [0])[0])
        if abs(kite_trigger - float(g['trigger_price'])) > 0.001:
            diffs.append(Diff('gtt', 'gtt_trigger', lot['symbol'], {'lot': lot['id'], 'trigger': float(g['trigger_price'])},
                              {'gtt': g['kite_gtt_id'], 'trigger': kite_trigger}))
        if cond.get('tradingsymbol') not in (None, lot['symbol']):
            diffs.append(Diff('gtt', 'gtt_symbol', lot['symbol'], {'lot': lot['id']},
                              {'gtt': g['kite_gtt_id'], 'symbol': cond.get('tradingsymbol')}))
    active = [k for k in kite_gtts if k.get('status') == 'active']
    for k in active:
        if int(k['id']) not in our_ids:
            diffs.append(Diff('gtt', 'gtt_not_ours', (k.get('condition') or {}).get('tradingsymbol'),
                              {'owner': 'stratrad'}, {'gtt': k['id'], 'status': k.get('status')}))
    if len(active) >= GTT_SOFT_CAP:
        diffs.append(Diff('gtt', 'gtt_cap', None, {'below': GTT_SOFT_CAP}, {'active': len(active)}))
    return diffs


def holding_differences(open_lots: list[dict], totals: dict[str, int]) -> list[Diff]:
    """Kite holds fewer shares than stratrad's lots: stratrad's shares were sold outside stratrad."""
    mine: dict[str, int] = defaultdict(int)
    for lot in open_lots:
        mine[lot['symbol']] += int(lot['qty'])
    return [Diff('holdings', 'holding_short', s, {'stratrad_lots': q}, {'kite': totals.get(s, 0)})
            for s, q in sorted(mine.items()) if totals.get(s, 0) < q]


def manual_targets(totals: dict[str, int], prices: dict[str, tuple[float, float]],
                   open_lots: list[dict]) -> dict[str, tuple[int, float]]:
    """symbol -> (remainder, entry price) for every stock where Kite holds more than stratrad's lots."""
    mine: dict[str, int] = defaultdict(int)
    for lot in open_lots:
        mine[lot['symbol']] += int(lot['qty'])
    out = {}
    for s, q in totals.items():
        rest = q - mine.get(s, 0)
        if rest > 0:
            avg, last = prices.get(s, (0.0, 0.0))
            price = avg if avg > 0 else last
            if price > 0:
                out[s] = (rest, round(price, 2))
    return out


# ---- the snapshot (pure) --------------------------------------------------------------------------------------------

def lot_charges(lot: dict) -> tuple[float, float]:
    """(buy charges, sell charges) as estimated at the time -- backtest/costs.py, as in execution/lifecycle.py."""
    cost = float(lot['entry_price']) * int(lot['qty'])
    buy = buy_charges(cost)['total']
    sell = sell_charges(float(lot['exit_price']) * int(lot['qty']), dp=True)['total'] if lot.get('exit_date') else 0.0
    return buy, sell


def is_open_on(lot: dict, on: date) -> bool:
    return lot['entry_date'] <= on and (lot.get('exit_date') is None or lot['exit_date'] > on)


def rolling_result(lots: list[dict], marks: dict[str, float], on: date, window_days: int | None = WINDOW_DAYS) -> float:
    """The backtest's Test 1 measure for one day (backtest/run.py pnl_at_cost, level b): open lots against their own
    buy price (value - cost - buy charges) + lots sold in the window (sale - sell charges - cost - buy charges)."""
    total = 0.0
    for lot in lots:
        qty, cost = int(lot['qty']), float(lot['entry_price']) * int(lot['qty'])
        buy, sell = lot_charges(lot)
        if is_open_on(lot, on):
            total += marks[lot['symbol']] * qty - cost - buy
        elif lot.get('exit_date') and lot['exit_date'] <= on and (
                window_days is None or lot['exit_date'] > on - timedelta(days=window_days)):
            total += float(lot['exit_price']) * qty - sell - cost - buy
    return total


def tier_for(result: float, stake: float, tiers: dict) -> str:
    frac = result / stake
    if frac <= -tiers['halt']:
        return 'halt'
    if frac <= -tiers['no_new_buys']:
        return 'no_buys'
    if frac <= -tiers['alert']:
        return 'alert'
    return 'ok'


def build_snapshot(on: date, lots: list[dict], marks: dict[str, float], price_source: str, stake: float,
                   tiers: dict) -> dict:
    """One `daily_snapshots` row for stratrad's lots (never `manual`). Raises KeyError if an open lot has no price."""
    open_ = [l for l in lots if is_open_on(l, on)]
    invested = sum(float(l['entry_price']) * int(l['qty']) for l in open_)
    value = sum(marks[l['symbol']] * int(l['qty']) for l in open_)
    realised = 0.0
    for l in lots:
        if l.get('exit_date') and l['exit_date'] <= on:
            buy, sell = lot_charges(l)
            realised += (float(l['exit_price']) - float(l['entry_price'])) * int(l['qty']) - buy - sell
    result = rolling_result(lots, marks, on)
    by_strategy: dict[str, float] = defaultdict(float)
    by_sector: dict[str, float] = defaultdict(float)
    for l in open_:
        by_strategy[l['strategy']] += float(l['entry_price']) * int(l['qty'])
        by_sector[l.get('sector') or 'unknown'] += float(l['entry_price']) * int(l['qty'])
    return {'snapshot_date': on, 'invested_at_cost': round(invested, 2), 'market_value': round(value, 2),
            'realised_pnl': round(realised, 2), 'unrealised_pnl': round(value - invested, 2),
            'loss_12m_pct': round(result / stake, 4), 'tier': tier_for(result, stake, tiers),
            'exposure_by_strategy': {k: round(v, 2) for k, v in by_strategy.items()},
            'exposure_by_sector': {k: round(v, 2) for k, v in by_sector.items()},
            'price_source': price_source if open_ else 'none'}


# ---- splits and bonuses (scripts.adjust_lot) ------------------------------------------------------------------------

def apply_adjustment(store: Any, lifecycle: Any, lot_id: int, factor: float, ex_date: date, action_type: str,
                     report: Report) -> bool:
    """Scale a lot by `factor` new shares per old share (a 1:5 split is 5; a 1:1 bonus is 2) and re-protect it.

    Refuses: not an open stratrad lot; a fractional new quantity; a GTT still active on Kite (Kite disables it on a
    split -- run the evening check first so our row shows it)."""
    lot = store.lot(lot_id)
    if lot is None or lot['status'] != 'open' or lot['strategy'] == 'manual':
        report.alert('lot %s is not an open stratrad lot', lot_id)
        return False
    if action_type not in ('split', 'bonus'):
        report.alert('action %r: only split / bonus', action_type)
        return False
    new_qty = int(lot['qty']) * factor
    if factor <= 0 or abs(new_qty - round(new_qty)) > 1e-9:
        report.alert('lot %s: %s x %s is not a whole number of shares', lot_id, lot['qty'], factor)
        return False
    if store.active_gtt_row(lot_id) is not None:
        report.alert('lot %s still has an active GTT: run the evening check first (Kite disables it on a split)', lot_id)
        return False
    old_stop = store.last_gtt_trigger(lot_id)
    new_stop = floor_tick(old_stop / factor) if old_stop else None
    new_entry = round(float(lot['entry_price']) / factor, 4)
    store.add_adjustment({'position_id': lot_id, 'ex_date': ex_date, 'action_type': action_type, 'factor': factor,
                          'qty_before': int(lot['qty']), 'qty_after': int(round(new_qty)),
                          'entry_price_before': float(lot['entry_price']), 'entry_price_after': new_entry,
                          'stop_before': old_stop, 'stop_after': new_stop})
    store.resize_lot(lot_id, int(round(new_qty)), new_entry)
    report.ok('lot %s adjusted (%s x%s from %s): qty %s -> %d, entry %.2f -> %.4f, stop %s -> %s', lot_id,
              action_type, factor, ex_date, lot['qty'], round(new_qty), float(lot['entry_price']), new_entry,
              old_stop, new_stop)
    return lifecycle.reprotect(store.lot(lot_id), report, trigger=new_stop)


# ---- the run --------------------------------------------------------------------------------------------------------

class EveningCheck:
    def __init__(self, store: Any, broker: Any, lifecycle: Any, prices: Any, calendar: Any, limits: dict):
        self.store, self.broker, self.lifecycle = store, broker, lifecycle
        self.prices, self.calendar, self.limits = prices, calendar, limits

    def run(self, on: date, report: Report | None = None) -> Report:
        report = report or Report()
        self.lifecycle.sync(report)
        holdings = self.broker.holdings()
        day = (self.broker.positions() or {}).get('day', [])
        totals, kprices = kite_totals(holdings, day), kite_prices(holdings)

        open_lots = self.store.open_lots()
        for lot in open_lots:                                      # 2. repair
            if self.store.active_gtt_row(lot['id']) is not None:
                continue
            if self.store.last_gtt_status(lot['id']) == 'disabled':
                report.alert('lot %s %s: GTT disabled by Kite (split / bonus?) -- run scripts.adjust_lot with the '
                             'factor', lot['id'], lot['symbol'])
            else:
                self.lifecycle.reprotect(lot, report)

        diffs = (gtt_differences(open_lots, self.store.active_gtt_rows(), self.store.all_kite_gtt_ids(),
                                 self.broker.gtts())
                 + holding_differences(open_lots, totals))       # 3. reconcile
        for d in diffs:
            self.store.add_reconciliation(on, d)
            report.alert('reconciliation %s', d.text())
        if not diffs:
            report.ok('reconciliation %s: no differences (%d open lots)', on, len(open_lots))

        self._manual(on, manual_targets(totals, kprices, open_lots), kprices, report)   # 4.

        trading = self.calendar.is_trading_day(on)
        if trading is None:
            report.alert('snapshot %s not written: trading calendar does not cover the year', on)
        elif trading:
            self._snapshot(on, kprices, report)                    # 5.
        else:
            report.ok('%s is not a trading day: no snapshot', on)
        return report

    def _manual(self, on: date, targets: dict, kprices: dict, report: Report) -> None:
        current = {l['symbol']: l for l in self.store.manual_lots()}
        added = changed = closed = 0
        for symbol, (qty, price) in targets.items():
            lot = current.get(symbol)
            if lot is None:
                self.store.add_manual_lot(symbol, on, price, qty)
                added += 1
            elif int(lot['qty']) != qty or abs(float(lot['entry_price']) - price) > 0.005:
                self.store.resize_lot(lot['id'], qty, price)
                changed += 1
        for symbol, lot in current.items():
            if symbol not in targets:
                last = kprices.get(symbol, (0.0, 0.0))[1] or float(lot['entry_price'])
                self.store.close_manual_lot(lot['id'], on, last, int(lot['qty']))
                closed += 1
        report.ok('manual lots: %d held, %d new, %d changed, %d closed', len(targets), added, changed, closed)

    def _snapshot(self, on: date, kprices: dict, report: Report) -> None:
        lots = self.store.stratrad_lots_until(on)
        open_symbols = {l['symbol'] for l in lots if is_open_on(l, on)}
        last = self.prices.last_date()
        if open_symbols and last is not None and last >= on:
            marks = {s: self.prices.close_on(s, on) for s in open_symbols}
            source = 'panel'
        else:
            marks = {s: kprices.get(s, (0.0, 0.0))[1] or None for s in open_symbols}
            source = 'kite'
        missing = sorted(s for s, p in marks.items() if not p)
        if missing:
            report.alert('snapshot %s not written: no price for %s (G11 keeps blocking)', on, missing)
            return
        row = build_snapshot(on, lots, marks, source, float(self.limits['capital']['cap']), self.limits['tiers'])
        self.store.upsert_snapshot(row)
        report.ok('snapshot %s: cost %.2f, value %.2f, 12-month result %.2f%% of stake, tier %s (prices: %s)', on,
                  row['invested_at_cost'], row['market_value'], row['loss_12m_pct'] * 100, row['tier'],
                  row['price_source'])


# ---- the real store -------------------------------------------------------------------------------------------------

class ReconStore(DbStore):
    """DbStore plus what the evening check reads and writes."""

    def last_gtt_status(self, position_id):
        r = self._one('SELECT status FROM gtt_orders WHERE position_id = %s ORDER BY id DESC LIMIT 1', (position_id,))
        return None if r is None else r['status']

    def all_kite_gtt_ids(self) -> set:
        return {int(r['kite_gtt_id']) for r in self._all('SELECT kite_gtt_id FROM gtt_orders '
                                                         'WHERE kite_gtt_id IS NOT NULL')}

    def add_reconciliation(self, on, d: Diff):
        from psycopg.types.json import Jsonb
        diff = {'what': d.what}
        self._write('INSERT INTO reconciliations (recon_date, kind, symbol, expected, actual, difference) '
                    'VALUES (%s, %s, %s, %s, %s, %s)', (on, d.kind, d.symbol, Jsonb(d.expected), Jsonb(d.actual),
                                                         Jsonb(diff)))

    def _manual_id(self) -> int:
        return self._one("SELECT id FROM strategies WHERE name = 'manual' ORDER BY version DESC LIMIT 1")['id']

    def manual_lots(self):
        return self._all("SELECT p.id, p.symbol, p.entry_qty AS qty, p.entry_price FROM positions p "
                         "JOIN strategies s ON s.id = p.strategy_id WHERE s.name = 'manual' AND p.status = 'open'")

    def add_manual_lot(self, symbol, on, price, qty):
        return self.add_lot({'strategy_id': self._manual_id(), 'symbol': symbol, 'sector': None, 'entry_date': on,
                             'entry_price': price, 'entry_qty': qty})

    def close_manual_lot(self, position_id, on, price, qty):
        self._write("UPDATE positions SET status = 'closed', exit_date = greatest(%s, entry_date), exit_price = %s, "
                    "exit_qty = %s, exit_reason = 'manual' WHERE id = %s", (on, price, qty, position_id))

    def stratrad_lots_until(self, on):
        return self._all("SELECT p.id, p.symbol, p.sector, s.name AS strategy, p.entry_date, p.entry_price, "
                         "p.entry_qty AS qty, p.exit_date, p.exit_price FROM positions p "
                         "JOIN strategies s ON s.id = p.strategy_id WHERE s.name <> 'manual' AND p.entry_date <= %s",
                         (on,))

    def upsert_snapshot(self, row):
        from psycopg.types.json import Jsonb
        params = dict(row, exposure_by_strategy=Jsonb(row['exposure_by_strategy']),
                      exposure_by_sector=Jsonb(row['exposure_by_sector']))
        self._write('INSERT INTO daily_snapshots (snapshot_date, invested_at_cost, market_value, realised_pnl, '
                    'unrealised_pnl, loss_12m_pct, tier, exposure_by_strategy, exposure_by_sector, price_source) '
                    'VALUES (%(snapshot_date)s, %(invested_at_cost)s, %(market_value)s, %(realised_pnl)s, '
                    '%(unrealised_pnl)s, %(loss_12m_pct)s, %(tier)s, %(exposure_by_strategy)s, %(exposure_by_sector)s, '
                    '%(price_source)s) ON CONFLICT (snapshot_date) DO UPDATE SET '
                    'invested_at_cost = EXCLUDED.invested_at_cost, market_value = EXCLUDED.market_value, '
                    'realised_pnl = EXCLUDED.realised_pnl, unrealised_pnl = EXCLUDED.unrealised_pnl, '
                    'loss_12m_pct = EXCLUDED.loss_12m_pct, tier = EXCLUDED.tier, '
                    'exposure_by_strategy = EXCLUDED.exposure_by_strategy, '
                    'exposure_by_sector = EXCLUDED.exposure_by_sector, price_source = EXCLUDED.price_source', params)

    def add_adjustment(self, row):
        self._write('INSERT INTO position_adjustments (position_id, ex_date, action_type, factor, qty_before, qty_after, '
                    'entry_price_before, entry_price_after, stop_before, stop_after) VALUES (%(position_id)s, '
                    '%(ex_date)s, %(action_type)s, %(factor)s, %(qty_before)s, %(qty_after)s, %(entry_price_before)s, '
                    '%(entry_price_after)s, %(stop_before)s, %(stop_after)s)', row)


def from_db(broker: Any) -> EveningCheck:
    """The real wiring: the lifecycle's gate and connection, a ReconStore on the same connection."""
    from core.trading_calendar import TradingCalendar
    from execution import lifecycle as lc
    from execution.guardrail_service import CONFIG_PATH
    from execution.guardrails import load_limits
    life = lc.from_db(broker)
    store = ReconStore(life.store.conn)
    life.store = store
    return EveningCheck(store, broker, life, life.prices, TradingCalendar(), load_limits(CONFIG_PATH))
