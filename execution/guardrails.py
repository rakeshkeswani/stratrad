# -*- coding: utf-8 -*-
# execution/guardrails.py
"""ST-008: the guardrail evaluator.

Pure rules: no I/O, no Kite, no database. Every limit and every piece of portfolio state arrives
as data, so each rule can be tested on its own. The caller that gathers state and writes
`guardrail_events` rows is execution/guardrail_service.py.

Three outcomes (Rakesh 2026-10-30, tech-debt/ST-008.md "Decided 2026-10-30"):
- BLOCK -- a hard rule failed: the order would be invalid (wrong instrument, no stop, stale or
  missing data) or the halt loss tier is active. Never shown to Rakesh as a question.
- ASK   -- every hard rule passed, but one or more LIMITS would be exceeded. Limits are soft:
  Rakesh may approve the lot anyway. One ask lists every breach.
- ALLOW -- nothing failed and nothing was exceeded.

Fail-closed (Rakesh 2026-10-29): a missing input BLOCKS, even inside a soft rule. A limit can only
be asked about when the number is known and exceeds it.

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

# Outcomes; the same strings go into guardrail_events.decision (migration 002 adds 'ask').
ALLOW = 'allow'
ASK = 'ask'
BLOCK = 'block'

# Loss tiers, spelled as daily_snapshots.tier allows them (migration 001).
TIER_OK = 'ok'
TIER_ALERT = 'alert'
TIER_NO_BUYS = 'no_buys'
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

    The committed_* fields hold COMMITTED money for stratrad strategies (ST-008 decision 2):
    open lots at cost + approved proposals not yet filled + live entry orders not yet filled +
    (before_proposal) proposals created earlier in the same run. The request being checked is
    never included. `manual` lots are excluded by the caller and never count.
    """

    committed_total: float | None = None
    committed_by_strategy: dict[str, float] | None = None
    committed_by_sector: dict[str, float] | None = None
    committed_by_stock: dict[str, float] | None = None
    week_committed: float | None = None          # new buys committed this week (Mon-Sun, IST)
    loss_tier: str | None = None
    in_universe: bool | None = None
    universe_age_days: int | None = None         # calendar days since the universe review in force took effect
    upcoming_ex_dates_known: bool | None = None  # True only when a source of announced FUTURE ex-dates was read
    days_to_corporate_action: int | None = None  # trading days to the next split/bonus/demerger ex-date; None = none due
    panel_age_trading_days: int | None = None
    corporate_actions_age_trading_days: int | None = None
    snapshot_present: bool | None = None
    surveillance_checked: bool = False
    news_checked: bool = False


@dataclass(frozen=True)
class Finding:
    """One rule that did not pass. `overage` is the rupee amount over the limit, when it has one."""

    rule: str
    outcome: str            # BLOCK or ASK
    detail: str
    overage: float | None = None


@dataclass(frozen=True)
class Decision:
    """The outcome of one guardrail check.

    `findings` holds every rule that did not pass, in evaluation order -- the blocks and the asks.
    """

    outcome: str
    detail: str
    findings: tuple[Finding, ...] = ()
    flags: tuple[str, ...] = field(default=())

    @property
    def allow(self) -> bool:
        return self.outcome == ALLOW

    @property
    def blocks(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.outcome == BLOCK)

    @property
    def asks(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.outcome == ASK)

    @property
    def rule(self) -> str:
        """The rule that decided it: the first block, else the first ask, else ''."""
        if self.blocks:
            return self.blocks[0].rule
        if self.asks:
            return self.asks[0].rule
        return ''

    @property
    def rules(self) -> tuple[str, ...]:
        """Every rule behind the outcome: the blocks on a block, every limit exceeded on an ask."""
        source = self.blocks if self.outcome == BLOCK else self.asks
        return tuple(f.rule for f in source)


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


def _block(rule: str, detail: str) -> Finding:
    return Finding(rule=rule, outcome=BLOCK, detail=detail)


def _ask(rule: str, detail: str, overage: float | None = None) -> Finding:
    return Finding(rule=rule, outcome=ASK, detail=detail, overage=overage)


# ---- hard rules: validity, and the halt tier -------------------------------------------------------------------------

