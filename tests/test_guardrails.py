# -*- coding: utf-8 -*-
# tests/test_guardrails.py
"""ST-008: one test per rule, each breaching exactly that rule (Done-when).

Hard rules must BLOCK; soft limits must ASK (Rakesh 2026-10-30); a missing input must BLOCK even
inside a soft rule (fail-closed, 2026-10-29).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from execution.guardrails import (
    ALLOW,
    ASK,
    BLOCK,
    STAGE_BEFORE_ORDER,
    STAGE_BEFORE_PROPOSAL,
    TIER_ALERT,
    TIER_HALT,
    TIER_NO_BUYS,
    TIER_OK,
    BuyRequest,
    GuardrailConfigError,
    PortfolioState,
    evaluate,
    load_limits,
)

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'guardrails.toml'


@pytest.fixture(scope='module')
def limits() -> dict:
    """The real config/guardrails.toml -- the rules are tested against the shipped limits."""
    return load_limits(CONFIG_PATH)


@pytest.fixture
def request_ok() -> BuyRequest:
    """A lot that passes every rule: Rs 7,200, stop 10% away (Rs 720 risk)."""
    return BuyRequest(
        symbol='INFY',
        strategy='rakesh_top4',
        qty=90,
        limit_price=80.0,
        stop_price=72.0,
        sector='IT',
        series='EQ',
    )


@pytest.fixture
def state_ok() -> PortfolioState:
    """Nothing committed, fresh data."""
    return PortfolioState(
        committed_total=0.0,
        committed_by_strategy={},
        committed_by_sector={},
        committed_by_stock={},
        week_committed=0.0,
        loss_tier=TIER_OK,
        in_universe=True,
        days_to_corporate_action=None,
        panel_age_trading_days=0,
        corporate_actions_age_trading_days=0,
        snapshot_present=True,
    )


def _outcome(request_ok, state_ok, limits, request=None, state=None):
    request = dataclasses.replace(request_ok, **(request or {}))
    state = dataclasses.replace(state_ok, **(state or {}))
    return evaluate(request, state, limits, STAGE_BEFORE_PROPOSAL)


def _blocked_by(request_ok, state_ok, limits, rule, **overrides):
    """Assert a BLOCK whose first hard failure is `rule`, and that nothing else blocked."""
    decision = _outcome(request_ok, state_ok, limits, **overrides)
    assert decision.outcome == BLOCK, f'expected {rule} to block, got {decision.outcome}: {decision.detail}'
    assert decision.rules == (rule,), f'expected only {rule} to block, got {decision.rules}: {decision.detail}'
    return decision


def _asked_by(request_ok, state_ok, limits, rule, **overrides):
    """Assert an ASK naming exactly `rule` -- the breach must be the only limit exceeded."""
    decision = _outcome(request_ok, state_ok, limits, **overrides)
    assert decision.outcome == ASK, f'expected {rule} to ask, got {decision.outcome}: {decision.detail}'
    assert decision.rules == (rule,), f'expected only {rule} to ask, got {decision.rules}: {decision.detail}'
    return decision


# ---- allow ---------------------------------------------------------------------------------------------------------

def test_clean_request_is_allowed(request_ok, state_ok, limits):
    decision = evaluate(request_ok, state_ok, limits, STAGE_BEFORE_PROPOSAL)
    assert decision.outcome == ALLOW and decision.allow
    assert decision.rule == '' and decision.findings == ()
    assert 'G14:not-evaluated' in decision.flags


def test_both_stages_are_accepted(request_ok, state_ok, limits):
    assert evaluate(request_ok, state_ok, limits, STAGE_BEFORE_ORDER).allow
    with pytest.raises(ValueError):
        evaluate(request_ok, state_ok, limits, 'whenever')


# ---- hard rules: BLOCK ---------------------------------------------------------------------------------------------

def test_g01_series_not_eq(request_ok, state_ok, limits):
    _blocked_by(request_ok, state_ok, limits, 'G01', request={'series': 'BE'})


def test_g01_surveillance_flag(request_ok, state_ok, limits):
    _blocked_by(request_ok, state_ok, limits, 'G01', request={'flags': ('ASM',)})


def test_g01_outside_universe(request_ok, state_ok, limits):
    _blocked_by(request_ok, state_ok, limits, 'G01', state={'in_universe': False})


def test_g01_manual_strategy_never_ordered(request_ok, state_ok, limits):
    _blocked_by(request_ok, state_ok, limits, 'G01', request={'strategy': 'manual'})


def test_g02_product_and_order_type(request_ok, state_ok, limits):
    _blocked_by(request_ok, state_ok, limits, 'G02', request={'product': 'MIS'})
    _blocked_by(request_ok, state_ok, limits, 'G02', request={'order_type': 'MARKET'})


def test_g02_non_positive_quantity_or_price(request_ok, state_ok, limits):
    _blocked_by(request_ok, state_ok, limits, 'G02', request={'qty': 0})
    _blocked_by(request_ok, state_ok, limits, 'G02', request={'limit_price': 0.0})


def test_g06_missing_stop_is_hard(request_ok, state_ok, limits):
    _blocked_by(request_ok, state_ok, limits, 'G06', request={'stop_price': None})


def test_g06_stop_above_entry_is_hard(request_ok, state_ok, limits):
    _blocked_by(request_ok, state_ok, limits, 'G06', request={'stop_price': 85.0})


def test_g11_halt_is_hard(request_ok, state_ok, limits):
    """Halt stays a block (2026-10-30); lifted only by editing the threshold in guardrails.toml."""
    _blocked_by(request_ok, state_ok, limits, 'G11', state={'loss_tier': TIER_HALT})


def test_g11_unknown_or_unrecognised_tier_blocks(request_ok, state_ok, limits):
    _blocked_by(request_ok, state_ok, limits, 'G11', state={'loss_tier': None})
    _blocked_by(request_ok, state_ok, limits, 'G11', state={'loss_tier': 'no-buys'})   # the old misspelling


def test_g13_stale_or_missing_data(request_ok, state_ok, limits):
    _blocked_by(request_ok, state_ok, limits, 'G13', state={'panel_age_trading_days': 4})
    _blocked_by(request_ok, state_ok, limits, 'G13', state={'panel_age_trading_days': None})
    _blocked_by(request_ok, state_ok, limits, 'G13', state={'corporate_actions_age_trading_days': None})
    _blocked_by(request_ok, state_ok, limits, 'G13', state={'corporate_actions_age_trading_days': 9})
    _blocked_by(request_ok, state_ok, limits, 'G13', state={'snapshot_present': False})


def test_g13_is_named_first_even_when_limits_are_also_exceeded(request_ok, state_ok, limits):
    """Stale data must not be masked: a block outranks any ask, and freshness leads the blocks."""
    decision = _outcome(request_ok, state_ok, limits,
                        state={'panel_age_trading_days': 5, 'committed_total': 99000.0})
    assert decision.outcome == BLOCK
    assert decision.rule == 'G13'
    assert 'G03' in {f.rule for f in decision.asks}   # still reported, never acted on


def test_g14_blocks_when_enabled_but_not_run(request_ok, state_ok, limits):
    enabled = dict(limits)
    enabled['news'] = {'enabled': True}
    decision = evaluate(request_ok, state_ok, enabled, STAGE_BEFORE_PROPOSAL)
    assert decision.outcome == BLOCK and decision.rule == 'G14'
    state = dataclasses.replace(state_ok, news_checked=True)
    assert evaluate(request_ok, state, enabled, STAGE_BEFORE_PROPOSAL).allow


# ---- soft limits: ASK ----------------------------------------------------------------------------------------------

def test_g03_capital_cap_asks(request_ok, state_ok, limits):
    decision = _asked_by(request_ok, state_ok, limits, 'G03', state={'committed_total': 95000.0})
    assert decision.asks[0].overage == pytest.approx(2200.0)
    assert 'headroom' in decision.detail


def test_g03_headroom_returns_when_a_lot_exits(request_ok, state_ok, limits):
    """The kitty revolves: at the cap the lot is asked about; once a lot's cost is released it passes."""
    at_cap = dataclasses.replace(state_ok, committed_total=100000.0)
    assert evaluate(request_ok, at_cap, limits, STAGE_BEFORE_PROPOSAL).outcome == ASK
    after_exit = dataclasses.replace(state_ok, committed_total=92000.0)
    assert evaluate(request_ok, after_exit, limits, STAGE_BEFORE_PROPOSAL).allow


