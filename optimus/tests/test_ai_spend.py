"""Session AI counters: portable atomic increments in a savepoint (a failure is logged and the
answer kept), one spend source per provider call (explicit attribution or the ambient session,
never both) and session saves that keep the stored counts."""

import json
import sys
from types import SimpleNamespace

import pytest

from optimus import ai_fix, analyze, api

pytestmark = pytest.mark.rq

_REAL_LOG_HTTP_ERROR = ai_fix._log_http_error


@pytest.mark.parametrize("dialect,quote", [("mariadb", "`"), ("postgres", '"')])
def test_counter_query_adds_in_database_with_null_coalescing(monkeypatch, dialect, quote):
	qb = pytest.importorskip("frappe.query_builder.utils", exc_type=ImportError).get_query_builder(dialect)
	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(qb=qb))
	sql = analyze._session_increment_query("fake-doc", "ai_tokens_spent", 42).get_sql()
	q = quote
	assert (
		sql
		== f"UPDATE {q}tabOptimus Session{q} SET {q}ai_tokens_spent{q}=COALESCE({q}ai_tokens_spent{q},0)+42 WHERE {q}name{q}='fake-doc'"
	)
	assert "modified" not in sql


class _Sql:
	"""A column, an expression or a condition that renders itself as a plain string."""

	def __init__(self, sql):
		self.sql = sql

	def __add__(self, other):
		return _Sql(f"{self.sql}+{other}")

	def __eq__(self, other):
		return _Sql(f"{self.sql}='{other}'")

	__hash__ = None


class _FakeUpdate:
	def __init__(self, table):
		self.table, self.assignment, self.condition = table, None, None

	def set(self, field, value):
		self.assignment = f"{field.sql}={value.sql}"
		return self

	def where(self, condition):
		self.condition = condition.sql
		return self

	def get_sql(self):
		return f"UPDATE `tab{self.table}` SET {self.assignment} WHERE {self.condition}"


class _FakeTable:
	def __init__(self, name):
		self.name = name

	def __getitem__(self, field):
		return _Sql(f"`{field}`")


def _plain_string_qb(monkeypatch):
	"""A query builder that needs no Frappe: it renders the MariaDB statement as a plain string,
	so the counter statement is checked under the CI stub too (where the real builder is not
	importable and the parametrized tests above skip)."""
	functions = type(sys)("frappe.query_builder.functions")
	functions.Coalesce = lambda field, default: _Sql(f"COALESCE({field.sql},{default})")
	monkeypatch.setitem(sys.modules, "frappe.query_builder", type(sys)("frappe.query_builder"))
	monkeypatch.setitem(sys.modules, "frappe.query_builder.functions", functions)
	monkeypatch.setattr(
		analyze, "frappe", SimpleNamespace(qb=SimpleNamespace(DocType=_FakeTable, update=lambda t: _FakeUpdate(t.name)))
	)


def test_counter_statement_as_a_plain_string_without_the_real_query_builder(monkeypatch):
	_plain_string_qb(monkeypatch)
	assert analyze._session_increment_query("fake-doc", "ai_tokens_spent", 42).get_sql() == (
		"UPDATE `tabOptimus Session` SET `ai_tokens_spent`=COALESCE(`ai_tokens_spent`,0)+42 WHERE `name`='fake-doc'"
	)
	assert analyze._session_increment_query("fake-uuid", "ai_refresh_count", 1, by="session_uuid").get_sql() == (
		"UPDATE `tabOptimus Session` SET `ai_refresh_count`=COALESCE(`ai_refresh_count`,0)+1 "
		"WHERE `session_uuid`='fake-uuid'"
	)


@pytest.mark.parametrize(
	"field,n", [("status", 1), ("ai_tokens_spent", -1), ("ai_tokens_spent", True), ("ai_tokens_spent", "3")]
)
def test_counter_rejects_non_counter_fields_and_invalid_counts(field, n):
	with pytest.raises(ValueError):
		analyze._session_increment_query("fake-doc", field, n)


@pytest.mark.parametrize("dialect,quote", [("mariadb", "`"), ("postgres", '"')])
def test_ambient_counter_query_matches_the_session_uuid(monkeypatch, dialect, quote):
	qb = pytest.importorskip("frappe.query_builder.utils", exc_type=ImportError).get_query_builder(dialect)
	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(qb=qb))
	sql = analyze._session_increment_query("fake-uuid", "ai_tokens_spent", 42, by="session_uuid").get_sql()
	q = quote
	assert sql.endswith(f" WHERE {q}session_uuid{q}='fake-uuid'")


@pytest.mark.parametrize("key,by", [("", "name"), (None, "name"), ("fake-doc", "modified"), ("fake-doc", "owner")])
def test_counter_query_rejects_an_empty_key_or_another_column(key, by):
	with pytest.raises(ValueError):
		analyze._session_increment_query(key, "ai_tokens_spent", 1, by=by)