def _check_order_values(request: BuyRequest, state: PortfolioState, limits: dict[str, Any]) -> list[Finding]:
    """G02: an order with no quantity or no price is not an order."""
    if request.qty <= 0 or request.limit_price <= 0:
        return [_block('G02', f'qty {request.qty} and limit price {request.limit_price} must both be positive')]
    return []


def _check_freshness(request: BuyRequest, state: PortfolioState, limits: dict[str, Any]) -> list[Finding]:
    """G13 (hard): the data behind every other rule must be current. Missing age = block."""
    fresh = limits['freshness']
    if state.panel_age_trading_days is None:
        return [_block('G13', 'price panel age unknown')]
    if state.panel_age_trading_days > fresh['panel_max_age_trading_days']:
        return [_block('G13', f'price panel is {state.panel_age_trading_days} trading days old')]
    if state.corporate_actions_age_trading_days is None:
        return [_block('G13', 'corporate-actions age unknown')]
    if state.corporate_actions_age_trading_days > fresh['corporate_actions_max_age_trading_days']:
        return [_block('G13', f'corporate actions are {state.corporate_actions_age_trading_days} trading days old')]
    if fresh['snapshot_required'] and not state.snapshot_present:
        return [_block('G13', 'no daily_snapshots row for the last trading day')]
    if limits['instrument']['require_universe']:
        if state.universe_age_days is None:
            return [_block('G13', 'universe review in force unknown')]
        if state.universe_age_days > fresh['universe_max_age_days']:
            return [_block('G13', f'universe review in force took effect {state.universe_age_days} days ago '
                                  f'(a semi-annual review was missed)')]
    return []


def _check_tier(request: BuyRequest, state: PortfolioState, limits: dict[str, Any]) -> list[Finding]:
    """G11: halt blocks (hard); no_buys asks (soft); alert only flags; unknown blocks."""
    if state.loss_tier is None:
        return [_block('G11', 'loss tier unknown')]
    if state.loss_tier == TIER_HALT:
        return [_block('G11', 'loss tier halt: rolling 12-month loss past the halt threshold; all trading stopped')]
    if state.loss_tier == TIER_NO_BUYS:
        return [_ask('G11', 'loss tier no_buys: rolling 12-month loss past the no-new-buys threshold')]
    if state.loss_tier not in (TIER_OK, TIER_ALERT):
        return [_block('G11', f'unrecognised loss tier {state.loss_tier!r}')]
    return []


def _check_instrument(request: BuyRequest, state: PortfolioState, limits: dict[str, Any]) -> list[Finding]:
    """G01/G02 (hard): what may be bought, and how."""
    instrument = limits['instrument']
    if request.strategy == MANUAL_STRATEGY:
        return [_block('G01', "strategy 'manual' is Rakesh's own book; stratrad never orders for it")]
    if request.series is None:
        return [_block('G01', 'instrument series unknown')]
    if request.series not in instrument['allowed_series']:
        return [_block('G01', f'series {request.series} is not tradable (allowed: {instrument["allowed_series"]})')]
    if instrument['surveillance_check'] and not state.surveillance_checked:
        return [_block('G01', 'surveillance check enabled but the ASM/GSM lists were not read')]
    flagged = sorted(set(request.flags) & set(instrument['blocked_flags']))
    if flagged:
        return [_block('G01', f'{request.symbol} carries surveillance flags: {", ".join(flagged)}')]
    if instrument['require_universe']:
        if state.in_universe is None:
            return [_block('G01', f'universe membership unknown for {request.symbol}')]
        if not state.in_universe:
            return [_block('G01', f'{request.symbol} is outside the Nifty 500 universe')]
    if request.product != instrument['product']:
        return [_block('G02', f'product {request.product} is not {instrument["product"]}')]
    if request.order_type != instrument['order_type']:
        return [_block('G02', f'order type {request.order_type} is not {instrument["order_type"]}')]
    return []


def _check_stop(request: BuyRequest, state: PortfolioState, limits: dict[str, Any]) -> list[Finding]:
    """G06a (hard): every lot gets a GTT stop, so the stop must exist and sit below the entry."""
    if limits['risk']['stop_required'] and request.stop_price is None:
        return [_block('G06', 'no stop price on the request')]
    if request.stop_price is not None and request.stop_price >= request.limit_price:
        return [_block('G06', f'stop {request.stop_price} is not below the limit price {request.limit_price}')]
    return []


