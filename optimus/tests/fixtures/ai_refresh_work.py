"""Worker inputs are tied to the initiating user and current SQL state."""


import json
import sys
from copy import deepcopy
from importlib import import_module
from types import ModuleType, SimpleNamespace

import pytest

from optimus import ai_fix, analyze
from optimus.tests.fixtures.ai_refresh_journal import journal as _journal_fixture  # noqa: F401


class Doc(dict):
	def __getattr__(self, key):
		if key.startswith("__"):
			raise AttributeError(key)
		return self.get(key)

	def as_dict(self):
		return dict(self)


@pytest.fixture(name="work")
def work(journal, monkeypatch):
	jobs = import_module("optimus.ai_jobs")
	run = journal.run
	run.update(
		requested_by="fake-owner",
		include_fixes=1,
		include_steps=0,
		cap=20,
		regenerate_all=0,
		requested_at_epoch=10,
		retry_uncertain=0,
	)
	parent = journal.db.rows["Optimus Session"]["fake-session-doc"]
	parent.update(owner="fake-owner", notes="", title="fake title", recordings_file="", actions=[])
	row = Doc(
		name="finding-1",
		parent=parent["name"],
		parenttype="Optimus Session",
		parentfield="findings",
		finding_type="N+1 Query",
		severity="High",
		estimated_impact_ms=10,
		title="fake finding",
		technical_detail_json="{}",
		llm_fix_json="",
		action_ref="0",
	)
	journal.db.rows["Optimus Finding"] = {row.name: row}
	journal.db.rows["User"] = {"fake-owner": {"name": "fake-owner", "enabled": 1}}
	checks, sends = [], []
	env = SimpleNamespace(
		jobs=jobs,
		journal=journal,
		parent=parent,
		row=row,
		checks=checks,
		sends=sends,
		can_read=True,
		can_write=False,
		roles=["Optimus User"],
		child_loads=0,
		user_permissions={},  # ``get_user_permissions(user)``: none
		hooks={},  # ``frappe.get_hooks("has_permission")``: no hook for a session
	)

	def get_doc(table, name=None):
		if isinstance(table, dict):  # ``frappe.get_doc(values)``: a document from values, no read
			return Doc(deepcopy(table))
		assert table == "Optimus Session" and name == parent["name"]
		env.child_loads += 1  # a whole document: every child table is read
		return Doc(**deepcopy(parent), findings=[Doc(deepcopy(row))])

	def permission(table, kind, doc, *, user):
		checks.append((kind, user))
		return env.can_read if kind == "read" else env.can_write

	monkeypatch.setattr(
		jobs,
		"frappe",
		SimpleNamespace(
			db=journal.db,
			get_doc=get_doc,
			get_all=journal.db.get_all,
			has_permission=permission,
			get_roles=lambda user: env.roles,
			get_hooks=lambda hook: env.hooks if hook == "has_permission" else [],
		),
	)
	user_permission = ModuleType("frappe.core.doctype.user_permission.user_permission")
	user_permission.get_user_permissions = lambda user=None: env.user_permissions
	monkeypatch.setitem(sys.modules, user_permission.__name__, user_permission)
	monkeypatch.setattr(jobs, "_now", lambda: 20)
	monkeypatch.setattr("optimus.settings.get_config", lambda: SimpleNamespace(
		ai_excluded_finding_types=[], ai_enabled=True, ai_auto_suggest=True, ai_suggest_findings=True,
	))
	monkeypatch.setattr(ai_fix, "is_available", lambda **kw: True)
	monkeypatch.setattr(
		jobs,
		"_build_payload",
		lambda row, cache, grounding, *, principal: {"title": row.title, "principal": principal},
	)
	monkeypatch.setattr(analyze, "phase2_index_for", lambda doc: {})
	monkeypatch.setattr(analyze, "load_recordings_light", lambda *a, **kw: [])
	monkeypatch.setattr(
		ai_fix,
		"suggest_fix",
		lambda payload, **kw: (
			sends.append((payload, kw))
			or {"suggestion": "fake answer", "tokens": {"total_tokens": 7}, "usage_complete": True}
		),
	)

	def transaction(fn):
		with journal.db.transaction():
			return fn()

	monkeypatch.setattr(jobs, "_transaction", transaction)
	with journal.db.transaction():
		journal.mod.claim(
			"fake-run",
			docname="fake-session-doc",
			slice_no=0,
			worker_token="worker",
			now=10,
			lease_seconds=300,
		)
	return env


def _v15_has_permission(work, decide):
	"""Frappe v15's ``frappe.has_permission`` for one document: given a name, it loads the whole
	document (``frappe.get_doc``: every child table) before it checks; given a document, it
	checks that one. ``decide(ptype, doc, user)`` stands for the owner, share and role checks,
	which read the document's ``owner`` and ``name``. Returns the documents it checked."""
	seen = []

	def has_permission(doctype, ptype, doc, *, user):
		if isinstance(doc, str):
			doc = work.jobs.frappe.get_doc(doctype, doc)
		seen.append(doc)
		return decide(ptype, doc, user)

	work.jobs.frappe.has_permission = has_permission
	return seen


def _steps_run(work, monkeypatch, **run):
	"""A run that asks for Steps only, on a session with one action to describe."""
	work.journal.run.update(include_fixes=0, include_steps=1, **run)
	monkeypatch.setattr(analyze, "actions_for_humanizer", lambda recordings: [{"label": "fake action"}])


def _begin(work, item):
	with work.journal.db.transaction():
		return work.jobs._begin_attempt(
			work.journal.run, item, run_id="fake-run", docname="fake-session-doc", worker_token="worker"
		)