class _CounterDb:
	"""``frappe.db`` stand-in recording the savepoint protocol of one counter."""

	def __init__(self, *, run_error=None, rollback_error=None):
		self.calls, self.logs = [], []
		self.run_error, self.rollback_error = run_error, rollback_error

	def savepoint(self, name):
		self.calls.append(("savepoint", name))

	def release_savepoint(self, name):
		self.calls.append(("release", name))

	def rollback(self, *, save_point=None, **kw):
		self.calls.append(("rollback", save_point))
		if self.rollback_error is not None:
			raise self.rollback_error

	def query(self, key, field, n, *, by="name"):
		def run():
			self.calls.append(("update", key, field, n, by))
			if self.run_error is not None:
				raise self.run_error

		return SimpleNamespace(run=run)


@pytest.fixture
def counter_db(monkeypatch):
	def install(**kw):
		db = _CounterDb(**kw)
		monkeypatch.setattr(analyze, "frappe", SimpleNamespace(db=db))
		monkeypatch.setattr(analyze, "_session_increment_query", db.query)

		def log(title, exc=None, **context):
			db.logs.append((title, exc, context, sys.exc_info()[0]))
			ai_fix._mark_logged(exc, "fake-log-row")  # a written row marks the exception, as the real one does
			return True

		monkeypatch.setattr(ai_fix, "log_ai_failure", log)
		return db

	return install


def test_counter_runs_in_a_released_savepoint_without_committing(counter_db):
	db = counter_db()
	analyze._add_ai_spend("fake-doc", 12)
	analyze._bump_ai_refresh_count("fake-doc")
	sp = analyze._COUNTER_SAVEPOINT
	assert db.calls == [
		("savepoint", sp), ("update", "fake-doc", "ai_tokens_spent", 12, "name"), ("release", sp),
		("savepoint", sp), ("update", "fake-doc", "ai_refresh_count", 1, "name"), ("release", sp),
	]
	assert db.logs == []


def test_counter_failure_rolls_back_only_the_counter_logs_once_and_keeps_the_answer(counter_db):
	error = RuntimeError("fake lock wait timeout")
	db = counter_db(run_error=error)
	analyze._add_ai_spend("fake-doc", 12)  # returns: the caller's billed answer is kept
	sp = analyze._COUNTER_SAVEPOINT
	assert db.calls == [("savepoint", sp), ("update", "fake-doc", "ai_tokens_spent", 12, "name"), ("rollback", sp)]
	assert len(db.logs) == 1
	title, exc, context, active = db.logs[0]
	assert title == "optimus ai spend" and exc is error
	assert active is None  # logged after the try, never inside the except
	assert context == {"docname": "fake-doc", "field": "ai_tokens_spent", "amount": 12, "savepoint_rollback": "done"}


def test_counter_reraises_when_the_savepoint_rollback_itself_fails(counter_db):
	error = RuntimeError("fake deadlock")
	db = counter_db(run_error=error, rollback_error=RuntimeError("fake savepoint does not exist"))
	with pytest.raises(RuntimeError) as caught:
		analyze._bump_ai_refresh_count("fake-doc")
	assert caught.value is error
	assert [log[0] for log in db.logs] == ["optimus ai refresh count"]
	assert db.logs[0][2]["savepoint_rollback"] == "failed: RuntimeError" and db.logs[0][3] is None


@pytest.mark.parametrize("stage", ["update", "rollback"])
def test_counter_rq_timeout_is_fresh(counter_db, stage):
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = Timeout("fake timeout")
	if stage == "update":
		db = counter_db(run_error=original)
	else:
		db = counter_db(run_error=RuntimeError("fake failure"), rollback_error=original)
	with pytest.raises(Timeout) as caught:
		analyze._add_ai_spend("fake-doc", 3)
	assert caught.value is not original
	assert caught.value.__context__ is None and caught.value.__cause__ is None
	assert db.logs == []


@pytest.mark.parametrize(
	"docname,tokens",
	[("", 5), (None, 5), ("fake-doc", 0), ("fake-doc", None), ("fake-doc", "abc"), ("fake-doc", -3),
	 ("fake-doc", True), ("fake-doc", 2**40)],
)
def test_no_tokens_or_no_session_is_a_no_op_without_sql(counter_db, docname, tokens):
	db = counter_db()
	analyze._add_ai_spend(docname, tokens)
	assert db.calls == [] and db.logs == []


def test_spend_takes_a_usage_dict_total(counter_db):
	db = counter_db()
	analyze._add_ai_spend("fake-doc", {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9})
	assert ("update", "fake-doc", "ai_tokens_spent", 9, "name") in db.calls


def test_refresh_count_without_a_docname_is_a_no_op(counter_db):
	db = counter_db()
	analyze._bump_ai_refresh_count("")
	analyze._bump_ai_refresh_count(None)
	assert db.calls == []


