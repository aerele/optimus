"""The engine against #72's contracts and the owner decisions Q1 to Q3 (F3).

Kinds and the breaker (Q2), the overlap with #72's Refresh (Q3), the selection counts, the
grounding the fix prompts share with the synchronous Refresh, the cap across a resume, the
Error Log titles and their context, and the public names the engine uses across modules."""

# ruff: noqa: F811  (the imported fixtures are used by name as parameters)
import ast
import re
from copy import deepcopy
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from optimus import ai_fix, ai_jobs, analyze, api
from optimus import ai_refresh_store as store
from optimus.tests.test_ai_refresh_journal import journal as _journal_fixture  # noqa: F401
from optimus.tests.test_ai_refresh_queue import _start, admitting, queue  # noqa: F401
from optimus.tests.test_ai_refresh_work import Doc, work  # noqa: F401
from optimus.tests.test_ai_refresh_worker import worker  # noqa: F401

pytestmark = pytest.mark.rq

_REAL_PHASE2_INDEX = analyze._phase2_index_for  # the work fixture replaces it
_REAL_LOG_AI_FAILURE = ai_fix.log_ai_failure  # the queue fixture replaces it
_PKG = Path(ai_jobs.__file__).resolve().parent
_DOCS = _PKG.parent / "docs"


# --- Q2: kinds, skips, tokens ------------------------------------------------------------


def _attempts(worker):
	return list(worker.journal.db.rows[worker.journal.mod.ATTEMPT].values())


def test_a_not_eligible_reply_is_a_skip_not_a_failure(worker):
	"""M25: the skip kinds are #72's (``AI_SKIP_KINDS``): the finding was refused before any
	request. It is recorded skipped, with its kind, and feeds neither the streak nor the breaker."""

	def refuse():
		raise ai_fix.AiFixError("fake: not eligible", kind="not_eligible")

	worker.count = 4
	worker.send_hook = refuse
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	run = worker.journal.run
	assert len(worker.calls) == 4 and run["skipped"] == 4 and run["failed"] == 0
	assert run["state"] == "complete" and not run.get("breaker_kind")
	assert {a["state"] for a in _attempts(worker)} == {"skipped"}
	assert {a["error_kind"] for a in _attempts(worker)} == {"not_eligible"}


@pytest.mark.parametrize("kind", ["excluded", "no_input", "made_up"])
def test_a_kind_that_is_not_ai_fixs_is_recorded_internal_and_failed(worker, kind):
	"""The hand-rolled skip kinds (``excluded``, ``no_input``) are gone: ai_fix never raises
	them, and only its own ``AI_SKIP_KINDS`` skip."""

	def fail():
		raise ai_fix.AiFixError("fake", kind=kind)

	worker.count = 1
	worker.send_hook = fail
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	[attempt] = _attempts(worker)
	assert attempt["state"] == "failed" and attempt["error_kind"] == "internal"
	assert not hasattr(worker.jobs, "_SKIP_KINDS")


@pytest.mark.parametrize("kind", ["context", "refused"])
def test_per_prompt_kinds_are_recorded_under_their_own_name(worker, kind):
	def fail():
		raise ai_fix.AiFixError("fake", kind=kind)

	worker.count = 5
	worker.send_hook = fail
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	run = worker.journal.run
	assert {a["error_kind"] for a in _attempts(worker)} == {kind}
	assert run["failed"] == 5 and not run.get("breaker_kind") and run["state"] == "complete"


@pytest.mark.parametrize("bad", [-5, True, 3.5, "7", None])
def test_a_reported_token_count_that_is_not_a_plain_count_is_recorded_as_zero(worker, monkeypatch, bad):
	"""M26: the engine's last check before the store: a negative, boolean or non-integer count
	must not reach (and fail) the settlement, nor be charged to the session."""
	worker.count = 1
	select = worker.jobs._next_item

	def next_item(run, memo):
		item = select(run, memo)
		if item is None:
			return None

		def send(timeout):
			worker.calls.append((item.target, timeout))
			return {"suggestion": "fake", "tokens": {"total_tokens": bad}, "usage_complete": True}

		return worker.jobs.PreparedItem(
			item.kind, item.target, item.input_hash, send, item.valid, item.persist
		)

	monkeypatch.setattr(worker.jobs, "_next_item", next_item)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	[attempt] = _attempts(worker)
	assert attempt["state"] == "succeeded" and attempt["tokens_reported"] == 0
	assert worker.journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_tokens_spent"] == 0


