# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Ready is set only after the report File is attached (phase 1 and phase 2).

Uses an in-memory fake of frappe with real commit and rollback."""

import copy
from types import SimpleNamespace

import pytest

from optimus import analyze
from optimus.analyzers.base import AnalyzerResult
from optimus.line_profile import analyzer as lp

DOCNAME = "OS-1"
SUUID = "sess-uuid"
RUN = "run-1"


class FakeDoc:
	def __init__(self, db, store):
		self._db = db
		self.name = DOCNAME
		self.flags = SimpleNamespace()
		self.session_uuid = SUUID
		self.actions = []
		self.findings = [SimpleNamespace(**f) for f in store["findings"]]
		self.phase_2_runs = [SimpleNamespace(**r) for r in store["runs"]]
		self.raw_report_file = store["session"].get("raw_report_file")

	def append(self, table, row):
		getattr(self, table).append(SimpleNamespace(**row))

	def save(self, **kw):
		store = self._db.store
		store["findings"] = [vars(f) for f in self.findings]
		store["runs"] = [vars(r) for r in self.phase_2_runs]


class FakeDB:
	def __init__(self):
		self.store = {
			"session": {"status": "Analyzing"},
			"findings": [],
			"runs": [{"run_uuid": RUN, "status": "Analyzing", "results_json": None, "parent": DOCNAME}],
			"files": [],
		}
		self.committed = copy.deepcopy(self.store)
		self.commits = 0

	def commit(self):
		self.commits += 1
		self.committed = copy.deepcopy(self.store)

	def rollback(self):
		self.store = copy.deepcopy(self.committed)

	def set_value(self, doctype, name, field, value=None):
		fields = field if isinstance(field, dict) else {field: value}
		self.store["session"].update(fields)

	def get_value(self, doctype, filters, field=None, as_dict=False):
		if doctype == "Optimus Phase Two Run":
			row = next((r for r in self.store["runs"] if r["run_uuid"] == filters.get("run_uuid")), None)
			if row is None:
				return None
			if isinstance(field, list):
				return {k: row.get(k) for k in field}
			return row.get(field)
		if field == "name":
			return DOCNAME
		return self.store["session"].get(field)

	def exists(self, doctype, filters):
		return any(f["file_url"] == filters["file_url"] for f in self.store["files"])


class FakeFrappe:
	def __init__(self):
		self.db = FakeDB()
		self.local = SimpleNamespace()
		self.errors = []
		self.events = []
		self.utils = SimpleNamespace(now_datetime=lambda: "now")

	def get_doc(self, doctype, name=None):
		return FakeDoc(self.db, self.db.store)

	def get_all(self, doctype, filters=None, fields=None, limit=None):
		return [{"name": "r1", "parent": DOCNAME}]

	def log_error(self, *a, **kw):
		self.errors.append(kw.get("title") or a)

	def publish_realtime(self, event, payload, user=None):
		self.events.append(event)


class Harness:
	"""Wires the fake frappe and a controllable render / attach into both modules."""

	def __init__(self, monkeypatch):
		self.f = FakeFrappe()
		self.render_error = None
		self.attach_ok = True
		self.after_attach = None
		for mod in (analyze, lp):
			monkeypatch.setattr(mod, "frappe", self.f)
		monkeypatch.setattr(lp, "_FRAPPE_AVAILABLE", True)
		monkeypatch.setattr(analyze, "safe_commit", self.f.db.commit)
		monkeypatch.setattr(lp, "safe_commit", self.f.db.commit)
		monkeypatch.setattr(analyze.renderer, "render_raw", self._render, raising=False)
		monkeypatch.setattr(analyze, "_save_report_file", self._attach)
		monkeypatch.setattr(analyze, "_fetch_recordings", lambda *a, **kw: iter(()))
		monkeypatch.setattr(analyze, "_load_recordings_bundle", lambda doc: None)
		monkeypatch.setattr(
			analyze, "_publish_session_event", lambda ev, **kw: self.f.events.append(ev)
		)
		monkeypatch.setattr(analyze, "_publish_progress", lambda *a, **kw: None)
		monkeypatch.setattr(analyze, "_auto_arm_phase2", lambda *a, **kw: None)
		from optimus.line_profile import capture

		self.cleaned = []
		monkeypatch.setattr(capture, "read_all_samples", lambda r: [])
		monkeypatch.setattr(capture, "read_picks_meta", lambda r: [])
		monkeypatch.setattr(capture, "aggregate_samples", lambda s, p: [{"dotted_path": "a.b"}])
		monkeypatch.setattr(capture, "budget_was_hit", lambda r: False)
		monkeypatch.setattr(capture, "cleanup_run", lambda r: self.cleaned.append(r))
		self.findings = [{"finding_type": "Hot Line", "title": "t", "technical_detail_json": "{}"}]
		monkeypatch.setattr(lp, "analyze", self._lp_analyze)

	def _lp_analyze(self, results_json, call_trees=None):
		return AnalyzerResult(
			findings=copy.deepcopy(self.findings),
			aggregate={"phase2_functions": [{"total_ms": 5}] if self.findings else []},
		)

	def _render(self, doc, recordings):
		# The phase-2 panel only renders Ready runs: prove the row is Ready by now.
		self.runs_seen_at_render = [r.status for r in doc.phase_2_runs]
		if self.render_error:
			raise self.render_error
		return "<html></html>"

	def _attach(self, **kw):
		if not self.attach_ok:
			return None
		url = "/private/files/report.html"
		self.f.db.store["files"].append({"file_url": url})
		if self.after_attach:
			raise self.after_attach
		return url


@pytest.fixture
def h(monkeypatch):
	return Harness(monkeypatch)


# --- phase 1 -----------------------------------------------------------


class TestRenderAndAttach:
	def test_success_attaches_and_commits(self, h):
		url = analyze._render_and_attach_reports(DOCNAME, [])
		assert url == "/private/files/report.html"
		assert h.f.db.committed["session"]["raw_report_file"] == url

	def test_render_failure_raises_and_sets_nothing(self, h):
		h.render_error = ValueError("boom")
		with pytest.raises(analyze.ReportAttachError, match="render failed"):
			analyze._render_and_attach_reports(DOCNAME, [])
		assert "raw_report_file" not in h.f.db.store["session"]

	def test_attach_failure_raises(self, h):
		h.attach_ok = False
		with pytest.raises(analyze.ReportAttachError, match="could not be saved"):
			analyze._render_and_attach_reports(DOCNAME, [])

	def test_invariant_detects_missing_file_row(self, h):
		h.f.db.store["session"]["raw_report_file"] = "/private/files/gone.html"
		with pytest.raises(analyze.ReportAttachError, match="not attached"):
			analyze._assert_report_attached(DOCNAME)

	def test_invariant_detects_unset_field(self, h):
		with pytest.raises(analyze.ReportAttachError, match="not set"):
			analyze._assert_report_attached(DOCNAME)


class TestEmptySession:
	def test_ready_only_after_report(self, h):
		analyze._finalize_with_empty_session(DOCNAME)
		assert h.f.db.committed["session"]["status"] == "Ready"
		assert h.f.db.committed["session"]["raw_report_file"]

	def test_render_failure_does_not_mark_ready(self, h):
		h.render_error = RuntimeError("x")
		with pytest.raises(analyze.ReportAttachError):
			analyze._finalize_with_empty_session(DOCNAME)
		assert h.f.db.store["session"]["status"] == "Analyzing"


class TestFailureHandling:
	def test_failed_status_carries_visible_message(self, h):
		analyze._mark_session_failed(DOCNAME, analyze.ReportAttachError("nope"))
		s = h.f.db.committed["session"]
		assert s["status"] == "Failed"
		assert "nope" in s["analyzer_warnings"]


# --- phase 2 -----------------------------------------------------------


class TestPhase2:
	def test_success_ready_after_report_with_results(self, h):
		lp.run_analyze(SUUID, RUN)
		row = h.f.db.committed["runs"][0]
		assert row["status"] == "Ready" and row["results_json"]
		assert h.f.db.committed["session"]["raw_report_file"]
		assert h.runs_seen_at_render == ["Ready"]
		assert len(h.f.db.committed["findings"]) == 1
		assert h.f.events[-1] == "phase_2_run_ready"
		assert h.cleaned == [RUN]

	def test_nothing_committed_before_report_attached(self, h, monkeypatch):
		seen = []

		def spy():
			seen.append((h.f.db.store["runs"][0]["status"], bool(h.f.db.store["files"])))
			h.f.db.commit()

		monkeypatch.setattr(analyze, "safe_commit", spy)
		monkeypatch.setattr(lp, "safe_commit", spy)
		lp.run_analyze(SUUID, RUN)
		# no commit may persist a Ready row before the File exists
		assert seen and all(attached for status, attached in seen if status == "Ready")

	def test_render_failure_marks_failed_never_ready(self, h):
		h.render_error = ValueError("render boom")
		with pytest.raises(analyze.ReportAttachError):
			lp.run_analyze(SUUID, RUN)
		row = h.f.db.committed["runs"][0]
		assert row["status"] == "Failed"
		assert "render boom" in row["warnings_json"]
		assert h.f.db.committed["findings"] == []  # rolled back with the row
		assert "phase_2_run_ready" not in h.f.events
		assert "phase_2_run_failed" in h.f.events
		assert h.cleaned == []  # samples kept for retry

	def test_attach_failure_marks_failed(self, h):
		h.attach_ok = False
		with pytest.raises(analyze.ReportAttachError):
			lp.run_analyze(SUUID, RUN)
		assert h.f.db.committed["runs"][0]["status"] == "Failed"
		assert "phase_2_run_ready" not in h.f.events

	def test_exception_between_steps_after_attach_never_ready(self, h):
		h.after_attach = RuntimeError("died after File insert")
		with pytest.raises(RuntimeError):
			lp.run_analyze(SUUID, RUN)
		assert h.f.db.committed["runs"][0]["status"] == "Failed"
		assert "phase_2_run_ready" not in h.f.events

	def test_timeout_base_exception_marks_failed(self, h):
		class Timeout(BaseException):
			pass

		h.after_attach = Timeout()
		with pytest.raises(Timeout):
			lp.run_analyze(SUUID, RUN)
		assert h.f.db.committed["runs"][0]["status"] == "Failed"

	def test_retry_after_failure_succeeds_without_duplicate_findings(self, h):
		h.render_error = ValueError("once")
		with pytest.raises(analyze.ReportAttachError):
			lp.run_analyze(SUUID, RUN)
		h.render_error = None
		lp.run_analyze(SUUID, RUN)
		assert h.f.db.committed["runs"][0]["status"] == "Ready"
		assert len(h.f.db.committed["findings"]) == 1

	def test_rerun_same_findings_not_duplicated(self, h):
		lp.run_analyze(SUUID, RUN)
		lp.run_analyze(SUUID, RUN)
		assert len(h.f.db.committed["findings"]) == 1

	def test_empty_results_ready_with_warning(self, h):
		h.findings = []
		lp.run_analyze(SUUID, RUN)
		row = h.f.db.committed["runs"][0]
		assert row["status"] == "Ready"
		assert "No line data" in row["warnings_json"]
		assert h.f.db.committed["session"]["raw_report_file"]

	def test_run_ready_invariant_rejects_missing_results(self, h):
		h.f.db.store["runs"][0].update(status="Ready", results_json="")
		with pytest.raises(RuntimeError):
			lp._assert_run_ready(DOCNAME, RUN)


class TestNewFindings:
	def test_filters_existing_and_within_batch_duplicates(self):
		have = [SimpleNamespace(finding_type="Hot Line", title="a", technical_detail_json="1")]
		cand = [
			{"finding_type": "Hot Line", "title": "a", "technical_detail_json": "1"},
			{"finding_type": "Hot Line", "title": "b", "technical_detail_json": "1"},
			{"finding_type": "Hot Line", "title": "b", "technical_detail_json": "1"},
		]
		assert [f["title"] for f in lp._new_findings(have, cand)] == ["b"]


class TestRunOrdering:
	def test_run_asserts_report_before_ready_and_cleans_redis_after(self):
		import inspect

		src = inspect.getsource(analyze.run)
		render = src.index("_render_and_attach_reports(docname, recordings)")
		assert_ = src.index("_assert_report_attached(docname)")
		ready = src.index('"status", "Ready"')
		cleanup = src.index("_cleanup_redis(session_uuid")
		assert render < assert_ < ready < cleanup
		assert "except BaseException" in src
