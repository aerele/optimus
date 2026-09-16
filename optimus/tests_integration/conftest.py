# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Pytest fixtures for the real-bench integration suite.

Imported only under ``bench --site … run-tests`` (the pure-pytest workflow
never traverses this directory), so these fixtures assume a live Frappe site
is initialised and use ``frappe.db`` / ``frappe.cache`` / ``frappe.get_doc``
directly.

Fixtures:

* :func:`test_site`: yields the connected site name.
* :func:`cleanup_session` (autouse): hard-deletes any leftover ``Optimus
  Session`` rows and their ``profiler:*`` Redis keys, since the analyze
  pipeline writes through a background connection that escapes FrappeTestCase's
  per-test rollback (and Redis isn't transactional).
* :func:`seeded_session`: calls ``api.start``, yields the session_uuid and on
  teardown calls ``api.stop`` and waits for analyze to finalise.
"""

from __future__ import annotations

import time

import frappe
import pytest


@pytest.fixture(scope="session")
def test_site() -> str:
	"""The Frappe site the test runner is connected to (``frappe.local.site``), so
	tests stay site-agnostic."""
	return frappe.local.site


@pytest.fixture(autouse=True)
def cleanup_session():
	"""Defensive teardown after every integration test: purges leftover Optimus
	Session rows and Redis state that escape FrappeTestCase's rollback (the
	analyze pipeline writes through a separate background connection; Redis isn't
	transactional either). Best-effort: a failed delete is logged and tolerated.
	"""
	yield
	# Lazy imports analyze.py / session.py do their own frappe imports
	# at module top, but the conftest shouldn't reach them at module load
	# (it would slow down test-collection in the bench runner).
	try:
		_purge_test_sessions()
	except Exception as exc:  # pragma: no cover best-effort path
		frappe.log_error(
			title="optimus integration: cleanup_session",
			message=f"{type(exc).__name__}: {exc}",
		)


def _purge_test_sessions() -> None:
	"""Delete every ``Optimus Session`` row for the current user plus its Redis
	state. User-scoped, so it is safe on a bench holding other users' sessions."""
	user = getattr(frappe.session, "user", None) or "Administrator"
	rows = frappe.get_all(
		"Optimus Session",
		filters={"user": user},
		pluck="name",
	)
	for name in rows:
		try:
			frappe.delete_doc(
				"Optimus Session",
				name,
				ignore_permissions=True,
				force=True,
			)
		except Exception:
			pass
	# Clear the user's active-session pointer + any per-session metadata
	# hashes. The keys are scoped by Frappe's cache.make_key which
	# prefixes by site name, so we won't clobber state on a sibling site.
	try:
		frappe.cache.delete_value(f"profiler:active:{user}")
		# Per-uuid meta hashes are harder to enumerate without a SCAN;
		# the next janitor cron sweeps them via TTL anyway.
	except Exception:
		pass
	frappe.db.commit()


@pytest.fixture
def seeded_session(test_site):
	"""Start a session as Administrator, yield the session_uuid, then on teardown
	call ``api.stop`` and wait for analyze to finish (capped at 60 s). Use for a
	fully-finalized session; tests asserting mid-flight state call ``api.start``
	directly instead."""
	frappe.set_user("Administrator")
	from optimus import api

	result = api.start(label="integration test seeded_session")
	session_uuid = result["session_uuid"]
	yield session_uuid

	# Teardown: stop the session if it's still active, then wait for
	# analyze to finalise. ``api.stop`` returns ran_inline=True when the
	# inline-analyze cap (v0.5.0) fires; in that case the session is
	# already in a terminal state and the poll loop below short-circuits.
	try:
		api.stop()
	except Exception:
		pass
	_wait_for_terminal_status(session_uuid, timeout_seconds=60)


def _wait_for_terminal_status(session_uuid: str, *, timeout_seconds: int = 60) -> str | None:
	"""Poll the session's ``status`` every 500 ms until ``Ready`` / ``Failed`` or
	``timeout_seconds`` elapses. Returns the final status, or ``None`` on timeout."""
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		try:
			status = frappe.db.get_value(
				"Optimus Session",
				{"session_uuid": session_uuid},
				"status",
			)
		except Exception:
			status = None
		if status in ("Ready", "Failed"):
			return status
		time.sleep(0.5)
	return None
