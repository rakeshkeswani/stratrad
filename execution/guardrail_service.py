# -*- coding: utf-8 -*-
# execution/guardrail_service.py
"""ST-008: the guardrail service -- the only door to a Kite order.

Gathers the real state (committed money, loss tier, universe, sector, data ages), runs the pure rules in
execution/guardrails.py, and writes one `guardrail_events` row per check. Called twice per buy
(docs/DATA_MODEL.md s.5):

    check_before_proposal(request, run_picks=...)   # before Rakesh is asked; a block is logged, never shown
    check_before_order(proposal_id, approval_id)    # just before the order; recomputed from scratch

Committed money (ST-008 decision 2, 2026-10-30) = open stratrad lots at cost (entry price x qty)
+ the unfilled part of live entry orders + approved proposals with no order yet + earlier picks of the same run.
`manual` lots never count. The week (Mon-Sun, IST) counts lots entered this week plus everything pending.
ST-009 must keep a filled quantity in `positions` OR in an order's unfilled part, never both.

Soft limits ASK; an ASK becomes an ALLOW at `before_order` only when Rakesh's approval (an `audit_log` row, written
by ST-011) covers every current breach at no larger an overage. Otherwise it asks again.

Every source is reached through `Sources`, so the service is tested without a database (tests use a fake).

ST-009 adds the sell side and CLEARANCES. `check_before_sell` (sell orders and GTT place / modify / delete; hard
checks S01-S06 in execution/guardrails.py) and `authorise_buy` (the entry order) return a one-time `Clearance` on an
ALLOW, issued only after the `guardrail_events` row is written. execution/broker.py builds every Kite write from a
clearance alone and refuses one that is forged, used, stale or for another action -- so no order reaches Kite without
a guardrail check and its event row.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any, Iterable, Protocol
from zoneinfo import ZoneInfo

from core.config import REPO_ROOT
from core.trading_calendar import TradingCalendar
from execution.guardrails import (
    ALLOW,
    ASK,
    BLOCK,
    GTT_DELETE,
    SELL,
    STAGE_BEFORE_GTT,
    STAGE_BEFORE_ORDER,
    STAGE_BEFORE_PROPOSAL,
    STAGE_BEFORE_SELL,
    BuyRequest,
    Decision,
    Finding,
    LotState,
    PortfolioState,
    SellRequest,
    evaluate,
    evaluate_sell,
    load_limits,
)

logger = logging.getLogger(__name__)

IST = ZoneInfo('Asia/Kolkata')
CONFIG_PATH = REPO_ROOT / 'config' / 'guardrails.toml'

APPROVAL_ACTION = 'guardrail_approval'   # audit_log.action for Rakesh approving a lot's soft-limit breaches (ST-011)
OVERAGE_TOLERANCE = 0.01                 # rupees; rounding slack when comparing an overage to the approved one
RULE_NOT_APPROVED = 'G00'                # before_order only: the proposal must be approved by Rakesh
BUY = 'buy'                              # the entry order's clearance action
CLEARANCE_TTL = timedelta(seconds=60)    # a clearance older than this is refused: the state it was judged on has moved
_ISSUER = object()                       # only this module can mint a Clearance the broker accepts


@dataclass
class Clearance:
    """Permission for exactly one Kite write, minted by this module after a check and its event row.

    execution/broker.py builds the request from these fields only. `redeem()` is called by the broker and fails if the
    clearance is forged, used, stale or for another action.
    """

    action: str                       # 'buy' | 'sell' | 'gtt_place' | 'gtt_modify' | 'gtt_delete'
    symbol: str
    qty: int
    event_id: int
    issued_at: datetime
    position_id: int | None = None
    proposal_id: int | None = None
    trigger_price: float | None = None
    limit_price: float | None = None
    last_price: float | None = None
    gtt_id: int | None = None
    order_type: str = 'LIMIT'
    product: str = 'CNC'
    market_protection: float | None = None
    probe: bool = False
    _issuer: object = field(default=None, repr=False)
    used: bool = False

    def redeem(self, action: str, now: datetime | None = None) -> None:
        """Mark used; raise ClearanceRefused unless genuine, unused, fresh and for `action`."""
        if self._issuer is not _ISSUER:
            raise ClearanceRefused('clearance not issued by the guardrail service')
        if self.used:
            raise ClearanceRefused(f'clearance for event {self.event_id} already used')
        if self.action != action:
            raise ClearanceRefused(f'clearance is for {self.action}, not {action}')
        age = (now or datetime.now(IST)) - self.issued_at
        if age > CLEARANCE_TTL:
            raise ClearanceRefused(f'clearance is {age.total_seconds():.0f}s old (limit {CLEARANCE_TTL.seconds}s)')
        self.used = True


class ClearanceRefused(Exception):
    """The broker would not use a clearance."""


def _mint(**fields) -> Clearance:
    return Clearance(_issuer=_ISSUER, issued_at=datetime.now(IST), **fields)


# ---- what the sources return ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Commitment:
    """Money already spoken for: an open lot, a live entry order's unfilled part, or an approved proposal."""

    symbol: str
    strategy: str
    sector: str | None
    amount: float
    this_week: bool
    kind: str                 # 'lot' | 'order' | 'proposal' | 'run_pick'


