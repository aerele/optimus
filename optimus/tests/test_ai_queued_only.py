"""Session AI runs only in the journal worker; no worker-local session attribution."""

import ast
from pathlib import Path
from types import SimpleNamespace

import frappe
import pytest

from optimus import ai_fix


def test_only_the_background_worker_calls_session_ai():
	root = Path(__file__).resolve().parents[1]
	for path in root.rglob("*.py"):
		if "tests" in path.parts or "tests_integration" in path.parts or path.name in {"ai_fix.py", "ai_jobs.py"}:
			continue
		calls = [n for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.Call)]
		assert not [n for n in calls if isinstance(n.func, ast.Attribute) and n.func.attr in {"suggest_fix", "humanize_steps"}], path


def test_ambient_spend_marker_has_no_production_reader_or_writer():
	root = Path(__file__).resolve().parents[1]
	for name in ("ai_fix.py", "api.py", "analyze.py"):
		assert "_optimus_spend_session" not in (root / name).read_text()


@pytest.mark.parametrize("session_uuid", [None, "explicit-fake-session"])
def test_usage_collection_cannot_write_to_a_previous_workers_session(monkeypatch, session_uuid):
	writes = []
	monkeypatch.setattr(frappe, "db", SimpleNamespace(sql=lambda *a, **kw: writes.append(True)))
	monkeypatch.setattr(frappe, "local", SimpleNamespace(_optimus_spend_session="previous-fake-session"))
	usage = ai_fix.Usage()
	usage.begin()
	ai_fix._accept_usage(usage, ai_fix._response_usage({"usage": {"total_tokens": 7}}, "openai"), session_uuid=session_uuid)
	assert usage["total_tokens"] == 7 and usage.complete
	assert not writes


def test_http_failure_metadata_never_uses_a_previous_workers_session(monkeypatch):
	monkeypatch.setattr(frappe, "local", SimpleNamespace(_optimus_spend_session="previous-fake-session"))
	seen = []
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **kw: seen.append(kw["session_uuid"]))
	ai_fix._log_http_error("fake-provider", "fake-call", 500)
	ai_fix._log_http_error("fake-provider", "fake-call", 500, session_uuid="explicit-fake-session")
	assert seen == [None, "explicit-fake-session"]
