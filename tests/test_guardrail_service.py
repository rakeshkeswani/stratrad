# -*- coding: utf-8 -*-
# tests/test_guardrail_service.py
"""ST-008: the guardrail service against fake sources -- state gathering, committed money, events, approvals."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pytest

from core.trading_calendar import TradingCalendar
from execution.guardrail_service import (
    APPROVAL_ACTION,
    Commitment,
    Proposal,
    aggregate,
    approval_covers,
    approval_details,
    check_before_order,
    check_before_proposal,
    week_start,
)
from execution.guardrails import ALLOW, ASK, BLOCK, BuyRequest, load_limits

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'guardrails.toml'
ON = date(2026, 11, 4)          # a Wednesday


@pytest.fixture(scope='module')
def limits():
    """Shipped limits. The service sets upcoming_ex_dates_known=False today, so every outcome here includes a G12
    block; tests assert on the findings they are about (all rules run, so each finding is reported)."""
    return load_limits(CONFIG_PATH)


@pytest.fixture
def calendar(tmp_path):
    doc = {'year': 2026, 'holidays': [{'tradingDate': '10-Nov-2026', 'description': 'x'}]}
    (tmp_path / 'nse_cm_holidays_2026.json').write_text(json.dumps(doc), encoding='utf-8')
    return TradingCalendar(tmp_path)


@dataclass
class FakeSources:
    """A healthy book by default: fresh data, nothing committed."""

    commits: list = field(default_factory=list)
    snapshot: tuple | None = (date(2026, 11, 3), 'ok')
    sectors: dict = field(default_factory=lambda: {'INFY': 'IT', 'TCS': 'IT'})
    ca_on: date | None = date(2026, 11, 3)
    panel: tuple | None = (date(2026, 11, 3), 'EQ')
    universe: tuple | None = (date(2026, 10, 1), {'INFY', 'TCS', 'AAA'})
    proposals: dict = field(default_factory=dict)
    approvals: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    excluded: list = field(default_factory=list)

    def commitments(self, week_start, exclude_proposal_id):
        self.excluded.append(exclude_proposal_id)
        return list(self.commits)

    def latest_snapshot(self):
        return self.snapshot

    def sector_of(self, symbol):
        return self.sectors.get(symbol)

    def corporate_actions_fetched_on(self):
        return self.ca_on

    def panel_latest(self, symbol):
        return self.panel

    def universe_on(self, on):
        return self.universe

    def proposal(self, proposal_id):
        return self.proposals.get(proposal_id)

    def approval(self, approval_id):
        return self.approvals.get(approval_id)

    def write_event(self, row):
        self.events.append(row)
        return len(self.events)


def _req(symbol='INFY', qty=90, price=80.0, stop=72.0, strategy='rakesh_top4'):
    return BuyRequest(symbol=symbol, strategy=strategy, qty=qty, limit_price=price, stop_price=stop)


def _rules(decision):
    return [f.rule for f in decision.findings]


# ---- pure helpers ---------------------------------------------------------------------------------------------------

def test_week_starts_on_monday():
    assert week_start(ON) == date(2026, 11, 2)
    assert week_start(date(2026, 11, 2)) == date(2026, 11, 2)


def test_aggregate_totals():
    cs = [Commitment('INFY', 'rakesh_top4', 'IT', 5000.0, False, 'lot'),
          Commitment('TCS', 'rakesh_top4', 'IT', 3000.0, True, 'order'),
          Commitment('AAA', 'baseline', None, 2000.0, True, 'proposal')]
    t = aggregate(cs)
    assert t['committed_total'] == 10000.0 and t['week_committed'] == 5000.0
    assert t['committed_by_strategy'] == {'rakesh_top4': 8000.0, 'baseline': 2000.0}
    assert t['committed_by_sector'] == {'IT': 8000.0}
    assert t['committed_by_stock']['INFY'] == 5000.0


# ---- before_proposal ------------------------------------------------------------------------------------------------

def test_today_g12_blocks_because_no_future_ex_date_source(limits, calendar):
    """2026-11-06 decision: until stratrad keeps announced ex-dates, every check blocks on G12."""
    src = FakeSources()
    d = check_before_proposal(_req(), sources=src, limits=limits, calendar=calendar, on=ON)
    assert d.outcome == BLOCK and _rules(d) == ['G12']
    assert len(src.events) == 1 and src.events[0]['decision'] == 'block' and src.events[0]['rule'] == 'G12'


def test_event_row_carries_limits_request_state_and_notes(limits, calendar):
    src = FakeSources()
    check_before_proposal(_req(), sources=src, limits=limits, calendar=calendar, on=ON)
    row = src.events[0]
    assert row['stage'] == 'before_proposal' and row['proposal_id'] is None and row['approval_id'] is None
    assert row['limits']['capital']['cap'] == 100000
    ctx = row['context']
    assert ctx['request']['symbol'] == 'INFY' and ctx['request']['sector'] == 'IT' and ctx['request']['amount'] == 7200.0
    assert ctx['state']['panel_age_trading_days'] == 0
    assert ctx['notes']['universe_effective_from'] == '2026-10-01'


def test_write_false_writes_nothing(limits, calendar):
    src = FakeSources()
    check_before_proposal(_req(), sources=src, limits=limits, calendar=calendar, on=ON, write=False)
    assert src.events == []


def test_sector_comes_from_the_source_not_the_caller(limits, calendar):
    src = FakeSources(sectors={})
    request = BuyRequest(symbol='INFY', strategy='rakesh_top4', qty=90, limit_price=80.0, stop_price=72.0,
                         sector='made-up')
    d = check_before_proposal(request, sources=src, limits=limits, calendar=calendar, on=ON)
    assert 'G08' in _rules(d)                      # unknown sector blocks, the caller's label is ignored


def test_stale_inputs_block_and_name_themselves(limits, calendar):
    cases = {
        'panel': FakeSources(panel=(date(2026, 10, 28), 'EQ')),
        'ca': FakeSources(ca_on=None),
        'snapshot': FakeSources(snapshot=(date(2026, 10, 30), 'ok')),
        'universe': FakeSources(universe=(date(2025, 10, 1), {'INFY'})),
    }
    for name, src in cases.items():
        d = check_before_proposal(_req(), sources=src, limits=limits, calendar=calendar, on=ON)
        assert d.outcome == BLOCK and 'G13' in _rules(d), name


def test_uncovered_calendar_year_blocks(limits, tmp_path):
    """Panel ending in 2025 with no 2025 calendar: its age is unknown, so G13 blocks."""
    src = FakeSources(panel=(date(2025, 8, 25), 'EQ'))
    d = check_before_proposal(_req(), sources=src, limits=limits, calendar=TradingCalendar(tmp_path), on=ON)
    assert d.outcome == BLOCK and 'G13' in _rules(d)


def test_outside_universe_and_unknown_series_block(limits, calendar):
    d = check_before_proposal(_req('ZZZ'), sources=FakeSources(sectors={'ZZZ': 'IT'}), limits=limits,
                              calendar=calendar, on=ON)
    assert 'G01' in _rules(d)
    d = check_before_proposal(_req(), sources=FakeSources(panel=(date(2026, 11, 3), None)), limits=limits,
                              calendar=calendar, on=ON)
    assert 'G01' in _rules(d)


def test_committed_money_includes_run_picks(limits, calendar):
    """Kitty Rs 92,000 committed; two earlier picks of the run take it past Rs 1 lakh -> G03 is among the asks."""
    src = FakeSources(commits=[Commitment('AAA', 'rakesh_top4', 'Auto', 92000.0, False, 'lot')])
    picks = [BuyRequest('TCS', 'rakesh_top4', 30, 100.0, 92.0, sector='IT'),
             BuyRequest('AAA', 'rakesh_top4', 30, 100.0, 92.0, sector='Auto')]
    check_before_proposal(_req(), sources=src, run_picks=picks, limits=limits, calendar=calendar, on=ON)
    state = src.events[0]['context']['state']
    assert state['committed_total'] == 98000.0
    assert state['week_committed'] == 6000.0
    finding_rules = {f['rule'] for f in src.events[0]['context']['findings']}
    assert 'G03' in finding_rules


# ---- before_order ---------------------------------------------------------------------------------------------------

def _proposal(status='approved', qty=90, price=80.0):
    return Proposal(id=7, symbol='INFY', strategy='rakesh_top4', sector='IT', qty=qty, limit_price=price,
                    stop_price=72.0, status=status)


def test_before_order_excludes_its_own_proposal(limits, calendar):
    src = FakeSources(proposals={7: _proposal()})
    check_before_order(7, sources=src, limits=limits, calendar=calendar, on=ON)
    assert src.excluded == [7]
    assert src.events[0]['proposal_id'] == 7 and src.events[0]['stage'] == 'before_order'


def test_before_order_refuses_an_unapproved_proposal(limits, calendar):
    src = FakeSources(proposals={7: _proposal(status='pending')})
    d = check_before_order(7, sources=src, limits=limits, calendar=calendar, on=ON)
    assert d.outcome == BLOCK and _rules(d)[0] == 'G00'


def test_before_order_unknown_proposal_raises(limits, calendar):
    with pytest.raises(ValueError):
        check_before_order(99, sources=FakeSources(), limits=limits, calendar=calendar, on=ON)


# ---- approvals (the falsifying clause: an over-limit allow must point to Rakesh's approval) --------------------------

class _AskDecision:
    """A minimal stand-in for an ASK decision, for approval_covers."""

    def __init__(self, asks):
        self.asks = asks


def _finding(rule, overage):
    from execution.guardrails import Finding
    return Finding(rule=rule, outcome=ASK, detail='x', overage=overage)


def _approval(breaches, proposal_id=7, actor='rakesh', action=APPROVAL_ACTION):
    return {'actor': actor, 'action': action,
            'details': {'proposal_id': proposal_id, 'breaches': breaches}}


def test_approval_covers_same_or_smaller_overage():
    d = _AskDecision([_finding('G03', 2000.0)])
    assert approval_covers(_approval([{'rule': 'G03', 'overage': 2000.0}]), 7, d)
    assert approval_covers(_approval([{'rule': 'G03', 'overage': 2500.0}]), 7, d)


def test_approval_does_not_cover_a_grown_or_new_breach():
    approved = _approval([{'rule': 'G03', 'overage': 2000.0}])
    assert not approval_covers(approved, 7, _AskDecision([_finding('G03', 3000.0)]))
    assert not approval_covers(approved, 7, _AskDecision([_finding('G03', 2000.0), _finding('G04', 500.0)]))


def test_approval_must_be_rakeshs_for_this_proposal():
    d = _AskDecision([_finding('G03', 100.0)])
    ok = [{'rule': 'G03', 'overage': 100.0}]
    assert not approval_covers(_approval(ok, actor='system'), 7, d)
    assert not approval_covers(_approval(ok, action='other'), 7, d)
    assert not approval_covers(_approval(ok, proposal_id=8), 7, d)
    assert not approval_covers(None, 7, d)


def test_approval_details_round_trip():
    from execution.guardrails import Decision
    decision = Decision(outcome=ASK, detail='x', findings=(_finding('G03', 2000.0),))
    details = approval_details(7, decision)
    assert approval_covers({'actor': 'rakesh', 'action': APPROVAL_ACTION, 'details': details}, 7, decision)