@dataclass(frozen=True)
class Proposal:
    """The fields of a stored proposal that an order is built from."""

    id: int
    symbol: str
    strategy: str
    sector: str | None
    qty: int
    limit_price: float
    stop_price: float | None
    status: str


class Sources(Protocol):
    """Everything the service reads or writes. `DbSources` is the real one."""

    def commitments(self, week_start: date, exclude_proposal_id: int | None) -> list[Commitment]: ...
    def latest_snapshot(self) -> tuple[date, str] | None: ...
    def sector_of(self, symbol: str) -> str | None: ...
    def corporate_actions_fetched_on(self) -> date | None: ...
    def panel_latest(self, symbol: str) -> tuple[date, str | None] | None: ...
    def universe_on(self, on: date) -> tuple[date, set[str]] | None: ...
    def proposal(self, proposal_id: int) -> Proposal | None: ...
    def approval(self, approval_id: int) -> dict | None: ...
    def write_event(self, row: dict) -> int: ...
    # ST-009 (sell side)
    def lot(self, position_id: int) -> dict | None: ...               # status, strategy, symbol, qty
    def active_gtt(self, position_id: int) -> dict | None: ...        # the lot's active GTT: kite_gtt_id, trigger_price
    def gtt_is_a_lots(self, kite_gtt_id: int) -> bool: ...            # does gtt_orders know this GTT


class MarketView(Protocol):
    """Live facts from Kite the sell checks need (execution/broker.py implements it)."""

    def holding_qty(self, symbol: str) -> int | None: ...
    def last_price(self, symbol: str) -> float | None: ...
    def gtt_status(self, kite_gtt_id: int) -> str | None: ...


# ---- pure helpers ---------------------------------------------------------------------------------------------------

def today_ist(now: datetime | None = None) -> date:
    """The IST calendar date."""
    return (now or datetime.now(IST)).astimezone(IST).date()


def week_start(on: date) -> date:
    """Monday of `on`'s week."""
    return on - timedelta(days=on.weekday())


def aggregate(commitments: Iterable[Commitment]) -> dict[str, Any]:
    """Totals the rules need, from a list of commitments. Pure."""
    total, week = 0.0, 0.0
    by_strategy: dict[str, float] = {}
    by_sector: dict[str, float] = {}
    by_stock: dict[str, float] = {}
    for c in commitments:
        total += c.amount
        if c.this_week:
            week += c.amount
        by_strategy[c.strategy] = by_strategy.get(c.strategy, 0.0) + c.amount
        by_stock[c.symbol] = by_stock.get(c.symbol, 0.0) + c.amount
        if c.sector is not None:
            by_sector[c.sector] = by_sector.get(c.sector, 0.0) + c.amount
    return {'committed_total': round(total, 2), 'week_committed': round(week, 2),
            'committed_by_strategy': by_strategy, 'committed_by_sector': by_sector, 'committed_by_stock': by_stock}


