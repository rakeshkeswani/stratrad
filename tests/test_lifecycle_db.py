# -*- coding: utf-8 -*-
# tests/test_lifecycle_db.py
"""ST-009 piece 3 on the DEV database: DbStore's SQL through a whole lot life, inside one transaction rolled back.

Opt-in (needs the dev Postgres): set STRATRAD_DB_TESTS=1. Never run against prod.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

from execution.guardrail_service import check_before_sell
from execution.lifecycle import DbStore, Gate, Lifecycle
from tests.test_lifecycle import CONFIG, TODAY, Kite, Prices, _mint, BUY, Decision, ALLOW, CONFIG_PATH
from execution.guardrails import load_limits

pytestmark = pytest.mark.skipif(os.getenv('STRATRAD_DB_TESTS') != '1', reason='dev database test: STRATRAD_DB_TESTS=1')


class DbGuardSources:
    """The sell-side guardrail Sources over the same uncommitted transaction (events kept in memory)."""

    def __init__(self, store: DbStore):
        self.store, self.events = store, []

    def lot(self, pid):
        r = self.store.lot(pid)
        return None if r is None else {'status': r['status'], 'strategy': r['strategy'], 'symbol': r['symbol'],
                                       'qty': r['qty']}

    def active_gtt(self, pid):
        g = self.store.active_gtt_row(pid)
        return None if g is None else {'kite_gtt_id': g['kite_gtt_id'], 'trigger_price': float(g['trigger_price'])}

    def gtt_is_a_lots(self, kite_id):
        return self.store._one('SELECT 1 AS x FROM gtt_orders WHERE kite_gtt_id = %s', (kite_id,)) is not None

    def write_event(self, row):
        self.events.append(row)
        return len(self.events)


@pytest.fixture
def conn():
    from psycopg.types.json import Jsonb
    from core.db import connect
    c = connect('app')
    try:
        with c.cursor() as cur:
            cur.execute("INSERT INTO strategies (name, version, config, status) VALUES ('zz_test_lifecycle', 999, %s, "
                        "'draft') RETURNING id", (Jsonb(CONFIG),))
            sid = cur.fetchone()[0]
            cur.execute("INSERT INTO signals (run_date, strategy_id, symbol, sector) VALUES (%s, %s, 'INFY', 'IT') "
                        'RETURNING id', (TODAY, sid))
            sig = cur.fetchone()[0]
            cur.execute("INSERT INTO proposals (signal_id, symbol, sector, qty, limit_price, stop_price, amount, status, "
                        "decided_at) VALUES (%s, 'INFY', 'IT', 10, 100, 92, 1000, 'approved', now()) RETURNING id",
                        (sig,))
            c.info_pid = cur.fetchone()[0]
        yield c
    finally:
        c.rollback()
        c.close()


def test_a_lot_life_on_the_dev_database(conn):
    limits = load_limits(CONFIG_PATH)
    store = DbStore(conn, commit=False)
    guard = DbGuardSources(store)
    kite, prices = Kite(), Prices()
    pid = conn.info_pid

    def buy(p, a):
        row = store.proposal(p)
        return Decision(outcome=ALLOW, detail='ok'), _mint(action=BUY, symbol=row['symbol'], qty=row['qty'],
                                                           event_id=1, proposal_id=p,
                                                           limit_price=float(row['limit_price']))

    gate = Gate(buy=buy, sell=lambda req: check_before_sell(req, sources=guard, market=kite, limits=limits))
    life = Lifecycle(store, kite, gate, prices, min_step=0.005, today=lambda: TODAY)

    oid = life.place_entry(pid)
    kid = store._one('SELECT kite_order_id FROM orders WHERE id = %s', (oid,))['kite_order_id']
    kite.held['INFY'] = 4
    kite.fill(kid, 4, 100.0)
    life.sync()
    lot = store.open_lots()
    lot = [l for l in lot if l['strategy'] == 'zz_test_lifecycle']
    assert len(lot) == 1 and lot[0]['qty'] == 4
    lot_id = lot[0]['id']

    kite.held['INFY'] = 10
    kite.fill(kid, 10, 100.0, 'COMPLETE')
    life.sync()
    assert store.lot(lot_id)['qty'] == 10
    g = store.active_gtt_row(lot_id)
    assert float(g['trigger_price']) == 92.0
    assert store._one('SELECT count(*) AS n FROM order_charges WHERE order_id = %s', (oid,))['n'] == 1

    kite.prices['INFY'] = 130.0
    prices.series['INFY'] = [(TODAY, 120.0)]
    life.trail(TODAY)
    assert float(store.active_gtt_row(lot_id)['trigger_price']) == 110.4

    sell_kid = kite.trigger(g['kite_gtt_id'])
    life.sync()
    kite.fill(sell_kid, 10, 109.0, 'COMPLETE')
    life.sync()
    closed = store.lot(lot_id)
    assert closed['status'] == 'closed'
    row = store._one('SELECT exit_reason, realised_pnl FROM positions WHERE id = %s', (lot_id,))
    assert row['exit_reason'] == 'trail' and float(row['realised_pnl']) > 0
    n = store._one('SELECT count(*) AS n FROM order_events oe JOIN orders o ON o.id = oe.order_id '
                   'WHERE o.position_id = %s', (lot_id,))['n']
    assert n >= 3
