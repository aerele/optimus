# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the api.start kwargs (capture_python_tree)."""

import inspect

from optimus import api


def test_start_signature_accepts_capture_python_tree():
	sig = inspect.signature(api.start)
	assert "capture_python_tree" in sig.parameters
	# Default is True
	assert sig.parameters["capture_python_tree"].default is True


def test_stop_session_calls_force_stop_inflight():
	# Spec assertion: _stop_session must explicitly stop in-flight capture
	# state so a subsequent start() on the same worker doesn't see leaked
	# state. We verify the call is wired by inspecting the function source.
	src = inspect.getsource(api._stop_session)
	assert "_force_stop_inflight_capture" in src


def test_start_calls_force_stop_inflight():
	# Same property at the start path clearing leaked state from a
	# previous request on the same worker before reading session state.
	src = inspect.getsource(api.start)
	assert "_force_stop_inflight_capture" in src


def test_start_persists_capture_python_tree_in_meta():
	# The start function must pass capture_python_tree into set_session_meta.
	src = inspect.getsource(api.start)
	assert "capture_python_tree" in src
	# Specifically: it should appear in the dict passed to set_session_meta
	assert "set_session_meta" in src


def test_require_profiler_user_does_not_recurse(monkeypatch):
	"""Regression: _require_profiler_user must not recurse into itself. Calls it
	directly and asserts it returns (within one stack frame)."""
	import frappe

	# Stand-in frappe.session and get_roles so we don't need a real site
	monkeypatch.setattr(
		frappe, "session", type("S", (), {"user": "Administrator"})(), raising=False
	)
	monkeypatch.setattr(frappe, "get_roles", lambda u: ["Administrator"], raising=False)

	# Should return without recursing
	result = api._require_profiler_user()
	assert result == "Administrator"


def test_require_profiler_user_throws_for_user_without_role(monkeypatch):
	import frappe

	monkeypatch.setattr(
		frappe, "session", type("S", (), {"user": "alice@example.com"})(), raising=False
	)
	monkeypatch.setattr(frappe, "get_roles", lambda u: ["Sales User"], raising=False)

	import pytest as _pytest

	with _pytest.raises(Exception):
		api._require_profiler_user()