@pytest.mark.parametrize("failing", [-5, True])
def test_a_failures_reported_usage_that_is_not_a_plain_count_is_recorded_as_zero(worker, failing):
	def fail():
		error = ai_fix.AiFixError("fake", kind="timeout")
		error.usage = {"total_tokens": failing}
		raise error

	worker.count = 1
	worker.send_hook = fail
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	[attempt] = _attempts(worker)
	assert attempt["state"] == "failed" and attempt["tokens_reported"] == 0


def test_the_skip_and_fatal_kinds_have_one_source():
	"""Q2: no copy of #72's lists in the engine."""
	source = (_PKG / "ai_refresh_store.py").read_text() + (_PKG / "ai_jobs.py").read_text()
	assert "ai_fix.AI_FATAL_KINDS" in source and "ai_fix.AI_SKIP_KINDS" in source
	assert ai_jobs._ERROR_KINDS >= ai_fix.AI_SKIP_KINDS | ai_fix.AI_FATAL_KINDS | {
		"context",
		"refused",
		"rate_limited",
	}
	assert not ai_fix.AI_FATAL_KINDS & store.PER_PROMPT_KINDS


# --- Q3: the overlap with #72's Refresh ---------------------------------------------------


def test_the_engine_refuses_to_start_while_a_refresh_holds_the_session(admitting, monkeypatch):
	queue = admitting
	queue.journal.db.rows[queue.journal.mod.RUN] = {}
	monkeypatch.setattr(queue.jobs, "_refresh_flag_held", lambda session_uuid: True)
	out = _start(queue)
	assert out["status"] == "refused" and out["reason"] == "refresh_running"
	assert "already running" in out["message"]
	assert not queue.journal.db.rows[queue.journal.mod.RUN] and not queue.sent
	assert queue.journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_refresh_count"] == 0


def test_the_engine_starts_when_no_refresh_holds_the_session(admitting):
	queue = admitting
	queue.journal.db.rows[queue.journal.mod.RUN] = {}
	assert _start(queue)["status"] == "queued"
	assert queue.journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_refresh_count"] == 1


def test_a_running_engine_run_is_attached_to_even_while_the_flag_is_held(admitting, monkeypatch):
	"""An existing run is shown, not refused: attaching only reads."""
	queue = admitting
	monkeypatch.setattr(queue.jobs, "_refresh_flag_held", lambda session_uuid: True)
	assert _start(queue)["status"] == "already_running"


def test_a_live_run_is_read_in_a_fresh_transaction_and_an_expired_one_is_not_live(queue, monkeypatch):
	"""K3's lookup: the caller's read view is closed first, and only a live run holds the
	session (a dead run is taken over by the next engine click, not waited for)."""
	opened = []
	inner = queue.jobs._transaction
	monkeypatch.setattr(queue.jobs, "_transaction", lambda fn: opened.append(True) or inner(fn))
	run = queue.jobs.live_run("fake-session-doc")
	assert run["name"] == "fake-run" and len(opened) == 2  # the commit, then the read
	queue.journal.run["lease_until"] = 40  # now is 50
	assert queue.jobs.live_run("fake-session-doc") is None
	queue.journal.run.update(lease_until=1800, state="complete")
	assert queue.jobs.live_run("fake-session-doc") is None
	assert queue.jobs.live_run("another-session") is None


def test_the_engine_reads_the_flag_fresh_from_redis(monkeypatch):
	"""#72's reader: a raw GET of the flag key (never ``frappe.local.cache``); a hold is a
	value, an unreadable Redis is no hold (a cache failure never blocks a refresh)."""
	from optimus import redis_keys
	from optimus.tests.gate_fakes import SESSION_UUID, FakeCache

	cache = FakeCache()
	cache.get = lambda key: cache.calls.append(("get", key)) or cache.store.get(key)
	monkeypatch.setattr(api, "frappe", SimpleNamespace(cache=cache))
	key = "site|" + redis_keys.ai_refresh_inflight(SESSION_UUID)
	assert api._refresh_flight_held(SESSION_UUID) is False
	cache.store[key] = b"token"
	assert api._refresh_flight_held(SESSION_UUID) is True
	assert cache.calls == [("get", key), ("get", key)]  # nothing cached between the two reads

	def down(key):
		raise ConnectionError("fake redis down")

	cache.get = down
	assert api._refresh_flight_held(SESSION_UUID) is False


