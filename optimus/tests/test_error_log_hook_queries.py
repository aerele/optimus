# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The Error Log hook's own ``__Auth`` SELECT (its stored-key read, inside
the user's ``frappe.log_error``) as the analyzers and the session totals see
it. The analyzers recognise the hook's frame by a path suffix derived from
the hook module itself (``error_log_mask.HOOK_FRAME_SUFFIX``), so renaming
or moving the module cannot silently stop the match."""

from pathlib import Path

import pytest

from optimus import error_log_mask
from optimus.analyzers import base


class TestTheHookFrameSuffix:
	def test_it_is_derived_from_the_module_and_matches_its_real_path(self):
		assert error_log_mask.HOOK_FRAME_SUFFIX == error_log_mask.__name__.replace(".", "/") + ".py"
		assert Path(error_log_mask.__file__).resolve().as_posix().endswith("/" + error_log_mask.HOOK_FRAME_SUFFIX)

	def test_the_analyzers_use_it(self):
		assert base._ERROR_LOG_HOOK_FRAME is error_log_mask.HOOK_FRAME_SUFFIX
		assert base._is_error_log_hook_frame(Path(error_log_mask.__file__).resolve().as_posix())
		assert not base._is_error_log_hook_frame("apps/optimus/optimus/error_log_mask_helpers.py")

	def test_the_analyzers_never_spell_the_path_out(self):
		source = Path(base.__file__).read_text(encoding="utf-8")
		assert '"optimus/error_log_mask.py"' not in source


# ---------------------------------------------------------------------------
# The session totals leave the hook's key read out
# ---------------------------------------------------------------------------

_USER = {"filename": "apps/myapp/myapp/importer.py", "lineno": 42, "function": "import_rows"}
_LOG_ERROR = [
	{"filename": "apps/frappe/frappe/utils/error.py", "lineno": 80, "function": "log_error"},
	{"filename": "apps/frappe/frappe/model/document.py", "lineno": 1184, "function": "run_method"},
]
_KEY_READ = [
	{"filename": "apps/frappe/frappe/utils/password.py", "lineno": 34, "function": "get_decrypted_password"},
	{"filename": "apps/frappe/frappe/database/database.py", "lineno": 270, "function": "sql"},
]
_HOOK = {"filename": "apps/optimus/optimus/error_log_mask.py", "lineno": 131, "function": "_read_key"}
_AI_FIX = {"filename": "apps/optimus/optimus/ai_fix.py", "lineno": 819, "function": "_current_key_or_empty"}
_DB = {"filename": "apps/frappe/frappe/database/database.py", "lineno": 270, "function": "sql"}


def _calls():
	"""A user query, the hook's key read (through ai_fix, and straight from
	the hook), a framework-only query and Optimus's own infra snapshot."""
	return [
		{"query": "SELECT 1 FROM `tabItem`", "duration": 10.0, "stack": [_USER, _DB]},
		{"query": "SELECT `password` FROM `__Auth`", "duration": 3.0, "stack": [_USER, *_LOG_ERROR, _HOOK, _AI_FIX, *_KEY_READ]},
		{"query": "SELECT `password` FROM `__Auth`", "duration": 2.0, "stack": [_USER, *_LOG_ERROR, _HOOK, *_KEY_READ]},
		{"query": "SELECT `name` FROM `tabDocType`", "duration": 1.5, "stack": [_DB]},
		{"query": "SHOW GLOBAL STATUS", "duration": 0.5,
		 "stack": [{"filename": "apps/optimus/optimus/infra_capture.py", "lineno": 5, "function": "snap"}, _DB]},
		{"query": "SELECT 2", "duration": 1.0},  # no stack recorded
	]


class TestSessionQueryTotals:
	def test_the_predicate_marks_only_the_hooks_read(self):
		marked = [base.is_error_log_hook_query(c.get("stack")) for c in _calls()]
		assert marked == [False, True, True, False, False, False]
		# a user frame between the hook and the query: the user's query
		assert not base.is_error_log_hook_query([_HOOK, _USER, *_KEY_READ])

	def test_the_totals_leave_the_hooks_read_out(self):
		from optimus import analyze

		recordings = [{"calls": _calls()}, {"calls": _calls()[:1]}, {"calls": None}, {}]
		assert analyze._session_query_totals(recordings) == (5, 23.0)

	def test_persist_stores_those_totals(self, monkeypatch):
		import pytest

		from optimus import analyze

		class _Stop(Exception):
			pass

		class _Doc:
			notes = "<p>kept</p>"
			title = "t"

			def __setattr__(self, name, value):
				if name == "top_severity":
					raise _Stop  # everything the test needs is set by now
				object.__setattr__(self, name, value)

		doc = _Doc()
		monkeypatch.setattr(analyze.frappe, "get_doc", lambda *a, **k: doc, raising=False)
		context = base.AnalyzeContext(session_uuid="u", docname="d")
		with pytest.raises(_Stop):
			analyze._persist("d", context, [{"calls": _calls(), "duration": 50}])
		assert (doc.total_queries, doc.total_query_time_ms) == (4, 13.0)


