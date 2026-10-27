"""A failed or stale render cannot replace the last usable report."""

from copy import deepcopy
from importlib import import_module
from types import SimpleNamespace

import pytest

from optimus.tests.fixtures.ai_refresh_journal import journal as _journal_fixture  # noqa: F401


@pytest.fixture
def report(journal, monkeypatch):
	mod = import_module("optimus.report_refresh")
	parent = journal.db.rows["Optimus Session"]["fake-session-doc"]
	parent.update(modified="original-generation", raw_report_file="/private/files/old.html",
		raw_report_pdf_file="/private/files/old.pdf", actions=[])
	journal.db.rows[journal.mod.RUN] = {}
	state = SimpleNamespace(fail_insert=False, during_render=None, empty_url=False, public_url=False)

	class Doc(dict):
		def __getattr__(self, name):
			return self.get(name)

	def get_doc(table, name=None):
		if isinstance(table, str):
			return Doc(deepcopy(parent))
		assert table["doctype"] == "File" and table["is_private"] == 1
		assert table["attached_to_name"] == "fake-session-doc"
		doc = Doc(**table, name="created-file", file_url="/private/files/new.html")
		def insert(**kw):
			assert journal.db.active
			assert fake.local.request is None
			journal.db.rows.setdefault("File", {})[doc.name] = dict(doc)
			if state.fail_insert:
				raise RuntimeError("fake file write failed")
			if state.empty_url:
				doc["file_url"] = ""
			if state.public_url:
				doc["file_url"] = "/files/unexpected.html"
			return doc
		doc.insert = insert
		return doc

	fake = SimpleNamespace(db=journal.db, get_doc=get_doc, local=SimpleNamespace(request="fake-request"))
	monkeypatch.setattr(mod, "frappe", fake)
	monkeypatch.setattr(mod.ai_jobs, "frappe", fake)
	def transaction(fn):
		with journal.db.transaction():
			return fn()
	monkeypatch.setattr(mod, "_transaction", transaction)
	monkeypatch.setattr(mod.sql_work, "touch_session", lambda db, name, **values: journal.db.set_value(
		"Optimus Session", name, {"modified": "rendered-generation", **values}))
	monkeypatch.setattr(mod.analyze, "load_recordings_light", lambda doc: [])
	def render(doc, recordings):
		assert not journal.db.active, "rendering must not hold a session lock"
		if state.during_render:
			state.during_render()
		return "<html>fake saved result</html>"
	monkeypatch.setattr(mod.renderer, "render_raw", render)
	return SimpleNamespace(mod=mod, journal=journal, state=state, fake=fake)


@pytest.mark.parametrize("failure", ["fail_insert", "empty_url", "public_url"])
def test_attachment_failure_preserves_old_report_pdf_and_request(report, failure):
	setattr(report.state, failure, True)
	with pytest.raises(Exception):
		report.mod.render_report("fake-session-doc")
	parent = report.journal.db.rows["Optimus Session"]["fake-session-doc"]
	assert parent["raw_report_file"] == "/private/files/old.html"
	assert parent["raw_report_pdf_file"] == "/private/files/old.pdf"
	assert not report.journal.db.rows.get("File")
	assert report.fake.local.request == "fake-request"


def test_changed_session_during_render_cannot_publish_a_stale_attachment(report):
	parent = report.journal.db.rows["Optimus Session"]["fake-session-doc"]
	report.state.during_render = lambda: parent.update(modified="changed-by-user")
	with pytest.raises(report.mod.StaleReport):
		report.mod.render_report("fake-session-doc")
	assert parent["raw_report_file"] == "/private/files/old.html"
	assert not report.journal.db.rows.get("File")


def test_valid_render_atomically_replaces_report_and_invalidates_only_pdf_reference(report):
	out = report.mod.render_report("fake-session-doc")
	assert out == {"regenerated": True, "recordings_available": 0, "actions_total": 0}
	parent = report.journal.db.rows["Optimus Session"]["fake-session-doc"]
	assert parent["raw_report_file"] == "/private/files/new.html"
	assert parent["raw_report_pdf_file"] is None
	assert len(report.journal.db.rows["File"]) == 1
	assert report.fake.local.request == "fake-request"


def test_lost_report_commit_acknowledgement_can_leave_the_new_report_saved(report, monkeypatch):
	transaction = report.mod._transaction
	def lose_ack(operation):
		result = transaction(operation)
		if report.journal.db.rows.get("File"):
			raise ConnectionError("fake lost commit acknowledgement")
		return result
	monkeypatch.setattr(report.mod, "_transaction", lose_ack)
	with pytest.raises(ConnectionError):
		report.mod.render_report("fake-session-doc")
	parent = report.journal.db.rows["Optimus Session"]["fake-session-doc"]
	assert parent["raw_report_file"] == "/private/files/new.html"
	assert parent["raw_report_pdf_file"] is None
	assert len(report.journal.db.rows["File"]) == 1
	assert report.fake.local.request == "fake-request"


