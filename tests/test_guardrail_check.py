# -*- coding: utf-8 -*-
# tests/test_guardrail_check.py
"""ST-008: the dry-run CLI -- allow / ask / block and its exit codes."""

from __future__ import annotations

from scripts.guardrail_check import EXIT_ALLOW, EXIT_ASK, EXIT_BLOCK, main

BASE_ARGS = ['--symbol', 'INFY', '--strategy', 'rakesh_top4', '--qty', '90', '--price', '80',
             '--stop', '72', '--sector', 'IT']


def test_cli_allows_a_clean_lot_on_an_empty_book():
    assert main(BASE_ARGS + ['--empty-book']) == EXIT_ALLOW


def test_cli_blocks_without_portfolio_state():
    """Fail-closed: told nothing about the book, the CLI must not report an allow or an ask."""
    assert main(BASE_ARGS) == EXIT_BLOCK


def test_cli_asks_when_the_kitty_is_full():
    """The kitty is soft (2026-10-30): over it is a question for Rakesh, not a block."""
    assert main(BASE_ARGS + ['--empty-book', '--committed', '99000']) == EXIT_ASK


def test_cli_lists_every_breach_in_one_ask(caplog):
    with caplog.at_level('INFO'):
        assert main(BASE_ARGS + ['--empty-book', '--committed', '99000', '--week-buys', '9500']) == EXIT_ASK
    assert 'ASK' in caplog.text
    assert 'G03' in caplog.text and 'G04' in caplog.text
    assert 'over by Rs' in caplog.text


def test_cli_halt_blocks():
    assert main(BASE_ARGS + ['--empty-book', '--tier', 'halt']) == EXIT_BLOCK


def test_cli_no_buys_asks():
    assert main(BASE_ARGS + ['--empty-book', '--tier', 'no_buys']) == EXIT_ASK


def test_cli_stale_panel_blocks_even_with_limits_exceeded(caplog):
    with caplog.at_level('INFO'):
        assert main(BASE_ARGS + ['--empty-book', '--panel-age', '5', '--committed', '99000']) == EXIT_BLOCK
    assert 'G13' in caplog.text
    assert 'moot while blocked' in caplog.text


def test_cli_missing_snapshot_blocks():
    assert main(BASE_ARGS + ['--empty-book', '--no-snapshot']) == EXIT_BLOCK


def test_cli_refuses_the_manual_strategy():
    assert main(['--symbol', 'INFY', '--strategy', 'manual', '--qty', '10', '--price', '80',
                 '--stop', '72', '--sector', 'IT', '--empty-book']) == EXIT_BLOCK
