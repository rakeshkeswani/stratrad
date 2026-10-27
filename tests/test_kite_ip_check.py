# -*- coding: utf-8 -*-
"""ST-007: the static-IP check can only ever send an order that cannot execute; verdict logic. No network."""

from scripts import kite_ip_check as k

IP_MSG = 'The static IP (13.205.57.59) configured is not valid. Your current IP is (49.37.1.2)'
QTY_MSG = 'Quantity should be greater than 0.'


class Resp:
    def __init__(self, body, status):
        self._b, self.status_code = body, status

    def json(self):
        return self._b


def test_safe_order_cannot_execute():
    assert k.SAFE_ORDER['quantity'] == '0'
    assert k.SAFE_ORDER['price'] == '1' and k.SAFE_ORDER['order_type'] == 'LIMIT'
    assert k.SAFE_ORDER['transaction_type'] == 'BUY' and k.SAFE_ORDER['product'] == 'CNC'


def test_attempt_posts_only_the_safe_order_with_the_proxy():
    seen = {}

    def post(url, data, timeout, headers, proxies):
        seen.update(url=url, data=data, proxies=proxies)
        return Resp({'status': 'error', 'error_type': 'InputException', 'message': QTY_MSG}, 400)

    r = k.attempt('tok', 'http://100.70.129.17:8888', post=post)
    assert seen['url'].endswith('/orders/regular') and seen['data'] is k.SAFE_ORDER
    assert seen['proxies'] == {'https': 'http://100.70.129.17:8888'}
    assert r['error_type'] == 'InputException' and r['order_id'] is None
    k.attempt('tok', None, post=post)
    assert seen['proxies'] is None


def test_ip_refusal_is_told_apart_from_order_refusal():
    assert k.is_ip_refusal(IP_MSG)
    assert not k.is_ip_refusal(QTY_MSG)
    assert not k.is_ip_refusal('Invalid price. Price should be within the circuit limits.')


def r(msg, status='error'):
    return {'status': status, 'http': 400, 'error_type': 'X', 'message': msg, 'order_id': None}


def test_verdict():
    assert k.verdict(r(QTY_MSG), r(IP_MSG))[0] is True             # proxy: order refusal; direct: IP refusal
    assert k.verdict(r(IP_MSG), r(IP_MSG))[0] is False             # proxy IP not accepted
    assert k.verdict(r(QTY_MSG), r(QTY_MSG))[0] is False           # whitelist not enforced on direct calls
    assert k.verdict(r('', 'success'), r(IP_MSG))[0] is False      # accepted = never a pass