def _check_news(request: BuyRequest, state: PortfolioState, limits: dict[str, Any]) -> list[Finding]:
    """G14 (hard when enabled): a check that is switched on must actually have run."""
    if limits['news']['enabled'] and not state.news_checked:
        return [_block('G14', 'news check enabled but not performed')]
    return []


# ---- soft rules: limits, subject to Rakesh's approval ----------------------------------------------------------------

def _check_corporate_action(request: BuyRequest, state: PortfolioState, limits: dict[str, Any]) -> list[Finding]:
    """G12 (soft): a split/bonus/demerger ex-date within the window (ST-019: the gap is not a loss).

    Fail-closed (2026-11-06): with no source of announced future ex-dates, "none known" is not "none due",
    so it blocks. MyInvestIQ stores only past ex-dates; stratrad's own corporate-action fetch is to keep them.
    """
    if not state.upcoming_ex_dates_known:
        return [_block('G12', 'upcoming ex-dates unknown (no source of announced corporate actions)')]
    window = limits['corporate_actions']['blackout_trading_days']
    days = state.days_to_corporate_action
    if days is not None and 0 <= days <= window:
        return [_ask('G12', f'{request.symbol} has a corporate-action ex-date in {days} trading days '
                            f'(blackout {window})')]
    return []


def _check_risk(request: BuyRequest, state: PortfolioState, limits: dict[str, Any]) -> list[Finding]:
    """G06b (soft): entry-to-stop risk. Only measurable once G06a has passed."""
    if request.stop_price is None or request.stop_price >= request.limit_price:
        return []
    cap = limits['risk']['max_per_trade']
    exposure = (request.limit_price - request.stop_price) * request.qty
    if exposure > cap:
        return [_ask('G06', f'entry-to-stop risk Rs {exposure:,.2f} exceeds Rs {cap:,}', exposure - cap)]
    return []


def _check_lot_size(request: BuyRequest, state: PortfolioState, limits: dict[str, Any]) -> list[Finding]:
    """G05 (soft): one lot's cost."""
    cap = limits['capital']['max_lot_amount']
    if request.amount > cap:
        return [_ask('G05', f'lot of Rs {request.amount:,.2f} exceeds Rs {cap:,}', request.amount - cap)]
    return []


def _check_per_stock(request: BuyRequest, state: PortfolioState, limits: dict[str, Any]) -> list[Finding]:
    """G09 (soft): one stock across ALL stratrad strategies, as a fraction of the capital cap."""
    capital = limits['capital']
    cap = capital['cap'] * capital['per_stock_pct']
    if state.committed_by_stock is None:
        return [_block('G09', 'committed money per stock unknown')]
    total = state.committed_by_stock.get(request.symbol, 0.0) + request.amount
    if total > cap:
        return [_ask('G09', f'{request.symbol} would reach Rs {total:,.2f} of Rs {cap:,.2f}', total - cap)]
    return []


def _check_per_strategy(request: BuyRequest, state: PortfolioState, limits: dict[str, Any]) -> list[Finding]:
    """G07 (soft): one strategy's committed money."""
    cap = limits['capital']['per_strategy']
    if state.committed_by_strategy is None:
        return [_block('G07', 'committed money per strategy unknown')]
    total = state.committed_by_strategy.get(request.strategy, 0.0) + request.amount
    if total > cap:
        return [_ask('G07', f'strategy {request.strategy} would reach Rs {total:,.2f} of Rs {cap:,}', total - cap)]
    return []


def _check_per_sector(request: BuyRequest, state: PortfolioState, limits: dict[str, Any]) -> list[Finding]:
    """G08 (soft): one sector's committed money. An unknown sector blocks (fail-closed)."""
    cap = limits['capital']['per_sector']
    if request.sector is None:
        return [_block('G08', f'sector unknown for {request.symbol}')]
    if state.committed_by_sector is None:
        return [_block('G08', 'committed money per sector unknown')]
    total = state.committed_by_sector.get(request.sector, 0.0) + request.amount
    if total > cap:
        return [_ask('G08', f'sector {request.sector} would reach Rs {total:,.2f} of Rs {cap:,}', total - cap)]
    return []