def run_pick_commitments(picks: Iterable[BuyRequest]) -> list[Commitment]:
    """Earlier picks of the same strategy run, as commitments (they count this week)."""
    return [Commitment(symbol=p.symbol, strategy=p.strategy, sector=p.sector, amount=p.amount, this_week=True,
                       kind='run_pick') for p in picks]


def approval_details(proposal_id: int, decision: Decision) -> dict:
    """What ST-011 writes to `audit_log.details` when Rakesh approves an ASK (actor 'rakesh', APPROVAL_ACTION)."""
    return {'proposal_id': proposal_id,
            'breaches': [{'rule': f.rule, 'overage': f.overage, 'detail': f.detail} for f in decision.asks]}


def approval_covers(approval: dict | None, proposal_id: int, decision: Decision) -> bool:
    """True when `approval` (an audit_log row as a dict) approves every current breach at no larger an overage."""
    if not approval or approval.get('actor') != 'rakesh' or approval.get('action') != APPROVAL_ACTION:
        return False
    details = approval.get('details') or {}
    if details.get('proposal_id') != proposal_id:
        return False
    approved = {b['rule']: b.get('overage') for b in details.get('breaches') or []}
    for f in decision.asks:
        if f.rule not in approved:
            return False
        if f.overage is not None:
            limit = approved[f.rule]
            if limit is None or f.overage > float(limit) + OVERAGE_TOLERANCE:
                return False
    return True


# ---- state ---------------------------------------------------------------------------------------------------------

def gather_state(request: BuyRequest, *, sources: Sources, calendar: TradingCalendar, on: date,
                 run_picks: Iterable[BuyRequest] = (), exclude_proposal_id: int | None = None
                 ) -> tuple[PortfolioState, BuyRequest, dict]:
    """Read everything the rules need. Returns (state, request with series filled in, notes for the event row).

    Anything a source cannot answer stays None, which the rules treat as a block. Nothing here decides anything.
    """
    notes: dict[str, Any] = {'on': on.isoformat()}

    commitments = sources.commitments(week_start(on), exclude_proposal_id) + run_pick_commitments(run_picks)
    totals = aggregate(commitments)
    notes['commitments'] = {k: sum(1 for c in commitments if c.kind == k) for k in ('lot', 'order', 'proposal', 'run_pick')}

    loss_tier, snapshot_present = None, None
    snap = sources.latest_snapshot()
    prev = calendar.previous_trading_day(on)
    if snap is not None:
        loss_tier = snap[1]
        snapshot_present = None if prev is None else snap[0] >= prev
        notes['snapshot_date'] = snap[0].isoformat()
    else:
        snapshot_present = False

    panel = sources.panel_latest(request.symbol)
    panel_age, series = None, None
    if panel is not None:
        panel_age = calendar.sessions_between(panel[0], on)
        series = panel[1]
        notes['panel_last_date'] = panel[0].isoformat()

    ca_fetched = sources.corporate_actions_fetched_on()
    ca_age = calendar.sessions_between(ca_fetched, on) if ca_fetched is not None else None
    notes['corporate_actions_fetched_on'] = ca_fetched.isoformat() if ca_fetched else None

    in_universe, universe_age = None, None
    uni = sources.universe_on(on)
    if uni is not None:
        effective_from, members = uni
        in_universe = request.symbol in members
        universe_age = (on - effective_from).days
        notes['universe_effective_from'] = effective_from.isoformat()

    state = PortfolioState(
        **totals,
        loss_tier=loss_tier,
        in_universe=in_universe,
        universe_age_days=universe_age,
        upcoming_ex_dates_known=False,          # no source yet (2026-11-06); G12 blocks until stratrad's CA fetch
        days_to_corporate_action=None,
        panel_age_trading_days=panel_age,
        corporate_actions_age_trading_days=ca_age,
        snapshot_present=snapshot_present,
    )
    return state, dataclasses.replace(request, series=series), notes


# ---- the two checks ------------------------------------------------------------------------------------------------

