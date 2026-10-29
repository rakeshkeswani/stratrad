# -*- coding: utf-8 -*-
# execution/guardrails.py
"""ST-008: the guardrail evaluator.

Pure rules: no I/O, no Kite, no database. Every limit and every piece of portfolio state arrives
as data, so each rule can be tested on its own. The caller that gathers state and writes
`guardrail_events` rows is execution/guardrail_service.py.

Fail-closed (Rakesh 2026-10-29): a missing or stale input BLOCKS. An unknown sector, an absent
daily snapshot or a stale panel can never produce an `allow`.

Rule ids match tech-debt/ST-008.md and the `rule` column of guardrail_events.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Stages a check can run at (docs/DATA_MODEL.md section 5).
STAGE_BEFORE_PROPOSAL = 'before_proposal'
STAGE_BEFORE_ORDER = 'before_order'

# Loss tiers, worst last.
TIER_OK = 'ok'
TIER_ALERT = 'alert'
TIER_NO_BUYS = 'no-buys'
TIER_HALT = 'halt'

MANUAL_STRATEGY = 'manual'


class GuardrailConfigError(ValueError):
    """Raised when config/guardrails.toml is missing, unreadable or incomplete."""


@dataclass(frozen=True)
class BuyRequest:
    """One proposed buy of one lot, as it would be sent to Kite."""

    symbol: str
    strategy: str
    qty: int
    limit_price: float
    stop_price: float | None
    sector: str | None = None
    series: str | None = None
    flags: tuple[str, ...] = ()          # SME / ASM / GSM surveillance flags on the instrument
    product: str = 'CNC'
    order_type: str = 'LIMIT'

    @property
    def amount(self) -> float:
        """Cost of this lot at the limit price, in rupees."""
        return self.qty * self.limit_price


@dataclass(frozen=True)
class PortfolioState:
    """Everything the rules need to know about the account and the data behind it.

    `None` anywhere means "not known" and blocks the request; it never passes a rule.
    Cost maps hold the cost of OPEN stratrad lots only -- `manual` lots are excluded by the caller
    and never count against any cap.
    """

    open_cost_total: float | None = None
    cost_by_strategy: dict[str, float] | None = None
    cost_by_sector: dict[str, float] | None = None
    cost_by_stock: dict[str, float] | None = None
    week_buys_amount: float | None = None
    loss_tier: str | None = None
    in_universe: bool | None = None
    days_to_corporate_action: int | None = None   # trading days to the next split/bonus/demerger ex-date; None = none due
    panel_age_trading_days: int | None = None
    corporate_actions_age_trading_days: int | None = None
    snapshot_present: bool | None = None
    news_checked: bool = False


@dataclass(frozen=True)
class Decision:
    """The outcome of one guardrail check."""

    allow: bool
    rule: str
    detail: str
    flags: tuple[str, ...] = field(default=())

    def __bool__(self) -> bool:
        return self.allow


def load_limits(path: Path) -> dict[str, Any]:
    """Read config/guardrails.toml.

    tomllib is read-only by design, which is what enforces "code never writes this file".
    Raises GuardrailConfigError when the file is absent or a required section is missing, so a
    deployment without limits cannot start up permissive.
    """
    if not path.is_file():
        raise GuardrailConfigError(f'guardrails config not found: {path}')
    try:
        with path.open('rb') as handle:
            limits = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise GuardrailConfigError(f'guardrails config is not valid TOML: {path}: {exc}') from exc

    required = ('capital', 'risk', 'tiers', 'instrument', 'corporate_actions', 'freshness', 'news')
    missing = [section for section in required if section not in limits]
    if missing:
        raise GuardrailConfigError(f'guardrails config {path} is missing sections: {", ".join(missing)}')
    return limits


def _block(rule: str, detail: str) -> Decision:
    return Decision(allow=False, rule=rule, detail=detail)


def _check_freshness(state: PortfolioState, limits: dict[str, Any]) -> Decision | None:
    """G13: the data behind every other rule must be current. Missing age = block."""
    fresh = limits['freshness']
    if state.panel_age_trading_days is None:
        return _block('G13', 'price panel age unknown')
    if state.panel_age_trading_days > fresh['panel_max_age_trading_days']:
        return _block('G13', f'price panel is {state.panel_age_trading_days} trading days old')
    if state.corporate_actions_age_trading_days is None:
        return _block('G13', 'corporate-actions age unknown')
    if state.corporate_actions_age_trading_days > fresh['corporate_actions_max_age_trading_days']:
        return _block('G13', f'corporate actions are {state.corporate_actions_age_trading_days} trading days old')
    if fresh['snapshot_required'] and not state.snapshot_present:
        return _block('G13', 'no daily_snapshots row for the last trading day')
    return None


def _check_tier(state: PortfolioState, limits: dict[str, Any]) -> Decision | None:
    """G11: the rolling 12-month loss tier gates new buys entirely."""
    if state.loss_tier is None:
        return _block('G11', 'loss tier unknown')
    if state.loss_tier == TIER_HALT:
        return _block('G11', 'loss tier halt: all trading stopped')
    if state.loss_tier == TIER_NO_BUYS:
        return _block('G11', 'loss tier no-buys: new buys blocked')
    if state.loss_tier not in (TIER_OK, TIER_ALERT):
        return _block('G11', f'unrecognised loss tier {state.loss_tier!r}')
    return None


def _check_instrument(request: BuyRequest, state: PortfolioState, limits: dict[str, Any]) -> Decision | None:
    """G01/G02: what may be bought, and how."""
    instrument = limits['instrument']
    if request.series is None:
        return _block('G01', 'instrument series unknown')
    if request.series not in instrument['allowed_series']:
        return _block('G01', f'series {request.series} is not tradable (allowed: {instrument["allowed_series"]})')
    blocked = sorted(set(request.flags) & set(instrument['blocked_flags']))
    if blocked:
        return _block('G01', f'{request.symbol} carries surveillance flags: {", ".join(blocked)}')
    if instrument['require_universe']:
        if state.in_universe is None:
            return _block('G01', f'universe membership unknown for {request.symbol}')
        if not state.in_universe:
            return _block('G01', f'{request.symbol} is outside the Nifty 500 universe')
    if request.product != instrument['product']:
        return _block('G02', f'product {request.product} is not {instrument["product"]}')
    if request.order_type != instrument['order_type']:
        return _block('G02', f'order type {request.order_type} is not {instrument["order_type"]}')
    return None


def _check_corporate_action(request: BuyRequest, state: PortfolioState, limits: dict[str, Any]) -> Decision | None:
    """G12: don't buy into a split/bonus/demerger ex-date (ST-019: the gap is not a loss)."""
    window = limits['corporate_actions']['blackout_trading_days']
    days = state.days_to_corporate_action
    if days is not None and 0 <= days <= window:
        return _block('G12', f'{request.symbol} has a corporate-action ex-date in {days} trading days')
    return None


