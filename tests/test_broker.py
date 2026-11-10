# -*- coding: utf-8 -*-
# tests/test_broker.py
"""ST-009: the broker builds Kite writes from clearances only, through the proxy, and never shows the token."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from core.kite import Session
from execution.broker import Broker, ProxyMissing
from execution.guardrail_service import BUY, Clearance, ClearanceRefused, _mint
from execution.guardrails import GTT_DELETE, GTT_MODIFY, GTT_PLACE, SELL

TOKEN = 'secret-token-xyz'
PROXY = 'http://proxy:8888'


class Resp:
    def __init__(self, data, status=200):
        self.status_code = status
        self._body = {'status': 'success', 'data': data} if status == 200 else {'status': 'error',
                                                                                 'error_type': 'InputException',
                                                                                 'message': 'bad'}

    def json(self):
        return self._body


class Recorder:
    """Stands in for requests.request; answers by path."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        path = url.split('api.kite.trade', 1)[1]
        for prefix, data in self.answers.items():
            if path.startswith(prefix):
                return data if isinstance(data, Resp) else Resp(data)
        return Resp({'order_id': 'O1', 'trigger_id': 55})


def _broker(rec, proxy=PROXY):
    s = Session(user_id='AB1234', access_token=TOKEN, issued_at=datetime.now(timezone.utc))
    return Broker(s, api_key='key', proxy=proxy, request=rec)


def _gtt_clearance(**kw):
    base = dict(action=GTT_PLACE, symbol='INFY', qty=10, event_id=7, position_id=1, trigger_price=92.0,
                last_price=100.0, order_type='MARKET', market_protection=-1)
    base.update(kw)
    return _mint(**base)


def test_repr_hides_token():
    b = _broker(Recorder())
    assert TOKEN not in repr(b) and 'AB1234' not in repr(b)


def test_place_gtt_goes_through_proxy_with_market_sell_leg():
    rec = Recorder()
    assert _broker(rec).place_gtt(_gtt_clearance()) == 55
    method, url, kw = rec.calls[0]
    assert method == 'POST' and url.endswith('/gtt/triggers') and kw['proxies'] == {'https': PROXY}
    form = kw['data']
    assert form['type'] == 'single'
    cond = json.loads(form['condition'])
    assert cond == {'exchange': 'NSE', 'tradingsymbol': 'INFY', 'trigger_values': [92.0], 'last_price': 100.0}
    [leg] = json.loads(form['orders'])
    assert leg['transaction_type'] == 'SELL' and leg['order_type'] == 'MARKET' and leg['quantity'] == 10
    assert leg['product'] == 'CNC' and leg['market_protection'] == -1


def test_modify_and_delete_use_the_gtt_id():
    rec = Recorder()
    b = _broker(rec)
    b.modify_gtt(_gtt_clearance(action=GTT_MODIFY, gtt_id=55, trigger_price=95.0))
    b.delete_gtt(_mint(action=GTT_DELETE, symbol='INFY', qty=10, event_id=8, gtt_id=55))
    assert [(m, u.rsplit('/', 1)[1]) for m, u, _ in rec.calls] == [('PUT', '55'), ('DELETE', '55')]


def test_buy_is_limit_and_sell_is_market_both_tagged():
    rec = Recorder()
    b = _broker(rec)
    b.place_buy(_mint(action=BUY, symbol='INFY', qty=10, event_id=3, limit_price=101.5, order_type='LIMIT'))
    b.place_sell(_mint(action=SELL, symbol='INFY', qty=10, event_id=4, order_type='MARKET', market_protection=-1))
    buy, sell = rec.calls[0][2]['data'], rec.calls[1][2]['data']
    assert buy['order_type'] == 'LIMIT' and buy['price'] == 101.5 and buy['transaction_type'] == 'BUY'
    assert sell['order_type'] == 'MARKET' and sell['market_protection'] == -1 and 'price' not in sell
    assert buy['tag'] == 'st3' and sell['tag'] == 'st4'
    assert all(u.endswith('/orders/regular') for _, u, _ in rec.calls)


def test_forged_clearance_refused_and_nothing_sent():
    rec = Recorder()
    forged = Clearance(action=GTT_PLACE, symbol='INFY', qty=10, event_id=1, issued_at=datetime.now(timezone.utc),
                       trigger_price=92.0, last_price=100.0)
    with pytest.raises(ClearanceRefused):
        _broker(rec).place_gtt(forged)
    assert rec.calls == []


def test_used_stale_and_wrong_action_refused():
    rec = Recorder()
    b = _broker(rec)
    c = _gtt_clearance()
    b.place_gtt(c)
    with pytest.raises(ClearanceRefused):
        b.place_gtt(c)                                      # one-shot
    with pytest.raises(ClearanceRefused):
        b.place_gtt(_gtt_clearance(), now=datetime.now(timezone.utc) + timedelta(seconds=61))
    with pytest.raises(ClearanceRefused):
        b.place_sell(_gtt_clearance())                      # a GTT clearance cannot sell
    assert len(rec.calls) == 1


def test_no_proxy_no_write_and_clearance_not_burned():
    rec = Recorder()
    c = _gtt_clearance()
    with pytest.raises(ProxyMissing):
        _broker(rec, proxy='').place_gtt(c)
    assert rec.calls == [] and not c.used


def test_reads_are_direct():
    rec = Recorder({'/quote/ltp': {'NSE:INFY': {'instrument_token': 1, 'last_price': 101.25}}})
    assert _broker(rec).last_price('INFY') == 101.25
    assert 'proxies' not in rec.calls[0][2]


def test_holding_qty_counts_t1_and_todays_cnc_buys():
    rec = Recorder({
        '/portfolio/holdings': [{'tradingsymbol': 'INFY', 'exchange': 'NSE', 'quantity': 5, 't1_quantity': 2},
                                {'tradingsymbol': 'TCS', 'exchange': 'NSE', 'quantity': 9, 't1_quantity': 0}],
        '/portfolio/positions': {'day': [{'tradingsymbol': 'INFY', 'exchange': 'NSE', 'product': 'CNC', 'quantity': 3},
                                         {'tradingsymbol': 'INFY', 'exchange': 'NSE', 'product': 'MIS',
                                          'quantity': 50}], 'net': []},
    })
    assert _broker(rec).holding_qty('INFY') == 10


def test_gtt_trigger_read_from_kite():
    rec = Recorder({'/gtt/triggers/9': {'id': 9, 'status': 'active',
                                        'condition': {'tradingsymbol': 'RELIANCE', 'trigger_values': [450.5]}}})
    b = _broker(rec)
    assert b.gtt_trigger(9) == 450.5 and b.gtt_status(9) == 'active'


def test_unknown_reads_return_none():
    rec = Recorder({'/portfolio/holdings': Resp(None, status=500), '/gtt/triggers/9': Resp(None, status=400),
                    '/quote/ltp': {}})
    b = _broker(rec)
    assert b.holding_qty('INFY') is None and b.gtt_status(9) is None and b.last_price('INFY') is None
    assert b.gtt_trigger(9) is None


def test_kite_error_message_has_no_token():
    rec = Recorder({'/gtt/triggers': Resp(None, status=400)})
    with pytest.raises(Exception) as e:
        _broker(rec).place_gtt(_gtt_clearance())
    assert TOKEN not in str(e.value)
