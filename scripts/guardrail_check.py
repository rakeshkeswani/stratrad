# -*- coding: utf-8 -*-
# scripts/guardrail_check.py
"""ST-008: dry-run one proposed buy against the guardrails. Places no orders and writes nothing.

Shows one of three outcomes (tech-debt/ST-008.md, decided 2026-10-30):
    ALLOW  nothing failed, no limit exceeded
    ASK    a limit would be exceeded; Rakesh may approve it -- every breach is listed
    BLOCK  a hard rule failed (invalid order, stale or missing data, halt tier)

Two ways to supply portfolio state:
    --live        read the real book and data through execution.guardrail_service (prod), writing nothing
    options       a hypothetical book; --empty-book is a clean start, other options override it
Anything not supplied is unknown, and unknown blocks -- so a bare run reports a block, by design.

Usage (from the repo root):
    python -m scripts.guardrail_check --symbol INFY --strategy rakesh_top4 --qty 90 \
        --price 80 --stop 72 --sector IT --empty-book
    python -m scripts.guardrail_check ... --empty-book --committed 95000 --week-buys 9500
    python -m scripts.guardrail_check --symbol INFY --strategy rakesh_top4 --qty 90 --price 80 --stop 72 --live

Exit code: 0 allow, 3 ask, 1 block, 2 guardrails config unreadable.
"""

from __future__ import annotations

import argparse
import logging
import sys

from core.config import REPO_ROOT
from execution.guardrails import (
    ASK,
    BLOCK,
    STAGE_BEFORE_ORDER,
    STAGE_BEFORE_PROPOSAL,
    TIER_OK,
    BuyRequest,
    GuardrailConfigError,
    PortfolioState,
    evaluate,
    load_limits,
)

LOGGER = logging.getLogger('guardrail_check')
CONFIG_PATH = REPO_ROOT / 'config' / 'guardrails.toml'

EXIT_ALLOW, EXIT_BLOCK, EXIT_CONFIG, EXIT_ASK = 0, 1, 2, 3


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Dry-run a proposed buy against the guardrails (ST-008).')
    lot = parser.add_argument_group('the proposed lot')
    lot.add_argument('--symbol', required=True)
    lot.add_argument('--strategy', required=True, help='strategy name; "manual" is always refused')
    lot.add_argument('--qty', type=int, required=True)
    lot.add_argument('--price', type=float, required=True, help='limit price')
    lot.add_argument('--stop', type=float, default=None, help='stop price (required)')
    lot.add_argument('--sector', default=None, help='unknown sector blocks')
    lot.add_argument('--series', default='EQ')
    lot.add_argument('--flags', default='', help='comma-separated surveillance flags, e.g. ASM,GSM')
    lot.add_argument('--product', default='CNC')
    lot.add_argument('--order-type', default='LIMIT')

    book = parser.add_argument_group('portfolio state -- committed money (unknown blocks)')
    book.add_argument('--empty-book', action='store_true',
                      help='nothing committed, fresh data, tier ok -- the hypothetical starting point')
    book.add_argument('--committed', type=float, default=None,
                      help='committed money across stratrad: open lots at cost + unfilled approved buys')
    book.add_argument('--week-buys', type=float, default=None, help='new buys already committed this week')
    book.add_argument('--held-stock', type=float, default=None, help='committed in this symbol already')
    book.add_argument('--held-strategy', type=float, default=None, help='committed in this strategy already')
    book.add_argument('--held-sector', type=float, default=None, help='committed in this sector already')
    book.add_argument('--tier', default=None, help='ok / alert / no_buys / halt')
    book.add_argument('--outside-universe', action='store_true', help='treat the symbol as outside the Nifty 500')
    book.add_argument('--ca-in-days', type=int, default=None, help='trading days to the next ex-date')
    book.add_argument('--panel-age', type=int, default=None, help='price panel age in trading days')
    book.add_argument('--ca-age', type=int, default=None, help='corporate-actions age in trading days')
    book.add_argument('--no-snapshot', action='store_true', help='no daily_snapshots row for the last trading day')
    book.add_argument('--live', action='store_true',
                      help='read the real book and data (prod); the options above and --sector are ignored')

    parser.add_argument('--stage', choices=[STAGE_BEFORE_PROPOSAL, STAGE_BEFORE_ORDER],
                        default=STAGE_BEFORE_PROPOSAL)
    return parser.parse_args(argv)