class TestPerActionReconcilesWithTheTotals:
	"""The per-action breakdown leaves the hook's key read out, as the session
	totals do, so its rows add up to them."""

	def test_per_action_sums_equal_the_session_totals(self):
		from optimus import analyze
		from optimus.analyzers import per_action

		recordings = [
			{"uuid": "r1", "calls": _calls(), "duration": 50},
			{"uuid": "r2", "calls": _calls()[1:3], "duration": 9},
			{"uuid": "r3", "calls": _calls()[:1], "duration": 12},
			{"uuid": "r4", "calls": None},
		]
		actions = per_action.analyze(recordings, None).actions
		count, time_ms = analyze._session_query_totals(recordings)
		assert sum(a["queries_count"] for a in actions) == count == 5
		assert round(sum(a["query_time_ms"] for a in actions), 2) == round(time_ms, 2) == 23.0
		by_uuid = {a["recording_uuid"]: a for a in actions}
		assert by_uuid["r2"]["queries_count"] == 0 and by_uuid["r2"]["slowest_query_ms"] == 0
		assert by_uuid["r1"]["slowest_query_ms"] == 10.0


@pytest.mark.parametrize("query", [
	"SELECT password FROM __Auth WHERE doctype = 'Optimus Settings'",
	"SELECT value FROM tabSingles WHERE field = 'ai_api_key'",
])
@pytest.mark.parametrize("stack", [[_USER, *_LOG_ERROR, _HOOK, *_KEY_READ], [_USER, _HOOK, _AI_FIX, *_KEY_READ]])
def test_table_consumers_exclude_only_hook_reads(monkeypatch, query, stack):
	from optimus import analyze
	from optimus.analyzers import index_suggestions, table_breakdown
	from optimus.tests.test_index_suggestions import _install_fake_recorder_module

	user_query = "SELECT item_code FROM tabItem WHERE item_group = 'Products'"
	user_calls = [
		{"query": user_query, "duration": 10.0, "stack": [_USER, _DB]},
		# Same SQL used outside the hook remains visible, even when the hook
		# called the user's code. Missing stacks also remain visible.
		{"query": query, "duration": 5.0, "stack": [_HOOK, _USER, _DB]},
		{"query": query, "duration": 2.0},
	]
	recordings = [{"calls": [{"query": query, "duration": 100.0, "stack": stack}, *user_calls]}]
	for call in recordings[0]["calls"]:
		call["normalized_query"] = call["query"]
	baseline = [{"calls": user_calls}]
	rows = table_breakdown.analyze(recordings, None).aggregate["table_breakdown"]
	assert rows == table_breakdown.analyze(baseline, None).aggregate["table_breakdown"]
	assert (sum(row["queries"] for row in rows), sum(row["consolidated_time_ms"] for row in rows)) == analyze._session_query_totals(recordings)

	optimized = []
	_install_fake_recorder_module(monkeypatch, lambda sql: optimized.append(sql))
	index_suggestions.analyze(recordings, None)
	assert optimized == [user_query, query]


def test_hook_only_queries_never_reach_index_optimizer(monkeypatch):
	from optimus.analyzers import index_suggestions
	from optimus.tests.test_index_suggestions import _install_fake_recorder_module

	optimized = []
	_install_fake_recorder_module(monkeypatch, lambda sql: optimized.append(sql))
	result = index_suggestions.analyze([{"calls": [dict(c, normalized_query=c["query"]) for c in _calls()[1:3]]}], None)
	assert optimized == [] and result.findings == []


def test_call_tree_sql_total_matches_session_total():
	from optimus import analyze
	from optimus.analyzers import call_tree

	recordings = [{"calls": _calls(), "duration": 50.0}]
	result = call_tree.analyze(recordings, base.AnalyzeContext(session_uuid="fake", docname="fake"))
	assert result.aggregate["total_sql_ms"] == analyze._session_query_totals(recordings)[1] == 13.0


def test_call_tree_excludes_hook_reads_from_reconciliation(monkeypatch):
	from optimus.analyzers import call_tree

	seen = []
	def reconcile(pyi, calls, wall_time):
		seen.extend(calls)
		return {"function": "<root>", "children": [], "cumulative_ms": 0, "self_ms": 0, "kind": "python"}
	monkeypatch.setattr(call_tree, "reconcile", reconcile)
	call_tree.analyze([{"calls": _calls(), "pyi_session": object()}], base.AnalyzeContext(session_uuid="fake", docname="fake"))
	assert seen == [_calls()[i] for i in (0, 3, 4, 5)]