def _check_risk(request: BuyRequest, limits: dict[str, Any]) -> Decision | None:
    """G06: every lot needs a stop, and the entry-to-stop risk is capped."""
    risk = limits['risk']
    if risk['stop_required'] and request.stop_price is None:
        return _block('G06', 'no stop price on the request')
    if request.stop_price is not None:
        if request.stop_price >= request.limit_price:
            return _block('G06', f'stop {request.stop_price} is not below the limit price {request.limit_price}')
        exposure = (request.limit_price - request.stop_price) * request.qty
        if exposure > risk['max_per_trade']:
            return _block('G06', f'entry-to-stop risk Rs {exposure:,.2f} exceeds Rs {risk["max_per_trade"]:,}')
    return None


def _check_sizing(request: BuyRequest, state: PortfolioState, limits: dict[str, Any]) -> Decision | None:
    """G05/G09/G07/G08/G04/G03: the caps, cheapest first, portfolio-wide last."""
    capital = limits['capital']
    amount = request.amount

    if amount > capital['max_lot_amount']:
        return _block('G05', f'lot of Rs {amount:,.2f} exceeds Rs {capital["max_lot_amount"]:,}')

    per_stock_cap = capital['cap'] * capital['per_stock_pct']
    if state.cost_by_stock is None:
        return _block('G09', 'open cost per stock unknown')
    held_stock = state.cost_by_stock.get(request.symbol, 0.0)
    if held_stock + amount > per_stock_cap:
        return _block('G09', f'{request.symbol} would reach Rs {held_stock + amount:,.2f} of Rs {per_stock_cap:,.2f}')

    if state.cost_by_strategy is None:
        return _block('G07', 'open cost per strategy unknown')
    held_strategy = state.cost_by_strategy.get(request.strategy, 0.0)
    if held_strategy + amount > capital['per_strategy']:
        return _block(
            'G07',
            f'strategy {request.strategy} would reach Rs {held_strategy + amount:,.2f} '
            f'of Rs {capital["per_strategy"]:,}',
        )

    if request.sector is None:
        return _block('G08', f'sector unknown for {request.symbol}')
    if state.cost_by_sector is None:
        return _block('G08', 'open cost per sector unknown')
    held_sector = state.cost_by_sector.get(request.sector, 0.0)
    if held_sector + amount > capital['per_sector']:
        return _block(
            'G08',
            f'sector {request.sector} would reach Rs {held_sector + amount:,.2f} of Rs {capital["per_sector"]:,}',
        )

    if state.week_buys_amount is None:
        return _block('G04', 'this week\'s buys unknown')
    if state.week_buys_amount + amount > capital['weekly_new_buys']:
        return _block(
            'G04',
            f'week would reach Rs {state.week_buys_amount + amount:,.2f} of Rs {capital["weekly_new_buys"]:,}',
        )

    if state.open_cost_total is None:
        return _block('G03', 'open cost of stratrad lots unknown')
    if state.open_cost_total + amount > capital['cap']:
        headroom = capital['cap'] - state.open_cost_total
        return _block(
            'G03',
            f'capital cap: headroom is Rs {headroom:,.2f}, this lot costs Rs {amount:,.2f} '
            f'(cap Rs {capital["cap"]:,} at cost)',
        )
    return None