# --- one spend source -------------------------------------------------------------------

_PROVIDER = {
	"name": "fake", "protocol": "openai", "base_url": "https://fake.invalid/v1", "model": "fake",
	"needs_key": False, "context_tokens": 128000,
}
_ANSWER = "## Diagnosis\nRepeated work.\n## Fix\nBatch the call.\n## Why it works\nFewer calls.\n## Verify\nProfile again."


class _Reply:
	def __init__(self, content, total, status=200, protocol="openai"):
		self.status_code = status
		self.payload = {
			"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
			"usage": {"prompt_tokens": total - 1, "completion_tokens": 1, "total_tokens": total},
		} if protocol == "openai" else {
			"content": [{"type": "text", "text": content}], "stop_reason": "end_turn",
			"usage": {"input_tokens": total - 1, "output_tokens": 1},
		}
		self.text = json.dumps(self.payload)
		self.headers = {}

	def json(self):
		return self.payload


@pytest.fixture
def provider(monkeypatch):
	import frappe

	monkeypatch.setattr(frappe.local, "_optimus_spend_session", "uuid-A", raising=False)
	monkeypatch.setattr(ai_fix, "_provider_config", lambda: dict(_PROVIDER))
	monkeypatch.setattr(ai_fix, "_resolve_display_threshold_ms", lambda: 1000)
	monkeypatch.setattr(ai_fix, "_get_api_key", lambda *a: "")
	monkeypatch.setattr(ai_fix, "_current_key_or_empty", lambda: "")
	monkeypatch.setattr(ai_fix, "is_finding_type_excluded", lambda *a, **k: False)
	monkeypatch.setattr(ai_fix, "_log_http_error", lambda *a, **k: None)
	monkeypatch.setattr(ai_fix, "_reask_enabled", lambda: False)  # one provider call per entry
	posts = []

	def install(*replies):
		pending = iter(replies)

		def post(url, **kw):
			posts.append(kw["json"])
			reply = next(pending)
			if isinstance(reply, BaseException):
				raise reply
			return reply

		monkeypatch.setattr(ai_fix.requests, "post", post)

	def use(protocol):
		monkeypatch.setattr(ai_fix, "_provider_config", lambda: dict(_PROVIDER, protocol=protocol))

	return SimpleNamespace(install=install, posts=posts, use=use)


@pytest.fixture
def charged(monkeypatch, provider):
	"""Every session counter increment, as (key, field, amount, matched column)."""
	seen = []

	def counter(key, field, n, *, by="name", title=""):
		seen.append((key, field, n, by))

	monkeypatch.setattr(analyze, "_increment_session_counter", counter)
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **k: True)
	return seen


def _ask(entry, **attribution):
	if entry == "fix":
		return ai_fix.suggest_fix({"finding_type": "N+1 Query"}, **attribution)
	return ai_fix.humanize_steps([{"label": "fake"}], **attribution)


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("entry", ["fix", "steps"])
def test_an_unattributed_call_charges_the_ambient_session_once(provider, charged, entry, protocol):
	provider.use(protocol)
	provider.install(_Reply(_ANSWER if entry == "fix" else "1. step", 7, protocol=protocol))
	_ask(entry)
	assert charged == [("uuid-A", "ai_tokens_spent", 7, "session_uuid")]


@pytest.mark.parametrize(
	"attribution",
	[{"session_uuid": "uuid-B", "docname": "SESS-B"}, {"session_uuid": "uuid-B"}, {"docname": "SESS-B"}],
)
@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("entry", ["fix", "steps"])
def test_explicit_attribution_never_charges_the_ambient_session(provider, charged, entry, attribution, protocol):
	provider.use(protocol)
	provider.install(_Reply(_ANSWER if entry == "fix" else "1. step", 7, protocol=protocol))
	_ask(entry, **attribution)
	assert len(provider.posts) == 1 and charged == []


def test_an_explicit_caller_records_its_spend_once_into_its_own_session(provider, charged):
	provider.install(_Reply(_ANSWER, 7))
	result = ai_fix.suggest_fix({"finding_type": "N+1 Query"}, session_uuid="uuid-B", docname="SESS-B")
	analyze._add_ai_spend("SESS-B", result["tokens"]["total_tokens"])
	assert charged == [("SESS-B", "ai_tokens_spent", 7, "name")]


def test_an_explicit_billed_failure_carries_its_usage_and_charges_nothing(provider, charged):
	provider.install(_Reply("<think>unfinished", 9))
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix.suggest_fix({"finding_type": "N+1 Query"}, session_uuid="uuid-B", docname="SESS-B")
	assert caught.value.usage["total_tokens"] == 9 and charged == []