# --- selection counts (item 5) -----------------------------------------------------------


def _selection(gated, excluded):
	chosen = analyze.AiSelection([])
	chosen.gated, chosen.excluded = gated, excluded
	return chosen


def test_start_keeps_the_selections_gated_and_excluded_counts_on_the_run(admitting, monkeypatch):
	queue = admitting
	queue.journal.db.rows[queue.journal.mod.RUN] = {}
	monkeypatch.setattr(queue.jobs, "_selection_counts", lambda *a, **kw: (3, 2))
	out = _start(queue)
	assert out["refresh"]["gated"] == 3 and out["refresh"]["excluded"] == 2
	[run] = queue.journal.db.rows[queue.journal.mod.RUN].values()
	assert run["gated_count"] == 3 and run["excluded_count"] == 2


def test_the_counts_come_from_the_selection_before_it_is_sliced(work, monkeypatch):
	"""``AiSelection.gated`` / ``.excluded`` as ``eligible_findings`` returns them."""
	seen = []

	def eligible(rows, cfg, **kw):
		seen.append(kw)
		return _selection(4, 1)

	monkeypatch.setattr(analyze, "eligible_findings", eligible)
	monkeypatch.setattr("optimus.settings.get_config", lambda: SimpleNamespace())
	assert work.jobs._selection_counts("fake-session-doc", True) == (4, 1)
	assert seen == [{}]  # counts do not depend on what was asked for, only on the settings


def test_no_counts_when_no_fix_is_asked_for_or_the_findings_toggle_is_off(work, monkeypatch):
	monkeypatch.setattr(analyze, "eligible_findings", lambda *a, **kw: _selection(9, 9))
	assert work.jobs._selection_counts("fake-session-doc", False) == (0, 0)
	monkeypatch.setattr(ai_fix, "is_available", lambda **kw: False)
	assert work.jobs._selection_counts("fake-session-doc", True) == (0, 0)


def test_a_resume_asks_what_the_run_it_continues_asked(work, monkeypatch):
	monkeypatch.setattr(analyze, "eligible_findings", lambda *a, **kw: _selection(9, 9))
	work.journal.db.rows[work.journal.mod.RUN]["old-run"] = {"name": "old-run", "include_fixes": 0}
	assert work.jobs._selection_counts("fake-session-doc", True, "old-run") == (0, 0)
	work.journal.db.rows[work.journal.mod.RUN]["old-run"]["include_fixes"] = 1
	assert work.jobs._selection_counts("fake-session-doc", False, "old-run") == (9, 9)


def test_a_failed_count_is_logged_with_the_session_and_the_run_goes_on(work, monkeypatch):
	rows = []
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda title, exc=None, **kw: rows.append((title, kw)))

	def broken(*a, **kw):
		raise ValueError("fake selection failure")

	monkeypatch.setattr(analyze, "eligible_findings", broken)
	assert work.jobs._selection_counts("fake-session-doc", True) == (0, 0)
	assert rows == [("optimus ai refresh selection", {"session_uuid": None, "docname": "fake-session-doc"})]


def test_the_public_state_exposes_the_counts(queue):
	run = {**queue.journal.run, "gated_count": 3, "excluded_count": 2}
	out = queue.jobs.public_state(run)
	assert out["gated"] == 3 and out["excluded"] == 2
	assert queue.jobs.public_state({**queue.journal.run})["gated"] == 0


@pytest.mark.parametrize("bad", [-1, True, 2**31, 1.5, "3"])
def test_admission_rejects_a_count_that_is_not_a_count(journal, bad):
	from optimus.tests.test_ai_refresh_journal import admit

	journal.db.rows[journal.mod.RUN] = {}
	with pytest.raises(ValueError):
		admit(journal, gated=bad)
	with pytest.raises(ValueError):
		admit(journal, excluded=bad)


# --- grounding (item 6) ------------------------------------------------------------------


