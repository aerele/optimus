# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Shared fakes for the session-gate, rate-limit and endpoint tests (PR-1).

Not a test module (no ``test_`` prefix), so pytest does not collect it. The unit suite runs on a
bench (real ``frappe`` importable) and in CI (the baseline stub in ``conftest.py``); both work
because every test here replaces ``frappe`` wholesale on the module under test
(``optimus.api``, ``optimus.ratelimit``) instead of patching attributes of the real
``frappe.db``, which is a Werkzeug Local proxy.
"""

from __future__ import annotations

import datetime
import importlib
import json
import sys
import types
from types import SimpleNamespace

OWNER = "owner@example.com"
SESSION_UUID = "uuid-1"
DOCNAME = "SESS-1"

# The reviewed set of session-scoped endpoints and the gate each must call first.
# test_api_gate_audit.py checks api.py against it; test_api_gate_deny_paths.py drives every
# entry through the deny path. Adding a gated endpoint means adding it here on purpose.
GATED_ENDPOINTS: dict[str, str] = {
	"refill_ai_suggestions": "_ai_session_gate",
	"regenerate_reports": "_session_action_gate",
	"retry_analyze": "_session_action_gate",
	"start_line_profile_pass": "_session_action_gate",
	"stop_line_profile_pass": "_phase2_run_gate",
	"retry_phase2_analyze": "_phase2_run_gate",
}


class FakePermissionError(Exception):
	pass


class FakeDoesNotExistError(Exception):
	pass


class FakeValidationError(Exception):
	pass


class FakeRateLimitExceededError(FakeValidationError):
	pass


class FakeCache:
	"""Just enough of ``redis.Redis`` / Frappe's ``RedisWrapper`` for optimus.ratelimit:
	``make_key``, ``incr``, ``ttl`` and ``expire`` (Redis semantics: ``ttl`` is -2 for a missing
	key and -1 for a key without an expiry). Every call is recorded in ``calls``; ``ttls`` holds
	the armed expiries. Seed ``store`` without ``ttls`` to simulate a counter that lost its TTL."""

	def __init__(self):
		self.store: dict = {}
		self.ttls: dict = {}
		self.calls: list = []

	def make_key(self, key, user=None, shared=False):
		return f"site|{key}"

	def incr(self, key):
		self.calls.append(("incr", key))
		self.store[key] = self.store.get(key, 0) + 1
		return self.store[key]

	def ttl(self, key):
		self.calls.append(("ttl", key))
		if key not in self.store:
			return -2
		return self.ttls.get(key, -1)

	def expire(self, key, seconds):
		self.calls.append(("expire", key, seconds))
		self.ttls[key] = seconds
		return True


class FakeDoc(SimpleNamespace):
	"""Optimus Session stand-in: an attribute bag plus ``append`` for child tables."""

	def append(self, table: str, row: dict) -> None:
		getattr(self, table).append(SimpleNamespace(**row))


def fake_session_doc(**fields) -> FakeDoc:
	base = {
		"name": DOCNAME,
		"session_uuid": SESSION_UUID,
		"findings": [],
		"actions": [],
		"phase_2_runs": [],
		"table_breakdown_json": "[]",
		"ai_refresh_count": 0,
		"flags": SimpleNamespace(),
	}
	base.update(fields)
	return FakeDoc(**base)


def session_row(*, status="Ready", owner=OWNER, user=None, title="Checkout flow", name=DOCNAME) -> dict:
	return {"name": name, "owner": owner, "user": user or owner, "status": status, "title": title}


def owner_perms(user=OWNER, docname=DOCNAME, *, write=False) -> dict:
	perms = {("read", docname, user): True}
	if write:
		perms[("write", docname, user)] = True
	return perms


def _cint(value, default=0):
	"""Same contract as ``frappe.utils.cint``: ``int(float(value))`` or ``default``."""
	try:
		return int(float(value))
	except (TypeError, ValueError):
		return default


def make_fake_frappe(
	*,
	user: str = OWNER,
	roles: tuple[str, ...] = ("Optimus User",),
	sessions: dict[str, dict] | None = None,
	runs: dict[str, dict] | None = None,
	perms: dict[tuple[str, str, str], bool] | None = None,
	has_permission=None,
	conf: dict | None = None,
	docs: dict[str, object] | None = None,
) -> SimpleNamespace:
	"""A ``frappe`` stand-in for optimus.api and optimus.ratelimit.

	sessions: session_uuid -> row (keys name, owner, user, status, title).
	runs: run_uuid -> Phase 2 Run row (keys name, parent, status).
	perms: (ptype, docname, user) -> bool. A missing key is False: deny by default.
	has_permission: optional ``(doctype, ptype, doc, user) -> bool`` replacing the keyed lookup
	(use it to make the permission engine raise).
	docs: docname -> object returned by ``frappe.get_doc("Optimus Session", docname)``.
	Every write-ish call is recorded on ``fake.spies``.
	"""
	perms = dict(perms or {})
	docs = dict(docs or {})
	by_uuid = {u: dict(row, session_uuid=u) for u, row in (sessions or {}).items()}
	by_docname = {row["name"]: row for row in by_uuid.values()}
	run_rows = {u: dict(row, run_uuid=u) for u, row in (runs or {}).items()}
	spies = SimpleNamespace(
		set_value=[], sql=[], enqueue=[], get_doc=[], throws=[], has_permission=[],
		publish_realtime=[], log_error=[], rollback=[],
	)

	def _pick(row, fieldname, as_dict):
		if row is None:
			return None
		if isinstance(fieldname, (list, tuple)):
			picked = {f: row.get(f) for f in fieldname}
			return picked if as_dict else tuple(picked.values())
		return row.get(fieldname)

	def get_value(doctype, filters=None, fieldname="name", as_dict=False, **kwargs):
		if doctype == "Optimus Session":
			if isinstance(filters, dict):
				row = by_uuid.get(filters.get("session_uuid"))
			else:
				row = by_docname.get(filters)
		elif doctype == "Optimus Phase Two Run" and isinstance(filters, dict):
			row = run_rows.get(filters.get("run_uuid"))
		else:
			row = None
		return _pick(row, fieldname, as_dict)

	def set_value(*args, **kwargs):
		spies.set_value.append((args, kwargs))

	def sql(*args, **kwargs):
		spies.sql.append((args, kwargs))
		return []

	def rollback(*args, **kwargs):
		spies.rollback.append((args, kwargs))

	def _has_permission(doctype=None, ptype="read", doc=None, user=None, **kwargs):
		spies.has_permission.append((doctype, ptype, doc, user))
		if has_permission is not None:
			return has_permission(doctype, ptype, doc, user)
		return bool(perms.get((ptype, doc, user), False))

	def throw(msg=None, exc=None, title=None, **kwargs):
		spies.throws.append({"msg": msg, "exc": exc, "title": title})
		raise (exc or FakeValidationError)(msg)

	def get_doc(*args, **kwargs):
		spies.get_doc.append(args)
		if len(args) >= 2 and args[0] == "Optimus Session" and args[1] in docs:
			return docs[args[1]]
		raise AssertionError(f"unexpected frappe.get_doc{args!r}")

	def enqueue(*args, **kwargs):
		spies.enqueue.append((args, kwargs))

	def _logger(*args, **kwargs):
		noop = lambda *a, **k: None  # noqa: E731
		return SimpleNamespace(warning=noop, info=noop, error=noop, debug=noop)

	return SimpleNamespace(
		session=SimpleNamespace(user=user),
		get_roles=lambda *a, **k: list(roles),
		db=SimpleNamespace(
			get_value=get_value, set_value=set_value, sql=sql, commit=lambda: None, rollback=rollback,
		),
		has_permission=_has_permission,
		throw=throw,
		get_doc=get_doc,
		enqueue=enqueue,
		publish_realtime=lambda *a, **k: spies.publish_realtime.append((a, k)),
		log_error=lambda *a, **k: spies.log_error.append((a, k)),
		logger=_logger,
		cache=FakeCache(),
		conf=dict(conf or {}),
		form_dict={},
		local=SimpleNamespace(),
		utils=SimpleNamespace(cint=_cint, now_datetime=datetime.datetime.now),
		as_json=json.dumps,
		_dict=dict,
		PermissionError=FakePermissionError,
		DoesNotExistError=FakeDoesNotExistError,
		ValidationError=FakeValidationError,
		RateLimitExceededError=FakeRateLimitExceededError,
		spies=spies,
	)


def install(monkeypatch, fake) -> None:
	"""Point ``frappe`` at ``fake`` on optimus.api and optimus.ratelimit."""
	from optimus import api, ratelimit

	for module in (api, ratelimit):
		monkeypatch.setattr(module, "frappe", fake)


def install_module(monkeypatch, dotted: str, obj) -> None:
	"""Make ``import <dotted>`` and ``from <parent> import <leaf>`` return ``obj``."""
	parent_name, leaf = dotted.rsplit(".", 1)
	parent = importlib.import_module(parent_name)
	monkeypatch.setitem(sys.modules, dotted, obj)
	monkeypatch.setattr(parent, leaf, obj, raising=False)


class Tripwire(types.ModuleType):
	"""Module stand-in that fails loudly on any attribute access (deny-path spies)."""

	def __init__(self, name: str):
		super().__init__(name)
		self.touched: list[str] = []

	def __getattr__(self, attr):
		if attr.startswith("__"):
			raise AttributeError(attr)
		self.touched.append(attr)
		raise AssertionError(f"{self.__name__}.{attr} was used on a denied path")


def install_tripwires(monkeypatch, *dotted: str) -> dict[str, Tripwire]:
	wires = {}
	for name in dotted:
		wire = Tripwire(name)
		install_module(monkeypatch, name, wire)
		wires[name] = wire
	return wires
