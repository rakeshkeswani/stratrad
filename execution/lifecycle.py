"""ST-009 piece 3: a stratrad lot from approved proposal to closed lot.

  place_entry(proposal)  approved proposal -> authorise_buy -> LIMIT CNC buy -> `orders` row (reason entry)
  sync()                 Kite's GTTs and orders -> our rows: a fill creates or grows the lot and places / resizes its
                         GTT; a triggered GTT records its sell; a completed sell closes the lot (stop / trail / time)
  trail(as_of)           after the close: stop = max(stop, highest official close since entry x (1 - trailing)),
                         sent only when it rises by `trailing_min_step`; never down (S03 enforces it too)
  time_exits(today)      lots held more than 12 months: MARKET sell (the GTT is deleted after the lot closes, S05)

The rules match backtest/engine.py: initial stop = fill x (1 - initial_stop); highest close starts at the fill price;
time exit = backtest/costs.py `is_long_term`; charges = backtest/costs.py (source 'estimate', decision 4).

Decisions 2026-11-10 (Rakesh): trailing uses official closes from stratrad's panel (a stale panel skips the day and
reports it -- a skipped day never lowers a stop); exit rules come from the APPROVED strategy row
(`strategies.config.exit`), never from the toml; a partial fill becomes the lot at once and grows, its one GTT resized.

Every Kite write goes through the guardrail gate (check_before_sell / authorise_buy -> clearance -> broker). The
database row for an order is written BEFORE the order is sent, so a crash after sending leaves a row to reconcile.
Nothing here runs on a schedule until piece 5.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Protocol

from backtest.costs import buy_charges, is_long_term, sell_charges
from execution.guardrails import ALLOW, GTT_DELETE, GTT_MODIFY, GTT_PLACE, SELL, Decision, SellRequest

logger = logging.getLogger(__name__)

TICK = 0.1                       # stops in whole 10 paise: a multiple of every NSE tick size in use
TERMINAL = frozenset({'COMPLETE', 'CANCELLED', 'REJECTED'})
NEW = 'NEW'                      # our row exists, the order is not yet acknowledged by Kite
SEND_FAILED = 'SEND_FAILED'      # Kite refused or the call failed: nothing to reconcile


# ---- rules (pure) ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ExitRules:
    initial_stop: float
    trailing_stop: float
    time_exit_months: int

    @classmethod
    def from_config(cls, config: dict | None) -> 'ExitRules':
        """From an approved strategy's `config.exit`. Anything missing or unsupported raises (fail closed)."""
        ex = (config or {}).get('exit') or {}
        if ex.get('mode') != 'lot_stop':
            raise ValueError(f"exit mode {ex.get('mode')!r} is not 'lot_stop'")
        months = int(ex['time_exit_months'])
        if months != 12:
            raise ValueError(f'time_exit_months {months}: only 12 is supported (backtest is_long_term)')
        rules = cls(float(ex['initial_stop']), float(ex['trailing_stop']), months)
        if not (0 < rules.initial_stop < 1 and 0 < rules.trailing_stop < 1):
            raise ValueError(f'stops must be fractions in (0, 1): {rules}')
        return rules


def floor_tick(price: float) -> float:
    return round(math.floor(price / TICK + 1e-9) * TICK, 2)


def initial_stop(avg_price: float, rules: ExitRules) -> float:
    return floor_tick(avg_price * (1 - rules.initial_stop))


def trailed_stop(current: float, high_close: float, rules: ExitRules, min_step: float) -> float | None:
    """The raised stop, or None when it would not rise by at least `min_step` (a fraction of the current stop)."""
    candidate = floor_tick(high_close * (1 - rules.trailing_stop))
    return candidate if candidate >= current * (1 + min_step) else None


def time_exit_due(entry_date: date, today: date) -> bool:
    return is_long_term(entry_date, today)


def stop_exit_reason(trigger: float, entry_price: float, rules: ExitRules) -> str:
    """'trail' if the stop had been raised above the initial stop when it fired, else 'stop'."""
    return 'trail' if trigger > initial_stop(entry_price, rules) + 1e-6 else 'stop'


