# -*- coding: utf-8 -*-
# tests/test_sell_guardrails.py
"""ST-009: the sell-side hard checks S01-S06 (pure rules) and check_before_sell (service, fake sources)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from execution.guardrail_service import ClearanceRefused, check_before_sell
from execution.guardrails import (
    ALLOW,
    BLOCK,
    GTT_DELETE,
    GTT_MODIFY,
    GTT_PLACE,
    SELL,
    STAGE_BEFORE_GTT,
    STAGE_BEFORE_SELL,
    LotState,
    SellRequest,
    evaluate_sell,
    load_limits,
)

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'guardrails.toml'


@pytest.fixture(scope='module')
def limits():
    return load_limits(CONFIG_PATH)


LOT = LotState(exists=True, status='open', strategy='baseline_momentum', symbol='INFY', qty=10, active_trigger=None,
               holding_qty=10, last_price=100.0)


def _rules(d):
    return [f.rule for f in d.findings]


# ---- rules ----------------------------------------------------------------------------------------------------------

def test_place_gtt_on_open_lot_allowed(limits):
    d = evaluate_sell(SellRequest(GTT_PLACE, 'INFY', 10, position_id=1, trigger_price=92.0), LOT, limits)
    assert d.outcome == ALLOW


def test_unknown_action_raises(limits):
    with pytest.raises(ValueError):
        evaluate_sell(SellRequest('buy', 'INFY', 10, position_id=1), LOT, limits)


@pytest.mark.parametrize('lot, rule', [
    (LotState(), 'S01'),                                            # no such lot
    (replace(LOT, strategy='manual'), 'S01'),                       # Rakesh's own holding: never touched
    (replace(LOT, symbol='TCS'), 'S01'),
    (replace(LOT, status='closed'), 'S01'),
])
def test_s01_only_open_stratrad_lots(limits, lot, rule):
    d = evaluate_sell(SellRequest(SELL, 'INFY', 10, position_id=1), lot, limits)
    assert d.outcome == BLOCK and rule in _rules(d)


def test_s02_quantity(limits):
    assert 'S02' in _rules(evaluate_sell(SellRequest(SELL, 'INFY', 11, position_id=1), LOT, limits))
    assert 'S02' in _rules(evaluate_sell(SellRequest(GTT_PLACE, 'INFY', 5, position_id=1, trigger_price=92.0),
                                         LOT, limits))                       # a GTT covers exactly its lot
    assert 'S02' in _rules(evaluate_sell(SellRequest(SELL, 'INFY', 10, position_id=1),
                                         replace(LOT, holding_qty=8), limits))
    assert 'S02' in _rules(evaluate_sell(SellRequest(SELL, 'INFY', 10, position_id=1),
                                         replace(LOT, holding_qty=None), limits))  # unknown holding blocks
    assert evaluate_sell(SellRequest(SELL, 'INFY', 4, position_id=1), LOT, limits).outcome == ALLOW


def test_s03_trigger_below_market_by_kite_gap(limits):
    gap = limits['sell']['kite_min_trigger_gap']
    at_gap = round(100.0 * (1 - gap), 4)
    assert 'S03' in _rules(evaluate_sell(SellRequest(GTT_PLACE, 'INFY', 10, position_id=1, trigger_price=at_gap),
                                         LOT, limits))
    assert 'S03' in _rules(evaluate_sell(SellRequest(GTT_PLACE, 'INFY', 10, position_id=1, trigger_price=92.0),
                                         replace(LOT, last_price=None), limits))


def test_s03_one_active_gtt_and_stops_only_rise(limits):
    with_gtt = replace(LOT, active_trigger=92.0)
    assert 'S03' in _rules(evaluate_sell(SellRequest(GTT_PLACE, 'INFY', 10, position_id=1, trigger_price=93.0),
                                         with_gtt, limits))
    assert 'S03' in _rules(evaluate_sell(SellRequest(GTT_MODIFY, 'INFY', 10, position_id=1, trigger_price=91.0,
                                                     gtt_id=7), with_gtt, limits))
    assert 'S03' in _rules(evaluate_sell(SellRequest(GTT_MODIFY, 'INFY', 10, position_id=1, trigger_price=94.0,
                                                     gtt_id=7), LOT, limits))  # nothing to modify
    assert evaluate_sell(SellRequest(GTT_MODIFY, 'INFY', 10, position_id=1, trigger_price=94.0, gtt_id=7),
                         with_gtt, limits).outcome == ALLOW


def test_s04_shape(limits):
    assert 'S04' in _rules(evaluate_sell(SellRequest(SELL, 'INFY', 10, position_id=1, product='MIS'), LOT, limits))
    assert 'S04' in _rules(evaluate_sell(SellRequest(SELL, 'INFY', 10, position_id=1, order_type='LIMIT'),
                                         LOT, limits))


def test_s05_delete_never_strips_an_open_lot(limits):
    active = replace(LOT, gtt_status='active', active_trigger=92.0)
    assert 'S05' in _rules(evaluate_sell(SellRequest(GTT_DELETE, 'INFY', 10, position_id=1, gtt_id=7), active, limits))
    assert 'S05' in _rules(evaluate_sell(SellRequest(GTT_DELETE, 'INFY', 10, position_id=1, gtt_id=7),
                                         replace(LOT, status='closed'), limits))      # status unknown
    closed = replace(LOT, status='closed', gtt_status='active')
    assert evaluate_sell(SellRequest(GTT_DELETE, 'INFY', 10, position_id=1, gtt_id=7), closed, limits).outcome == ALLOW


PROBE_LOT = LotState(holding_qty=3, last_price=1000.0, gtt_belongs_to_lot=False)


def test_s06_probe(limits):
    ok = SellRequest(GTT_PLACE, 'RELIANCE', 1, trigger_price=400.0, probe=True)
    assert evaluate_sell(ok, PROBE_LOT, limits).outcome == ALLOW
    assert 'S06' in _rules(evaluate_sell(replace(ok, qty=2), PROBE_LOT, limits))
    assert 'S06' in _rules(evaluate_sell(replace(ok, trigger_price=600.0), PROBE_LOT, limits))
    assert 'S06' in _rules(evaluate_sell(ok, replace(PROBE_LOT, holding_qty=0), limits))
    assert 'S06' in _rules(evaluate_sell(replace(ok, action=SELL), PROBE_LOT, limits))
    delete = SellRequest(GTT_DELETE, 'RELIANCE', 1, gtt_id=9, probe=True)
    assert evaluate_sell(delete, PROBE_LOT, limits).outcome == ALLOW
    assert 'S06' in _rules(evaluate_sell(delete, replace(PROBE_LOT, gtt_belongs_to_lot=True), limits))
    assert 'S06' in _rules(evaluate_sell(delete, replace(PROBE_LOT, gtt_belongs_to_lot=None), limits))


# ---- service --------------------------------------------------------------------------------------------------------

@dataclass
class FakeSources:
    lots: dict = field(default_factory=lambda: {1: {'status': 'open', 'strategy': 'baseline_momentum',
                                                    'symbol': 'INFY', 'qty': 10}})
    gtts: dict = field(default_factory=dict)        # position_id -> {'kite_gtt_id', 'trigger_price'}
    known_gtt_ids: set = field(default_factory=set)
    events: list = field(default_factory=list)

    def lot(self, position_id):
        return self.lots.get(position_id)

    def active_gtt(self, position_id):
        return self.gtts.get(position_id)

    def gtt_is_a_lots(self, kite_gtt_id):
        return kite_gtt_id in self.known_gtt_ids

    def write_event(self, row):
        self.events.append(row)
        return 100 + len(self.events)


@dataclass
class FakeMarket:
    holdings: dict = field(default_factory=lambda: {'INFY': 10, 'RELIANCE': 3})
    prices: dict = field(default_factory=lambda: {'INFY': 100.0, 'RELIANCE': 1000.0})
    statuses: dict = field(default_factory=dict)

    def holding_qty(self, symbol):
        return self.holdings.get(symbol)

    def last_price(self, symbol):
        return self.prices.get(symbol)

    def gtt_status(self, kite_gtt_id):
        return self.statuses.get(kite_gtt_id)


def test_allow_writes_event_then_mints_clearance(limits):
    src = FakeSources()
    d, c = check_before_sell(SellRequest(GTT_PLACE, 'INFY', 10, position_id=1, trigger_price=92.0),
                             sources=src, market=FakeMarket(), limits=limits)
    assert d.outcome == ALLOW and c is not None
    assert len(src.events) == 1 and src.events[0]['stage'] == STAGE_BEFORE_GTT and src.events[0]['decision'] == ALLOW
    assert c.event_id == 101 and c.action == GTT_PLACE and c.qty == 10 and c.trigger_price == 92.0
    assert c.last_price == 100.0 and c.order_type == 'MARKET'
    assert c.market_protection == limits['sell']['market_protection']


def test_block_writes_event_and_no_clearance(limits):
    src = FakeSources()
    d, c = check_before_sell(SellRequest(SELL, 'INFY', 10, position_id=2), sources=src, market=FakeMarket(),
                             limits=limits)
    assert d.outcome == BLOCK and c is None
    assert src.events[0]['stage'] == STAGE_BEFORE_SELL and src.events[0]['decision'] == BLOCK
    assert 'S01' in src.events[0]['rule']


def test_service_reads_active_gtt_for_the_lot(limits):
    src = FakeSources(gtts={1: {'kite_gtt_id': 7, 'trigger_price': 92.0}}, known_gtt_ids={7})
    d, _ = check_before_sell(SellRequest(GTT_PLACE, 'INFY', 10, position_id=1, trigger_price=93.0),
                             sources=src, market=FakeMarket(), limits=limits)
    assert 'S03' in _rules(d)                       # second GTT on one lot refused
    d, c = check_before_sell(SellRequest(GTT_MODIFY, 'INFY', 10, position_id=1, trigger_price=94.0, gtt_id=7),
                             sources=src, market=FakeMarket(statuses={7: 'active'}), limits=limits)
    assert d.outcome == ALLOW and c.gtt_id == 7


def test_probe_cannot_touch_a_lots_gtt(limits):
    src = FakeSources(known_gtt_ids={7})
    d, c = check_before_sell(SellRequest(GTT_DELETE, 'INFY', 1, gtt_id=7, probe=True), sources=src,
                             market=FakeMarket(), limits=limits)
    assert d.outcome == BLOCK and c is None


def test_clearance_is_one_shot_and_action_bound(limits):
    d, c = check_before_sell(SellRequest(SELL, 'INFY', 10, position_id=1), sources=FakeSources(),
                             market=FakeMarket(), limits=limits)
    with pytest.raises(ClearanceRefused):
        c.redeem(GTT_PLACE)
    c.redeem(SELL)
    with pytest.raises(ClearanceRefused):
        c.redeem(SELL)
