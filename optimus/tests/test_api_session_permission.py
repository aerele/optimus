# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""_require_session_permission is the per-session ownership gate the phase-2
(and report/AI/export) endpoints rely on. It must fail CLOSED: deny when
has_permission returns False AND when has_permission itself raises — a
permission check that errors must never downgrade to the role-only gate."""

from __future__ import annotations

import types

import pytest

from optimus import api


def _throw(msg=None, exc=None, **k):
	raise (exc or RuntimeError)(msg or "throw")


def _patch(monkeypatch, *, has_permission):
	# frappe.db is a Werkzeug Local proxy — replace it wholesale, never patch
	# its attributes (reading an unbound proxy attr raises RuntimeError). Real
	# frappe.throw touches the unbound frappe.local, so stub it too.
	monkeypatch.setattr(
		api.frappe, "db",
		types.SimpleNamespace(get_value=lambda *a, **k: "sess-doc-1"),
		raising=False,
	)
	monkeypatch.setattr(api.frappe, "has_permission", has_permission, raising=False)
	monkeypatch.setattr(api.frappe, "throw", _throw, raising=False)


def test_allows_when_permitted(monkeypatch):
	_patch(monkeypatch, has_permission=lambda *a, **k: True)
	assert api._require_session_permission("uuid-1", "read") == "sess-doc-1"


def test_denies_when_not_permitted(monkeypatch):
	_patch(monkeypatch, has_permission=lambda *a, **k: False)
	with pytest.raises(api.frappe.PermissionError):
		api._require_session_permission("uuid-1", "write")


def test_fails_closed_when_has_permission_raises(monkeypatch):
	"""Regression: previously this branch returned the docname (fail-OPEN),
	letting any Optimus User reach another user's session if has_permission
	hit a transient error. It must now raise PermissionError."""
	def boom(*a, **k):
		raise RuntimeError("permission engine hiccup")

	_patch(monkeypatch, has_permission=boom)
	with pytest.raises(api.frappe.PermissionError):
		api._require_session_permission("uuid-1", "read")
