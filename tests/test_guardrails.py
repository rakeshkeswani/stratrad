# -*- coding: utf-8 -*-
# tests/test_guardrails.py
"""ST-008: one test per guardrail rule, each breaching exactly that rule (Done-when)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from execution.guardrails import (
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
    """An empty book with fresh data."""
    return PortfolioState(
        open_cost_total=0.0,
        cost_by_strategy={},
        cost_by_sector={},
        cost_by_stock={},
        week_buys_amount=0.0,
        loss_tier=TIER_OK,
        in_universe=True,
        days_to_corporate_action=None,
        panel_age_trading_days=0,
        corporate_actions_age_trading_days=0,
        snapshot_present=True,
    )


def _blocked_by(request_ok, state_ok, limits, rule: str, **overrides):
    """Apply overrides to the request/state and assert `rule` is what blocks."""
    request = dataclasses.replace(request_ok, **overrides.pop('request', {}))
    state = dataclasses.replace(state_ok, **overrides.pop('state', {}))
    decision = evaluate(request, state, limits, STAGE_BEFORE_PROPOSAL)
    assert not decision.allow, f'expected {rule} to block, got an allow: {decision.detail}'
    assert decision.rule == rule, f'expected {rule}, got {decision.rule}: {decision.detail}'
    return decision


def test_clean_request_is_allowed(request_ok, state_ok, limits):
    decision = evaluate(request_ok, state_ok, limits, STAGE_BEFORE_PROPOSAL)
    assert decision.allow
    assert decision.rule == ''
    assert 'G14:not-evaluated' in decision.flags


def test_both_stages_are_accepted(request_ok, state_ok, limits):
    assert evaluate(request_ok, state_ok, limits, STAGE_BEFORE_ORDER).allow
    with pytest.raises(ValueError):
        evaluate(request_ok, state_ok, limits, 'whenever')


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


def test_g03_capital_cap(request_ok, state_ok, limits):
    decision = _blocked_by(request_ok, state_ok, limits, 'G03', state={'open_cost_total': 95000.0})
    assert 'headroom' in decision.detail


def test_g03_headroom_returns_when_a_lot_exits(request_ok, state_ok, limits):
    """The kitty revolves: at the cap nothing passes; once a lot's cost is released it does."""
    at_cap = dataclasses.replace(state_ok, open_cost_total=100000.0)
    assert not evaluate(request_ok, at_cap, limits, STAGE_BEFORE_PROPOSAL).allow
    after_exit = dataclasses.replace(state_ok, open_cost_total=92000.0)
    assert evaluate(request_ok, after_exit, limits, STAGE_BEFORE_PROPOSAL).allow


def test_g04_weekly_cap(request_ok, state_ok, limits):
    _blocked_by(request_ok, state_ok, limits, 'G04', state={'week_buys_amount': 9000.0})


def test_g05_lot_too_large(request_ok, state_ok, limits):
    """Rs 13,000 lot with a Rs 100 entry-to-stop risk: breaches the lot cap and nothing else."""
    _blocked_by(
        request_ok,
        state_ok,
        limits,
        'G05',
        request={'qty': 100, 'limit_price': 130.0, 'stop_price': 129.0},
    )


def test_g05_non_positive_quantity(request_ok, state_ok, limits):
    _blocked_by(request_ok, state_ok, limits, 'G05', request={'qty': 0})


def test_g06_missing_stop(request_ok, state_ok, limits):
    _blocked_by(request_ok, state_ok, limits, 'G06', request={'stop_price': None})


def test_g06_stop_above_entry(request_ok, state_ok, limits):
    _blocked_by(request_ok, state_ok, limits, 'G06', request={'stop_price': 85.0})


def test_g06_risk_per_trade(request_ok, state_ok, limits):
    _blocked_by(request_ok, state_ok, limits, 'G06', request={'stop_price': 60.0})


def test_g07_per_strategy_cap(request_ok, state_ok, limits):
    _blocked_by(request_ok, state_ok, limits, 'G07', state={'cost_by_strategy': {'rakesh_top4': 39000.0}})


