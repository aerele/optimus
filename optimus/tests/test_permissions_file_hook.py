# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""optimus.permissions.file_has_permission: deny with False, and defer with
the running Frappe's own "no objection" value.

Frappe calls every has_permission hook registered for a doctype in
reversed(install order). Optimus is installed after frappe, so its File
hook runs BEFORE frappe.core.doctype.file.file.has_permission, the check
that actually decides private-file, owner, share and attached-document
access. What a hook must return to hand the decision on differs by
version:

- Frappe 16 (frappe/permissions.py:481-498) stops at the first FALSY
  answer, so None is a deny and "no objection" is True.
- Frappe 15 (frappe/permissions.py:442-460) stops at the first NON-None
  answer, so True is a grant that skips Frappe's own File check and
  "no objection" is None.

Before this fix the hook always returned None to defer. That was right on
Frappe 15, and on Frappe 16 it denied every File to every non-Administrator
user (form load, delete, library attach, REST), confirmed by manually
driving Frappe's has_controller_permissions loop against a real v16 site
and watching a non-Administrator's request for an unrelated public File
get denied.
Always returning True instead would fix Frappe 16 and let any logged-in
user read any private file on Frappe 15. The fix returns
permissions._no_objection(), which reads frappe.__version__.

_GATED_FIELDS also gains recordings_file in this PR: the raw recordings
snapshot (a compressed JSON bundle of the whole flow, including SQL
parameters and Python call trees) was reachable by anyone with read
access to the parent Optimus Session, including a read-sharee who was
never meant to see raw capture data, only the rendered report. Gating
it the same way as the two report files closes that gap.

