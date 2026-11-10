# -*- coding: utf-8 -*-
# tests/test_gtt_probe.py
"""ST-009 piece 2: the dry-run script against a fake Kite -- the full sequence, the refusal, and the cleanup."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from execution.guardrails import GTT_DELETE, GTT_MODIFY, GTT_PLACE, load_limits
from scripts.gtt_probe import EXPECTED_EVENTS, Probe, event_shape_ok

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'guardrails.toml'


@pytest.fixture(scope='module')
def limits():
    return load_limits(CONFIG_PATH)


@dataclass
class FakeSources:
    events: list = field(default_factory=list)

    def gtt_is_a_lots(self, kite_gtt_id):
        return False

    def write_event(self, row):
        self.events.append(row)
        return len(self.events)


@dataclass
class FakeKite:
    """A broker stand-in that redeems clearances like the real one and keeps GTTs in memory."""

    gtts: dict = field(default_factory=dict)
    writes: list = field(default_factory=list)
    fail_modify: bool = False

    def holding_qty(self, symbol):
        return 5

    def last_price(self, symbol):
        return 1000.0

    def gtt(self, gid):
        g = self.gtts[gid]
        return {'id': gid, 'status': g['status'],
                'condition': {'tradingsymbol': 'RELIANCE', 'trigger_values': [g['trigger']]},
                'orders': [{'tradingsymbol': 'RELIANCE', 'transaction_type': 'SELL', 'quantity': 1,
                            'order_type': 'MARKET', 'product': 'CNC', 'market_protection': -1}]}

    def gtt_status(self, gid):
        return self.gtts[gid]['status'] if gid in self.gtts else None

    def gtt_trigger(self, gid):
        return self.gtts[gid]['trigger'] if gid in self.gtts else None

    def place_gtt(self, c):
        c.redeem(GTT_PLACE)
        self.writes.append(('place', c.trigger_price))
        self.gtts[55] = {'status': 'active', 'trigger': c.trigger_price}
        return 55

    def modify_gtt(self, c):
        c.redeem(GTT_MODIFY)
        if self.fail_modify:
            raise RuntimeError('kite said no')
        self.writes.append(('modify', c.trigger_price))
        self.gtts[c.gtt_id]['trigger'] = c.trigger_price
        return c.gtt_id

    def delete_gtt(self, c):
        c.redeem(GTT_DELETE)
        self.writes.append(('delete', c.gtt_id))
        self.gtts[c.gtt_id]['status'] = 'deleted'
        return c.gtt_id


def test_clean_run_passes_and_never_lowers(limits):
    kite, src = FakeKite(), FakeSources()
    p = Probe('RELIANCE', kite, src, limits)
    p.run()
    p.delete()
    assert p.failures == []
    assert kite.writes == [('place', 450.0), ('modify', 460.0), ('delete', 55)]     # the lower was never sent
    rows = [(i, e['stage'], e['decision'], e['rule']) for i, e in enumerate(src.events, 1)]
    assert event_shape_ok(rows)
    assert len(src.events) == len(EXPECTED_EVENTS)


def test_failure_after_place_still_deletes(limits):
    kite = FakeKite(fail_modify=True)
    p = Probe('RELIANCE', kite, FakeSources(), limits)
    with pytest.raises(RuntimeError):
        p.run()
    p.delete()
    assert p.deleted and kite.gtts[55]['status'] == 'deleted'


def test_event_shape_rejects_extra_or_missing_rows():
    rows = [(1, *e, 'OK') for e in EXPECTED_EVENTS]
    assert event_shape_ok(rows)
    assert not event_shape_ok(rows[:3])
    assert not event_shape_ok(rows + [(9, 'before_gtt', 'allow', 'OK')])