def _json(obj: Any) -> Any:
    """Round-trip through JSON so dates and decimals become plain values."""
    return json.loads(json.dumps(obj, default=str))


def _event(stage: str, decision: Decision, request: BuyRequest, state: PortfolioState, limits: dict, notes: dict,
           proposal_id: int | None = None, approval_id: int | None = None) -> dict:
    return {
        'stage': stage,
        'rule': ','.join(decision.rules) or 'OK',
        'decision': decision.outcome,
        'proposal_id': proposal_id,
        'approval_id': approval_id,
        'limits': _json(limits),
        'context': _json({
            'request': dataclasses.asdict(request) | {'amount': request.amount},
            'state': dataclasses.asdict(state),
            'findings': [dataclasses.asdict(f) for f in decision.findings],
            'flags': list(decision.flags),
            'detail': decision.detail,
            'notes': notes,
        }),
    }


def check_before_proposal(request: BuyRequest, *, sources: Sources, run_picks: Iterable[BuyRequest] = (),
                          limits: dict | None = None, calendar: TradingCalendar | None = None,
                          on: date | None = None, write: bool = True) -> Decision:
    """Guardrails #1. The sector comes from the source, never from the caller. Writes one event unless write=False."""
    limits = limits or load_limits(CONFIG_PATH)
    calendar = calendar or TradingCalendar()
    on = on or today_ist()
    request = dataclasses.replace(request, sector=sources.sector_of(request.symbol))
    state, request, notes = gather_state(request, sources=sources, calendar=calendar, on=on, run_picks=run_picks)
    decision = evaluate(request, state, limits, STAGE_BEFORE_PROPOSAL)
    if write:
        notes['event_id'] = sources.write_event(_event(STAGE_BEFORE_PROPOSAL, decision, request, state, limits, notes))
    logger.info('guardrails before_proposal %s %s: %s %s', request.strategy, request.symbol, decision.outcome,
                decision.rule)
    return decision


def check_before_order(proposal_id: int, approval_id: int | None = None, *, sources: Sources,
                       limits: dict | None = None, calendar: TradingCalendar | None = None,
                       on: date | None = None, write: bool = True) -> Decision:
    """Guardrails #2, recomputed from the stored proposal. Only an ALLOW may be ordered.

    The proposal must be approved (G00). The proposal itself is left out of committed money so it is not counted
    twice. An ASK becomes an ALLOW only when `approval_id` covers every current breach (approval_covers).
    To place the order, use `authorise_buy`, which runs this check and mints the clearance the broker needs.
    """
    decision, _request, _event_id = _before_order(proposal_id, approval_id, sources=sources, limits=limits,
                                                  calendar=calendar, on=on, write=write)
    return decision


def authorise_buy(proposal_id: int, approval_id: int | None = None, *, sources: Sources,
                  limits: dict | None = None, calendar: TradingCalendar | None = None,
                  on: date | None = None) -> tuple[Decision, Clearance | None]:
    """`check_before_order`, plus a one-time clearance for the buy LIMIT order when it allows (ST-009)."""
    decision, request, event_id = _before_order(proposal_id, approval_id, sources=sources, limits=limits,
                                                calendar=calendar, on=on, write=True)
    if decision.outcome != ALLOW or event_id is None:
        return decision, None
    return decision, _mint(action=BUY, symbol=request.symbol, qty=request.qty, event_id=event_id,
                           proposal_id=proposal_id, limit_price=request.limit_price, order_type='LIMIT',
                           product=request.product)