def lot_pnl(entry_price: float, exit_price: float, qty: int) -> tuple[float, dict, dict]:
    """Realised P&L net of estimated charges, with the buy and sell charge estimates."""
    buy = buy_charges(entry_price * qty)
    sell = sell_charges(exit_price * qty, dp=True)
    return (exit_price - entry_price) * qty - buy['total'] - sell['total'], buy, sell


# ---- what lifecycle reads and writes --------------------------------------------------------------------------------

class Store(Protocol):
    """stratrad's tables. `DbStore` is the real one."""

    def proposal(self, proposal_id: int) -> dict | None: ...          # id, symbol, sector, strategy_id, qty, limit_price
    def entry_order(self, proposal_id: int) -> dict | None: ...
    def add_order(self, row: dict) -> int: ...
    def set_kite_order_id(self, order_id: int, kite_order_id: str | None, status: str) -> None: ...
    def update_order(self, order_id: int, status: str, filled_qty: int, avg_price: float | None, raw: dict) -> None: ...
    def link_order(self, order_id: int, position_id: int) -> None: ...
    def pending_orders(self) -> list[dict]: ...                       # not terminal, has a kite id
    def order_by_kite_id(self, kite_order_id: str) -> dict | None: ...
    def open_lots(self) -> list[dict]: ...                            # id, symbol, qty, entry_price, entry_date, config
    def lot(self, position_id: int) -> dict | None: ...
    def add_lot(self, row: dict) -> int: ...
    def resize_lot(self, position_id: int, qty: int, entry_price: float) -> None: ...
    def close_lot(self, position_id: int, exit_date: date, exit_price: float, exit_qty: int, reason: str,
                  pnl: float) -> None: ...
    def active_gtt_row(self, position_id: int) -> dict | None: ...    # id, kite_gtt_id, trigger_price
    def active_gtt_rows(self) -> list[dict]: ...                      # + position_id
    def last_gtt_trigger(self, position_id: int) -> float | None: ...  # latest GTT row's trigger, any status
    def add_gtt(self, position_id: int, kite_gtt_id: int, trigger: float) -> int: ...
    def update_gtt(self, gtt_row_id: int, status: str, trigger: float | None = None) -> None: ...
    def last_adjustment(self, position_id: int) -> date | None: ...
    def add_charges(self, order_id: int, charges: dict) -> None: ...
    def add_event(self, *, order_id: int | None = None, gtt_id: int | None = None, from_status: str | None,
                  to_status: str, detail: dict) -> None: ...
    def live_exit_order(self, position_id: int) -> dict | None: ...   # a SELL for the lot not yet terminal


class Prices(Protocol):
    def last_date(self) -> date | None: ...
    def closes(self, symbol: str, start: date, end: date) -> list[float]: ...


@dataclass
class Gate:
    """The guardrail door: `buy(proposal_id, approval_id)` and `sell(SellRequest)` -> (Decision, Clearance | None)."""

    buy: Callable[[int, int | None], tuple[Decision, Any]]
    sell: Callable[[SellRequest], tuple[Decision, Any]]


@dataclass
class Report:
    """What a step did and what needs attention (alerts go to the log until ST-011's Telegram)."""

    done: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)

    def ok(self, msg: str, *args) -> None:
        text = msg % args if args else msg
        self.done.append(text)
        logger.info(text)

    def alert(self, msg: str, *args) -> None:
        text = msg % args if args else msg
        self.alerts.append(text)
        logger.warning('ALERT %s', text)


# ---- the lifecycle --------------------------------------------------------------------------------------------------