def test_an_internal_call_with_an_explicit_session_never_charges_the_ambient_one(provider, charged):
	provider.install(_Reply("ok", 42), _Reply("ok", 5))
	ai_fix._dispatch_call(dict(_PROVIDER), "system", [], usage_out={}, session_uuid="uuid-B")
	assert charged == []
	ai_fix._dispatch_call(dict(_PROVIDER), "system", [], usage_out={})
	assert charged == [("uuid-A", "ai_tokens_spent", 5, "session_uuid")]


@pytest.mark.parametrize("attribution", [{}, {"docname": "SESS-B"}])
def test_each_billed_call_of_a_reask_is_charged_once(provider, charged, monkeypatch, attribution):
	violation = ai_fix.ai_guardrails.Violation
	verdicts = iter([[violation("invented-api")], []])
	monkeypatch.setattr(ai_fix.ai_guardrails, "verify_fix", lambda text, **k: next(verdicts))
	monkeypatch.setattr(ai_fix.ai_guardrails, "reaskable", lambda found: list(found))
	monkeypatch.setattr(ai_fix.ai_budget, "reask_fits", lambda *a, **k: True)
	monkeypatch.setattr(ai_fix, "_reask_enabled", lambda: True)
	provider.install(_Reply(_ANSWER, 7), _Reply(_ANSWER, 11))
	result = ai_fix.suggest_fix({"finding_type": "N+1 Query"}, **attribution)
	assert len(provider.posts) == 2 and result["tokens"]["total_tokens"] == 18
	# unattributed: both replies charged to the ambient session; attributed: neither (the caller's)
	assert [amount for _key, _field, amount, _by in charged] == ([] if attribution else [7, 11])


def test_humanize_reports_each_call_once_through_a_reused_usage_out(provider, charged):
	import requests

	usage_out = {}
	provider.install(_Reply("1. step", 7), _Reply("   ", 11), requests.exceptions.ConnectionError("down"))
	assert ai_fix.humanize_steps([{"label": "fake"}], usage_out=usage_out) == "1. step"
	assert usage_out["total_tokens"] == 7
	with pytest.raises(ai_fix.AiFixError) as empty:
		ai_fix.humanize_steps([{"label": "fake"}], usage_out=usage_out)
	assert empty.value.usage["total_tokens"] == 11 and usage_out["total_tokens"] == 11
	with pytest.raises(ai_fix.AiFixError) as down:
		ai_fix.humanize_steps([{"label": "fake"}], usage_out=usage_out)
	assert down.value.usage is None  # nothing billed by this call, whatever the caller's dict holds
	assert [amount for _key, _field, amount, _by in charged] == [7, 11]


def test_humanize_without_usage_out_still_charges_an_unattributed_call(provider, charged):
	provider.install(_Reply("1. step", 9))
	ai_fix.humanize_steps([{"label": "fake"}])
	assert charged == [("uuid-A", "ai_tokens_spent", 9, "session_uuid")]


@pytest.mark.parametrize(
	"attribution", [{"session_uuid": ""}, {"docname": ""}, {"session_uuid": "", "docname": ""}],
)
@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("entry", ["fix", "steps"])
def test_an_empty_session_or_docname_is_unattributed(provider, charged, entry, attribution, protocol):
	provider.use(protocol)
	provider.install(_Reply(_ANSWER if entry == "fix" else "1. step", 7, protocol=protocol))
	_ask(entry, **attribution)
	assert charged == [("uuid-A", "ai_tokens_spent", 7, "session_uuid")]


def test_an_internal_call_with_an_empty_session_is_charged_and_logged_to_the_ambient_one(
	provider, charged, monkeypatch,
):
	provider.install(_Reply("ok", 5))
	ai_fix._dispatch_call(dict(_PROVIDER), "system", [], usage_out={}, session_uuid="")
	assert charged == [("uuid-A", "ai_tokens_spent", 5, "session_uuid")]
	rows = []
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda title, **kw: rows.append(kw["session_uuid"]) or True)
	monkeypatch.setattr(ai_fix, "_log_http_error", _REAL_LOG_HTTP_ERROR)
	ai_fix._log_http_error("openai", "chat/completions", 500, session_uuid="")
	assert rows == ["uuid-A"]


def test_the_ambient_recorder_is_a_thin_wrapper_over_the_session_counter(charged):
	ai_fix._record_session_spend(150)
	assert charged == [("uuid-A", "ai_tokens_spent", 150, "session_uuid")]


@pytest.mark.parametrize("tokens", [0, None, -1, "abc", True])
def test_the_ambient_recorder_ignores_a_call_without_tokens(charged, tokens):
	ai_fix._record_session_spend(tokens)
	assert charged == []


def test_the_ambient_recorder_without_a_marker_is_a_no_op(charged, monkeypatch):
	import frappe

	monkeypatch.setattr(frappe.local, "_optimus_spend_session", None, raising=False)
	ai_fix._record_session_spend(150)
	assert charged == []