def test_g04_weekly_cap_asks(request_ok, state_ok, limits):
    decision = _asked_by(request_ok, state_ok, limits, 'G04', state={'week_committed': 9000.0})
    assert decision.asks[0].overage == pytest.approx(6200.0)


def test_g05_lot_too_large_asks(request_ok, state_ok, limits):
    """Rs 13,000 lot with a Rs 100 entry-to-stop risk: exceeds the lot cap and nothing else.

    Per-stock (8% = Rs 8,000) and the weekly cap (Rs 10,000) would also be exceeded by Rs 13,000, so
    both are lifted in this test's limits to keep the breach single.
    """
    roomy = {**limits, 'capital': {**limits['capital'], 'per_stock_pct': 0.20, 'weekly_new_buys': 20000}}
    request = dataclasses.replace(request_ok, qty=100, limit_price=130.0, stop_price=129.0)
    decision = evaluate(request, state_ok, roomy, STAGE_BEFORE_PROPOSAL)
    assert decision.outcome == ASK and decision.rules == ('G05',), decision.detail


def test_g06_risk_per_trade_asks(request_ok, state_ok, limits):
    _asked_by(request_ok, state_ok, limits, 'G06', request={'stop_price': 60.0})


def test_g07_per_strategy_cap_asks(request_ok, state_ok, limits):
    _asked_by(request_ok, state_ok, limits, 'G07', state={'committed_by_strategy': {'rakesh_top4': 39000.0}})