def _before_order(proposal_id: int, approval_id: int | None, *, sources: Sources, limits: dict | None,
                  calendar: TradingCalendar | None, on: date | None, write: bool
                  ) -> tuple[Decision, BuyRequest, int | None]:
    """The body of check_before_order; also returns the request and the event id."""
    limits = limits or load_limits(CONFIG_PATH)
    calendar = calendar or TradingCalendar()
    on = on or today_ist()
    p = sources.proposal(proposal_id)
    if p is None:
        raise ValueError(f'proposal {proposal_id} not found')
    request = BuyRequest(symbol=p.symbol, strategy=p.strategy, qty=p.qty, limit_price=p.limit_price,
                         stop_price=p.stop_price, sector=p.sector)
    state, request, notes = gather_state(request, sources=sources, calendar=calendar, on=on,
                                         exclude_proposal_id=proposal_id)
    decision = evaluate(request, state, limits, STAGE_BEFORE_ORDER)

    used_approval = None
    if p.status != 'approved':
        g00 = Finding(rule=RULE_NOT_APPROVED, outcome=BLOCK, detail=f'proposal {proposal_id} is {p.status}, not approved')
        decision = Decision(outcome=BLOCK, detail=f'{RULE_NOT_APPROVED}: {g00.detail}',
                            findings=(g00,) + decision.findings, flags=decision.flags)
    elif decision.outcome == ASK and approval_id is not None:
        approval = sources.approval(approval_id)
        if approval_covers(approval, proposal_id, decision):
            used_approval = approval_id
            decision = Decision(outcome=ALLOW, detail=f'allowed on Rakesh\'s approval {approval_id}: {decision.detail}',
                                findings=decision.findings, flags=decision.flags + ('approved-overage',))
        else:
            notes['approval_not_covering'] = approval_id

    event_id = None
    if write:
        event_id = sources.write_event(_event(STAGE_BEFORE_ORDER, decision, request, state, limits, notes,
                                              proposal_id=proposal_id, approval_id=used_approval))
    logger.info('guardrails before_order proposal %s: %s %s', proposal_id, decision.outcome, decision.rule)
    return decision, request, event_id


# ---- sell side (ST-009) --------------------------------------------------------------------------------------------

def gather_lot_state(req: SellRequest, *, sources: Sources, market: MarketView) -> LotState:
    """Read what the sell rules need. A probe has no lot: only the holding, the price and the GTT's ownership."""
    holding = market.holding_qty(req.symbol)
    price = market.last_price(req.symbol)
    gtt_status = market.gtt_status(req.gtt_id) if req.gtt_id is not None else None
    belongs = sources.gtt_is_a_lots(req.gtt_id) if req.gtt_id is not None else None
    if req.probe or req.position_id is None:
        return LotState(holding_qty=holding, last_price=price, gtt_status=gtt_status, gtt_belongs_to_lot=belongs)
    row = sources.lot(req.position_id)
    if row is None:
        return LotState(holding_qty=holding, last_price=price, gtt_status=gtt_status, gtt_belongs_to_lot=belongs)
    active = sources.active_gtt(req.position_id)
    return LotState(exists=True, status=row['status'], strategy=row['strategy'], symbol=row['symbol'], qty=row['qty'],
                    active_trigger=None if active is None else float(active['trigger_price']),
                    gtt_status=gtt_status, gtt_belongs_to_lot=belongs, holding_qty=holding, last_price=price)


def check_before_sell(req: SellRequest, *, sources: Sources, market: MarketView, limits: dict | None = None
                      ) -> tuple[Decision, Clearance | None]:
    """Hard checks for a sell or a GTT action; one event row; a clearance on ALLOW (never without the row)."""
    limits = limits or load_limits(CONFIG_PATH)
    lot = gather_lot_state(req, sources=sources, market=market)
    decision = evaluate_sell(req, lot, limits)
    stage = STAGE_BEFORE_SELL if req.action == SELL else STAGE_BEFORE_GTT
    event_id = sources.write_event({
        'stage': stage,
        'rule': ','.join(f.rule for f in decision.findings) or 'OK',
        'decision': decision.outcome,
        'proposal_id': None,
        'approval_id': None,
        'limits': _json({'sell': limits['sell'], 'instrument': limits['instrument']}),
        'context': _json({'request': dataclasses.asdict(req), 'lot': dataclasses.asdict(lot),
                          'findings': [dataclasses.asdict(f) for f in decision.findings], 'detail': decision.detail}),
    })
    logger.info('guardrails %s %s %s: %s %s', stage, req.action, req.symbol, decision.outcome, decision.rule)
    if decision.outcome != ALLOW:
        return decision, None
    order_type = None if req.action == GTT_DELETE else req.order_type
    return decision, _mint(action=req.action, symbol=req.symbol, qty=req.qty, event_id=event_id,
                           position_id=req.position_id, trigger_price=req.trigger_price, gtt_id=req.gtt_id,
                           last_price=lot.last_price, order_type=order_type or 'MARKET', product=req.product,
                           market_protection=limits['sell']['market_protection'], probe=req.probe)


