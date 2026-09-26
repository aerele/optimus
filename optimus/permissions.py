# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Server-side permission gates for profiler artifacts.

Exposes one gate: a ``has_permission`` for the File DocType that
double-checks downloads of the profiler artifacts (raw_report_file,
raw_report_pdf_file, recordings_file). It runs in Frappe's permission
checks (form load, REST, ``frappe.has_permission``), so a read-sharee on
the parent Optimus Session (granted access via a DocShare, not ownership
or System Manager) can't download the raw recordings bundle through those
checks either. A direct ``/private/files`` download is checked by
Frappe's own File rule (read on the parent Optimus Session), not by this
hook. Admin/owner-scoped.

This hook may only deny. When it has no objection it must hand the
decision on to the next hook, and the value that means "no objection"
depends on the Frappe version. On every version Frappe registers each
app's hooks in install order and calls them in ``reversed()`` order, so
this hook (Optimus is installed after frappe) runs BEFORE
``frappe.core.doctype.file.file.has_permission``, the check that actually
decides private-file, owner, share and attached-document access.

Frappe 16 (frappe/permissions.py:481-498) ANDs the hooks and stops at the
first FALSY answer, so ``None`` is a deny and "no objection" is ``True``::

    for method in reversed(methods):
        controller_permission = frappe.call(method, doc=doc, ptype=ptype, user=user, debug=debug)
        if not controller_permission:
            return bool(controller_permission)
    return True

Frappe 15 (frappe/permissions.py:442-460) stops at the first NON-None
answer, so ``True`` is a grant that skips Frappe's own File check (and the
File DocType's "All" role permission then lets any logged-in user read any
private file), and "no objection" is ``None``::

    for method in reversed(methods):
        controller_permission = frappe.call(method, doc=doc, ptype=ptype, user=user, debug=debug)
        if controller_permission is not None:
            return bool(controller_permission)
    return True

``_no_objection()`` returns the right value for the running Frappe. If the
major version can't be read it returns ``None``: on Frappe 16 that
over-denies Files (loud, reported at once, and exactly the behaviour
before this fix), whereas ``True`` on Frappe 15 would silently expose every
private file on the site.
"""

import re

import frappe

PROFILER_SESSION_DOCTYPE = "Optimus Session"
_GATED_FIELDS = frozenset({"raw_report_file", "raw_report_pdf_file", "recordings_file"})
_MAJOR_VERSION_RE = re.compile(r"\s*([0-9]+)")


def _frappe_major() -> int | None:
	"""Major version of the running Frappe, or None when it can't be read.

	Takes the leading number of ``frappe.__version__``, so release strings
	("16.18.0"), dev builds ("16.0.0-dev") and branch builds
	("15.x.x-develop") all parse. An empty, missing or non-numeric version
	gives None.
	"""
	match = _MAJOR_VERSION_RE.match(str(getattr(frappe, "__version__", "") or ""))
	return int(match.group(1)) if match else None


def _no_objection() -> bool | None:
	"""The hook result that means "no objection, ask the next hook".

	True on Frappe 16 and later. None on Frappe 15 and earlier, and also
	when the version can't be read (fail closed on confidentiality, see the
	module docstring).
	"""
	major = _frappe_major()
	return True if major is not None and major >= 16 else None


def file_has_permission(doc, ptype=None, user=None) -> bool | None:
	"""Gate downloads of the profiler artifacts (report HTML + PDF, raw
	recordings snapshot).

	On top of Frappe's standard File permission check, restricts these
	files to the System Manager or the recording user (even if another role,
	including a read-sharee on the parent Optimus Session, got read access to
	the parent). Returns False to deny. Every other branch returns
	``_no_objection()`` (True on Frappe 16, None on Frappe 15), so Frappe's
	own File check still runs. Never a literal None (a deny on Frappe 16)
	and never a literal True (a grant that skips Frappe's File check on
	Frappe 15): see the module docstring.
	"""
	if not doc:
		return _no_objection()

	# Only intercept files attached to a Optimus Session.
	if doc.attached_to_doctype != PROFILER_SESSION_DOCTYPE:
		return _no_objection()

	# Only intercept the report + recordings files.
	if doc.attached_to_field not in _GATED_FIELDS:
		return _no_objection()

	user = user or frappe.session.user
	roles = frappe.get_roles(user)

	if "System Manager" in roles or "Administrator" in roles:
		return _no_objection()  # defer to standard checks

	# Otherwise the user must be the recording user
	if not doc.attached_to_name:
		return False
	recording_user = frappe.db.get_value(
		PROFILER_SESSION_DOCTYPE,
		doc.attached_to_name,
		"user",
	)
	if recording_user != user:
		return False

	return _no_objection()  # passed our gate; let standard checks run