def test_g08_per_sector_cap_asks(request_ok, state_ok, limits):
    _asked_by(request_ok, state_ok, limits, 'G08', state={'committed_by_sector': {'IT': 24000.0}})


def test_g09_per_stock_cap_asks(request_ok, state_ok, limits):
    _asked_by(request_ok, state_ok, limits, 'G09', state={'committed_by_stock': {'INFY': 5000.0}})


def test_g09_counts_all_strategies(request_ok, state_ok, limits):
    """The per-stock cap is across strategies, so another strategy's lot still counts."""
    decision = _outcome(request_ok, state_ok, limits, state={'committed_by_stock': {'INFY': 7000.0},
                                                             'committed_by_strategy': {'baseline_momentum': 7000.0}})
    assert decision.outcome == ASK and decision.rules == ('G09',)


def test_g11_no_buys_asks(request_ok, state_ok, limits):
    _asked_by(request_ok, state_ok, limits, 'G11', state={'loss_tier': TIER_NO_BUYS})


def test_g11_alert_allows_and_flags(request_ok, state_ok, limits):
    decision = _outcome(request_ok, state_ok, limits, state={'loss_tier': TIER_ALERT})
    assert decision.allow
    assert 'G11:alert' in decision.flags


def test_g12_corporate_action_blackout_asks(request_ok, state_ok, limits):
    _asked_by(request_ok, state_ok, limits, 'G12', state={'days_to_corporate_action': 3})
    _asked_by(request_ok, state_ok, limits, 'G12', state={'days_to_corporate_action': 0})


def test_g12_outside_the_window_is_allowed(request_ok, state_ok, limits):
    assert _outcome(request_ok, state_ok, limits, state={'days_to_corporate_action': 6}).allow


def test_one_ask_lists_every_breach(request_ok, state_ok, limits):
    """Several limits at once: one ASK naming all of them (decision 3), not one ask per rule."""
    decision = _outcome(request_ok, state_ok, limits,
                        state={'committed_total': 99000.0, 'week_committed': 9500.0,
                               'committed_by_sector': {'IT': 24000.0}})
    assert decision.outcome == ASK
    assert set(decision.rules) == {'G03', 'G04', 'G08'}


