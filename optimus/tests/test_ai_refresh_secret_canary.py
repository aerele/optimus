"""Run the existing secret canary's provider failures through the SQL worker."""

import pytest

from optimus import ai_fix, ai_jobs
from optimus.tests import test_ai_refresh_journal as journal_tests
from optimus.tests import test_ai_secret_canary as base

pytestmark = pytest.mark.rq


@pytest.fixture
def canary(monkeypatch, request):
	return base.canary.__wrapped__(monkeypatch, request)


@pytest.mark.parametrize("canary", [(s, p) for s in base._SCENARIOS for p in base._PROVIDERS], indirect=True)
@pytest.mark.parametrize("kind", ["fix", "steps"])
def test_queued_worker_keeps_all_secret_failure_channels_clean(canary, monkeypatch, kind):
	import frappe

	sinks, scenario, job_timeout = canary
	journal = journal_tests.journal.__wrapped__(monkeypatch)
	monkeypatch.setattr(ai_jobs, "frappe", frappe)
	monkeypatch.setattr(ai_jobs, "_now", lambda: 20)
	monkeypatch.setattr(ai_jobs.time, "monotonic", lambda: 20)
	monkeypatch.setattr(ai_jobs, "slice_seconds", lambda: 120)
	monkeypatch.setattr(ai_jobs, "_call_timeout", lambda: 60)
	monkeypatch.setattr(ai_jobs, "_check_run", lambda *a, **kw: None)
	monkeypatch.setattr(ai_jobs, "_read_run", lambda rid: journal.db.get_value(journal.mod.RUN, rid, "*"))

	def transaction(fn):
		with journal.db.transaction():
			out = fn()
		frappe.db.commit()
		return out

	monkeypatch.setattr(ai_jobs, "_transaction", transaction)

	def next_item(run, memo):
		if journal.db.rows.get(journal.mod.ATTEMPT):
			return None

		def send(timeout):
			if kind == "fix":
				return ai_fix.suggest_fix(
					dict(base._FINDING), timeout=timeout, session_uuid="fake-session", docname="fake-doc"
				)
			usage = ai_fix.Usage()
			text = ai_fix.humanize_steps(
				[dict(x) for x in base._ACTIONS],
				usage_out=usage,
				timeout=timeout,
				session_uuid="fake-session",
				docname="fake-doc",
			)
			return {"notes": text, "tokens": usage, "usage_complete": usage.complete}

		return ai_jobs.PreparedItem(
			kind, "fake-target", "fake-fingerprint", send, lambda: True, lambda result: True
		)

	monkeypatch.setattr(ai_jobs, "_next_item", next_item)
	base._drive(
		"ai_jobs.run_ai_refresh_slice",
		ai_jobs.run_ai_refresh_slice,
		lambda: (("fake-run", 0), {}),
		sinks,
		scenario,
		job_timeout,
	)
	frappe.db.rollback()
	assert sinks.posts == 0 if scenario == "non_latin_key" else sinks.posts > 0
	channels = {
		"stored": [(e, t) for e, t, _ in sinks.stored],
		"sentry": sinks.sentry,
		"stack": sinks.stack,
		"escaped": sinks.escaped,
		"returned": sinks.returned,
		"wire": sinks.wire,
		"pending": sinks.pending,
	}
	for channel, items in channels.items():
		for _entry, text in items:
			assert all(mark not in text for mark in base.KEY_MARKS), f"key leaked via {channel}"
	for _entry, text, check_prompt in sinks.stored:
		if check_prompt:
			assert base.PII not in text
	assert not sinks.sentry
	if scenario not in base._NO_ROW_SCENARIOS and scenario != "rq_timeout":
		assert sinks.stored, "the worker must report a provider failure"
	if scenario in {"system_exit", "rq_timeout"}:
		assert journal.run["state"] == "interrupted" and journal.run["uncertain"] == 1
		assert sinks.escaped_types == [SystemExit if scenario == "system_exit" else job_timeout]
	if scenario == "malformed_usage":
		assert journal.run["completed"] == 1 and journal.run["usage_incomplete"] == 1