# ---- the real sources ----------------------------------------------------------------------------------------------

_COMMITMENTS_SQL = """
SELECT p.symbol, s.name, p.sector, p.entry_price * p.entry_qty AS amount, p.entry_date >= %(week)s AS this_week,
       'lot' AS kind
  FROM positions p JOIN strategies s ON s.id = p.strategy_id
 WHERE p.status = 'open' AND s.name <> 'manual'
UNION ALL
SELECT o.symbol, s.name, pr.sector, (o.qty - o.filled_qty) * COALESCE(o.price, pr.limit_price), true, 'order'
  FROM orders o
  JOIN proposals pr ON pr.id = o.proposal_id
  JOIN signals sg ON sg.id = pr.signal_id
  JOIN strategies s ON s.id = sg.strategy_id
 WHERE o.reason = 'entry' AND o.side = 'BUY' AND o.qty > o.filled_qty
   AND o.status NOT IN ('COMPLETE', 'CANCELLED', 'REJECTED')
   AND (%(exclude)s::bigint IS NULL OR pr.id <> %(exclude)s::bigint)
UNION ALL
SELECT pr.symbol, s.name, pr.sector, pr.amount, true, 'proposal'
  FROM proposals pr
  JOIN signals sg ON sg.id = pr.signal_id
  JOIN strategies s ON s.id = sg.strategy_id
 WHERE pr.status = 'approved'
   AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.proposal_id = pr.id)
   AND (%(exclude)s::bigint IS NULL OR pr.id <> %(exclude)s::bigint)
"""

_PROPOSAL_SQL = """
SELECT pr.id, pr.symbol, s.name, pr.sector, pr.qty, pr.limit_price, pr.stop_price, pr.status
  FROM proposals pr JOIN signals sg ON sg.id = pr.signal_id JOIN strategies s ON s.id = sg.strategy_id
 WHERE pr.id = %s
"""

_EVENT_SQL = """
INSERT INTO guardrail_events (stage, rule, decision, proposal_id, approval_id, limits, context)
VALUES (%(stage)s, %(rule)s, %(decision)s, %(proposal_id)s, %(approval_id)s, %(limits)s, %(context)s)
RETURNING id
"""