def _check_weekly(request: BuyRequest, state: PortfolioState, limits: dict[str, Any]) -> list[Finding]:
    """G04 (soft): the week's new-buy pace."""
    cap = limits['capital']['weekly_new_buys']
    if state.week_committed is None:
        return [_block('G04', "this week's committed buys unknown")]
    total = state.week_committed + request.amount
    if total > cap:
        return [_ask('G04', f'week would reach Rs {total:,.2f} of Rs {cap:,}', total - cap)]
    return []


def _check_capital(request: BuyRequest, state: PortfolioState, limits: dict[str, Any]) -> list[Finding]:
    """G03 (soft): the buy kitty, at cost of committed money. Headroom returns when a lot exits."""
    cap = limits['capital']['cap']
    if state.committed_total is None:
        return [_block('G03', 'committed money for stratrad unknown')]
    total = state.committed_total + request.amount
    if total > cap:
        headroom = cap - state.committed_total
        return [_ask('G03', f'kitty: headroom Rs {headroom:,.2f}, this lot costs Rs {request.amount:,.2f}, '
                            f'would reach Rs {total:,.2f} of Rs {cap:,} at cost', total - cap)]
    return []


# Evaluation order. Every rule runs (they are pure and cheap), so an ask lists every breach and a
# block's detail lists every hard failure; the order decides which rule is named first. Hard rules
# lead, data freshness first of all, so a stale-data block is never masked by anything else.
RULE_CHECKS = (
    _check_order_values,
    _check_freshness,
    _check_tier,
    _check_instrument,
    _check_stop,
    _check_news,
    _check_corporate_action,
    _check_risk,
    _check_lot_size,
    _check_per_stock,
    _check_per_strategy,
    _check_per_sector,
    _check_weekly,
    _check_capital,
)


def evaluate(request: BuyRequest, state: PortfolioState, limits: dict[str, Any], stage: str) -> Decision:
    """Run every rule against one proposed buy.

    Args:
        request: the proposed lot.
        state: committed money and data state; None fields block.
        limits: the parsed config/guardrails.toml (see load_limits).
        stage: STAGE_BEFORE_PROPOSAL or STAGE_BEFORE_ORDER; recorded by the caller.

    Returns:
        Decision with outcome BLOCK if any hard rule failed (or any input was missing), else ASK if
        any limit would be exceeded, else ALLOW. `findings` holds every rule that did not pass.
        `flags` carries non-blocking observations (an alert tier, a news check that did not run).
    """
    if stage not in (STAGE_BEFORE_PROPOSAL, STAGE_BEFORE_ORDER):
        raise ValueError(f'unknown guardrail stage: {stage!r}')

    findings: list[Finding] = []
    if request.qty > 0 and request.limit_price > 0:
        for check in RULE_CHECKS:
            findings.extend(check(request, state, limits))
    else:
        # Amount-based rules are meaningless without a quantity and price; report the validity failure alone.
        findings.extend(_check_order_values(request, state, limits))

    flags: list[str] = []
    if state.loss_tier == TIER_ALERT:
        flags.append('G11:alert')
    if not limits['instrument']['surveillance_check']:
        flags.append('G01:surveillance-not-evaluated')
    if not limits['news']['enabled']:
        flags.append('G14:not-evaluated')

    blocks = [f for f in findings if f.outcome == BLOCK]
    asks = [f for f in findings if f.outcome == ASK]
    if blocks:
        detail = '; '.join(f'{f.rule}: {f.detail}' for f in blocks)
        return Decision(outcome=BLOCK, detail=detail, findings=tuple(findings), flags=tuple(flags))
    if asks:
        detail = '; '.join(f'{f.rule}: {f.detail}' for f in asks)
        return Decision(outcome=ASK, detail=detail, findings=tuple(findings), flags=tuple(flags))
    return Decision(outcome=ALLOW, detail=f'{request.symbol} Rs {request.amount:,.2f} allowed',
                    findings=(), flags=tuple(flags))
