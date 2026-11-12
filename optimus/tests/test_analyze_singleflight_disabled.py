# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""``optimus_singleflight_max_wait_seconds`` of 0 (or below) turns single-flight off, as the
README says: the gate proceeds without touching Redis or re-enqueueing. Unset, empty and an
unparsable value keep the default wait."""

import types

import frappe
import pytest

from optimus import analyze


class ExplodingCache:
	def __init__(self):
		self.calls = []

	def __getattr__(self, name):
		def call(*args, **kwargs):
			self.calls.append(name)
			raise AssertionError(f"single-flight touched Redis: {name}")

		return call


@pytest.fixture
def gate(monkeypatch):
	state = types.SimpleNamespace(cache=ExplodingCache(), enqueued=[], conf={})
	monkeypatch.setattr(frappe, "cache", state.cache, raising=False)
	monkeypatch.setattr(
		frappe, "conf", types.SimpleNamespace(get=lambda k, d=None: state.conf.get(k, d)), raising=False
	)
	monkeypatch.setattr(frappe, "enqueue", lambda *a, **k: state.enqueued.append((a, k)), raising=False)
	monkeypatch.setattr(analyze, "is_scheduler_disabled", lambda: False, raising=False)
	return state


@pytest.mark.parametrize("configured", [0, "0", -1])
def test_zero_or_negative_wait_disables_single_flight(gate, configured):
	gate.conf["optimus_singleflight_max_wait_seconds"] = configured
	assert analyze._acquire_singleflight("A", "PS-A", None) is True
	assert gate.cache.calls == []
	assert gate.enqueued == []


@pytest.mark.parametrize("configured", [None, "", "not a number"])
def test_unset_or_unusable_wait_keeps_single_flight_on(gate, configured):
	gate.conf["optimus_singleflight_max_wait_seconds"] = configured
	assert analyze._acquire_singleflight("A", "PS-A", None) is True  # cache error degrades to proceed
	assert gate.cache.calls, "the default wait must still try the Redis flag"
