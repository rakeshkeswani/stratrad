# -*- coding: utf-8 -*-
# tests/test_reconcile.py
"""ST-009 piece 4: the evening check -- every planted difference reported, `manual` lots, splits, the snapshot equal
to the backtest's Test 1 function on the same lots."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from backtest.costs import buy_charges, sell_charges
from backtest.run import pnl_at_cost
from execution.lifecycle import Report
from execution.reconcile import (
    EveningCheck,
    apply_adjustment,
    build_snapshot,
    gtt_differences,
    holding_differences,
    kite_totals,
    manual_targets,
    rolling_result,
    tier_for,
)
from tests.test_lifecycle import TODAY, Book, Kite, Prices, _entry_filled, _world, assert_protected

TIERS = {'alert': 0.10, 'no_new_buys': 0.15, 'halt': 0.20}


# ---- the backtest's Test 1, same lots -------------------------------------------------------------------------------

def _lots():
    d = date(2025, 1, 1)
    return [
        {'symbol': 'A', 'strategy': 's', 'sector': 'IT', 'entry_date': d, 'entry_price': 100.0, 'qty': 50,
         'exit_date': d + timedelta(days=40), 'exit_price': 90.0},
        {'symbol': 'B', 'strategy': 's', 'sector': 'Bank', 'entry_date': d + timedelta(days=10), 'entry_price': 200.0,
         'qty': 20, 'exit_date': None, 'exit_price': None},
        {'symbol': 'C', 'strategy': 's', 'sector': 'IT', 'entry_date': d + timedelta(days=20), 'entry_price': 50.0,
         'qty': 100, 'exit_date': d + timedelta(days=400), 'exit_price': 70.0},
        {'symbol': 'D', 'strategy': 's', 'sector': None, 'entry_date': d + timedelta(days=300), 'entry_price': 30.0,
         'qty': 100, 'exit_date': None, 'exit_price': None},
    ]


def test_snapshot_result_equals_the_backtest_test1_function():
    """Done-when: the live 12-month result is backtest/run.py pnl_at_cost on the same lots, day by day."""
    lots = _lots()
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(0, 520, 7)]
    rng = np.random.default_rng(7)
    marks = {d: {s: float(p * (1 + rng.normal(0, 0.1))) for s, p in (('A', 100), ('B', 200), ('C', 50), ('D', 30))}
             for d in days}
    daily, rows = [], []
    for d in days:
        open_ = [l for l in lots if l['entry_date'] <= d and (l['exit_date'] is None or l['exit_date'] > d)]
        daily.append({'date': d, 'holdings': sum(marks[d][l['symbol']] * l['qty'] for l in open_),
                      'invested_cost': sum(l['entry_price'] * l['qty'] for l in open_)})
    for l in lots:
        bv = l['entry_price'] * l['qty']
        sv = l['exit_price'] * l['qty'] if l['exit_date'] else np.nan
        rows.append({'entry_date': l['entry_date'], 'exit_date': l['exit_date'], 'buy_value': bv,
                     'buy_charges': buy_charges(bv)['total'], 'sell_value': sv,
                     'sell_charges': sell_charges(sv, dp=True)['total'] if l['exit_date'] else np.nan})
    expected = pnl_at_cost(pd.DataFrame(daily), pd.DataFrame(rows), 365)
    for d, want in zip(days, expected['pnl']):
        assert rolling_result(lots, marks[d], d) == pytest.approx(want, abs=0.01), d


def test_tier_thresholds_on_the_stake():
    assert tier_for(-9_999, 100_000, TIERS) == 'ok'
    assert tier_for(-10_000, 100_000, TIERS) == 'alert'
    assert tier_for(-15_000, 100_000, TIERS) == 'no_buys'
    assert tier_for(-20_000, 100_000, TIERS) == 'halt'


def test_empty_book_snapshot_is_ok():
    row = build_snapshot(TODAY, [], {}, 'panel', 100_000, TIERS)
    assert row['tier'] == 'ok' and row['invested_at_cost'] == 0 and row['price_source'] == 'none'


def test_snapshot_rows():
    lots = _lots()
    on = date(2025, 11, 1)
    row = build_snapshot(on, lots, {'B': 220.0, 'C': 60.0, 'D': 33.0}, 'panel', 100_000, TIERS)
    assert row['invested_at_cost'] == 200 * 20 + 50 * 100 + 30 * 100
    assert row['market_value'] == 220 * 20 + 60 * 100 + 33 * 100
    assert row['exposure_by_sector'] == {'Bank': 4000.0, 'IT': 5000.0, 'unknown': 3000.0}
    assert row['realised_pnl'] < -500                              # lot A sold at a loss
    with pytest.raises(KeyError):
        build_snapshot(on, lots, {'B': 220.0}, 'panel', 100_000, TIERS)   # an open lot without a price: no row


# ---- reconciliation (pure) ------------------------------------------------------------------------------------------

LOT = {'id': 1, 'symbol': 'INFY', 'qty': 10}
OURS = [{'position_id': 1, 'kite_gtt_id': 55, 'trigger_price': 92.0}]


def _kgtt(gid=55, status='active', qty=10, trigger=92.0, symbol='INFY'):
    return {'id': gid, 'status': status, 'condition': {'tradingsymbol': symbol, 'trigger_values': [trigger]},
            'orders': [{'quantity': qty}]}


def _whats(diffs):
    return [d.what for d in diffs]


def test_clean_book_has_no_differences():
    assert gtt_differences([LOT], OURS, {55}, [_kgtt()]) == []
    assert holding_differences([LOT], {'INFY': 10}) == []


@pytest.mark.parametrize('ours, kite, what', [
    ([], [_kgtt()], 'missing_gtt'),
    (OURS, [_kgtt(qty=5)], 'gtt_qty'),
    (OURS, [_kgtt(trigger=90.0)], 'gtt_trigger'),
    (OURS, [_kgtt(status='disabled')], 'gtt_inactive'),
    (OURS, [], 'gtt_inactive'),
    (OURS, [_kgtt(), _kgtt(gid=77, symbol='TCS')], 'gtt_not_ours'),
    (OURS + [{'position_id': 1, 'kite_gtt_id': 56, 'trigger_price': 92.0}], [_kgtt(), _kgtt(gid=56)], 'extra_gtt_rows'),
])
def test_each_planted_gtt_difference_is_reported(ours, kite, what):
    ids = {g['kite_gtt_id'] for g in ours} | {55}
    assert what in _whats(gtt_differences([LOT], ours, ids, kite))


def test_gtt_cap_and_holding_short():
    many = [_kgtt(gid=i) for i in range(1000, 1450)]
    assert 'gtt_cap' in _whats(gtt_differences([], [], {g['id'] for g in many}, many))
    assert _whats(holding_differences([LOT], {'INFY': 8})) == ['holding_short']


def test_manual_remainder_per_stock():
    totals = kite_totals([{'tradingsymbol': 'INFY', 'quantity': 25, 't1_quantity': 0},
                          {'tradingsymbol': 'AIIL', 'quantity': 1315, 't1_quantity': 0},
                          {'tradingsymbol': 'X', 'quantity': 0, 't1_quantity': 0, 'average_price': 0}], [])
    prices = {'INFY': (95.0, 100.0), 'AIIL': (0.0, 591.8)}
    t = manual_targets(totals, prices, [LOT])
    assert t == {'INFY': (15, 95.0), 'AIIL': (1315, 591.8)}          # no average price: the last price


# ---- the evening run ------------------------------------------------------------------------------------------------

@dataclass
class ReconBook(Book):
    recons: list = field(default_factory=list)
    manual: dict = field(default_factory=dict)
    snapshots: dict = field(default_factory=dict)
    adjustments_rows: list = field(default_factory=list)

    def last_gtt_status(self, lid):
        rows = [g for g in self.gtts.values() if g['position_id'] == lid]
        return rows[-1]['status'] if rows else None

    def all_kite_gtt_ids(self):
        return {g['kite_gtt_id'] for g in self.gtts.values()}

    def add_reconciliation(self, on, d):
        self.recons.append(d.what)

    def manual_lots(self):
        return [dict(v, symbol=k) for k, v in self.manual.items() if v['status'] == 'open']

    def add_manual_lot(self, symbol, on, price, qty):
        self.manual[symbol] = {'id': 900 + len(self.manual), 'qty': qty, 'entry_price': price, 'status': 'open'}

    def resize_lot(self, lid, qty, price):
        for m in self.manual.values():
            if m['id'] == lid:
                m.update(qty=qty, entry_price=price)
                return
        super().resize_lot(lid, qty, price)

    def close_manual_lot(self, lid, on, price, qty):
        for m in self.manual.values():
            if m['id'] == lid:
                m['status'] = 'closed'

    def stratrad_lots_until(self, on):
        out = []
        for l in self.lots.values():
            v = self._lot_view(l)
            out.append(dict(v, sector='IT', exit_date=l.get('exit_date'), exit_price=l.get('exit_price')))
        return out

    def upsert_snapshot(self, row):
        self.snapshots[row['snapshot_date']] = row

    def add_adjustment(self, row):
        self.adjustments_rows.append(row)
        self.adjustments[row['position_id']] = row['ex_date']


@dataclass
class ReconKite(Kite):
    extra_holdings: list = field(default_factory=list)

    def holdings(self):
        rows = [{'tradingsymbol': s, 'exchange': 'NSE', 'quantity': q, 't1_quantity': 0, 'average_price': 98.0,
                 'last_price': self.prices.get(s, 0)} for s, q in self.held.items()]
        return rows + self.extra_holdings

    def positions(self):
        return {'day': [], 'net': []}

    def gtts(self):
        return [{'id': gid, 'status': g['status'], 'condition': {'tradingsymbol': g['symbol'],
                                                               'trigger_values': [g['trigger']]},
                 'orders': [{'quantity': g['qty']}]} for gid, g in self.kgtts.items()]


class Calendar:
    def __init__(self, trading=True):
        self.trading = trading

    def is_trading_day(self, d):
        return self.trading


class PanelPrices(Prices):
    def close_on(self, symbol, on):
        return dict(self.series.get(symbol, [])).get(on)


def _evening(limits, trading=True):
    from execution.lifecycle import Gate, Lifecycle
    from execution.guardrail_service import BUY, _mint, check_before_sell
    from execution.guardrails import ALLOW, Decision
    book, kite, prices = ReconBook(), ReconKite(), PanelPrices()

    def buy(pid, aid):
        p = book.proposals[pid]
        eid = book.write_event({'stage': 'before_order', 'decision': 'allow'})
        return Decision(outcome=ALLOW, detail='ok'), _mint(action=BUY, symbol=p['symbol'], qty=p['qty'], event_id=eid,
                                                           proposal_id=pid, limit_price=p['limit_price'])

    gate = Gate(buy=buy, sell=lambda req: check_before_sell(req, sources=book, market=kite, limits=limits))
    life = Lifecycle(book, kite, gate, prices, min_step=0.005, today=lambda: TODAY)
    check = EveningCheck(book, kite, life, prices, Calendar(trading), limits)
    return book, kite, prices, life, check


@pytest.fixture(scope='module')
def limits():
    from execution.guardrails import load_limits
    from tests.test_lifecycle import CONFIG_PATH
    return load_limits(CONFIG_PATH)


def test_clean_evening_writes_snapshot_and_manual_lots(limits):
    book, kite, prices, life, check = _evening(limits)
    _entry_filled(book, kite, life)
    kite.held['INFY'] = 25                                         # 10 stratrad + 15 Rakesh's
    kite.held['AIIL'] = 1315
    kite.prices['AIIL'] = 591.8
    prices.series['INFY'] = [(TODAY, 101.0)]
    r = check.run(TODAY)
    assert r.alerts == [] and book.recons == []
    assert {s: m['qty'] for s, m in book.manual.items()} == {'INFY': 15, 'AIIL': 1315}
    snap = book.snapshots[TODAY]
    assert snap['price_source'] == 'panel' and snap['market_value'] == 1010.0 and snap['tier'] == 'ok'
    assert kite.writes.count(('gtt_place', 10, 92.0)) == 1          # nothing re-placed on a clean book
    kite.held['AIIL'] = 0                                          # Rakesh sold AIIL
    check.run(TODAY)
    assert book.manual['AIIL']['status'] == 'closed'


def test_missing_gtt_is_re_placed_at_the_last_stop(limits):
    book, kite, prices, life, check = _evening(limits)
    _entry_filled(book, kite, life)
    [g] = book.gtts.values()
    kite.prices['INFY'] = 130.0
    prices.series['INFY'] = [(TODAY, 120.0)]
    life.trail(TODAY)                                              # stop 110.4
    kite.kgtts[g['kite_gtt_id']]['status'] = 'cancelled'           # someone deleted it in Kite
    r = check.run(TODAY)
    assert kite.writes[-1] == ('gtt_place', 10, 110.4)             # never back to the initial 92
    assert_protected(book, kite)
    assert any('unprotected' in a for a in r.alerts)               # the cancellation itself is still reported


def test_split_disables_gtt_alerts_then_adjust_lot_re_protects(limits):
    book, kite, prices, life, check = _evening(limits)
    _entry_filled(book, kite, life)
    [lot] = book.lots.values()
    [g] = book.gtts.values()
    kite.kgtts[g['kite_gtt_id']]['status'] = 'disabled'            # Kite, on a 1:2 split
    kite.held['INFY'] = 20
    kite.prices['INFY'] = 50.0
    prices.series['INFY'] = [(TODAY, 50.0)]
    r = check.run(TODAY)
    assert any('adjust_lot' in a for a in r.alerts)
    assert not any(w[0] == 'gtt_place' and w[2] != 92.0 for w in kite.writes)  # not re-placed at a pre-split stop
    rep = Report()
    assert apply_adjustment(book, life, lot['id'], 2, TODAY, 'split', rep)
    assert lot['qty'] == 20 and lot['entry_price'] == 50.0
    assert kite.writes[-1] == ('gtt_place', 20, 46.0)
    assert book.adjustments_rows[0]['stop_after'] == 46.0
    assert_protected(book, kite)
    r = check.run(TODAY)
    assert not any('gtt/' in a for a in r.alerts)


def test_adjust_lot_refuses_fractions_and_active_gtts(limits):
    book, kite, prices, life, check = _evening(limits)
    _entry_filled(book, kite, life)
    [lot] = book.lots.values()
    assert not apply_adjustment(book, life, lot['id'], 2, TODAY, 'split', Report())          # GTT still active
    [g] = book.gtts.values()
    book.gtts[g['id']]['status'] = 'disabled'
    assert not apply_adjustment(book, life, lot['id'], 1.25, TODAY, 'bonus', Report())       # 12.5 shares
    assert lot['qty'] == 10


def test_planted_differences_in_a_full_run(limits):
    book, kite, prices, life, check = _evening(limits)
    _entry_filled(book, kite, life)
    [g] = book.gtts.values()
    kite.kgtts[g['kite_gtt_id']]['qty'] = 7                        # GTT quantity != lot
    kite.kgtts[999] = {'status': 'active', 'trigger': 10.0, 'qty': 1, 'symbol': 'TCS'}   # not stratrad's
    kite.held['INFY'] = 6                                          # stratrad's shares sold outside stratrad
    prices.series['INFY'] = [(TODAY, 100.0)]
    check.run(TODAY)
    assert {'gtt_qty', 'gtt_not_ours', 'holding_short'} <= set(book.recons)
    assert 'INFY' not in book.manual                               # negative remainder: no manual lot


def test_stale_panel_uses_kite_prices_and_says_so(limits):
    book, kite, prices, life, check = _evening(limits)
    _entry_filled(book, kite, life)
    prices.last = TODAY - timedelta(days=3)
    check.run(TODAY)
    assert book.snapshots[TODAY]['price_source'] == 'kite'


def test_no_snapshot_on_a_holiday(limits):
    book, kite, prices, life, check = _evening(limits, trading=False)
    check.run(TODAY)
    assert book.snapshots == {}


def test_evening_check_never_writes_to_kite_for_manual_lots(limits):
    book, kite, prices, life, check = _evening(limits)
    kite.held['AIIL'] = 1315
    kite.prices['AIIL'] = 591.8
    check.run(TODAY)
    assert kite.writes == [] and book.manual['AIIL']['qty'] == 1315