def test_manual_render_cannot_bypass_an_active_ai_reservation(report, monkeypatch):
	monkeypatch.setattr(report.mod.ai_jobs, "_now", lambda: 20)
	report.journal.db.rows[report.journal.mod.RUN]["fake-run"] = report.journal.run
	with pytest.raises(report.mod.StaleReport):
		report.mod.render_report("fake-session-doc")
	assert not report.journal.db.rows.get("File")


@pytest.mark.parametrize("change", ["cancel", "expire", "owner"])
def test_worker_loses_render_ownership_while_html_is_built(report, monkeypatch, change):
	run = report.journal.run
	run.update(state="running", worker_token="worker-a", lease_until=200, deadline=300, render_pending=1)
	report.journal.db.rows[report.journal.mod.RUN][run["name"]] = run
	monkeypatch.setattr(report.mod.ai_jobs, "_now", lambda: 20)
	monkeypatch.setattr(report.mod.ai_jobs, "_check_run", lambda *a, **kw: None)
	def changed():
		if change == "cancel":
			run.update(state="cancelled", active_session=None)
		elif change == "expire":
			run.update(lease_until=10)
		else:
			run.update(worker_token="another-worker")
	report.state.during_render = changed
	with pytest.raises(report.mod.StaleReport):
		report.mod.render_report("fake-session-doc", run_id="fake-run", worker_token="worker-a")
	assert not report.journal.db.rows.get("File")
	assert run["render_pending"] == 1


def test_report_replacement_and_pending_render_accounting_roll_back_together(report, monkeypatch):
	run = report.journal.run
	run.update(state="complete", active_session=None, render_pending=1)
	report.journal.db.rows[report.journal.mod.RUN][run["name"]] = run
	def fail(*a, **kw):
		raise RuntimeError("fake pending-render write failure")
	monkeypatch.setattr(report.journal.mod, "_update", fail)
	with pytest.raises(RuntimeError):
		report.mod.render_report("fake-session-doc")
	assert report.journal.db.rows["Optimus Session"]["fake-session-doc"]["raw_report_file"] == "/private/files/old.html"
	assert not report.journal.db.rows.get("File")
	assert report.journal.db.rows[report.journal.mod.RUN][run["name"]]["render_pending"] == 1


@pytest.mark.parametrize("fail", [False, True])
def test_phase2_render_pending_clears_only_with_report_commit(report, fail):
	report.journal.db.rows["Optimus Phase Two Run"] = {
		"fake-child": {"name": "fake-child", "parent": "fake-session-doc", "analyze_render_pending": 1, "status": "Ready"},
	}
	report.state.fail_insert = fail
	if fail:
		with pytest.raises(RuntimeError):
			report.mod.render_report("fake-session-doc")
	else:
		report.mod.render_report("fake-session-doc")
	assert report.journal.db.rows["Optimus Phase Two Run"]["fake-child"]["analyze_render_pending"] == int(fail)


@pytest.mark.parametrize("expired_field", ["lease_until", "deadline"])
def test_manual_report_recovers_expired_ai_without_provider_or_scheduler(report, monkeypatch, expired_field):
	run = report.journal.run
	run.update(lease_until=200, deadline=300, render_pending=1)
	run[expired_field] = 20
	report.journal.db.rows[report.journal.mod.RUN][run["name"]] = run
	monkeypatch.setattr(report.mod.ai_jobs, "_now", lambda: 50)
	assert report.mod.render_report("fake-session-doc")["regenerated"]
	assert run["state"] == "interrupted" and run["active_session"] is None
	assert run["render_pending"] == 0
	assert report.journal.db.rows["Optimus Session"]["fake-session-doc"]["raw_report_file"].endswith("new.html")


def test_failed_report_recovery_rolls_back_expired_release(report, monkeypatch):
	run = report.journal.run
	run.update(lease_until=20, deadline=300)
	report.journal.db.rows[report.journal.mod.RUN][run["name"]] = run
	monkeypatch.setattr(report.mod.ai_jobs, "_now", lambda: 50)
	report.state.fail_insert = True
	with pytest.raises(RuntimeError, match="fake file write failed"):
		report.mod.render_report("fake-session-doc")
	assert report.journal.db.rows[report.journal.mod.RUN][run["name"]]["active_session"] == "fake-session-doc"
	assert not report.journal.db.rows.get("File")


@pytest.mark.parametrize("winner", ["Ready", "Failed"])
def test_analyze_report_replacement_requires_its_pending_status(report, winner):
	parent = report.journal.db.rows["Optimus Session"]["fake-session-doc"]
	parent["status"] = "Analyzing"
	report.state.during_render = lambda: parent.update(status=winner)
	with pytest.raises(report.mod.StaleReport):
		report.mod.render_report("fake-session-doc", [], required_status="Analyzing")
	assert parent["raw_report_file"] == "/private/files/old.html"
	assert parent["raw_report_pdf_file"] == "/private/files/old.pdf"
	assert not report.journal.db.rows.get("File")