def test_a_deadlocked_ambient_charge_keeps_the_answer_and_logs_once(provider, counter_db):
	# MariaDB rolled the whole transaction back: the savepoint is gone, so the counter logs and
	# raises again. A per-call charge has no short transaction to retry: the reply is kept.
	db = counter_db(run_error=RuntimeError("fake deadlock"), rollback_error=RuntimeError("fake no savepoint"))
	provider.install(_Reply(_ANSWER, 7))
	result = ai_fix.suggest_fix({"finding_type": "N+1 Query"})
	assert result["suggestion"].startswith("## Diagnosis")
	assert [(log[0], log[2]["savepoint_rollback"]) for log in db.logs] == [("optimus ai spend", "failed: RuntimeError")]


def test_an_unlogged_counter_error_is_logged_once_and_the_reply_kept(charged, monkeypatch):
	error = RuntimeError("fake unexpected counter failure")

	def counter(*a, **k):
		raise error

	monkeypatch.setattr(analyze, "_increment_session_counter", counter)
	rows = []
	monkeypatch.setattr(
		ai_fix, "log_ai_failure",
		lambda title, exc=None, **kw: rows.append((title, exc, kw.get("session_uuid"), sys.exc_info()[0])) or True,
	)
	ai_fix._record_session_spend(7)  # returns: the reply that billed these tokens is kept
	assert rows == [("optimus ai spend", error, "uuid-A", None)]


def test_an_rq_timeout_in_the_ambient_charge_leaves_fresh(charged, monkeypatch):
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = Timeout("fake timeout")

	def counter(*a, **k):
		raise original

	monkeypatch.setattr(analyze, "_increment_session_counter", counter)
	with pytest.raises(Timeout) as caught:
		ai_fix._record_session_spend(7)
	assert caught.value is not original and caught.value.__context__ is None


def test_a_failed_spend_counter_keeps_the_billed_answer(provider, counter_db):
	db = counter_db(run_error=RuntimeError("fake lock wait timeout"))
	provider.install(_Reply(_ANSWER, 7))
	result = ai_fix.suggest_fix({"finding_type": "N+1 Query"})
	assert result["suggestion"].startswith("## Diagnosis")
	assert ("update", "uuid-A", "ai_tokens_spent", 7, "session_uuid") in db.calls
	assert [(log[0], log[2].get("session_uuid")) for log in db.logs] == [("optimus ai spend", "uuid-A")]


@pytest.mark.parametrize("entry", ["fix", "steps"])
def test_public_call_explicit_session_reaches_protocol(monkeypatch, entry):
	provider = {
		"name": "fake",
		"protocol": "openai",
		"base_url": "https://fake.invalid/v1",
		"model": "fake",
		"needs_key": False,
		"context_tokens": 128000,
	}
	monkeypatch.setattr(ai_fix, "_provider_config", lambda: provider)
	monkeypatch.setattr(ai_fix, "_resolve_display_threshold_ms", lambda: 1000)
	monkeypatch.setattr(ai_fix, "_get_api_key", lambda *a: "")
	monkeypatch.setattr(ai_fix, "is_finding_type_excluded", lambda *a: False)
	seen = []

	def call(*a, **kw):
		seen.append(kw)
		return "## Diagnosis\nRepeated work.\n## Fix\nBatch the call.\n## Why it works\nFewer calls.\n## Verify\nProfile again."

	monkeypatch.setattr(ai_fix, "_call_openai_chat", call)
	if entry == "fix":
		ai_fix.suggest_fix(
			{"finding_type": "N+1 Query"}, session_uuid="fake-session", docname="fake-doc", timeout=17
		)
	else:
		ai_fix.humanize_steps(
			[{"label": "fake"}], session_uuid="fake-session", docname="fake-doc", timeout=17
		)
	assert seen[0]["session_uuid"] == "fake-session" and seen[0]["timeout"] == 17
	# the explicit caller records this call's spend itself (analyze._add_ai_spend)
	assert seen[0]["record_spend"] is False


def test_health_window_uses_stopped_at_not_ai_modified(monkeypatch):
	qb = pytest.importorskip("frappe.query_builder.utils", exc_type=ImportError).get_query_builder("postgres")
	queries = []
	monkeypatch.setattr(api, "frappe", SimpleNamespace(qb=qb))
	monkeypatch.setattr(api, "now_datetime", lambda: __import__("datetime").datetime(2026, 1, 3))
	monkeypatch.setattr(api, "add_to_date", lambda *a, **kw: "2026-01-02")
	# QueryBuilder instances carry run on the returned query class.
	query = qb.from_(qb.DocType("Optimus Session"))
	monkeypatch.setattr(
		type(query), "run", lambda self, **kw: queries.append(self.get_sql()) or [], raising=False
	)
	api._session_perf_24h()
	assert '"stopped_at"' in queries[0] and '"modified"' not in queries[0]


# --- a session save never writes stale counters back -------------------------------------


