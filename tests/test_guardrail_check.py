# -*- coding: utf-8 -*-
# tests/test_guardrail_check.py
"""ST-008: evaluate_all (every breach, not just the first) and the dry-run CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from execution.guardrails import BuyRequest, PortfolioState, TIER_OK, evaluate_all, load_limits
from scripts.guardrail_check import main

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'guardrails.toml'

BASE_ARGS = ['--symbol', 'INFY', '--strategy', 'rakesh_top4', '--qty', '90', '--price', '80',
             '--stop', '72', '--sector', 'IT']


@pytest.fixture(scope='module')
def limits() -> dict:
    return load_limits(CONFIG_PATH)


def test_evaluate_all_reports_every_breach(limits):
    """A lot can break several limits at once; evaluate_all names them all."""
    request = BuyRequest(symbol='INFY', strategy='rakesh_top4', qty=100, limit_price=130.0,
                         stop_price=100.0, sector='IT', series='BE')
    state = PortfolioState(
        open_cost_total=99000.0, cost_by_strategy={}, cost_by_sector={}, cost_by_stock={},
        week_buys_amount=9500.0, loss_tier=TIER_OK, in_universe=True,
        panel_age_trading_days=0, corporate_actions_age_trading_days=0, snapshot_present=True,
    )
    rules = {decision.rule for decision in evaluate_all(request, state, limits)}
    assert {'G01', 'G06', 'G05', 'G09', 'G04', 'G03'} <= rules


def test_evaluate_all_empty_when_clean(limits):
    request = BuyRequest(symbol='INFY', strategy='rakesh_top4', qty=90, limit_price=80.0,
                         stop_price=72.0, sector='IT', series='EQ')
    state = PortfolioState(
        open_cost_total=0.0, cost_by_strategy={}, cost_by_sector={}, cost_by_stock={},
        week_buys_amount=0.0, loss_tier=TIER_OK, in_universe=True,
        panel_age_trading_days=0, corporate_actions_age_trading_days=0, snapshot_present=True,
    )
    assert evaluate_all(request, state, limits) == []


def test_cli_allows_a_clean_lot_on_an_empty_book():
    assert main(BASE_ARGS + ['--empty-book']) == 0


def test_cli_blocks_without_portfolio_state():
    """Fail-closed: told nothing about the book, the CLI must not report an allow."""
    assert main(BASE_ARGS) == 1


def test_cli_blocks_when_the_kitty_is_full():
    assert main(BASE_ARGS + ['--empty-book', '--open-cost', '99000']) == 1


def test_cli_lists_every_breach_with_all(capsys, caplog):
    with caplog.at_level('INFO'):
        assert main(BASE_ARGS + ['--empty-book', '--open-cost', '99000', '--week-buys', '9500', '--all']) == 1
    logged = caplog.text
    assert 'BLOCK' in logged
    assert 'also' in logged


def test_cli_refuses_the_manual_strategy():
    assert main(['--symbol', 'INFY', '--strategy', 'manual', '--qty', '10', '--price', '80',
                 '--stop', '72', '--sector', 'IT', '--empty-book']) == 1