def evaluate(request: BuyRequest, state: PortfolioState, limits: dict[str, Any], stage: str) -> Decision:
    """Run every rule against one proposed buy and return the first block, else an allow.

    Args:
        request: the proposed lot.
        state: the account and data state the rules are judged against; None fields block.
        limits: the parsed config/guardrails.toml (see load_limits).
        stage: STAGE_BEFORE_PROPOSAL or STAGE_BEFORE_ORDER; recorded by the caller.

    Returns:
        Decision. `rule` names the first rule that blocked, or '' on an allow. `flags` carries
        non-blocking observations (an alert tier, a news check that did not run).
    """
    if stage not in (STAGE_BEFORE_PROPOSAL, STAGE_BEFORE_ORDER):
        raise ValueError(f'unknown guardrail stage: {stage!r}')
    if request.strategy == MANUAL_STRATEGY:
        return _block('G01', "strategy 'manual' is Rakesh's own book; stratrad never orders for it")
    if request.qty <= 0 or request.limit_price <= 0:
        return _block('G05', f'qty {request.qty} and limit price {request.limit_price} must both be positive')

    for decision in (
        _check_freshness(state, limits),
        _check_tier(state, limits),
        _check_instrument(request, state, limits),
        _check_corporate_action(request, state, limits),
        _check_risk(request, limits),
        _check_sizing(request, state, limits),
    ):
        if decision is not None:
            return decision

    flags: list[str] = []
    if state.loss_tier == TIER_ALERT:
        flags.append('G11:alert')
    if limits['news']['enabled']:
        if not state.news_checked:
            return _block('G14', 'news check enabled but not performed')
    else:
        flags.append('G14:not-evaluated')

    return Decision(allow=True, rule='', detail=f'{request.symbol} Rs {request.amount:,.2f} allowed', flags=tuple(flags))
