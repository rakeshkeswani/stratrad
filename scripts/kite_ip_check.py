# -*- coding: utf-8 -*-
# scripts/kite_ip_check.py
"""
ST-007 -- prove that Kite accepts order calls from the static IP and refuses them from anywhere else, WITHOUT trading.

Sends one deliberately invalid order twice: through the static-IP proxy (KITE_ORDER_PROXY) and directly. The order can
never execute: quantity 0 (rejected by validation) and a BUY limit of Rs 1 (outside any circuit band). Expected:
- through the proxy: Kite rejects it for the ORDER (quantity/price) -- so the IP check passed;
- direct: Kite rejects it for the IP (not the registered static IP).
If Kite ever accepted it, the script cancels it at once and exits 3.

Needs today's Kite session (scripts.kite_check). Usage (prod, repo root): python -m scripts.kite_ip_check
"""

import sys

import requests

from core.config import KITE_API_KEY, KITE_ORDER_PROXY
from core.kite import API_ROOT, TIMEOUT, LoginNeeded, load_session

ORDER_PATH = '/orders/regular'
SAFE_ORDER = {'exchange': 'NSE', 'tradingsymbol': 'SBIN', 'transaction_type': 'BUY', 'order_type': 'LIMIT',
              'quantity': '0', 'price': '1', 'product': 'CNC', 'validity': 'DAY'}


def is_ip_refusal(message: str) -> bool:
    """Kite's static-IP refusal mentions the IP; an order-validation refusal does not."""
    m = message.lower()
    return ' ip' in f' {m}' and any(w in m for w in ('static', 'whitelist', 'registered', 'not allowed', 'not valid'))


def attempt(token: str, proxy: str | None, post=requests.post) -> dict:
    """POST the safe order once; returns {'status', 'http', 'error_type', 'message', 'order_id'}."""
    resp = post(f'{API_ROOT}{ORDER_PATH}', data=SAFE_ORDER, timeout=TIMEOUT,
                headers={'X-Kite-Version': '3', 'Authorization': f'token {KITE_API_KEY}:{token}'},
                proxies={'https': proxy} if proxy else None)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return {'status': body.get('status', ''), 'http': resp.status_code, 'error_type': body.get('error_type', ''),
            'message': body.get('message', ''), 'order_id': (body.get('data') or {}).get('order_id')}


def cancel(token: str, order_id: str, proxy: str | None) -> None:
    """Safety net only: cancel an order Kite should never have accepted."""
    requests.delete(f'{API_ROOT}{ORDER_PATH}/{order_id}', timeout=TIMEOUT,
                    headers={'X-Kite-Version': '3', 'Authorization': f'token {KITE_API_KEY}:{token}'},
                    proxies={'https': proxy} if proxy else None)


def verdict(via_proxy: dict, direct: dict) -> tuple[bool, list[str]]:
    """(passed, lines). Pass = proxy refused for the order (not the IP) AND direct refused for the IP."""
    lines, ok = [], True
    for name, r, want_ip in (('through proxy', via_proxy, False), ('direct', direct, True)):
        ip = is_ip_refusal(r['message'])
        good = r['status'] != 'success' and ip == want_ip
        ok &= good
        lines.append(f"{'ok  ' if good else 'FAIL'} {name}: HTTP {r['http']} {r['error_type']}: {r['message']}"
                     f"  -> {'IP refusal' if ip else 'order refusal'} (expected {'IP' if want_ip else 'order'})")
    return ok, lines


def main() -> int:
    """CLI entry point."""
    if not KITE_ORDER_PROXY:
        print('RESULT: FAIL -- KITE_ORDER_PROXY not set (.env)')
        return 1
    try:
        s = load_session()
    except LoginNeeded as e:
        print(f'RESULT: LOGIN NEEDED -- {e}')
        return 1
    via_proxy = attempt(s.access_token, KITE_ORDER_PROXY)
    direct = attempt(s.access_token, None)
    for r, proxy in ((via_proxy, KITE_ORDER_PROXY), (direct, None)):
        if r['status'] == 'success' and r['order_id']:
            cancel(s.access_token, r['order_id'], proxy)
            print(f"RESULT: DANGER -- Kite ACCEPTED the safe order ({'proxy' if proxy else 'direct'}); cancel sent")
            return 3
    ok, lines = verdict(via_proxy, direct)
    print('\n'.join(lines))
    print('RESULT: ' + ('PASS -- orders accepted only from the static IP' if ok else 'FAIL'))
    return 0 if ok else 2


if __name__ == '__main__':
    sys.exit(main())