@pytest.mark.parametrize("loaded", [(0, 1), (10_000, 99)])  # stale; larger (a crafted REST save)
def test_a_save_keeps_the_stored_counters_and_its_own_other_fields(loaded):
	stored = SimpleNamespace(ai_tokens_spent=42, ai_refresh_count=3, notes="stored notes")
	doc = SimpleNamespace(
		ai_tokens_spent=loaded[0], ai_refresh_count=loaded[1], notes="new notes", get_doc_before_save=lambda: stored,
	)
	analyze._keep_session_counters(doc)
	assert (doc.ai_tokens_spent, doc.ai_refresh_count, doc.notes) == (42, 3, "new notes")


def test_every_session_save_runs_the_counter_hook():
	"""Stub-safe: the controller's ``before_validate`` (it runs on every save, ``ignore_validate``
	included, after Frappe has read the row ``for_update``) calls ``_keep_session_counters(self)``."""
	import ast
	from pathlib import Path

	path = Path(analyze.__file__).parent / "optimus" / "doctype" / "optimus_session" / "optimus_session.py"
	cls = next(
		n for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
		if isinstance(n, ast.ClassDef) and n.name == "OptimusSession"
	)
	hooks = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
	assert set(hooks) == {"before_validate"}
	calls = [
		n for n in ast.walk(hooks["before_validate"])
		if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_keep_session_counters"
	]
	assert len(calls) == 1
	assert [a.id for a in calls[0].args if isinstance(a, ast.Name)] == ["self"] and not calls[0].keywords


def test_a_new_session_keeps_its_own_counters():
	doc = SimpleNamespace(ai_tokens_spent=5, ai_refresh_count=0, get_doc_before_save=lambda: None)
	analyze._keep_session_counters(doc)
	assert (doc.ai_tokens_spent, doc.ai_refresh_count) == (5, 0)


_SAVE_STEPS_WITH_DB_OR_META = (
	"check_if_locked", "_set_defaults", "check_permission", "set_user_and_timestamp", "set_docstatus",
	"set_parent_in_children", "set_name_in_children", "validate_higher_perm_levels", "_validate_links",
	"_validate", "update_children", "reset_computed_child_tables", "run_post_save_methods", "reset_seen",
	"set_title_field", "validate_update_after_submit",
)


@pytest.mark.parametrize("ignore_validate", [False, True])
def test_an_increment_after_get_doc_survives_a_real_session_save(monkeypatch, ignore_validate):
	"""The live loss: ``analyze._persist`` loads the session, the humanize call adds its tokens in
	SQL, then ``doc.save()`` wrote the loaded (older) count back. Drives Frappe's own
	``Document.save`` (``check_if_latest`` reads the row ``for_update`` before ``before_validate``);
	only the steps that need a database or DocType meta are stubbed."""
	pytest.importorskip("frappe.model.document", exc_type=ImportError)
	DocStatus = pytest.importorskip("frappe.model.docstatus", exc_type=ImportError).DocStatus
	import frappe

	from optimus.optimus.doctype.optimus_session.optimus_session import OptimusSession

	for name in _SAVE_STEPS_WITH_DB_OR_META:
		monkeypatch.setattr(OptimusSession, name, lambda self, *a, **k: None, raising=False)
	monkeypatch.setattr(OptimusSession, "_non_computed_table_fieldnames", property(lambda self: {}), raising=False)
	monkeypatch.setattr(OptimusSession, "meta", property(lambda self: SimpleNamespace(issingle=False)), raising=False)

	def run_method(self, method, *a, **k):
		hook = getattr(type(self), method, None)
		if callable(hook):
			hook(self)

	monkeypatch.setattr(OptimusSession, "run_method", run_method)
	written, loads = [], []
	monkeypatch.setattr(OptimusSession, "db_update", lambda self: written.append(
		{f: self.__dict__.get(f) for f in ("ai_tokens_spent", "ai_refresh_count", "notes")}
	))
	stored = SimpleNamespace(
		name="SESS-1", modified="2026-10-10 10:00:00", docstatus=DocStatus(0), ai_tokens_spent=10, ai_refresh_count=0,
	)
	monkeypatch.setattr(frappe, "get_doc", lambda *a, **kw: loads.append((a, kw)) or stored)
	doc = object.__new__(OptimusSession)
	doc.__dict__.update(
		doctype="Optimus Session", name="SESS-1", modified="2026-10-10 10:00:00",
		_original_modified="2026-10-10 10:00:00", docstatus=DocStatus(0),
		flags=frappe._dict(ignore_validate=ignore_validate),
		ai_tokens_spent=10, ai_refresh_count=0, notes="analyzed notes",
	)
	stored.ai_tokens_spent, stored.ai_refresh_count = 52, 1  # humanize tokens; a refresh
	doc.save(ignore_permissions=True)
	assert loads == [(("Optimus Session", "SESS-1"), {"for_update": True})]
	assert written == [{"ai_tokens_spent": 52, "ai_refresh_count": 1, "notes": "analyzed notes"}]