def test_committed_money_catches_picks_that_fit_alone_but_not_together(limits):
    """Done-when: three picks in one run each fit alone, not together; the third is asked about.

    Kitty Rs 92,000 committed. Picks Rs 4,000 / 3,000 / 3,000 (the worked example in ST-008.md).
    The caller adds each earlier pick to committed money before checking the next.
    """
    picks = [('AAA', 50, 80.0, 74.0), ('BBB', 30, 100.0, 92.0), ('CCC', 30, 100.0, 92.0)]
    committed = 92000.0
    outcomes = []
    for symbol, qty, price, stop in picks:
        request = BuyRequest(symbol=symbol, strategy='rakesh_top4', qty=qty, limit_price=price,
                             stop_price=stop, sector=f'S-{symbol}', series='EQ')
        state = PortfolioState(
            committed_total=committed, committed_by_strategy={}, committed_by_sector={},
            committed_by_stock={}, week_committed=0.0, loss_tier=TIER_OK, in_universe=True,
            panel_age_trading_days=0, corporate_actions_age_trading_days=0, snapshot_present=True,
        )
        decision = evaluate(request, state, limits, STAGE_BEFORE_PROPOSAL)
        outcomes.append((decision.outcome, decision.rules))
        committed += request.amount
    assert outcomes[0] == (ALLOW, ())
    assert outcomes[1] == (ALLOW, ())
    assert outcomes[2] == (ASK, ('G03',))


# ---- fail-closed and the hard/soft boundary --------------------------------------------------------------------------

def test_missing_inputs_block_even_in_soft_rules(request_ok, state_ok, limits):
    """A limit can only be asked about when its number is known; missing input is a block."""
    _blocked_by(request_ok, state_ok, limits, 'G08', request={'sector': None})
    _blocked_by(request_ok, state_ok, limits, 'G08', state={'committed_by_sector': None})
    _blocked_by(request_ok, state_ok, limits, 'G03', state={'committed_total': None})
    _blocked_by(request_ok, state_ok, limits, 'G04', state={'week_committed': None})
    _blocked_by(request_ok, state_ok, limits, 'G07', state={'committed_by_strategy': None})
    _blocked_by(request_ok, state_ok, limits, 'G09', state={'committed_by_stock': None})


def test_fail_closed_on_unknown_portfolio_state(request_ok, limits):
    """An empty state knows nothing, so the outcome is a block -- never an allow and never an ask."""
    decision = evaluate(request_ok, PortfolioState(), limits, STAGE_BEFORE_PROPOSAL)
    assert decision.outcome == BLOCK


def test_no_hard_rule_ever_asks(request_ok, state_ok, limits):
    """Falsifying clause: a hard rule breached must never produce ask or allow."""
    breaches = [
        {'request': {'series': 'BE'}},
        {'request': {'flags': ('GSM',)}},
        {'request': {'strategy': 'manual'}},
        {'request': {'product': 'MIS'}},
        {'request': {'order_type': 'MARKET'}},
        {'request': {'stop_price': None}},
        {'state': {'loss_tier': TIER_HALT}},
        {'state': {'panel_age_trading_days': 3}},
        {'state': {'in_universe': False}},
    ]
    for overrides in breaches:
        decision = _outcome(request_ok, state_ok, limits, **overrides)
        assert decision.outcome == BLOCK, f'{overrides} produced {decision.outcome}'


# ---- config ---------------------------------------------------------------------------------------------------------

def test_missing_config_raises(tmp_path):
    with pytest.raises(GuardrailConfigError):
        load_limits(tmp_path / 'nope.toml')


def test_incomplete_config_raises(tmp_path):
    partial = tmp_path / 'guardrails.toml'
    partial.write_text('[capital]\ncap = 100000\n', encoding='utf-8')
    with pytest.raises(GuardrailConfigError):
        load_limits(partial)


def test_shipped_limits_match_the_agreed_numbers(limits):
    """CLAUDE.md "Agreed Limits" and ST-008 -- the file is the enforced copy."""
    assert limits['capital']['cap'] == 100000
    assert limits['capital']['weekly_new_buys'] == 10000
    assert limits['capital']['max_lot_amount'] == 12000
    assert limits['capital']['per_strategy'] == 40000
    assert limits['capital']['per_sector'] == 25000
    assert limits['capital']['per_stock_pct'] == 0.08
    assert limits['risk']['max_per_trade'] == 1000
    assert limits['tiers'] == {'alert': 0.10, 'no_new_buys': 0.15, 'halt': 0.20}
    assert limits['corporate_actions']['blackout_trading_days'] == 5
