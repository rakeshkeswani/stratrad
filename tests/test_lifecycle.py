# -*- coding: utf-8 -*-
# tests/test_lifecycle.py
"""ST-009 piece 3: the lot lifecycle against an in-memory book and a fake Kite, with the REAL sell guardrails.

The falsifying clause is checked after every scenario (`assert_protected`): every open lot has exactly one active GTT,
on Kite and in our rows, for its full quantity; no stop ever moved down; every Kite write had a guardrail event.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pytest

from execution.guardrail_service import BUY, _mint, check_before_sell
from execution.guardrails import ALLOW, BLOCK, GTT_DELETE, GTT_MODIFY, GTT_PLACE, SELL, Decision, load_limits
from execution.lifecycle import (
    SEND_FAILED,
    ExitRules,
    Gate,
    Lifecycle,
    floor_tick,
    initial_stop,
    stop_exit_reason,
    time_exit_due,
    trailed_stop,
)

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'guardrails.toml'
CONFIG = {'exit': {'mode': 'lot_stop', 'initial_stop': 0.08, 'trailing_stop': 0.08, 'time_exit_months': 12}}
RULES = ExitRules.from_config(CONFIG)
TODAY = date(2026, 11, 11)


@pytest.fixture(scope='module')
def limits():
    return load_limits(CONFIG_PATH)


# ---- rules ----------------------------------------------------------------------------------------------------------

def test_rules_match_the_backtest():
    assert initial_stop(100.0, RULES) == 92.0
    assert floor_tick(91.99) == 91.9
    assert trailed_stop(92.0, 110.0, RULES, 0.005) == 101.2        # 110 x 0.92
    assert trailed_stop(92.0, 100.2, RULES, 0.005) is None          # 92.18: less than a 0.5% rise
    assert trailed_stop(101.2, 105.0, RULES, 0.005) is None         # never down
    assert not time_exit_due(date(2025, 11, 11), date(2026, 11, 11))  # exactly 12 months: not yet
    assert time_exit_due(date(2025, 11, 11), date(2026, 11, 12))
    assert stop_exit_reason(92.0, 100.0, RULES) == 'stop' and stop_exit_reason(95.0, 100.0, RULES) == 'trail'


@pytest.mark.parametrize('config', [None, {}, {'exit': {'mode': 'stock_average'}},
                                    {'exit': dict(CONFIG['exit'], time_exit_months=6)},
                                    {'exit': dict(CONFIG['exit'], initial_stop=0)}])
def test_exit_rules_fail_closed(config):
    with pytest.raises((ValueError, KeyError)):
        ExitRules.from_config(config)


# ---- the fake world -------------------------------------------------------------------------------------------------

@dataclass
class Book:
    """In-memory stratrad tables; also the guardrail Sources the sell checks read (one source of truth)."""

    proposals: dict = field(default_factory=lambda: {7: {'id': 7, 'symbol': 'INFY', 'sector': 'IT', 'strategy_id': 2,
                                                          'qty': 10, 'limit_price': 100.0}})
    strategies: dict = field(default_factory=lambda: {2: ('baseline_momentum', CONFIG)})
    orders: dict = field(default_factory=dict)
    lots: dict = field(default_factory=dict)
    gtts: dict = field(default_factory=dict)
    charges: list = field(default_factory=list)
    events: list = field(default_factory=list)
    guard_events: list = field(default_factory=list)
    adjustments: dict = field(default_factory=dict)
    ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    # Store
    def proposal(self, pid):
        return self.proposals.get(pid)

    def entry_order(self, pid):
        return next((o for o in self.orders.values() if o['proposal_id'] == pid and o['side'] == 'BUY'), None)

    def add_order(self, row):
        oid = next(self.ids)
        self.orders[oid] = dict(row, id=oid, kite_order_id=None, filled_qty=0)
        return oid

    def set_kite_order_id(self, oid, kite_id, status):
        self.orders[oid].update(kite_order_id=kite_id, status=status)

    def update_order(self, oid, status, filled, avg, raw):
        self.orders[oid].update(status=status, filled_qty=filled, avg_price=avg)

    def link_order(self, oid, lot_id):
        self.orders[oid]['position_id'] = lot_id

    def pending_orders(self):
        return [dict(o) for o in self.orders.values()
                if o['kite_order_id'] and o['status'] not in ('COMPLETE', 'CANCELLED', 'REJECTED', SEND_FAILED)]

    def order_by_kite_id(self, kite_id):
        return next((o for o in self.orders.values() if o['kite_order_id'] == kite_id), None)

    def _lot_view(self, lot):
        name, config = self.strategies[lot['strategy_id']]
        return {'id': lot['id'], 'symbol': lot['symbol'], 'qty': lot['qty'], 'entry_price': lot['entry_price'],
                'entry_date': lot['entry_date'], 'status': lot['status'], 'strategy': name, 'config': config}

    def open_lots(self):
        return [self._lot_view(l) for l in self.lots.values() if l['status'] == 'open']

    def lot(self, lot_id):
        return self._lot_view(self.lots[lot_id]) if lot_id in self.lots else None

    def add_lot(self, row):
        lid = next(self.ids)
        self.lots[lid] = {'id': lid, 'strategy_id': row['strategy_id'], 'symbol': row['symbol'],
                          'qty': row['entry_qty'], 'entry_price': row['entry_price'], 'entry_date': row['entry_date'],
                          'status': 'open'}
        return lid

    def resize_lot(self, lid, qty, price):
        self.lots[lid].update(qty=qty, entry_price=price)

    def close_lot(self, lid, d, price, qty, reason, pnl):
        self.lots[lid].update(status='closed', exit_date=d, exit_price=price, exit_reason=reason, pnl=pnl)

    def active_gtt_row(self, lid):
        rows = [g for g in self.gtts.values() if g['position_id'] == lid and g['status'] == 'active']
        return dict(rows[-1]) if rows else None

    def active_gtt_rows(self):
        return [dict(g) for g in self.gtts.values() if g['status'] == 'active']

    def last_gtt_trigger(self, lid):
        rows = [g for g in self.gtts.values() if g['position_id'] == lid]
        return rows[-1]['trigger_price'] if rows else None

    def add_gtt(self, lid, kite_id, trigger):
        gid = next(self.ids)
        self.gtts[gid] = {'id': gid, 'position_id': lid, 'kite_gtt_id': kite_id, 'trigger_price': trigger,
                          'status': 'active', 'history': [trigger]}
        return gid

    def update_gtt(self, gid, status, trigger=None):
        self.gtts[gid]['status'] = status
        if trigger is not None:
            self.gtts[gid]['trigger_price'] = trigger
            self.gtts[gid]['history'].append(trigger)

    def last_adjustment(self, lid):
        return self.adjustments.get(lid)

    def add_charges(self, oid, ch):
        self.charges.append((oid, round(ch['total'], 4)))

    def add_event(self, **kw):
        self.events.append(kw)

    def live_exit_order(self, lid):
        return next((o for o in self.pending_orders() if o['position_id'] == lid and o['side'] == 'SELL'), None)

    # guardrail Sources (sell side)
    def active_gtt(self, lid):
        g = self.active_gtt_row(lid)
        return None if g is None else {'kite_gtt_id': g['kite_gtt_id'], 'trigger_price': g['trigger_price']}

    def gtt_is_a_lots(self, kite_id):
        return any(g['kite_gtt_id'] == kite_id for g in self.gtts.values())

    def write_event(self, row):
        self.guard_events.append(row)
        return len(self.guard_events)


@dataclass
class Kite:
    """Fake Kite. Writes redeem clearances like execution/broker.py; reads serve MarketView."""

    held: dict = field(default_factory=dict)
    prices: dict = field(default_factory=lambda: {'INFY': 100.0})
    korders: dict = field(default_factory=dict)
    kgtts: dict = field(default_factory=dict)
    writes: list = field(default_factory=list)
    fail_buy: bool = False
    ids: itertools.count = field(default_factory=lambda: itertools.count(1001))

    def holding_qty(self, s):
        return self.held.get(s, 0)

    def last_price(self, s):
        return self.prices.get(s)

    def gtt_status(self, gid):
        return self.kgtts[gid]['status'] if gid in self.kgtts else None

    def gtt_trigger(self, gid):
        return self.kgtts[gid]['trigger'] if gid in self.kgtts else None

    def gtt(self, gid):
        g = self.kgtts[gid]
        leg = {'quantity': g['qty'], 'transaction_type': 'SELL'}
        if g.get('result'):
            leg['result'] = {'order_result': {'order_id': g['result'], 'status': 'success'}}
        return {'id': gid, 'status': g['status'], 'condition': {'trigger_values': [g['trigger']]}, 'orders': [leg]}

    def orders(self):
        return list(self.korders.values())

    def _order(self, c, side):
        kid = f'K{next(self.ids)}'
        self.korders[kid] = {'order_id': kid, 'status': 'OPEN', 'filled_quantity': 0, 'average_price': 0,
                             'side': side, 'symbol': c.symbol, 'qty': c.qty}
        return kid

    def place_buy(self, c):
        c.redeem(BUY)
        if self.fail_buy:
            raise RuntimeError('Kite: margin insufficient')
        self.writes.append(('buy', c.symbol, c.qty, c.limit_price))
        return self._order(c, 'BUY')

    def place_sell(self, c):
        c.redeem(SELL)
        self.writes.append(('sell', c.symbol, c.qty))
        return self._order(c, 'SELL')

    def place_gtt(self, c):
        c.redeem(GTT_PLACE)
        gid = next(self.ids)
        self.kgtts[gid] = {'status': 'active', 'trigger': c.trigger_price, 'qty': c.qty, 'symbol': c.symbol}
        self.writes.append(('gtt_place', c.qty, c.trigger_price))
        return gid

    def modify_gtt(self, c):
        c.redeem(GTT_MODIFY)
        self.kgtts[c.gtt_id].update(trigger=c.trigger_price, qty=c.qty)
        self.writes.append(('gtt_modify', c.qty, c.trigger_price))
        return c.gtt_id

    def delete_gtt(self, c):
        c.redeem(GTT_DELETE)
        self.kgtts[c.gtt_id]['status'] = 'deleted'
        self.writes.append(('gtt_delete', c.gtt_id))
        return c.gtt_id

    # test helpers
    def fill(self, kid, qty, avg, status='OPEN'):
        self.korders[kid].update(filled_quantity=qty, average_price=avg, status=status)

    def trigger(self, gid):
        g = self.kgtts[gid]
        kid = f'K{next(self.ids)}'
        g.update(status='triggered', result=kid)
        self.korders[kid] = {'order_id': kid, 'status': 'OPEN', 'filled_quantity': 0, 'average_price': 0}
        return kid


@dataclass
class Prices:
    last: date | None = TODAY
    series: dict = field(default_factory=dict)        # symbol -> [(date, close)]

    def last_date(self):
        return self.last

    def closes(self, symbol, start, end):
        return [c for d, c in self.series.get(symbol, []) if start <= d <= end]


def _world(limits, *, buy_allowed=True, today=TODAY):
    book, kite, prices = Book(), Kite(), Prices()

    def buy(pid, aid):
        p = book.proposals[pid]
        if not buy_allowed:
            return Decision(outcome=BLOCK, detail='G12: future ex-dates unknown'), None
        eid = book.write_event({'stage': 'before_order', 'decision': 'allow'})
        return Decision(outcome=ALLOW, detail='ok'), _mint(action=BUY, symbol=p['symbol'], qty=p['qty'], event_id=eid,
                                                           proposal_id=pid, limit_price=p['limit_price'])

    gate = Gate(buy=buy, sell=lambda req: check_before_sell(req, sources=book, market=kite, limits=limits))
    life = Lifecycle(book, kite, gate, prices, min_step=limits['sell']['trailing_min_step'], today=lambda: today)
    return book, kite, prices, life


def assert_protected(book: Book, kite: Kite):
    """The falsifying clause, checked on the state."""
    for lot in book.lots.values():
        mine = [g for g in book.gtts.values() if g['position_id'] == lot['id'] and g['status'] == 'active']
        if lot['status'] == 'open':
            assert len(mine) == 1, f"lot {lot['id']} has {len(mine)} active GTTs"
            k = kite.kgtts[mine[0]['kite_gtt_id']]
            assert k['status'] == 'active' and k['qty'] == lot['qty'], f"lot {lot['id']} Kite GTT {k}"
            assert k['trigger'] == mine[0]['trigger_price']
    for g in book.gtts.values():
        assert g['history'] == sorted(g['history']), f"stop moved down: {g['history']}"
    gtt_and_sell_writes = [w for w in kite.writes if w[0] != 'buy']
    sell_events = [e for e in book.guard_events if e.get('stage') in ('before_gtt', 'before_sell')
                   and e['decision'] == 'allow']
    assert len(sell_events) == len(gtt_and_sell_writes), 'a Kite write without its guardrail event'


def _entry_filled(book, kite, life, qty=10, avg=100.0):
    oid = life.place_entry(7)
    kid = book.orders[oid]['kite_order_id']
    kite.held['INFY'] = qty
    kite.fill(kid, qty, avg, 'COMPLETE')
    life.sync()
    return oid, kid


# ---- entry, fills, protection ---------------------------------------------------------------------------------------

def test_approved_proposal_buys_and_a_fill_is_protected(limits):
    book, kite, _, life = _world(limits)
    oid = life.place_entry(7)
    assert kite.writes == [('buy', 'INFY', 10, 100.0)] and book.orders[oid]['status'] == 'OPEN'
    kid = book.orders[oid]['kite_order_id']
    kite.held['INFY'] = 10
    kite.fill(kid, 10, 99.5, 'COMPLETE')
    life.sync()
    [lot] = book.lots.values()
    assert (lot['qty'], lot['entry_price'], lot['entry_date']) == (10, 99.5, TODAY)
    assert kite.writes[-1] == ('gtt_place', 10, 91.5)              # 99.5 x 0.92 = 91.54 -> 91.5
    assert len(book.charges) == 1                                  # buy charges, estimate
    assert_protected(book, kite)
    with pytest.raises(ValueError):
        life.place_entry(7)                                        # one entry per proposal


def test_partial_fill_becomes_the_lot_and_grows(limits):
    book, kite, _, life = _world(limits)
    oid = life.place_entry(7)
    kid = book.orders[oid]['kite_order_id']
    kite.held['INFY'] = 4
    kite.fill(kid, 4, 100.0)
    life.sync()
    [lot] = book.lots.values()
    assert lot['qty'] == 4 and kite.writes[-1] == ('gtt_place', 4, 92.0)
    assert_protected(book, kite)
    life.sync()                                                    # nothing new: no Kite write
    n = len(kite.writes)
    life.sync()
    assert len(kite.writes) == n
    kite.held['INFY'] = 10
    kite.fill(kid, 10, 100.6, 'COMPLETE')
    life.sync()
    assert lot['qty'] == 10 and kite.writes[-1] == ('gtt_modify', 10, 92.5)  # 100.6 x 0.92 = 92.55 -> 92.5, up only
    assert len([g for g in book.gtts.values() if g['status'] == 'active']) == 1
    assert_protected(book, kite)


def test_cancelled_remainder_keeps_the_filled_lot(limits):
    book, kite, _, life = _world(limits)
    oid = life.place_entry(7)
    kid = book.orders[oid]['kite_order_id']
    kite.held['INFY'] = 3
    kite.fill(kid, 3, 100.0, 'CANCELLED')                          # Kite cancels the unfilled part at day end
    life.sync()
    [lot] = book.lots.values()
    assert lot['qty'] == 3 and book.orders[oid]['status'] == 'CANCELLED' and len(book.charges) == 1
    assert_protected(book, kite)


def test_blocked_buy_writes_no_order(limits):
    book, kite, _, life = _world(limits, buy_allowed=False)
    assert life.place_entry(7) is None
    assert book.orders == {} and kite.writes == []


def test_send_failure_is_recorded_not_retried(limits):
    book, kite, _, life = _world(limits)
    kite.fail_buy = True
    oid = life.place_entry(7)
    assert book.orders[oid]['status'] == SEND_FAILED and book.pending_orders() == []


# ---- trailing -------------------------------------------------------------------------------------------------------

def test_trailing_raises_only_and_skips_a_stale_panel(limits):
    book, kite, prices, life = _world(limits)
    _entry_filled(book, kite, life)
    [lot] = book.lots.values()
    kite.prices['INFY'] = 130.0
    prices.series['INFY'] = [(TODAY, 110.0)]
    life.trail(TODAY)
    assert kite.writes[-1] == ('gtt_modify', 10, 101.2)
    prices.series['INFY'] = [(TODAY, 110.0), (date(2026, 11, 12), 104.0)]
    prices.last = date(2026, 11, 12)
    n = len(kite.writes)
    life.trail(date(2026, 11, 12))
    assert len(kite.writes) == n                                   # the high still rules; never down
    r = life.trail(date(2026, 11, 13))                             # panel not updated for the 13th
    assert len(kite.writes) == n and 'trailing skipped' in r.alerts[0]
    assert_protected(book, kite)


def test_trailing_starts_after_a_corporate_action(limits):
    book, kite, prices, life = _world(limits)
    _entry_filled(book, kite, life)
    [lot] = book.lots.values()
    prices.series['INFY'] = [(date(2026, 11, 11), 300.0), (date(2026, 11, 20), 105.0)]  # pre-split close
    prices.last = date(2026, 11, 20)
    book.adjustments[lot['id']] = date(2026, 11, 18)
    kite.prices['INFY'] = 106.0
    life.trail(date(2026, 11, 20))
    # only closes from the adjustment on count: 105 x 0.92 = 96.6 (the raw pre-split 300 would give 276, above the market)
    assert kite.writes[-1] == ('gtt_modify', 10, 96.6)
    assert_protected(book, kite)


# ---- exits ----------------------------------------------------------------------------------------------------------

def test_gtt_trigger_closes_the_lot_as_stop(limits):
    book, kite, _, life = _world(limits)
    _entry_filled(book, kite, life)
    [lot] = book.lots.values()
    [g] = book.gtts.values()
    kid = kite.trigger(g['kite_gtt_id'])
    life.sync()
    kite.fill(kid, 10, 91.0, 'COMPLETE')
    kite.held['INFY'] = 0
    life.sync()
    assert lot['status'] == 'closed' and lot['exit_reason'] == 'stop' and lot['pnl'] < -80
    assert book.gtts[g['id']]['status'] == 'triggered'
    assert_protected(book, kite)


def test_raised_stop_closes_as_trail(limits):
    book, kite, prices, life = _world(limits)
    _entry_filled(book, kite, life)
    [lot] = book.lots.values()
    kite.prices['INFY'] = 125.0
    prices.series['INFY'] = [(TODAY, 120.0)]
    life.trail(TODAY)
    [g] = book.gtts.values()
    kid = kite.trigger(g['kite_gtt_id'])
    life.sync()
    kite.fill(kid, 10, 110.0, 'COMPLETE')
    life.sync()
    assert lot['exit_reason'] == 'trail' and lot['pnl'] > 0


def test_partly_filled_stop_sell_keeps_the_lot_open_and_alerts(limits):
    book, kite, _, life = _world(limits)
    _entry_filled(book, kite, life)
    [lot] = book.lots.values()
    [g] = book.gtts.values()
    kid = kite.trigger(g['kite_gtt_id'])
    life.sync()
    kite.fill(kid, 6, 91.0, 'CANCELLED')
    r = life.sync()
    assert lot['status'] == 'open' and any('backstop' in a for a in r.alerts)


def test_time_exit_sells_then_deletes_the_gtt(limits):
    book, kite, _, life = _world(limits, today=date(2025, 11, 10))
    _entry_filled(book, kite, life)
    [lot] = book.lots.values()
    life.today = lambda: date(2026, 11, 10)
    life.time_exits()                                               # exactly 12 months: not yet
    assert kite.writes[-1] == ('gtt_place', 10, 92.0)
    life.today = lambda: date(2026, 11, 11)                         # the first day after 12 months
    life.time_exits()
    assert kite.writes[-1] == ('sell', 'INFY', 10)
    life.time_exits()                                               # a live exit order: not sold twice
    assert [w[0] for w in kite.writes].count('sell') == 1
    sell = next(o for o in book.orders.values() if o['reason'] == 'exit')
    kite.fill(sell['kite_order_id'], 10, 140.0, 'COMPLETE')
    kite.held['INFY'] = 0
    life.sync()
    assert lot['status'] == 'closed' and lot['exit_reason'] == 'time'
    assert kite.writes[-1][0] == 'gtt_delete'
    assert all(g['status'] == 'deleted' for g in book.gtts.values())
    assert_protected(book, kite)


def test_disabled_gtt_alerts_unprotected(limits):
    book, kite, _, life = _world(limits)
    _entry_filled(book, kite, life)
    [g] = book.gtts.values()
    kite.kgtts[g['kite_gtt_id']]['status'] = 'disabled'           # e.g. after a split
    r = life.sync()
    assert any('unprotected' in a for a in r.alerts)