An autouse fixture pins frappe.__version__ to a Frappe 16 release, so
Parts A and B run on the v16 contract. Part A pins file_has_permission's
own return values directly (``is``, so None and True are told apart).
Part B replays v16's has_controller_permissions loop against the real
function plus a minimal stand-in for the core File hook, each scenario
contrasted against a parameterised copy of the pre-fix function. Part C
pins the version parsing and replays v15's loop. Part D (appended by
Task 2) is a static guard over the function's return statements.
"""

from __future__ import annotations

import ast
import functools
import inspect
import types

import pytest

from optimus import permissions

_V16 = "16.18.0"
_V15 = "15.107.2"


@pytest.fixture(autouse=True)
def _frappe_v16(monkeypatch):
	"""Run every test on the Frappe 16 contract unless it sets another
	version itself. The CI conftest's frappe stub has no __version__ at all,
	and _no_objection() fails closed (None) without one."""
	monkeypatch.setattr(permissions.frappe, "__version__", _V16, raising=False)


def _on_frappe(monkeypatch, version):
	monkeypatch.setattr(permissions.frappe, "__version__", version, raising=False)


def _patch(monkeypatch, *, roles=(), recording_user="owner@example.com", session_user="owner@example.com"):
	monkeypatch.setattr(
		permissions.frappe, "session", types.SimpleNamespace(user=session_user), raising=False,
	)
	monkeypatch.setattr(permissions.frappe, "get_roles", lambda user: list(roles), raising=False)
	monkeypatch.setattr(
		permissions.frappe, "db",
		types.SimpleNamespace(get_value=lambda *a, **kw: recording_user),
		raising=False,
	)


def _doc(**kw):
	base = dict(
		attached_to_doctype="Optimus Session",
		attached_to_field="raw_report_file",
		attached_to_name="OS-0001",
	)
	base.update(kw)
	return types.SimpleNamespace(**base)


# --- Part A (Frappe 16) -----------------------------------------------------

def test_no_doc_defers_true():
	assert permissions.file_has_permission(None) is True


def test_non_optimus_session_file_defers_true(monkeypatch):
	_patch(monkeypatch)
	doc = _doc(attached_to_doctype="ToDo")
	assert permissions.file_has_permission(doc, "read", user="stranger@example.com") is True


def test_generic_attachment_on_optimus_session_defers_true(monkeypatch):
	"""A File attached to an Optimus Session but not through one of the three
	gated Attach fields, e.g. a screenshot or note someone added via
	Frappe's own sidebar Attach button (which leaves attached_to_field
	unset), must defer, not get caught by the report/recordings gate. All
	three of the doctype's own Attach fields (raw_report_file,
	raw_report_pdf_file, recordings_file) are gated as of this PR, so this
	is the only remaining "attached to an Optimus Session, not gated"
	shape."""
	_patch(monkeypatch)
	doc = _doc(attached_to_field=None)
	assert permissions.file_has_permission(doc, "read", user="stranger@example.com") is True


def test_system_manager_defers_true(monkeypatch):
	_patch(monkeypatch, roles=["System Manager"])
	doc = _doc()
	assert permissions.file_has_permission(doc, "read", user="sysmgr@example.com") is True


def test_administrator_defers_true(monkeypatch):
	_patch(monkeypatch, roles=["Administrator"])
	doc = _doc()
	assert permissions.file_has_permission(doc, "read", user="Administrator") is True


def test_no_attached_to_name_denies_false(monkeypatch):
	_patch(monkeypatch)
	doc = _doc(attached_to_name=None)
	assert permissions.file_has_permission(doc, "read", user="stranger@example.com") is False


def test_stranger_denies_false(monkeypatch):
	_patch(monkeypatch, recording_user="owner@example.com")
	doc = _doc()
	assert permissions.file_has_permission(doc, "read", user="stranger@example.com") is False


def test_recording_user_defers_true(monkeypatch):
	_patch(monkeypatch, recording_user="owner@example.com")
	doc = _doc()
	assert permissions.file_has_permission(doc, "read", user="owner@example.com") is True


def test_user_defaults_to_session_user(monkeypatch):
	"""``user=None`` must default to ``frappe.session.user``. Exercised
	through the recording-user gate, not System Manager: ``_patch``'s
	``get_roles`` stub ignores its argument, so a System Manager-role
	assertion here would still pass with the defaulting line deleted (the
	stub returns the privileged role for None just as readily as for the
	real session user)."""
	_patch(monkeypatch, recording_user="owner@example.com", session_user="owner@example.com")
	doc = _doc()
	assert permissions.file_has_permission(doc, "read", user=None) is True


def test_user_defaults_to_session_user_denies_stranger(monkeypatch):
	"""Companion negative: once ``user=None`` resolves to the session user,
	a session user who is not the recording user is still denied."""
	_patch(monkeypatch, recording_user="owner@example.com", session_user="stranger@example.com")
	doc = _doc()
	assert permissions.file_has_permission(doc, "read", user=None) is False


# --- recordings_file gating (new in this PR) --------------------------------

def test_recordings_file_denies_stranger(monkeypatch):
	"""The raw recordings bundle is now gated exactly like the two report
	files: a stranger (not System Manager/Administrator, not the recording
	user) must be denied, not just able to see it because they can read the
	parent Optimus Session."""
	_patch(monkeypatch, recording_user="owner@example.com")
	doc = _doc(attached_to_field="recordings_file")
	assert permissions.file_has_permission(doc, "read", user="stranger@example.com") is False


def test_recordings_file_allows_recording_user(monkeypatch):
	_patch(monkeypatch, recording_user="owner@example.com")
	doc = _doc(attached_to_field="recordings_file")
	assert permissions.file_has_permission(doc, "read", user="owner@example.com") is True


def test_recordings_file_allows_system_manager(monkeypatch):
	_patch(monkeypatch, roles=["System Manager"])
	doc = _doc(attached_to_field="recordings_file")
	assert permissions.file_has_permission(doc, "read", user="sysmgr@example.com") is True


# --- Part B (Frappe 16 loop replay) -------------------------------------------

def _core_file_hook(doc, ptype=None, user=None, debug=False, *, ref_has_permission=None, shared_with=()):
	"""Minimal stand-in for frappe.core.doctype.file.file.has_permission
	(file.py:944-981 on Frappe 16.18.0; file.py:876-916 on 15.107.2 is the
	same apart from a ptype == "create" branch no test here uses):
	Administrator is always allowed; a non-private file is readable/
	selectable by anyone; the File doc's own owner is always allowed; a
	user the File itself is shared with (``shared_with``, standing in for
	frappe.share.get_shared on File) is allowed for read/write/share/submit;
	otherwise delegate to the attached-to document's has_permission, asking
	it for "write" when ptype is write/create/delete and for "read"
	otherwise. That delegation is where a System Manager's role-based
	DocPerm on Optimus Session actually grants access, since core's own
	hook has no special case for roles itself. It is also where a
	read-sharee (a DocShare on the Optimus Session, not a role) would be
	granted access if optimus's own hook did not intercept the request
	first."""
	if user == "Administrator":
		return True
	if not getattr(doc, "is_private", True) and ptype in ("read", "select"):
		return True
	if user != "Guest" and getattr(doc, "owner", None) == user:
		return True
	if user != "Guest" and ptype in ("read", "write", "share", "submit") and user in shared_with:
		return True
	if getattr(doc, "attached_to_doctype", None) and getattr(doc, "attached_to_name", None):
		ref_ptype = "write" if ptype in ("write", "create", "delete") else "read"
		return bool(ref_has_permission and ref_has_permission(ref_ptype, user))
	return False


def _spy_core(calls, **kw):
	"""The stand-in core File hook with its real signature, recording each
	call so a test can prove the loop reached it (or never did). Used in
	both Part B (v16) and Part C (v15): recording at hook entry, not
	inside a delegated callback like ``ref_has_permission``, is what makes
	``calls == []`` actually prove core never ran, rather than only
	proving one particular branch inside core was not reached."""

	def core(doc, ptype=None, user=None, debug=False):
		calls.append((ptype, user))
		return _core_file_hook(doc, ptype, user, debug, **kw)

	return core


def _pre_fix_file_has_permission(doc, ptype=None, user=None, *, roles=(), recording_user=None):
	"""Verbatim copy (parameterised so it needs no frappe) of
	optimus.permissions.file_has_permission exactly as it stood before
	this PR: every "no objection" branch returned None, and _GATED_FIELDS
	did not include recordings_file. Kept ONLY as a regression contrast
	below."""
	pre_fix_gated_fields = frozenset({"raw_report_file", "raw_report_pdf_file"})
	if not doc:
		return None
	if doc.attached_to_doctype != permissions.PROFILER_SESSION_DOCTYPE:
		return None
	if doc.attached_to_field not in pre_fix_gated_fields:
		return None
	if "System Manager" in roles or "Administrator" in roles:
		return None
	if not doc.attached_to_name:
		return False
	if recording_user != user:
		return False
	return None


def _newargs(fn, kwargs):
	"""Local copy of frappe.get_newargs's filtering (frappe/__init__.py:
	1150-1172): drop kwargs the callable's signature doesn't declare
	(unless it takes **kwargs). Needed because file_has_permission has no
	`debug` parameter while the stand-in core hook does, exactly like the
	real hooks frappe.call dispatches to."""
	params = inspect.signature(fn).parameters
	if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
		return dict(kwargs)
	return {k: v for k, v in kwargs.items() if k in params}


def _has_controller_permissions(doc, ptype, user, methods, *, debug=False):
	"""Verbatim copy of the control flow in frappe.permissions.
	has_controller_permissions on Frappe 16 (frappe/permissions.py:481-498,
	Frappe v16.18.0), plus frappe.call's kwarg filtering (frappe/__init__.py:
	1122-1129) so it runs against plain callables with real hook
	signatures. ``methods`` must be given in installed-app order (frappe's
	own File hook first, optimus's added after): this helper reverses it
	exactly as the real function does, so optimus's hook runs FIRST, and
	it stops at the first FALSY answer."""
	kwargs = dict(doc=doc, ptype=ptype, user=user, debug=debug)
	for method in reversed(methods):
		controller_permission = method(**_newargs(method, kwargs))
		if not controller_permission:
			return bool(controller_permission)
	return True


def test_non_optimus_file_allowed(monkeypatch):
	"""A File unrelated to Optimus (e.g. a public ToDo attachment) must be
	allowed for Guest on Frappe 16, exactly what core Frappe alone would
	decide. This is the scenario that exposed the original bug: before
	this fix, optimus's hook denied EVERY File, Optimus or not, because it
	ran first and returned None unconditionally for any doctype other than
	Optimus Session."""
	_patch(monkeypatch)
	doc = types.SimpleNamespace(
		attached_to_doctype="ToDo", attached_to_field="attachment",
		attached_to_name="TD-0001", is_private=False, owner="creator@example.com",
	)
	methods = [_core_file_hook, permissions.file_has_permission]
	assert _has_controller_permissions(doc, "read", "Guest", methods) is True

	assert _has_controller_permissions(
		doc, "read", "Guest", [_core_file_hook, _pre_fix_file_has_permission],
	) is False


def test_system_manager_allowed(monkeypatch):
	doc = types.SimpleNamespace(
		attached_to_doctype="Optimus Session", attached_to_field="raw_report_file",
		attached_to_name="OS-0001", is_private=True, owner="someone.else@example.com",
	)
	_patch(monkeypatch, roles=["System Manager"], recording_user="owner@example.com")
	core = functools.partial(_core_file_hook, ref_has_permission=lambda ptype, user: True)
	methods = [core, permissions.file_has_permission]
	assert _has_controller_permissions(doc, "read", "sysmgr@example.com", methods) is True

	pre_fix = functools.partial(
		_pre_fix_file_has_permission, roles=["System Manager"], recording_user="owner@example.com",
	)
	assert _has_controller_permissions(doc, "read", "sysmgr@example.com", [core, pre_fix]) is False


def test_owner_allowed(monkeypatch):
	"""The recording user (the Optimus Session's `user` field) must be
	allowed, independent of the File doc's own `owner` field (set here to
	an unrelated user to isolate optimus's own recording-user gate from
	core's owner shortcut)."""
	doc = types.SimpleNamespace(
		attached_to_doctype="Optimus Session", attached_to_field="raw_report_file",
		attached_to_name="OS-0002", is_private=True, owner="system@example.com",
	)
	_patch(monkeypatch, roles=[], recording_user="owner@example.com")
	core = functools.partial(_core_file_hook, ref_has_permission=lambda ptype, user: True)
	methods = [core, permissions.file_has_permission]
	assert _has_controller_permissions(doc, "read", "owner@example.com", methods) is True

	pre_fix = functools.partial(_pre_fix_file_has_permission, roles=[], recording_user="owner@example.com")
	assert _has_controller_permissions(doc, "read", "owner@example.com", [core, pre_fix]) is False


def test_stranger_denied(monkeypatch):
	"""A user who is neither System Manager/Administrator nor the
	recording user must be denied, and core's hook must never even run:
	hooks can only deny, so optimus's own gate is sufficient to stop the
	loop without needing to re-implement core's logic."""
	doc = types.SimpleNamespace(
		attached_to_doctype="Optimus Session", attached_to_field="raw_report_file",
		attached_to_name="OS-0003", is_private=True, owner="system@example.com",
	)
	_patch(monkeypatch, roles=[], recording_user="owner@example.com")
	calls = []
	core = _spy_core(calls, ref_has_permission=lambda ptype, user: True)
	methods = [core, permissions.file_has_permission]
	assert _has_controller_permissions(doc, "read", "stranger@example.com", methods) is False
	assert calls == []


def test_read_sharee_denied_recordings_file(monkeypatch):
	"""A read-sharee (someone granted read on the parent Optimus Session via
	a DocShare, not a role and not the recording user) must be denied the
	raw recordings bundle, and core's hook (the one that would actually
	grant a share-ee access by delegating to the parent doc's
	has_permission) must never even run. This is the exact gap this PR
	closes: recordings_file joining _GATED_FIELDS means a share-ee who can
	legitimately read the Optimus Session (and see its rendered report
	through the normal UI) still cannot fetch the raw capture bundle
	directly."""
	doc = types.SimpleNamespace(
		attached_to_doctype="Optimus Session", attached_to_field="recordings_file",
		attached_to_name="OS-0004", is_private=True, owner="system@example.com",
	)
	_patch(monkeypatch, roles=[], recording_user="owner@example.com")
	calls = []
	# A share-ee: core's own hook WOULD grant access by delegating to
	# the parent doc's has_permission, which returns True for a valid
	# DocShare. optimus's hook must deny before core ever gets asked.
	core = _spy_core(calls, ref_has_permission=lambda ptype, user: True)
	methods = [core, permissions.file_has_permission]
	assert _has_controller_permissions(doc, "read", "sharee@example.com", methods) is False
	assert calls == []


# --- Part C (version parsing, Frappe 15 contract and loop replay) -------------

@pytest.mark.parametrize(
	("version", "major"),
	[
		("16.18.0", 16),
		("15.107.2", 15),
		("16.0.0-dev", 16),
		("15.x.x-develop", 15),
		("", None),
		("garbage", None),
	],
)
def test_frappe_major_parses(monkeypatch, version, major):
	_on_frappe(monkeypatch, version)
	assert permissions._frappe_major() == major


def test_frappe_major_missing_version_is_none(monkeypatch):
	"""The CI conftest's frappe stub has no __version__ attribute at all."""
	monkeypatch.delattr(permissions.frappe, "__version__", raising=False)
	assert permissions._frappe_major() is None


@pytest.mark.parametrize(
	("version", "expected"),
	[
		("16.18.0", True),
		("16.0.0-dev", True),
		("17.0.0-dev", True),
		("15.107.2", None),
		("15.103.0", None),
		("15.x.x-develop", None),
		("", None),
		("garbage", None),
	],
)
def test_no_objection_by_version(monkeypatch, version, expected):
	"""True where Frappe ANDs the hooks (16 and later), None where the first
	non-None answer wins (15 and earlier), and None when the version can't
	be read: over-denying on an unreadable Frappe 16 is loud and
	recoverable, while True on an unreadable Frappe 15 would silently expose
	every private file."""
	_on_frappe(monkeypatch, version)
	assert permissions._no_objection() is expected


@pytest.mark.parametrize(
	("doc", "roles", "user"),
	[
		pytest.param(None, (), "stranger@example.com", id="no-doc"),
		pytest.param(_doc(attached_to_doctype="ToDo"), (), "stranger@example.com", id="non-optimus-file"),
		pytest.param(_doc(attached_to_field=None), (), "stranger@example.com", id="generic-attachment"),
		pytest.param(_doc(), ("System Manager",), "sysmgr@example.com", id="system-manager"),
		pytest.param(_doc(), (), "owner@example.com", id="recording-user"),
	],
)
def test_v15_defers_none(monkeypatch, doc, roles, user):
	"""On Frappe 15 every "no objection" branch returns None, exactly what
	develop returned, so the next hook (Frappe's own File check) still
	runs."""
	_on_frappe(monkeypatch, _V15)
	_patch(monkeypatch, roles=roles, recording_user="owner@example.com")
	assert permissions.file_has_permission(doc, "read", user=user) is None


def test_v15_gate_still_denies_false(monkeypatch):
	_on_frappe(monkeypatch, _V15)
	_patch(monkeypatch, recording_user="owner@example.com")
	assert permissions.file_has_permission(_doc(), "read", user="stranger@example.com") is False
	assert permissions.file_has_permission(
		_doc(attached_to_name=None), "read", user="stranger@example.com",
	) is False


def _has_controller_permissions_v15(doc, ptype, user, methods, *, debug=False):
	"""Verbatim copy of the control flow in frappe.permissions.
	has_controller_permissions on Frappe 15 (frappe/permissions.py:442-460,
	identical in 15.107.2 and 15.103.0), with the same frappe.call kwarg
	filtering as the v16 copy. Same reversed(install order), so optimus's
	hook still runs FIRST, but it stops at the first NON-None answer: a True
	from optimus would be a grant that skips Frappe's own File hook."""
	kwargs = dict(doc=doc, ptype=ptype, user=user, debug=debug)
	for method in reversed(methods):
		controller_permission = method(**_newargs(method, kwargs))
		if controller_permission is not None:
			return bool(controller_permission)
	return True


def _file(**kw):
	base = dict(
		attached_to_doctype=None, attached_to_field=None, attached_to_name=None,
		is_private=True, owner="alice@example.com",
	)
	base.update(kw)
	return types.SimpleNamespace(**base)


def _always_true_hook(doc, ptype=None, user=None):
	"""What an unconditional "no objection is True" fix answers for a File
	that is not an Optimus artifact."""
	return True


@pytest.mark.parametrize(
	"doc",
	[
		pytest.param(_file(), id="unattached"),
		pytest.param(_file(attached_to_doctype="ToDo", attached_to_name="TD-0002"), id="attached-to-unreadable-todo"),
	],
)
def test_v15_private_file_of_another_user_denied(monkeypatch, doc):
	"""The Frappe 15 confidentiality pin: bob asks for alice's private File.
	Optimus has no objection (None), so the v15 loop moves on to Frappe's
	own File hook, which denies: bob is not the owner, the File is not
	shared with him, and he can't read the document it is attached to. If
	optimus answered True here, the v15 loop would stop at optimus and
	grant, and the File DocType's "All" read permission would then let any
	logged-in user read any private file (the contrast at the end)."""
	_on_frappe(monkeypatch, _V15)
	_patch(monkeypatch)
	calls = []
	core = _spy_core(calls, ref_has_permission=lambda ref_ptype, user: False)
	methods = [core, permissions.file_has_permission]
	assert _has_controller_permissions_v15(doc, "read", "bob@example.com", methods) is False
	assert calls == [("read", "bob@example.com")]

	assert _has_controller_permissions_v15(doc, "read", "bob@example.com", [core, _pre_fix_file_has_permission]) is False
	assert _has_controller_permissions_v15(doc, "read", "bob@example.com", [core, _always_true_hook]) is True


@pytest.mark.parametrize(
	("doc", "user", "shared_with"),
	[
		pytest.param(_file(owner="bob@example.com"), "bob@example.com", (), id="own-private-file"),
		pytest.param(_file(), "bob@example.com", ("bob@example.com",), id="file-shared-with-user"),
		pytest.param(_file(is_private=False), "Guest", (), id="public-file-for-guest"),
	],
)
def test_v15_own_shared_public_file_allowed(monkeypatch, doc, user, shared_with):
	"""Positive controls for Frappe 15: optimus defers (None) and Frappe's
	own File hook, which the loop must actually reach, grants the owner, a
	user the File is shared with, and anyone for a public File."""
	_on_frappe(monkeypatch, _V15)
	_patch(monkeypatch)
	calls = []
	methods = [_spy_core(calls, shared_with=shared_with), permissions.file_has_permission]
	assert _has_controller_permissions_v15(doc, "read", user, methods) is True
	assert calls == [("read", user)]


def test_v15_gated_file_stranger_denied_before_core(monkeypatch):
	"""The gate's explicit False stops the Frappe 15 loop too: a read-sharee
	on the parent Optimus Session is denied the recordings bundle and
	Frappe's own File hook (which would grant through the share) never
	runs."""
	_on_frappe(monkeypatch, _V15)
	_patch(monkeypatch, recording_user="owner@example.com")
	doc = _file(
		attached_to_doctype="Optimus Session", attached_to_field="recordings_file",
		attached_to_name="OS-0005", owner="system@example.com",
	)
	calls = []
	methods = [_spy_core(calls, ref_has_permission=lambda ref_ptype, user: True), permissions.file_has_permission]
	assert _has_controller_permissions_v15(doc, "read", "sharee@example.com", methods) is False
	assert calls == []


@pytest.mark.parametrize(
	("user", "roles", "ptype", "parent_perms", "expected"),
	[
		pytest.param("owner@example.com", (), "read", {"read"}, True, id="recording-user-read"),
		pytest.param("sysmgr@example.com", ("System Manager",), "read", {"read", "write"}, True, id="system-manager-read"),
		pytest.param("owner@example.com", (), "delete", {"read"}, False, id="recording-user-delete-without-parent-write"),
	],
)
def test_v15_gate_passes_then_core_decides(monkeypatch, user, roles, ptype, parent_perms, expected):
	"""A user who passes optimus's gate is handed on (None, not True) to
	Frappe's own File hook, which then decides. Reading the report works
	through the parent Optimus Session's read permission. Deleting it needs
	write on the parent, which the Optimus User role does not have, so the
	recording user is denied. A literal True on the gate's pass-through
	branch would grant that delete on Frappe 15 (the File DocType's "All"
	role has delete)."""
	_on_frappe(monkeypatch, _V15)
	_patch(monkeypatch, roles=roles, recording_user="owner@example.com")
	doc = _file(
		attached_to_doctype="Optimus Session", attached_to_field="raw_report_file",
		attached_to_name="OS-0006", owner="system@example.com",
	)
	calls = []
	core = _spy_core(calls, ref_has_permission=lambda ref_ptype, u: ref_ptype in parent_perms)
	methods = [core, permissions.file_has_permission]
	assert _has_controller_permissions_v15(doc, ptype, user, methods) is expected
	assert calls == [(ptype, user)]


# --- Part D (static guard) ----------------------------------------------------

def _is_allowed_return(node: ast.Return) -> bool:
	"""True for ``return False`` (the gate's deny) and ``return
	_no_objection()`` (the version-aware defer). Everything else is
	forbidden: a literal None or a bare return (a deny on Frappe 16), a
	literal True (a grant that skips Frappe's own File check on Frappe 15),
	or any other expression."""
	value = node.value
	if isinstance(value, ast.Constant):
		return value.value is False
	return (
		isinstance(value, ast.Call)
		and isinstance(value.func, ast.Name)
		and value.func.id == "_no_objection"
		and not value.args
		and not value.keywords
	)


def _offending_returns(func: ast.FunctionDef) -> list[ast.Return]:
	return [n for n in ast.walk(func) if isinstance(n, ast.Return) and not _is_allowed_return(n)]


def test_file_has_permission_returns_only_false_or_no_objection():
	"""Static guard pinning the PR-0b fix on both Frappe contracts: every
	return in file_has_permission is either ``False`` or
	``_no_objection()``. Reads the LIVE function via inspect.getsource (not
	a copy), so a future edit that reintroduces ``return None`` (every File
	denied on Frappe 16) or writes ``return True`` (every File granted on
	Frappe 15) on any branch fails this test immediately."""
	func = ast.parse(inspect.getsource(permissions.file_has_permission)).body[0]
	assert isinstance(func, ast.FunctionDef)
	returns = [n for n in ast.walk(func) if isinstance(n, ast.Return)]
	assert returns, "file_has_permission has no return statements to check"
	offenders = _offending_returns(func)
	assert not offenders, "file_has_permission must return only False or _no_objection(): " + "; ".join(
		f"line {n.lineno}: {ast.unparse(n)}" for n in offenders
	)


@pytest.mark.parametrize(
	("body", "flagged"),
	[
		("return None", True),
		("return", True),
		("return True", True),
		("return bool(doc)", True),
		("return _no_objection(doc)", True),
		("return False", False),
		("return _no_objection()", False),
	],
)
def test_ast_guard_classifies_returns(body, flagged):
	"""Meta-test: the guard's own classifier (the same _offending_returns the
	live test uses) flags every forbidden shape and accepts the two allowed
	ones, checked on throwaway source rather than a mutated
	optimus/permissions.py."""
	src = f"def file_has_permission(doc, ptype=None, user=None):\n\t{body}\n"
	func = ast.parse(src).body[0]
	assert bool(_offending_returns(func)) is flagged


def test_gated_fields_include_recordings_file():
	"""Direct pin on the set itself (not just observed behaviour), so a
	future edit that silently drops recordings_file from _GATED_FIELDS
	(e.g. during an unrelated refactor of the frozenset literal) fails
	immediately."""
	assert permissions._GATED_FIELDS == frozenset(
		{"raw_report_file", "raw_report_pdf_file", "recordings_file"}
	)