def test_g08_per_sector_cap(request_ok, state_ok, limits):
    _blocked_by(request_ok, state_ok, limits, 'G08', state={'cost_by_sector': {'IT': 24000.0}})


def test_g08_unknown_sector_blocks(request_ok, state_ok, limits):
    """Fail-closed: no sector means the per-sector cap cannot be computed."""
    _blocked_by(request_ok, state_ok, limits, 'G08', request={'sector': None})


def test_g09_per_stock_cap(request_ok, state_ok, limits):
    _blocked_by(request_ok, state_ok, limits, 'G09', state={'cost_by_stock': {'INFY': 5000.0}})


def test_g09_counts_all_strategies(request_ok, state_ok, limits):
    """The per-stock cap is across strategies, so another strategy's lot still counts."""
    state = dataclasses.replace(state_ok, cost_by_stock={'INFY': 7000.0}, cost_by_strategy={'baseline_momentum': 7000.0})
    decision = evaluate(request_ok, state, limits, STAGE_BEFORE_PROPOSAL)
    assert not decision.allow and decision.rule == 'G09'


def test_g11_tiers(request_ok, state_ok, limits):
    _blocked_by(request_ok, state_ok, limits, 'G11', state={'loss_tier': TIER_NO_BUYS})
    _blocked_by(request_ok, state_ok, limits, 'G11', state={'loss_tier': TIER_HALT})
    _blocked_by(request_ok, state_ok, limits, 'G11', state={'loss_tier': None})
    _blocked_by(request_ok, state_ok, limits, 'G11', state={'loss_tier': 'green'})


def test_g11_alert_allows_and_flags(request_ok, state_ok, limits):
    state = dataclasses.replace(state_ok, loss_tier=TIER_ALERT)
    decision = evaluate(request_ok, state, limits, STAGE_BEFORE_PROPOSAL)
    assert decision.allow
    assert 'G11:alert' in decision.flags


def test_g12_corporate_action_blackout(request_ok, state_ok, limits):
    _blocked_by(request_ok, state_ok, limits, 'G12', state={'days_to_corporate_action': 3})
    _blocked_by(request_ok, state_ok, limits, 'G12', state={'days_to_corporate_action': 0})


def test_g12_outside_the_window_is_allowed(request_ok, state_ok, limits):
    state = dataclasses.replace(state_ok, days_to_corporate_action=6)
    assert evaluate(request_ok, state, limits, STAGE_BEFORE_PROPOSAL).allow


def test_g13_stale_or_missing_data(request_ok, state_ok, limits):
    _blocked_by(request_ok, state_ok, limits, 'G13', state={'panel_age_trading_days': 4})
    _blocked_by(request_ok, state_ok, limits, 'G13', state={'panel_age_trading_days': None})
    _blocked_by(request_ok, state_ok, limits, 'G13', state={'corporate_actions_age_trading_days': None})
    _blocked_by(request_ok, state_ok, limits, 'G13', state={'corporate_actions_age_trading_days': 9})
    _blocked_by(request_ok, state_ok, limits, 'G13', state={'snapshot_present': False})


def test_g13_runs_before_every_other_rule(request_ok, state_ok, limits):
    """Stale data must not be masked by some later rule's block."""
    state = dataclasses.replace(state_ok, panel_age_trading_days=5, open_cost_total=99000.0)
    assert evaluate(request_ok, state, limits, STAGE_BEFORE_PROPOSAL).rule == 'G13'


def test_g14_blocks_when_enabled_but_not_run(request_ok, state_ok, limits):
    enabled = dict(limits)
    enabled['news'] = {'enabled': True}
    decision = evaluate(request_ok, state_ok, enabled, STAGE_BEFORE_PROPOSAL)
    assert not decision.allow and decision.rule == 'G14'
    state = dataclasses.replace(state_ok, news_checked=True)
    assert evaluate(request_ok, state, enabled, STAGE_BEFORE_PROPOSAL).allow


def test_fail_closed_on_unknown_portfolio_state(request_ok, limits):
    """An empty state knows nothing, so nothing is allowed (never an accidental allow)."""
    decision = evaluate(request_ok, PortfolioState(), limits, STAGE_BEFORE_PROPOSAL)
    assert not decision.allow


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