class Lifecycle:
    def __init__(self, store: Store, broker: Any, gate: Gate, prices: Prices | None = None, *,
                 min_step: float = 0.005, today: Callable[[], date] | None = None):
        from execution.guardrail_service import today_ist
        self.store, self.broker, self.gate, self.prices = store, broker, gate, prices
        self.min_step = min_step
        self.today = today or today_ist

    # ---- entry ---------------------------------------------------------------------------------------------------

    def place_entry(self, proposal_id: int, approval_id: int | None = None, report: Report | None = None) -> int | None:
        """Buy for an approved proposal. Returns our order id, or None when the guardrails did not allow it."""
        report = report or Report()
        if self.store.entry_order(proposal_id) is not None:
            raise ValueError(f'proposal {proposal_id} already has an entry order')
        decision, clearance = self.gate.buy(proposal_id, approval_id)
        if decision.outcome != ALLOW or clearance is None:
            report.alert('proposal %s not ordered: %s %s', proposal_id, decision.outcome, decision.detail)
            return None
        oid = self.store.add_order({'proposal_id': proposal_id, 'position_id': None, 'symbol': clearance.symbol,
                                    'side': 'BUY', 'qty': clearance.qty, 'price': clearance.limit_price,
                                    'order_type': 'LIMIT', 'reason': 'entry', 'status': NEW})
        self._send(oid, lambda: self.broker.place_buy(clearance), report, f'buy {clearance.symbol} x{clearance.qty}')
        return oid

    def _send(self, oid: int, call: Callable[[], str], report: Report, what: str) -> str | None:
        try:
            kite_id = call()
        except Exception as e:                                   # noqa: BLE001 -- recorded, alerted, not retried
            self.store.set_kite_order_id(oid, None, SEND_FAILED)
            self.store.add_event(order_id=oid, from_status=NEW, to_status=SEND_FAILED, detail={'error': str(e)})
            report.alert('%s not sent: %s', what, e)
            return None
        self.store.set_kite_order_id(oid, str(kite_id), 'OPEN')
        self.store.add_event(order_id=oid, from_status=NEW, to_status='OPEN', detail={'kite_order_id': str(kite_id)})
        report.ok('%s sent: kite order %s', what, kite_id)
        return str(kite_id)

    # ---- sync: Kite -> our rows ---------------------------------------------------------------------------------------

    def sync(self, report: Report | None = None) -> Report:
        report = report or Report()
        self._sync_gtts(report)
        kite = {str(o['order_id']): o for o in self.broker.orders()}
        for row in self.store.pending_orders():
            k = kite.get(str(row['kite_order_id']))
            if k is None:
                report.alert('order %s (kite %s) not in today\'s Kite orders', row['id'], row['kite_order_id'])
                continue
            self._sync_order(row, k, report)
        return report

    def _sync_gtts(self, report: Report) -> None:
        for g in self.store.active_gtt_rows():
            status = self.broker.gtt_status(g['kite_gtt_id'])
            if status is None or status == 'active':
                continue
            self.store.update_gtt(g['id'], status)
            self.store.add_event(gtt_id=g['id'], from_status='active', to_status=status, detail={})
            if status != 'triggered':
                report.alert('GTT %s of lot %s is %s: the lot is unprotected', g['kite_gtt_id'], g['position_id'],
                             status)
                continue
            kite_order_id = _triggered_order_id(self.broker.gtt(g['kite_gtt_id']))
            lot = self.store.lot(g['position_id'])
            if kite_order_id is None or lot is None:
                report.alert('GTT %s triggered but its sell order is unknown (lot %s)', g['kite_gtt_id'],
                             g['position_id'])
                continue
            if self.store.order_by_kite_id(kite_order_id) is None:
                oid = self.store.add_order({'proposal_id': None, 'position_id': g['position_id'], 'symbol': lot['symbol'],
                                            'side': 'SELL', 'qty': lot['qty'], 'price': None, 'order_type': 'MARKET',
                                            'reason': 'stop', 'status': NEW})
                self.store.set_kite_order_id(oid, kite_order_id, 'OPEN')
                report.ok('GTT %s triggered at %s: sell order %s for lot %s', g['kite_gtt_id'], g['trigger_price'],
                          kite_order_id, g['position_id'])

    def _sync_order(self, row: dict, k: dict, report: Report) -> None:
        status = str(k.get('status') or '')
        filled = int(k.get('filled_quantity') or 0)
        avg = float(k['average_price']) if k.get('average_price') else None
        if status != row['status'] or filled != row['filled_qty']:
            self.store.update_order(row['id'], status, filled, avg, k)
            self.store.add_event(order_id=row['id'], from_status=row['status'], to_status=status,
                                 detail={'filled_qty': filled, 'avg_price': avg})
        if row['side'] == 'BUY':
            if filled > 0 and avg:
                self._lot_from_fill(row, filled, avg, report)
            if status in TERMINAL and filled > 0 and avg:
                self.store.add_charges(row['id'], buy_charges(avg * filled))
            if status in ('CANCELLED', 'REJECTED') and filled == 0:
                report.ok('entry order %s %s with nothing filled', row['id'], status)
            return
        if status in TERMINAL:
            self._close_from_sell(row, status, filled, avg, report)

    def _lot_from_fill(self, row: dict, filled: int, avg: float, report: Report) -> None:
        lot_id = row.get('position_id')
        if lot_id is None:
            p = self.store.proposal(row['proposal_id'])
            lot_id = self.store.add_lot({'strategy_id': p['strategy_id'], 'symbol': row['symbol'], 'sector': p['sector'],
                                         'entry_date': self.today(), 'entry_price': avg, 'entry_qty': filled})
            self.store.link_order(row['id'], lot_id)
            row['position_id'] = lot_id
            report.ok('lot %s: %s x%d @ %.2f', lot_id, row['symbol'], filled, avg)
        lot = self.store.lot(lot_id)
        if filled > lot['qty']:
            self.store.resize_lot(lot_id, filled, avg)
            report.ok('lot %s grew to %d @ %.2f', lot_id, filled, avg)
            lot = self.store.lot(lot_id)
        self._protect(lot, report)

    def _protect(self, lot: dict, report: Report) -> None:
        """Exactly one active GTT covering the whole lot, at least at the initial stop."""
        rules = ExitRules.from_config(lot['config'])
        floor = initial_stop(float(lot['entry_price']), rules)
        g = self.store.active_gtt_row(lot['id'])
        if g is None:
            req = SellRequest(GTT_PLACE, lot['symbol'], lot['qty'], position_id=lot['id'], trigger_price=floor)
            decision, c = self.gate.sell(req)
            if c is None:
                report.alert('lot %s UNPROTECTED: GTT refused: %s', lot['id'], decision.detail)
                return
            kite_id = self.broker.place_gtt(c)
            gid = self.store.add_gtt(lot['id'], kite_id, floor)
            self.store.add_event(gtt_id=gid, from_status=None, to_status='active', detail={'trigger': floor})
            report.ok('lot %s protected: GTT %s at %.2f', lot['id'], kite_id, floor)
            return
        kite_qty = _gtt_qty(self.broker.gtt(g['kite_gtt_id']))
        trigger = max(float(g['trigger_price']), floor)
        if kite_qty == lot['qty'] and trigger == float(g['trigger_price']):
            return
        req = SellRequest(GTT_MODIFY, lot['symbol'], lot['qty'], position_id=lot['id'], trigger_price=trigger,
                          gtt_id=g['kite_gtt_id'])
        decision, c = self.gate.sell(req)
        if c is None:
            report.alert('lot %s: GTT %s not resized to %d: %s', lot['id'], g['kite_gtt_id'], lot['qty'],
                         decision.detail)
            return
        self.broker.modify_gtt(c)
        self.store.update_gtt(g['id'], 'active', trigger)
        self.store.add_event(gtt_id=g['id'], from_status='active', to_status='active',
                             detail={'qty': lot['qty'], 'trigger': trigger})
        report.ok('lot %s: GTT %s now x%d at %.2f', lot['id'], g['kite_gtt_id'], lot['qty'], trigger)

    def _close_from_sell(self, row: dict, status: str, filled: int, avg: float | None, report: Report) -> None:
        lot = self.store.lot(row['position_id'])
        if lot is None or lot['status'] != 'open':
            return
        if status != 'COMPLETE' or filled < lot['qty'] or not avg:
            report.alert('lot %s: %s sell %s ended %s with %d of %d filled -- still open (backstop, piece 5)',
                         lot['id'], row['reason'], row['kite_order_id'], status, filled, lot['qty'])
            return
        rules = ExitRules.from_config(lot['config'])
        if row['reason'] == 'exit':
            reason = 'time'
        else:
            trigger = self.store.last_gtt_trigger(lot['id']) or 0.0
            reason = stop_exit_reason(float(trigger), float(lot['entry_price']), rules)
        pnl, _buy, sell = lot_pnl(float(lot['entry_price']), avg, lot['qty'])
        self.store.close_lot(lot['id'], self.today(), avg, lot['qty'], reason, pnl)
        self.store.add_charges(row['id'], sell)
        report.ok('lot %s closed (%s): %s x%d @ %.2f, P&L %.2f net of estimated charges', lot['id'], reason,
                  lot['symbol'], lot['qty'], avg, pnl)
        g = self.store.active_gtt_row(lot['id'])
        if g is not None:                                        # time exit: the stop is removed once the lot is closed
            decision, c = self.gate.sell(SellRequest(GTT_DELETE, lot['symbol'], lot['qty'], position_id=lot['id'],
                                                     gtt_id=g['kite_gtt_id']))
            if c is None:
                report.alert('lot %s closed but GTT %s not deleted: %s', lot['id'], g['kite_gtt_id'], decision.detail)
                return
            self.broker.delete_gtt(c)
            self.store.update_gtt(g['id'], 'deleted')
            self.store.add_event(gtt_id=g['id'], from_status='active', to_status='deleted', detail={})
            report.ok('lot %s: GTT %s deleted', lot['id'], g['kite_gtt_id'])

    # ---- after the close: trailing ------------------------------------------------------------------------------------

    def trail(self, as_of: date, report: Report | None = None) -> Report:
        report = report or Report()
        last = self.prices.last_date() if self.prices else None
        if last is None or last < as_of:
            report.alert('trailing skipped for %s: panel last date %s (stops unchanged, never lowered)', as_of, last)
            return report
        for lot in self.store.open_lots():
            g = self.store.active_gtt_row(lot['id'])
            if g is None:
                report.alert('lot %s has no active GTT: not trailed', lot['id'])
                continue
            rules = ExitRules.from_config(lot['config'])
            since = max(lot['entry_date'], self.store.last_adjustment(lot['id']) or lot['entry_date'])
            high = max([float(lot['entry_price'])] + self.prices.closes(lot['symbol'], since, as_of))
            new = trailed_stop(float(g['trigger_price']), high, rules, self.min_step)
            if new is None:
                continue
            decision, c = self.gate.sell(SellRequest(GTT_MODIFY, lot['symbol'], lot['qty'], position_id=lot['id'],
                                                     trigger_price=new, gtt_id=g['kite_gtt_id']))
            if c is None:
                report.alert('lot %s: stop not raised to %.2f: %s', lot['id'], new, decision.detail)
                continue
            self.broker.modify_gtt(c)
            self.store.update_gtt(g['id'], 'active', new)
            self.store.add_event(gtt_id=g['id'], from_status='active', to_status='active',
                                 detail={'trigger': new, 'previous': float(g['trigger_price']), 'high_close': high})
            report.ok('lot %s: stop %.2f -> %.2f (highest close %.2f)', lot['id'], float(g['trigger_price']), new, high)
        return report

    # ---- morning: time exits ------------------------------------------------------------------------------------------

    def time_exits(self, report: Report | None = None) -> Report:
        report = report or Report()
        today = self.today()
        for lot in self.store.open_lots():
            if not time_exit_due(lot['entry_date'], today) or self.store.live_exit_order(lot['id']) is not None:
                continue
            decision, c = self.gate.sell(SellRequest(SELL, lot['symbol'], lot['qty'], position_id=lot['id']))
            if c is None:
                report.alert('lot %s due for its 12-month exit but not sold: %s', lot['id'], decision.detail)
                continue
            oid = self.store.add_order({'proposal_id': None, 'position_id': lot['id'], 'symbol': lot['symbol'],
                                        'side': 'SELL', 'qty': lot['qty'], 'price': None, 'order_type': 'MARKET',
                                        'reason': 'exit', 'status': NEW})
            self._send(oid, lambda: self.broker.place_sell(c), report,
                       f"12-month exit lot {lot['id']} {lot['symbol']} x{lot['qty']}")
        return report