def _prepared_grounding(work, monkeypatch, rows=1):
	"""Fix items for ``rows`` findings in one slice (one memo); the grounding each was given."""
	seen = []
	monkeypatch.setattr(
		work.jobs,
		"_build_payload",
		lambda row, cache, grounding, *, principal: seen.append(grounding) or {"title": row.title},
	)
	return seen


def test_fix_prompts_get_the_slow_query_evidence_the_synchronous_refresh_gets(work, monkeypatch):
	"""#16: ``_ai_evidence_scope`` supplies ``evidence_lookup`` and ``tracked_apps`` to
	``_ai_payload_for_finding`` in ``_run_ai_backfill``; the background path passes them too."""
	seen = _prepared_grounding(work, monkeypatch)
	lookup = object()
	monkeypatch.setattr(analyze, "_ai_evidence_scope", lambda: (lookup, ("fake_app",)))
	work.jobs._next_item(work.journal.run, {})
	[grounding] = seen
	assert grounding["evidence_lookup"] is lookup and grounding["tracked_apps"] == ("fake_app",)


def test_the_slice_builds_its_phase2_index_and_evidence_once(work, monkeypatch):
	"""#14 / #16: once per slice (the slice's memo), not once per finding."""
	seen = _prepared_grounding(work, monkeypatch)
	built = []
	monkeypatch.setattr(analyze, "_phase2_index_for", lambda doc: built.append(doc.get("name")) or {"k": 1})
	monkeypatch.setattr(analyze, "_ai_evidence_scope", lambda: built.append("evidence") or (None, ()))
	memo = {}
	for _ in range(4):
		work.jobs._next_item(work.journal.run, memo)
	assert len(seen) == 4 and built == ["fake-session-doc", "evidence"]
	assert all(g["phase2_index"] == {"k": 1} for g in seen)


def test_a_failed_phase2_index_is_logged_once_per_slice_with_the_session(work, monkeypatch):
	rows = []
	seen = _prepared_grounding(work, monkeypatch)

	def fail(doc):
		raise ValueError("fake corrupt phase 2 data")

	monkeypatch.setattr(analyze.renderer, "_build_line_drilldown_callsite_index", fail)
	monkeypatch.setattr(analyze, "_phase2_index_for", _REAL_PHASE2_INDEX)
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda title, exc=None, **kw: rows.append((title, kw)))
	monkeypatch.setattr(analyze, "_ai_evidence_scope", lambda: (None, ()))
	memo = {}
	for _ in range(3):
		work.jobs._next_item(work.journal.run, memo)
	assert len(seen) == 3 and seen[0]["phase2_index"] == {}
	assert [r[0] for r in rows] == ["optimus ai phase 2 context"]
	assert rows[0][1]["docname"] == "fake-session-doc"


def test_a_failed_evidence_scope_is_logged_and_the_prompt_goes_without_advice(work, monkeypatch):
	rows = []
	seen = _prepared_grounding(work, monkeypatch)
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda title, exc=None, **kw: rows.append((title, kw)))

	def fail():
		raise ValueError("fake evidence failure")

	monkeypatch.setattr(analyze, "_ai_evidence_scope", fail)
	memo = {}
	work.jobs._next_item(work.journal.run, memo)
	work.jobs._next_item(work.journal.run, memo)
	assert [g["evidence_lookup"] for g in seen] == [None, None]
	assert [r[0] for r in rows] == ["optimus ai refresh evidence"]
	assert rows[0][1]["docname"] == "fake-session-doc" and rows[0][1]["run_id"] == "fake-run"


def test_the_recordings_memo_keeps_only_recordings(work, monkeypatch):
	"""#20: the memo #72 documents as recordings-only is its own dict inside the slice's."""
	_prepared_grounding(work, monkeypatch)
	captured = []
	monkeypatch.setattr(
		analyze, "load_recordings_light", lambda doc, uuids=None, memo=None: captured.append(memo) or []
	)
	memo = {}
	work.jobs._next_item(work.journal.run, memo)
	[recordings] = captured
	assert recordings is memo["recordings"] and recordings is not memo
	assert "source" not in recordings and "phase2_index" not in recordings


# --- _valid_item (E4) --------------------------------------------------------------------