@dataclass
class DbSources:
    """Sources backed by the `stratrad` database (as stratrad_app), MyInvestIQ's tables (as stratrad_ro), the panel
    and the universe file. Use as a context manager so both connections close."""

    _app: Any = field(default=None, repr=False)
    _miq: Any = field(default=None, repr=False)

    def __enter__(self) -> 'DbSources':
        return self

    def __exit__(self, *exc) -> None:
        for conn in (self._app, self._miq):
            if conn is not None:
                conn.close()

    def _app_conn(self):
        if self._app is None:
            from core.db import connect
            self._app = connect('app')
        return self._app

    def _miq_conn(self):
        if self._miq is None:
            from core.db import connect
            self._miq = connect('myinvestiq', autocommit=True)
        return self._miq

    def commitments(self, week_start: date, exclude_proposal_id: int | None) -> list[Commitment]:
        with self._app_conn().cursor() as cur:
            cur.execute(_COMMITMENTS_SQL, {'week': week_start, 'exclude': exclude_proposal_id})
            rows = cur.fetchall()
        return [Commitment(symbol=r[0], strategy=r[1], sector=r[2], amount=float(r[3]), this_week=bool(r[4]),
                           kind=r[5]) for r in rows]

    def latest_snapshot(self) -> tuple[date, str] | None:
        with self._app_conn().cursor() as cur:
            cur.execute('SELECT snapshot_date, tier FROM daily_snapshots ORDER BY snapshot_date DESC LIMIT 1')
            row = cur.fetchone()
        return (row[0], row[1]) if row else None

    def sector_of(self, symbol: str) -> str | None:
        with self._miq_conn().cursor() as cur:
            cur.execute('SELECT our_sector FROM stock_master WHERE nse_symbol = %s AND is_active '
                        'ORDER BY last_updated DESC NULLS LAST LIMIT 1', (symbol,))
            row = cur.fetchone()
        return row[0] if row and row[0] else None

    def corporate_actions_fetched_on(self) -> date | None:
        with self._miq_conn().cursor() as cur:
            cur.execute('SELECT max(fetched_at) FROM corporate_actions')
            row = cur.fetchone()
        return row[0].date() if row and row[0] else None

    def panel_latest(self, symbol: str) -> tuple[date, str | None] | None:
        from core.panel import read_panel, table_dir
        years = sorted(int(p.name.split('=')[1]) for p in table_dir().glob('year=*') if p.is_dir())
        if not years:
            return None
        df = read_panel(start=date(years[-1], 1, 1), columns=['trade_date', 'symbol', 'series'])
        if df.empty:
            return None
        last = max(df['trade_date'])
        mine = df[df['symbol'] == symbol]
        series = None if mine.empty else str(mine.sort_values('trade_date').iloc[-1]['series'])
        return last, series

    def universe_on(self, on: date) -> tuple[date, set[str]] | None:
        from core.universe import load_membership, members_on
        try:
            members = members_on(load_membership(), on)
        except FileNotFoundError:
            return None
        if members.empty:
            return None
        return members['effective_from'].iloc[0], set(members['symbol']) | set(members['company_key'])

    def proposal(self, proposal_id: int) -> Proposal | None:
        with self._app_conn().cursor() as cur:
            cur.execute(_PROPOSAL_SQL, (proposal_id,))
            r = cur.fetchone()
        if r is None:
            return None
        return Proposal(id=r[0], symbol=r[1], strategy=r[2], sector=r[3], qty=r[4], limit_price=float(r[5]),
                        stop_price=None if r[6] is None else float(r[6]), status=r[7])

    def approval(self, approval_id: int) -> dict | None:
        with self._app_conn().cursor() as cur:
            cur.execute('SELECT actor, action, details FROM audit_log WHERE id = %s', (approval_id,))
            r = cur.fetchone()
        return {'actor': r[0], 'action': r[1], 'details': r[2]} if r else None

    def lot(self, position_id: int) -> dict | None:
        with self._app_conn().cursor() as cur:
            cur.execute('SELECT p.status, s.name, p.symbol, p.entry_qty FROM positions p '
                        'JOIN strategies s ON s.id = p.strategy_id WHERE p.id = %s', (position_id,))
            r = cur.fetchone()
        return None if r is None else {'status': r[0], 'strategy': r[1], 'symbol': r[2], 'qty': r[3]}

    def active_gtt(self, position_id: int) -> dict | None:
        with self._app_conn().cursor() as cur:
            cur.execute("SELECT kite_gtt_id, trigger_price FROM gtt_orders WHERE position_id = %s AND status = 'active' "
                        'ORDER BY id DESC LIMIT 1', (position_id,))
            r = cur.fetchone()
        return None if r is None else {'kite_gtt_id': r[0], 'trigger_price': float(r[1])}

    def gtt_is_a_lots(self, kite_gtt_id: int) -> bool:
        with self._app_conn().cursor() as cur:
            cur.execute('SELECT 1 FROM gtt_orders WHERE kite_gtt_id = %s LIMIT 1', (kite_gtt_id,))
            return cur.fetchone() is not None

    def write_event(self, row: dict) -> int:
        from psycopg.types.json import Jsonb
        dumps = partial(json.dumps, default=str)
        params = dict(row, limits=Jsonb(row['limits'], dumps=dumps), context=Jsonb(row['context'], dumps=dumps))
        conn = self._app_conn()
        with conn.cursor() as cur:
            cur.execute(_EVENT_SQL, params)
            event_id = cur.fetchone()[0]
        conn.commit()
        return event_id