def _triggered_order_id(gtt: dict) -> str | None:
    """The sell order a triggered GTT placed: orders[0].result.order_result.order_id."""
    try:
        oid = gtt['orders'][0]['result']['order_result']['order_id']
    except (KeyError, IndexError, TypeError):
        return None
    return str(oid) if oid else None


def _gtt_qty(gtt: dict) -> int | None:
    try:
        return int(gtt['orders'][0]['quantity'])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


# ---- the real store, prices and gate --------------------------------------------------------------------------------

_LOT_SQL = """
SELECT p.id, p.symbol, p.entry_qty AS qty, p.entry_price, p.entry_date, p.status, s.name AS strategy, s.config
  FROM positions p JOIN strategies s ON s.id = p.strategy_id
"""
_ORDER_COLS = 'id, proposal_id, position_id, kite_order_id, symbol, side, qty, reason, status, filled_qty'
_NOT_LIVE = "('COMPLETE', 'CANCELLED', 'REJECTED', 'SEND_FAILED')"


class DbStore:
    """`Store` on the stratrad database (stratrad_app). Each write commits unless `commit=False` (tests roll back)."""

    def __init__(self, conn, commit: bool = True):
        self.conn, self._commit = conn, commit

    def _all(self, sql: str, params=()) -> list[dict]:
        from psycopg.rows import dict_row
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def _one(self, sql: str, params=()) -> dict | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    def _write(self, sql: str, params=()) -> Any:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            out = cur.fetchone()[0] if cur.description else None
        if self._commit:
            self.conn.commit()
        return out

    def proposal(self, proposal_id):
        return self._one('SELECT pr.id, pr.symbol, pr.sector, sg.strategy_id, pr.qty, pr.limit_price FROM proposals pr '
                         'JOIN signals sg ON sg.id = pr.signal_id WHERE pr.id = %s', (proposal_id,))

    def entry_order(self, proposal_id):
        return self._one(f"SELECT {_ORDER_COLS} FROM orders WHERE proposal_id = %s AND side = 'BUY' LIMIT 1",
                         (proposal_id,))

    def add_order(self, row):
        return self._write('INSERT INTO orders (proposal_id, position_id, symbol, side, qty, price, order_type, reason, '
                           'status) VALUES (%(proposal_id)s, %(position_id)s, %(symbol)s, %(side)s, %(qty)s, %(price)s, '
                           '%(order_type)s, %(reason)s, %(status)s) RETURNING id', row)

    def set_kite_order_id(self, order_id, kite_order_id, status):
        self._write('UPDATE orders SET kite_order_id = %s, status = %s WHERE id = %s', (kite_order_id, status, order_id))

    def update_order(self, order_id, status, filled_qty, avg_price, raw):
        from psycopg.types.json import Jsonb
        self._write('UPDATE orders SET status = %s, filled_qty = %s, avg_price = %s, raw = %s WHERE id = %s',
                    (status, filled_qty, avg_price, Jsonb(raw), order_id))

    def link_order(self, order_id, position_id):
        self._write('UPDATE orders SET position_id = %s WHERE id = %s', (position_id, order_id))

    def pending_orders(self):
        return self._all(f'SELECT {_ORDER_COLS} FROM orders WHERE kite_order_id IS NOT NULL '
                         f'AND status NOT IN {_NOT_LIVE} ORDER BY id')

    def order_by_kite_id(self, kite_order_id):
        return self._one(f'SELECT {_ORDER_COLS} FROM orders WHERE kite_order_id = %s', (kite_order_id,))

    def open_lots(self):
        return self._all(_LOT_SQL + " WHERE p.status = 'open' AND s.name <> 'manual' ORDER BY p.id")

    def lot(self, position_id):
        return self._one(_LOT_SQL + ' WHERE p.id = %s', (position_id,))

    def add_lot(self, row):
        return self._write('INSERT INTO positions (strategy_id, symbol, sector, entry_date, entry_price, entry_qty) '
                           'VALUES (%(strategy_id)s, %(symbol)s, %(sector)s, %(entry_date)s, %(entry_price)s, '
                           '%(entry_qty)s) RETURNING id', row)

    def resize_lot(self, position_id, qty, entry_price):
        self._write('UPDATE positions SET entry_qty = %s, entry_price = %s WHERE id = %s', (qty, entry_price, position_id))

    def close_lot(self, position_id, exit_date, exit_price, exit_qty, reason, pnl):
        self._write("UPDATE positions SET status = 'closed', exit_date = %s, exit_price = %s, exit_qty = %s, "
                    'exit_reason = %s, realised_pnl = %s WHERE id = %s',
                    (exit_date, exit_price, exit_qty, reason, round(pnl, 2), position_id))

    def active_gtt_row(self, position_id):
        return self._one("SELECT id, position_id, kite_gtt_id, trigger_price FROM gtt_orders WHERE position_id = %s "
                         "AND status = 'active' ORDER BY id DESC LIMIT 1", (position_id,))

    def active_gtt_rows(self):
        return self._all("SELECT id, position_id, kite_gtt_id, trigger_price FROM gtt_orders WHERE status = 'active' "
                         'ORDER BY id')

    def last_gtt_trigger(self, position_id):
        r = self._one('SELECT trigger_price FROM gtt_orders WHERE position_id = %s ORDER BY id DESC LIMIT 1',
                      (position_id,))
        return None if r is None else float(r['trigger_price'])

    def add_gtt(self, position_id, kite_gtt_id, trigger):
        return self._write("INSERT INTO gtt_orders (position_id, kite_gtt_id, trigger_price, limit_price, status) "
                           "VALUES (%s, %s, %s, NULL, 'active') RETURNING id", (position_id, kite_gtt_id, trigger))

    def update_gtt(self, gtt_row_id, status, trigger=None):
        self._write('UPDATE gtt_orders SET status = %s, trigger_price = coalesce(%s, trigger_price) WHERE id = %s',
                    (status, trigger, gtt_row_id))

    def last_adjustment(self, position_id):
        r = self._one('SELECT max(ex_date) AS d FROM position_adjustments WHERE position_id = %s', (position_id,))
        return None if r is None else r['d']

    def add_charges(self, order_id, charges):
        self._write("INSERT INTO order_charges (order_id, brokerage, stt, exchange_txn, sebi, stamp_duty, gst, total, "
                    "source) VALUES (%s, 0, %s, %s, %s, %s, %s, %s, 'estimate') "
                    'ON CONFLICT (order_id, source) DO NOTHING',
                    (order_id, charges['stt'], charges['txn'], charges['sebi'], charges['stamp'], charges['gst'],
                     charges['total']))

    def add_event(self, *, order_id=None, gtt_id=None, from_status, to_status, detail):
        from psycopg.types.json import Jsonb
        self._write('INSERT INTO order_events (order_id, gtt_id, from_status, to_status, detail) '
                    'VALUES (%s, %s, %s, %s, %s)', (order_id, gtt_id, from_status, to_status, Jsonb(detail, dumps=_dumps)))

    def live_exit_order(self, position_id):
        return self._one(f"SELECT {_ORDER_COLS} FROM orders WHERE position_id = %s AND side = 'SELL' "
                         f'AND status NOT IN {_NOT_LIVE} LIMIT 1', (position_id,))