def test_an_unrelated_session_save_no_longer_discards_a_paid_fix_answer(work, monkeypatch):
	"""E4: a fix item is guarded by its fingerprint and the Finding lock, not by the session's
	``modified``: a counter, a Desk edit or a Phase 2 save during the HTTP call keeps the answer."""
	monkeypatch.setattr("frappe.utils.now_datetime", lambda: "fake-new-modified")
	work.parent["modified"] = "t1"
	item = work.jobs._next_item(work.journal.run, {})
	work.parent["modified"] = "t2"
	with work.journal.db.transaction():
		assert item.valid() is True
		assert item.persist({"suggestion": "paid answer"}) is True
	assert "paid answer" in work.row["llm_fix_json"]


def test_a_changed_finding_still_discards_a_fix_answer(work):
	work.parent["modified"] = "t1"
	item = work.jobs._next_item(work.journal.run, {})
	work.row["title"] = "edited while the call ran"
	with work.journal.db.transaction():
		assert item.valid() is False


def test_the_steps_item_still_needs_an_unchanged_session(work, monkeypatch):
	"""Steps rewrites ``notes``, which the Desk form edits: any save since it was prepared
	makes the answer stale."""
	work.journal.run.update(include_fixes=0, include_steps=1)
	monkeypatch.setattr(analyze, "_actions_for_humanizer", lambda recordings: [{"label": "fake action"}])
	work.parent["modified"] = "t1"
	item = work.jobs._next_item(work.journal.run, {})
	assert item.kind == "steps"
	with work.journal.db.transaction():
		assert item.valid() is True
	work.parent["modified"] = "t2"
	with work.journal.db.transaction():
		assert item.valid() is False


# --- the cap across a resume (E3) --------------------------------------------------------


def _spent(work, run_id, count, kind="fix", state="succeeded"):
	attempts = work.journal.db.rows.setdefault(work.journal.mod.ATTEMPT, {})
	for i in range(count):
		attempts[f"{run_id}-{kind}-{i}"] = {
			"run_id": run_id,
			"session_name": work.parent["name"],
			"kind": kind,
			"target_name": f"{run_id}-done-{i}",
			"input_hash": "fake",
			"state": state,
		}


def _chain(work, *names):
	"""The fixture run resumed from ``names[0]``, which resumed from ``names[1]`` and so on."""
	runs = work.journal.db.rows[work.journal.mod.RUN]
	previous = None
	for name in reversed(names):
		runs[name] = {"name": name, "session_name": work.parent["name"], "resume_from": previous}
		previous = name
	work.journal.run["resume_from"] = names[0]


def test_a_resume_continues_the_cap_it_does_not_grant_it_again(work):
	work.journal.run["cap"] = 3
	_chain(work, "run-1")
	_spent(work, "run-1", 3)
	assert work.jobs._next_item(work.journal.run, {}) is None
	assert work.journal.run["total_items"] == 0


def test_the_cap_counts_the_whole_resume_chain(work):
	work.journal.run["cap"] = 5
	_chain(work, "run-2", "run-1")
	_spent(work, "run-1", 2)
	_spent(work, "run-2", 2)
	_spent(work, work.journal.run["name"], 1)
	assert work.jobs._next_item(work.journal.run, {}) is None
	work.journal.run["cap"] = 6
	assert work.jobs._next_item(work.journal.run, {}).kind == "fix"


def test_only_fix_attempts_spend_the_cap(work):
	work.journal.run["cap"] = 1
	_chain(work, "run-1")
	_spent(work, "run-1", 4, kind="steps")
	assert work.jobs._next_item(work.journal.run, {}).kind == "fix"


def test_a_run_that_resumed_nothing_keeps_its_own_cap(work):
	work.journal.run["cap"] = 2
	_spent(work, "unrelated-run", 9)
	assert work.jobs._next_item(work.journal.run, {}).kind == "fix"


def test_a_cycle_in_the_chain_ends_the_walk(work):
	runs = work.journal.db.rows[work.journal.mod.RUN]
	runs["a"] = {"name": "a", "resume_from": "b"}
	runs["b"] = {"name": "b", "resume_from": "a"}
	work.journal.run["resume_from"] = "a"
	assert work.jobs._chain(work.journal.run) == ["fake-run", "a", "b"]


# --- the signature hash (E5) -------------------------------------------------------------