# --- whose Error Log row: the call's own session, never a stale ambient one ---------------


class _Rejected:
	def __init__(self, status, message):
		self.status_code = status
		self.headers = {}
		self.payload = {"error": {"type": "invalid_request_error", "message": message}}
		self.text = json.dumps(self.payload)

	def json(self):
		return self.payload


@pytest.fixture
def http_rows(monkeypatch, provider):
	"""Every row the HTTP layer writes, as (title, session_uuid, docname); the worker's ambient
	spend marker names session A (the ``provider`` fixture), a stale one for an explicit call."""
	seen = []
	monkeypatch.setattr(ai_fix, "_log_http_error", _REAL_LOG_HTTP_ERROR)
	monkeypatch.setattr(
		ai_fix, "log_ai_failure",
		lambda title, exc=None, **kw: seen.append((title, kw.get("session_uuid"), kw.get("docname"))) or True,
	)
	monkeypatch.setattr(analyze, "_increment_session_counter", lambda *a, **k: None)
	return seen


_FAILURES = {
	"server": lambda protocol: [_Reply("x", 1, status=500, protocol=protocol)],
	"transport": lambda protocol: [__import__("requests").exceptions.ConnectionError("down")],
	# OpenAI-compatible only: the parameter ladder logs its final rejection itself
	"ladder": lambda protocol: [
		_Rejected(400, "Unsupported parameter: 'temperature'"), _Rejected(400, "The model is not available"),
	],
}


@pytest.mark.parametrize("failure,protocol", [
	("server", "openai"), ("server", "anthropic"), ("transport", "openai"), ("transport", "anthropic"),
	("ladder", "openai"),
])
@pytest.mark.parametrize("entry", ["fix", "steps"])
@pytest.mark.parametrize("attribution,row", [
	({}, ("uuid-A", None)),  # unattributed: the session the worker is processing
	({"session_uuid": "uuid-B"}, ("uuid-B", None)),
	({"docname": "SESS-B"}, (None, "SESS-B")),  # docname only: filed under B, never under stale A
	({"session_uuid": "uuid-B", "docname": "SESS-B"}, ("uuid-B", "SESS-B")),
])
def test_an_http_failure_row_is_filed_under_the_calls_own_session(
	provider, http_rows, entry, failure, protocol, attribution, row,
):
	provider.use(protocol)
	provider.install(*_FAILURES[failure](protocol))
	with pytest.raises(ai_fix.AiFixError):
		_ask(entry, **attribution)
	assert http_rows == [("optimus ai_fix", *row)]


def test_an_explicit_call_never_borrows_the_ambient_session_for_its_row(provider, http_rows):
	"""``record_spend`` False marks an explicit call: with no session or docname of its own, its
	row has no session rather than the worker's last one."""
	provider.install(_Reply("x", 1, status=500))
	with pytest.raises(ai_fix.AiFixError):
		ai_fix._dispatch_call(dict(_PROVIDER), "system", [], usage_out={}, record_spend=False)
	assert http_rows == [("optimus ai_fix", None, None)]


def test_the_ambient_and_the_explicit_session_differ_and_each_row_follows_its_call(provider, http_rows):
	provider.install(_Reply("x", 1, status=500), _Reply("x", 1, status=500))
	with pytest.raises(ai_fix.AiFixError):
		ai_fix.suggest_fix({"finding_type": "N+1 Query"}, docname="SESS-B")
	with pytest.raises(ai_fix.AiFixError):
		ai_fix.suggest_fix({"finding_type": "N+1 Query"})
	assert http_rows == [("optimus ai_fix", None, "SESS-B"), ("optimus ai_fix", "uuid-A", None)]


# --- the .usage boundary ------------------------------------------------------------------------


