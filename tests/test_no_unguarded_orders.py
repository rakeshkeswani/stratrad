# -*- coding: utf-8 -*-
# tests/test_no_unguarded_orders.py
"""ST-008 falsifying clause: no module may reach a Kite order endpoint without going through the guardrail service.

Any Python file that names an order endpoint must import execution.guardrail_service, or be on the reviewed list
below with its reason. A new order module (ST-009) that skips the service fails this test.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CODE_DIRS = ('core', 'execution', 'ingestion', 'scripts', 'backtest', 'notify', 'ml')

# Kite Connect order and GTT endpoints, and the kiteconnect library's order methods.
ORDER_PATTERN = re.compile(r"""['"]/(orders|gtt)(/|['"])|\.place_order\(|\.modify_order\(|\.place_gtt\(""")

REVIEWED = {
    # ST-007 diagnostic: sends one order that cannot execute (qty 0, BUY limit Rs 1) to prove Kite refuses orders
    # from any IP but the static one. Reviewed 2026-11-06; it must never be changed to send a real quantity.
    'scripts/kite_ip_check.py',
}


def _code_files():
    for d in CODE_DIRS:
        root = REPO / d
        if root.is_dir():
            yield from root.rglob('*.py')


def test_every_order_caller_goes_through_the_guardrail_service():
    offenders = []
    for path in _code_files():
        rel = path.relative_to(REPO).as_posix()
        text = path.read_text(encoding='utf-8')
        if not ORDER_PATTERN.search(text) or rel in REVIEWED:
            continue
        if rel == 'execution/guardrail_service.py':
            continue
        if 'guardrail_service' not in text:
            offenders.append(rel)
    assert offenders == [], f'order endpoint used without the guardrail service: {offenders}'


def test_reviewed_exceptions_still_exist_and_still_need_reviewing():
    """A stale exception would hide a future file of the same name; each must exist and still name an endpoint."""
    for rel in REVIEWED:
        path = REPO / rel
        assert path.is_file(), f'{rel} is gone: remove it from REVIEWED'
        assert ORDER_PATTERN.search(path.read_text(encoding='utf-8')), f'{rel} no longer orders: remove it'


def test_the_pattern_catches_an_order_call():
    assert ORDER_PATTERN.search("ORDER_PATH = '/orders/regular'")
    assert ORDER_PATTERN.search('kite.place_order(variety="regular")')
    assert ORDER_PATTERN.search("url = base + '/gtt/triggers'")
    assert not ORDER_PATTERN.search("orders.to_csv('orders.csv')")