def _many(work, monkeypatch, n):
	rows = [
		Doc(deepcopy(work.row), name=f"finding-{i}", title=f"fake finding {i}", estimated_impact_ms=100 - i)
		for i in range(n)
	]
	for row in rows:
		work.journal.db.rows["Optimus Finding"][row["name"]] = row

	def get_doc(table, name):
		return Doc(**deepcopy(work.parent), findings=[Doc(deepcopy(r)) for r in rows])

	monkeypatch.setattr(work.jobs.frappe, "get_doc", get_doc)
	calls = []
	real = work.jobs._signature
	monkeypatch.setattr(work.jobs, "_signature", lambda *a, **kw: calls.append(a[1:]) or real(*a, **kw))
	return rows, calls


def test_selection_hashes_only_the_chosen_item_when_nothing_is_uncertain(work, monkeypatch):
	"""E5: it hashed every row on every call, O(N^2) over a run."""
	_, calls = _many(work, monkeypatch, 60)
	item = work.jobs._next_item(work.journal.run, {})
	assert item is not None and len(calls) == 1


def test_selection_hashes_only_the_rows_an_uncertain_attempt_names(work, monkeypatch):
	rows, calls = _many(work, monkeypatch, 60)
	first = work.jobs._next_item(work.journal.run, {})
	calls.clear()
	work.journal.db.rows[work.journal.mod.ATTEMPT] = {
		"older": {
			"run_id": "old-run",
			"session_name": work.parent["name"],
			"kind": "fix",
			"target_name": first.target,
			"input_hash": first.input_hash,
			"state": "uncertain",
		}
	}
	second = work.jobs._next_item(work.journal.run, {})
	assert second.target != first.target
	assert len(calls) == 2  # the one named row, and the chosen item


def test_the_steps_selection_hashes_only_its_own_fingerprint_when_nothing_is_uncertain(work, monkeypatch):
	work.journal.run.update(include_fixes=0, include_steps=1)
	monkeypatch.setattr(analyze, "_actions_for_humanizer", lambda recordings: [{"label": "fake action"}])
	calls = []
	real = work.jobs._signature
	monkeypatch.setattr(work.jobs, "_signature", lambda *a, **kw: calls.append(a) or real(*a, **kw))
	item = work.jobs._next_item(work.journal.run, {})
	assert item.kind == "steps" and len(calls) == 1


# --- Error Log titles and context (item 4) -----------------------------------------------


def _log_calls(module):
	tree = ast.parse((_PKG / module).read_text(encoding="utf-8"))
	for node in ast.walk(tree):
		if isinstance(node, ast.Call):
			name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
			if name in ("log_ai_failure", "_run_ai_step"):
				yield name, node


def test_the_engine_is_in_the_title_audit():
	from optimus.tests import test_ai_error_log_titles as titles

	assert "ai_jobs.py" in titles._MODULES
	found = {t for t, modules in titles._code_titles().items() if "ai_jobs.py" in modules}
	assert found == {
		"optimus ai refresh worker",
		"optimus ai refresh cleanup",
		"optimus ai refresh item",
		"optimus ai refresh result storage",
		"optimus ai refresh enqueue",
		"optimus ai refresh worker availability",
		"optimus ai refresh recovery",
		"optimus ai refresh release",
		"optimus ai refresh selection",
		"optimus ai refresh evidence",
		"optimus ai refresh discarded",
		"optimus ai refresh duplicate blocked",
	}


def test_every_engine_title_is_lowercase_and_listed_in_both_docs():
	from optimus.tests import test_ai_error_log_titles as titles

	engine = {t for t, modules in titles._code_titles().items() if "ai_jobs.py" in modules}
	refresh_doc = (_DOCS / "AI-REFRESH.md").read_text(encoding="utf-8")
	fixing = (_DOCS / "AI-FIXING.md").read_text(encoding="utf-8")
	for title in engine:
		assert title.startswith("optimus ai refresh") and title == title.lower(), title
		assert f"`{title}`" in fixing, f"{title} is not in AI-FIXING section 5.3"
		assert f"`{title}`" in refresh_doc, f"{title} is not in AI-REFRESH.md"


