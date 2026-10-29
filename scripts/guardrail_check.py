# -*- coding: utf-8 -*-
# scripts/guardrail_check.py
"""ST-008: dry-run one proposed buy against the guardrails. Places no orders and writes nothing.

Portfolio state comes from command-line options for now; once the service layer lands (same ST) it
will be read from the `stratrad` database instead. Anything not supplied is unknown, and unknown
blocks -- so a bare run reports a block, by design.

Usage (laptop or prod, from the repo root):
    python -m scripts.guardrail_check --symbol INFY --strategy rakesh_top4 --qty 90 \
        --price 80 --stop 72 --sector IT --empty-book
    python -m scripts.guardrail_check ... --open-cost 95000 --week-buys 4000 --all
"""

from __future__ import annotations

import argparse
import logging
import sys

from core.config import REPO_ROOT
from execution.guardrails import (
    STAGE_BEFORE_ORDER,
    STAGE_BEFORE_PROPOSAL,
    TIER_OK,
    BuyRequest,
    GuardrailConfigError,
    PortfolioState,
    evaluate,
    evaluate_all,
    load_limits,
)

LOGGER = logging.getLogger('guardrail_check')
CONFIG_PATH = REPO_ROOT / 'config' / 'guardrails.toml'


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Dry-run a proposed buy against the guardrails (ST-008).')
    lot = parser.add_argument_group('the proposed lot')
    lot.add_argument('--symbol', required=True)
    lot.add_argument('--strategy', required=True, help='strategy name; "manual" is always refused')
    lot.add_argument('--qty', type=int, required=True)
    lot.add_argument('--price', type=float, required=True, help='limit price')
    lot.add_argument('--stop', type=float, default=None, help='stop price (required by G06)')
    lot.add_argument('--sector', default=None, help='unknown sector blocks on G08')
    lot.add_argument('--series', default='EQ')
    lot.add_argument('--flags', default='', help='comma-separated surveillance flags, e.g. ASM,GSM')
    lot.add_argument('--product', default='CNC')
    lot.add_argument('--order-type', default='LIMIT')

    book = parser.add_argument_group('portfolio state (unknown blocks)')
    book.add_argument('--empty-book', action='store_true',
                      help='assume no open lots, fresh data and tier ok -- the hypothetical starting point')
    book.add_argument('--open-cost', type=float, default=None, help='cost of all open stratrad lots')
    book.add_argument('--week-buys', type=float, default=None, help='amount already bought this week')
    book.add_argument('--held-stock', type=float, default=None, help='open cost already in this symbol')
    book.add_argument('--held-strategy', type=float, default=None, help='open cost already in this strategy')
    book.add_argument('--held-sector', type=float, default=None, help='open cost already in this sector')
    book.add_argument('--tier', default=None, help='ok / alert / no-buys / halt')
    book.add_argument('--outside-universe', action='store_true', help='treat the symbol as outside the Nifty 500')
    book.add_argument('--ca-in-days', type=int, default=None, help='trading days to the next ex-date')
    book.add_argument('--panel-age', type=int, default=None, help='price panel age in trading days')
    book.add_argument('--ca-age', type=int, default=None, help='corporate-actions age in trading days')

    parser.add_argument('--stage', choices=[STAGE_BEFORE_PROPOSAL, STAGE_BEFORE_ORDER],
                        default=STAGE_BEFORE_PROPOSAL)
    parser.add_argument('--all', action='store_true', dest='show_all',
                        help='list every rule that blocks, not just the first')
    return parser.parse_args(argv)


def _build_state(args: argparse.Namespace) -> PortfolioState:
    """Turn the options into a PortfolioState. --empty-book supplies the clean defaults."""
    if args.empty_book:
        defaults = {
            'open_cost_total': 0.0, 'week_buys_amount': 0.0, 'loss_tier': TIER_OK,
            'panel_age_trading_days': 0, 'corporate_actions_age_trading_days': 0, 'snapshot_present': True,
        }
    else:
        defaults = {'snapshot_present': args.panel_age is not None}

    def _pick(name: str, value):
        return defaults.get(name) if value is None else value

    return PortfolioState(
        open_cost_total=_pick('open_cost_total', args.open_cost),
        cost_by_strategy={args.strategy: args.held_strategy} if args.held_strategy is not None else (
            {} if args.empty_book else None),
        cost_by_sector={args.sector: args.held_sector} if args.held_sector is not None and args.sector else (
            {} if args.empty_book else None),
        cost_by_stock={args.symbol: args.held_stock} if args.held_stock is not None else (
            {} if args.empty_book else None),
        week_buys_amount=_pick('week_buys_amount', args.week_buys),
        loss_tier=_pick('loss_tier', args.tier),
        in_universe=not args.outside_universe,
        days_to_corporate_action=args.ca_in_days,
        panel_age_trading_days=_pick('panel_age_trading_days', args.panel_age),
        corporate_actions_age_trading_days=_pick('corporate_actions_age_trading_days', args.ca_age),
        snapshot_present=defaults.get('snapshot_present', False),
    )


def main(argv: list[str] | None = None) -> int:
    """Evaluate one hypothetical buy and print the verdict. Returns 0 when allowed, 1 when blocked."""
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    args = _parse_args(argv)
    try:
        limits = load_limits(CONFIG_PATH)
    except GuardrailConfigError as exc:
        LOGGER.error('guardrails config: %s', exc)
        return 2

    request = BuyRequest(
        symbol=args.symbol.upper(),
        strategy=args.strategy,
        qty=args.qty,
        limit_price=args.price,
        stop_price=args.stop,
        sector=args.sector,
        series=args.series,
        flags=tuple(flag.strip().upper() for flag in args.flags.split(',') if flag.strip()),
        product=args.product,
        order_type=args.order_type,
    )
    state = _build_state(args)

    LOGGER.info('%s: %s x %d @ Rs %.2f = Rs %.2f',
                args.stage, request.symbol, request.qty, request.limit_price, request.amount)

    decision = evaluate(request, state, limits, args.stage)
    if decision.allow:
        LOGGER.info('ALLOW  %s', decision.detail)
        for flag in decision.flags:
            LOGGER.info('  flag %s', flag)
        return 0

    LOGGER.info('BLOCK  %s: %s', decision.rule, decision.detail)
    if args.show_all:
        for other in evaluate_all(request, state, limits):
            if other.rule != decision.rule:
                LOGGER.info('  also %s: %s', other.rule, other.detail)
    return 1


if __name__ == '__main__':
    sys.exit(main())