def _dumps(obj) -> str:
    import json
    return json.dumps(obj, default=str)


class PanelPrices:
    """Official NSE closes from stratrad's panel (decision 1, 2026-11-10)."""

    def last_date(self) -> date | None:
        from core.panel import read_panel, table_dir
        years = sorted(int(p.name.split('=')[1]) for p in table_dir().glob('year=*') if p.is_dir())
        if not years:
            return None
        df = read_panel(start=date(years[-1], 1, 1), columns=['trade_date'])
        return None if df.empty else max(df['trade_date'])

    def closes(self, symbol: str, start: date, end: date) -> list[float]:
        from core.panel import read_panel
        df = read_panel(start=start, end=end, columns=['trade_date', 'symbol', 'close'])
        return [float(c) for c in df.loc[df['symbol'] == symbol, 'close'].dropna()]


def from_db(broker: Any, *, sources: Any = None, prices: Prices | None = None) -> Lifecycle:
    """The real wiring: guardrails on DbSources (its own events), lots on the same stratrad_app connection."""
    from execution.guardrail_service import CONFIG_PATH, DbSources, authorise_buy, check_before_sell
    from execution.guardrails import load_limits
    sources = sources or DbSources()
    limits = load_limits(CONFIG_PATH)
    gate = Gate(buy=lambda pid, aid: authorise_buy(pid, aid, sources=sources, limits=limits),
                sell=lambda req: check_before_sell(req, sources=sources, market=broker, limits=limits))
    return Lifecycle(DbStore(sources._app_conn()), broker, gate, prices or PanelPrices(),
                     min_step=limits['sell']['trailing_min_step'])