def test_every_engine_row_names_its_session_and_run():
	"""K2: ``docname`` and ``run_id`` on every row the engine writes (the settlement rows
	and the sweep's pass them through one ``reference`` / the row's own keywords)."""
	# Written before any run exists (so no run_id): they carry the session only.
	# (``optimus ai phase 2 context`` is #72's shared title, written by analyze, so it is not
	# scanned here and has the session but no run.)
	before_a_run = {"optimus ai refresh worker availability", "optimus ai refresh selection"}
	missing = []
	for name, call in _log_calls("ai_jobs.py"):
		keys = {kw.arg for kw in call.keywords}
		if name == "log_ai_failure" and call.args and isinstance(call.args[0], ast.Constant):
			title = call.args[0].value
		elif name == "_run_ai_step":
			title = next((kw.value.value for kw in call.keywords if kw.arg == "title"), None)
		else:
			continue
		if not isinstance(title, str):
			continue
		needed = {"docname"} if title in before_a_run else {"docname", "run_id"}
		has_splat = any(kw.arg is None for kw in call.keywords)
		if not (has_splat or needed <= keys):
			missing.append((title, call.lineno))
	assert not missing, missing


def test_the_engine_uses_the_shared_interrupt_guard_and_helper():
	text = (_PKG / "ai_jobs.py").read_text(encoding="utf-8")
	assert "ai_fix._InterruptGuard" not in text and "safe_call.InterruptGuard" in text
	assert "_run_ai_step" in text


def test_the_worker_row_names_the_session_and_run(worker, monkeypatch):
	rows = []
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda title, exc=None, **kw: rows.append((title, kw)))

	def outage(*a, **kw):
		raise OSError("fake storage outage")

	monkeypatch.setattr(worker.journal.mod, "settle_attempt", outage)
	worker.count = 1
	with pytest.raises(worker.jobs.RefreshSliceFailed):
		worker.jobs.run_ai_refresh_slice("fake-run", 0)
	[(title, context)] = [r for r in rows if r[0] == "optimus ai refresh worker"]
	assert context["docname"] == "fake-session-doc" and context["run_id"] == "fake-run"


def test_the_item_row_names_the_attempt_kind_and_item(worker, monkeypatch):
	rows = []
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda title, exc=None, **kw: rows.append((title, kw)))

	def fail():
		raise ai_fix.AiFixError("fake", kind="timeout")

	worker.count = 1
	worker.send_hook = fail
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	[(title, context)] = [r for r in rows if r[0] == "optimus ai refresh item"]
	[attempt] = _attempts(worker)
	assert context["docname"] == "fake-session-doc" and context["run_id"] == "fake-run"
	assert context["attempt"] == attempt["name"] and context["kind"] == "timeout" and context["item"] == "fix"


def test_the_worker_availability_row_names_the_session(queue, monkeypatch):
	rows = []
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda title, exc=None, **kw: rows.append((title, kw)))
	background = SimpleNamespace(
		get_queue=lambda name: name, get_workers=lambda queue: (_ for _ in ()).throw(OSError("fake"))
	)
	monkeypatch.setitem(__import__("sys").modules, "frappe.utils.background_jobs", background)
	assert queue.jobs._workers_listening("fake-session-doc") is None
	assert rows == [
		("optimus ai refresh worker availability", {"session_uuid": None, "docname": "fake-session-doc"})
	]


def test_a_failed_enqueue_row_names_the_session_and_run(queue, monkeypatch):
	rows = []
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda title, exc=None, **kw: rows.append((title, kw)))

	def fail(*a, **kw):
		raise ConnectionError("fake Redis down")

	monkeypatch.setattr(queue.jobs.frappe.enqueue, "__wrapped__", fail)
	assert queue.jobs._dispatch_pending("fake-run", "fake-session-doc") is False
	assert rows == [
		(
			"optimus ai refresh enqueue",
			{"session_uuid": None, "docname": "fake-session-doc", "run_id": "fake-run"},
		)
	]


