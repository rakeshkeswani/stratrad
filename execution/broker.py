"""The only module that writes to Kite (ST-009).

Reads (holdings, positions, orders, trades, GTTs, last price) go direct, like core.kite.Client. Every WRITE -- a buy
LIMIT order, a sell MARKET order, a GTT placed / modified / deleted -- goes through the static-IP proxy
(KITE_ORDER_PROXY, ST-007) and is built ONLY from a `Clearance` minted by execution/guardrail_service.py after its
guardrail check and `guardrail_events` row. The broker redeems the clearance first; a forged, used, stale or
wrong-action clearance raises ClearanceRefused and nothing is sent. There is no write method that takes loose values.

The access token is held in a header dict and never shown (repr masks the user id; errors carry only Kite's message).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import requests

from core.config import KITE_API_KEY, KITE_ORDER_PROXY
from core.kite import API_ROOT, TIMEOUT, KiteError, Session, _check, mask
from execution.guardrail_service import BUY, Clearance, ClearanceRefused
from execution.guardrails import GTT_DELETE, GTT_MODIFY, GTT_PLACE, SELL

logger = logging.getLogger(__name__)

EXCHANGE = 'NSE'
ORDERS_PATH = '/orders/regular'
GTT_PATH = '/gtt/triggers'


class ProxyMissing(Exception):
    """KITE_ORDER_PROXY is not set: Kite would reject the write (static-IP rule), so it is not attempted."""


class Broker:
    """Kite reads, and writes from clearances. Implements guardrail_service.MarketView."""

    def __init__(self, session: Session, *, api_key: str = KITE_API_KEY, proxy: str = KITE_ORDER_PROXY,
                 request=requests.request):
        self._auth = {'X-Kite-Version': '3', 'Authorization': f'token {api_key}:{session.access_token}'}
        self._proxy = proxy
        self._request = request
        self.user_id = session.user_id

    def __repr__(self) -> str:
        return f'Broker(user_id={mask(self.user_id)!r}, proxy={"set" if self._proxy else "unset"})'

    # ---- reads (direct) --------------------------------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        return _check(self._request('GET', f'{API_ROOT}{path}', headers=self._auth, params=params, timeout=TIMEOUT))

    def holdings(self) -> list:
        return self._get('/portfolio/holdings')

    def positions(self) -> dict:
        return self._get('/portfolio/positions')

    def orders(self) -> list:
        return self._get('/orders')

    def trades(self) -> list:
        return self._get('/trades')

    def gtts(self) -> list:
        return self._get(GTT_PATH)

    def gtt(self, kite_gtt_id: int) -> dict:
        return self._get(f'{GTT_PATH}/{int(kite_gtt_id)}')

    def ltp(self, symbol: str) -> float | None:
        key = f'{EXCHANGE}:{symbol}'
        data = self._get('/quote/ltp', params={'i': key})
        row = data.get(key) if isinstance(data, dict) else None
        return None if row is None else float(row['last_price'])

    # ---- MarketView (None = unknown; the sell rules block on unknown) -----------------------------------------------

    def _rows(self, symbol: str) -> tuple[list, list]:
        """Holdings rows and today's CNC position rows for `symbol`, on any exchange: demat shares are one pool, and
        Kite lists a holding under the exchange it was bought on (an NSE sell of BSE-bought shares is fine)."""
        held = [h for h in self.holdings() if h.get('tradingsymbol') == symbol]
        day = [p for p in self.positions().get('day', [])
               if p.get('tradingsymbol') == symbol and p.get('product') == 'CNC']
        return held, day

    def holding_qty(self, symbol: str) -> int | None:
        """Shares sellable today: demat + T1 holdings, plus today's net CNC buys (not in holdings until T+1)."""
        try:
            held, day = self._rows(symbol)
            return (sum(int(h.get('quantity') or 0) + int(h.get('t1_quantity') or 0) for h in held)
                    + sum(int(p.get('quantity') or 0) for p in day))
        except (KiteError, requests.RequestException, ValueError, TypeError, AttributeError) as e:
            logger.warning('holding_qty %s unknown: %s', symbol, e)
            return None

    def last_price(self, symbol: str) -> float | None:
        """Last price from the holding (or today's position) row. stratrad's Kite Connect app (Personal) has no
        market-quote permission (/quote -> 403, 2026-11-10); every sell and GTT is on a held stock, so the holdings
        row carries the price. None when the stock is not held -- the sell rules then block."""
        try:
            held, day = self._rows(symbol)
        except (KiteError, requests.RequestException, ValueError, TypeError, AttributeError) as e:
            logger.warning('last_price %s unknown: %s', symbol, e)
            return None
        for row in held + day:
            price = row.get('last_price')
            if price:
                return float(price)
        logger.warning('last_price %s unknown: not in holdings or today\'s positions', symbol)
        return None

    def gtt_status(self, kite_gtt_id: int) -> str | None:
        try:
            return self.gtt(kite_gtt_id).get('status')
        except (KiteError, requests.RequestException, ValueError, TypeError, AttributeError) as e:
            logger.warning('gtt_status %s unknown: %s', kite_gtt_id, e)
            return None

    def gtt_trigger(self, kite_gtt_id: int) -> float | None:
        try:
            return float(self.gtt(kite_gtt_id)['condition']['trigger_values'][0])
        except (KiteError, requests.RequestException, ValueError, TypeError, KeyError, IndexError) as e:
            logger.warning('gtt_trigger %s unknown: %s', kite_gtt_id, e)
            return None

    # ---- writes (proxy, from a clearance only) ----------------------------------------------------------------------

    def _write(self, method: str, path: str, form: dict | None, c: Clearance, action: str,
               now: datetime | None) -> dict:
        if not self._proxy:
            raise ProxyMissing('KITE_ORDER_PROXY not set: order calls are prod-only (ST-007)')
        c.redeem(action, now)                       # raises ClearanceRefused; nothing is sent
        logger.info('kite %s %s %s qty %s (event %s)', action, method, c.symbol, c.qty, c.event_id)
        resp = self._request(method, f'{API_ROOT}{path}', headers=self._auth, data=form, timeout=TIMEOUT,
                             proxies={'https': self._proxy})
        return _check(resp)

    def place_buy(self, c: Clearance, *, now: datetime | None = None) -> str:
        """LIMIT CNC buy, DAY. Returns Kite's order_id."""
        _require(c.limit_price is not None, 'buy clearance without a limit price')
        form = {'exchange': EXCHANGE, 'tradingsymbol': c.symbol, 'transaction_type': 'BUY', 'order_type': 'LIMIT',
                'quantity': c.qty, 'product': c.product, 'price': c.limit_price, 'validity': 'DAY',
                'tag': f'st{c.event_id}'[:20]}
        return self._write('POST', ORDERS_PATH, form, c, BUY, now)['order_id']

    def place_sell(self, c: Clearance, *, now: datetime | None = None) -> str:
        """MARKET CNC sell with market protection (time exit, backstop). Returns Kite's order_id."""
        form = {'exchange': EXCHANGE, 'tradingsymbol': c.symbol, 'transaction_type': 'SELL', 'order_type': 'MARKET',
                'quantity': c.qty, 'product': c.product, 'validity': 'DAY', 'tag': f'st{c.event_id}'[:20]}
        if c.market_protection is not None:
            form['market_protection'] = c.market_protection
        return self._write('POST', ORDERS_PATH, form, c, SELL, now)['order_id']

    def place_gtt(self, c: Clearance, *, now: datetime | None = None) -> int:
        """Single-leg GTT: when LTP <= trigger, a MARKET CNC sell of the lot. Returns Kite's trigger_id."""
        return int(self._write('POST', GTT_PATH, _gtt_form(c), c, GTT_PLACE, now)['trigger_id'])

    def modify_gtt(self, c: Clearance, *, now: datetime | None = None) -> int:
        """Raise the lot's GTT trigger (trailing). Returns Kite's trigger_id."""
        _require(c.gtt_id is not None, 'modify clearance without a gtt id')
        return int(self._write('PUT', f'{GTT_PATH}/{int(c.gtt_id)}', _gtt_form(c), c, GTT_MODIFY, now)['trigger_id'])

    def delete_gtt(self, c: Clearance, *, now: datetime | None = None) -> int:
        _require(c.gtt_id is not None, 'delete clearance without a gtt id')
        return int(self._write('DELETE', f'{GTT_PATH}/{int(c.gtt_id)}', None, c, GTT_DELETE, now)['trigger_id'])


def _require(ok: bool, why: str) -> None:
    if not ok:
        raise ClearanceRefused(why)


def _gtt_form(c: Clearance) -> dict:
    _require(c.trigger_price is not None and c.last_price is not None, 'GTT clearance without trigger or last price')
    order = {'exchange': EXCHANGE, 'tradingsymbol': c.symbol, 'transaction_type': 'SELL', 'quantity': c.qty,
             'order_type': 'MARKET', 'product': c.product, 'price': 0}
    if c.market_protection is not None:
        order['market_protection'] = c.market_protection
    condition = {'exchange': EXCHANGE, 'tradingsymbol': c.symbol, 'trigger_values': [c.trigger_price],
                 'last_price': c.last_price}
    return {'type': 'single', 'condition': json.dumps(condition), 'orders': json.dumps([order])}
