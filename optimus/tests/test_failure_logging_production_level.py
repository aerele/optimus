"""Failure breadcrumbs survive the production ERROR threshold without secrets."""
import logging
from types import SimpleNamespace

import pytest

from optimus import ai_jobs, api
from optimus.line_profile import capture
from optimus.renderer import source
from optimus.tests.gate_fakes import SESSION_UUID, FakePermissionError
from optimus.tests.test_session_action_gate import _install


def test_permission_failure_logs_at_production_level(monkeypatch, caplog):
	def fail(*a, **kw):
		raise RuntimeError("PRIVATE-PERMISSION-CANARY")
	fake = _install(monkeypatch, has_permission=fail)
	logger = logging.getLogger("optimus.permission-test")
	monkeypatch.setattr(fake, "logger", lambda *a: logger)
	with caplog.at_level(logging.ERROR, logger=logger.name), pytest.raises(FakePermissionError):
		api._session_action_gate(SESSION_UUID, action="regenerate_reports")
	assert "has_permission raised" in caplog.text
	assert "PRIVATE-PERMISSION-CANARY" not in caplog.text


def test_source_boundary_failure_logs_at_production_level(monkeypatch, caplog):
	import frappe
	logger = logging.getLogger("optimus.source-test")
	monkeypatch.setattr(frappe, "logger", lambda *a: logger, raising=False)
	monkeypatch.setattr(frappe, "local", SimpleNamespace(site="site"), raising=False)
	def fail():
		raise RuntimeError("PRIVATE-SOURCE-CANARY")
	monkeypatch.setattr(frappe, "get_installed_apps", fail, raising=False)
	monkeypatch.setattr(source, "_APPS_WARNING_SENT", False)
	with caplog.at_level(logging.ERROR, logger=logger.name):
		assert source._installed_apps() == frozenset()
	assert "installed apps unavailable" in caplog.text
	assert "PRIVATE-SOURCE-CANARY" not in caplog.text


def test_capture_cleanup_failure_logs_counts_at_production_level(monkeypatch, caplog):
	logger = logging.getLogger("optimus.cleanup-test")
	monkeypatch.setattr(ai_jobs.frappe, "logger", lambda *a: logger, raising=False)
	def fail(*a):
		raise ConnectionError("PRIVATE-CAPTURE-CANARY")
	monkeypatch.setattr(capture, "stop_line_profile_pass", fail)
	with caplog.at_level(logging.ERROR, logger=logger.name):
		ai_jobs._cleanup_deleted_captures([("run", "actor")])
	assert "failed=1" in caplog.text
	assert "PRIVATE-CAPTURE-CANARY" not in caplog.text