@pytest.mark.parametrize("entry", ["fix", "steps"])
def test_usage_rides_a_fresh_timeout_only_to_the_public_entry(provider, monkeypatch, entry):
	"""A billed call interrupted by an RQ job timeout leaves ``suggest_fix`` / ``humanize_steps`` as
	a fresh timeout that carries the call's ``.usage``; any further re-wrap (a shared interrupt guard,
	``log_ai_failure`` raising it again) builds another fresh instance without it. A caller that
	records spend itself must read ``.usage`` from the exception it catches directly."""
	from optimus import safe_call

	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException

	def interrupted(*a, usage=None, usage_out=None, **kw):
		(usage if usage is not None else usage_out).update(
			{"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
		)
		raise Timeout("fake timeout")

	monkeypatch.setattr(ai_fix, "_complete_with_guardrails", interrupted)
	monkeypatch.setattr(ai_fix, "_dispatch_call", interrupted)
	with pytest.raises(Timeout) as caught:
		_ask(entry, session_uuid="uuid-B", docname="SESS-B")
	assert caught.value.usage["total_tokens"] == 55
	assert caught.value.__context__ is None and caught.value.__cause__ is None
	guard = safe_call.InterruptGuard()
	try:
		with guard:
			raise caught.value
	except Exception:
		pass
	again = guard.interrupt()
	assert type(again) is Timeout and getattr(again, "usage", None) is None


def test_a_failed_reask_row_is_filed_under_the_calls_own_session(provider, http_rows, monkeypatch):
	violation = ai_fix.ai_guardrails.Violation
	monkeypatch.setattr(ai_fix.ai_guardrails, "verify_fix", lambda text, **k: [violation("invented-api")])
	monkeypatch.setattr(ai_fix.ai_guardrails, "reaskable", lambda found: list(found))
	monkeypatch.setattr(ai_fix.ai_budget, "reask_fits", lambda *a, **k: True)
	monkeypatch.setattr(ai_fix, "_reask_enabled", lambda: True)
	provider.install(_Reply(_ANSWER, 7), _Reply("x", 1, status=500))
	ai_fix.suggest_fix({"finding_type": "N+1 Query"}, docname="SESS-B")  # the first answer is kept
	assert http_rows == [("optimus ai_fix", None, "SESS-B")]


def test_the_ladders_own_timeout_row_is_filed_under_the_calls_own_session(provider, http_rows, monkeypatch):
	clock = SimpleNamespace(now=0.0)
	monkeypatch.setattr(ai_fix, "time", SimpleNamespace(monotonic=lambda: clock.now))

	def post(url, **kw):
		clock.now += 10_000.0  # the whole budget goes on this one rejected post
		return _Rejected(400, "Unsupported parameter: 'temperature'")

	monkeypatch.setattr(ai_fix.requests, "post", post)
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix.suggest_fix({"finding_type": "N+1 Query"}, docname="SESS-B")
	assert caught.value.kind == "timeout"
	assert http_rows == [("optimus ai_fix", None, "SESS-B")]


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_an_internal_call_with_only_a_docname_is_explicit_for_charge_and_row(provider, charged, monkeypatch, protocol):
	"""A docname alone attributes a call, as a session_uuid does: no ambient charge, and a failure
	row filed under that docname, even when ``record_spend`` is left at its default."""
	provider.use(protocol)
	provider.install(_Reply("ok", 5, protocol=protocol), _Reply("x", 1, status=500, protocol=protocol))
	ai_fix._dispatch_call(dict(_PROVIDER, protocol=protocol), "system", [], usage_out={}, docname="SESS-B")
	assert charged == []
	rows = []
	monkeypatch.setattr(ai_fix, "_log_http_error", _REAL_LOG_HTTP_ERROR)
	monkeypatch.setattr(
		ai_fix, "log_ai_failure", lambda title, exc=None, **kw: rows.append((kw["session_uuid"], kw["docname"])) or True,
	)
	with pytest.raises(ai_fix.AiFixError):
		ai_fix._dispatch_call(dict(_PROVIDER, protocol=protocol), "system", [], usage_out={}, docname="SESS-B")
	assert rows == [(None, "SESS-B")]


class _UnreadableLocal:
	"""``frappe.local`` whose attributes cannot be read (``error`` is raised on every read)."""

	def __init__(self, error):
		object.__setattr__(self, "error", error)

	def __getattr__(self, name):
		raise object.__getattribute__(self, "error")


class _FakeTimeout(Exception):
	"""Stands for rq's job timeout (``safe_call.job_timeout_types`` is pointed at it)."""


def test_an_unreadable_spend_marker_files_the_row_without_a_session(monkeypatch):
	import frappe

	rows = []
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda title, **kw: rows.append(kw["session_uuid"]) or True)
	monkeypatch.setattr(frappe, "local", _UnreadableLocal(RuntimeError("no request context")))
	_REAL_LOG_HTTP_ERROR("openai", "chat/completions", 500)
	assert rows == [None]


def test_a_timeout_while_reading_the_spend_marker_leaves_fresh(monkeypatch):
	import frappe

	from optimus import safe_call

	monkeypatch.setattr(safe_call, "job_timeout_types", lambda: (_FakeTimeout,))
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **k: pytest.fail("no row after a timeout"))
	original = _FakeTimeout("expired")
	monkeypatch.setattr(frappe, "local", _UnreadableLocal(original))
	for read in (lambda: _REAL_LOG_HTTP_ERROR("openai", "chat/completions", 500), lambda: ai_fix._record_session_spend(7)):
		with pytest.raises(_FakeTimeout) as caught:
			read()
		assert caught.value is not original and caught.value.__context__ is None


def test_an_unreadable_spend_marker_charges_nothing(monkeypatch):
	import frappe

	seen = []
	monkeypatch.setattr(analyze, "_increment_session_counter", lambda *a, **k: seen.append(a))
	monkeypatch.setattr(frappe, "local", _UnreadableLocal(RuntimeError("no request context")))
	ai_fix._record_session_spend(7)
	assert seen == []