def test_a_job_timeout_in_the_worker_check_or_the_enqueue_escapes_fresh(queue, monkeypatch):
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = Timeout("fake timeout")
	monkeypatch.setattr(ai_fix, "log_ai_failure", _REAL_LOG_AI_FAILURE)  # it raises the timeout again

	def interrupted(*a, **kw):
		raise original

	monkeypatch.setattr(queue.jobs.frappe.enqueue, "__wrapped__", interrupted)
	with pytest.raises(Timeout) as caught:
		queue.jobs._dispatch_pending("fake-run", "fake-session-doc")
	assert caught.value is not original and caught.value.__context__ is None

	background = SimpleNamespace(get_queue=lambda name: name, get_workers=interrupted)
	monkeypatch.setitem(__import__("sys").modules, "frappe.utils.background_jobs", background)
	with pytest.raises(Timeout) as caught:
		queue.jobs._workers_listening("fake-session-doc")
	assert caught.value is not original and caught.value.__context__ is None


# --- public names, docs (T4, T6, #19) -----------------------------------------------------


def test_the_engine_calls_only_public_names_of_the_store():
	tree = ast.parse((_PKG / "ai_jobs.py").read_text(encoding="utf-8"))
	private = {
		node.attr
		for node in ast.walk(tree)
		if isinstance(node, ast.Attribute)
		and isinstance(node.value, ast.Name)
		and node.value.id == "store"
		and node.attr.startswith("_")
	}
	assert not private, private
	assert callable(store.lock_run) and callable(store.owns)


def test_the_payload_builder_says_the_principal_is_reserved():
	doc = " ".join(ai_jobs._build_payload.__doc__.split())
	assert "reserved for PR-4" in doc and "scoped to the requesting principal" not in doc


def test_the_changelog_documents_the_stored_answer_shape():
	text = (_PKG.parent / "CHANGELOG.md").read_text(encoding="utf-8")
	section = text.split("## [0.12.70]", 1)[1].split("\n## [", 1)[0] if "## [0.12.70]" in text else ""
	assert "usage_complete" in section and "llm_fix_json" in section


def test_the_docs_describe_the_refresh_count_as_one_bump_at_acceptance():
	text = (_DOCS / "AI-REFRESH.md").read_text(encoding="utf-8")
	flat = " ".join(text.split())
	assert "counted once per accepted user refresh" in flat and "No ending of a run counts" in flat
	assert "completion_counted" not in flat and "still counts as a refresh" not in flat


# --- the kinds are #72's (F4) -------------------------------------------------------------


def test_the_recorded_kinds_are_derived_from_ai_fix_plus_named_engine_only_kinds():
	assert ai_jobs._ERROR_KINDS == frozenset(ai_fix.KIND_HINTS) | ai_jobs._ENGINE_ONLY_KINDS
	assert "internal" in ai_jobs._ERROR_KINDS  # the fallback is itself a kind


@pytest.mark.parametrize("kind", sorted(ai_fix.KIND_HINTS))
def test_every_ai_fix_kind_is_recorded_under_its_own_name(worker, kind):
	"""A kind #72 adds to ``KIND_HINTS`` is covered here without a change to the engine: it is
	never recorded as ``internal``, and a fatal one stops the run at once."""

	def fail():
		raise ai_fix.AiFixError("fake", kind=kind)

	worker.count = 4
	worker.send_hook = fail
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	attempts = _attempts(worker)
	assert {a["error_kind"] for a in attempts} == {kind}
	if kind in ai_fix.AI_FATAL_KINDS:
		assert len(attempts) == 1 and worker.journal.run["breaker_kind"] == kind
	elif kind in ai_fix.AI_SKIP_KINDS | store.PER_PROMPT_KINDS:  # never trip the breaker
		assert len(attempts) == 4 and not worker.journal.run.get("breaker_kind")
	else:  # three in a row
		assert len(attempts) == 3 and worker.journal.run["breaker_kind"] == kind


# --- the cap over the longest resume chain (F3) -------------------------------------------


def test_the_cap_counts_a_full_chain_of_three_resumes(work):
	"""The retry limit allows 3 resumes, 4 runs; the first run's attempts still count."""
	work.journal.run["cap"] = 7
	_chain(work, "run-3", "run-2", "run-1")
	_spent(work, "run-1", 2)
	_spent(work, "run-2", 2)
	_spent(work, "run-3", 2)
	_spent(work, work.journal.run["name"], 1)
	assert work.jobs._chain(work.journal.run) == ["fake-run", "run-3", "run-2", "run-1"]
	assert work.jobs._next_item(work.journal.run, {}) is None
	work.journal.run["cap"] = 8
	assert work.jobs._next_item(work.journal.run, {}).kind == "fix"