def _per_key(key: str | None, value: float | None, empty_book: bool) -> dict[str, float] | None:
    """A one-entry map when the option was given; {} on an empty book; None (unknown) otherwise."""
    if value is not None and key:
        return {key: value}
    return {} if empty_book else None


def _build_state(args: argparse.Namespace) -> PortfolioState:
    """Turn the options into a PortfolioState. --empty-book supplies clean defaults; options override them."""
    clean = args.empty_book

    def _pick(value, clean_default):
        if value is not None:
            return value
        return clean_default if clean else None

    snapshot = None
    if args.no_snapshot:
        snapshot = False
    elif clean:
        snapshot = True

    return PortfolioState(
        universe_age_days=0 if clean else None,
        upcoming_ex_dates_known=True if (clean or args.ca_in_days is not None) else None,
        committed_total=_pick(args.committed, 0.0),
        committed_by_strategy=_per_key(args.strategy, args.held_strategy, clean),
        committed_by_sector=_per_key(args.sector, args.held_sector, clean),
        committed_by_stock=_per_key(args.symbol.upper(), args.held_stock, clean),
        week_committed=_pick(args.week_buys, 0.0),
        loss_tier=_pick(args.tier, TIER_OK),
        in_universe=not args.outside_universe,
        days_to_corporate_action=args.ca_in_days,
        panel_age_trading_days=_pick(args.panel_age, 0),
        corporate_actions_age_trading_days=_pick(args.ca_age, 0),
        snapshot_present=snapshot,
    )


def _live(request: BuyRequest, limits: dict, stage: str):
    """Evaluate against the real book and data, writing no guardrail_events row. Returns (decision, request)."""
    import dataclasses

    from core.trading_calendar import TradingCalendar
    from execution.guardrail_service import DbSources, gather_state, today_ist

    with DbSources() as sources:
        request = dataclasses.replace(request, sector=sources.sector_of(request.symbol))
        state, request, notes = gather_state(request, sources=sources, calendar=TradingCalendar(), on=today_ist())
    for key, value in notes.items():
        LOGGER.info('  read %s: %s', key, value)
    return evaluate(request, state, limits, stage), request


def main(argv: list[str] | None = None) -> int:
    """Evaluate one hypothetical buy and print the outcome. See the module docstring for exit codes."""
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    args = _parse_args(argv)
    try:
        limits = load_limits(CONFIG_PATH)
    except GuardrailConfigError as exc:
        LOGGER.error('guardrails config: %s', exc)
        return EXIT_CONFIG

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
    if args.live:
        decision, request = _live(request, limits, args.stage)
    else:
        state = _build_state(args)
        decision = evaluate(request, state, limits, args.stage)

    LOGGER.info('%s: %s x %d @ Rs %.2f = Rs %.2f%s',
                args.stage, request.symbol, request.qty, request.limit_price, request.amount,
                ' (live book)' if args.live else '')

    if decision.outcome == BLOCK:
        LOGGER.info('BLOCK  (hard rule -- cannot be approved)')
        for finding in decision.blocks:
            LOGGER.info('  %s: %s', finding.rule, finding.detail)
        if decision.asks:
            LOGGER.info('  limits also exceeded (moot while blocked):')
            for finding in decision.asks:
                LOGGER.info('    %s: %s', finding.rule, finding.detail)
        return EXIT_BLOCK

    if decision.outcome == ASK:
        LOGGER.info('ASK    over %d limit(s) -- needs Rakesh\'s approval', len(decision.asks))
        for finding in decision.asks:
            over = f' (over by Rs {finding.overage:,.2f})' if finding.overage is not None else ''
            LOGGER.info('  %s: %s%s', finding.rule, finding.detail, over)
        for flag in decision.flags:
            LOGGER.info('  flag %s', flag)
        return EXIT_ASK

    LOGGER.info('ALLOW  %s', decision.detail)
    for flag in decision.flags:
        LOGGER.info('  flag %s', flag)
    return EXIT_ALLOW


if __name__ == '__main__':
    sys.exit(main())
