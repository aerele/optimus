# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Resolve Server Script source code from the Frappe database at render time.

Frappe's ``safe_exec`` compiles Server Script bodies with a synthetic filename
(``<serverscript>`` or ``<serverscript>: <scrubbed-name>``) that never resolves
to an on-disk file, so callsites in Server Script bodies render without a code
snippet or editor link. This module loads the script's stored ``script`` field
from ``tabServer Script`` so the renderer can show inline code and link to the
Desk form (``/app/server-script/<name>``).

Best-effort: every public function returns ``None`` / a safe default on any
error so a DB hiccup at render time never breaks the report.
"""

from __future__ import annotations

import re

# The exact safe-exec prefix Frappe writes into the compiled filename. Kept
# explicit so a future Frappe rename surfaces as a test failure here rather
# than silent degradation across the report.
_SERVER_SCRIPT_PREFIX = "<serverscript>"
_SERVER_SCRIPT_FILENAME_RE = re.compile(r"^<serverscript>(?:\s*:\s*(?P<name>[^<>]+?))?\s*$")


def extract_script_name(filename) -> str | None:
	"""Parse the scrubbed Server Script name out of a synthetic ``safe_exec``
	filename (``<serverscript>: <name>``). Returns the scrubbed name, or ``None``
	when the filename doesn't match that shape or is bare ``<serverscript>`` (no
	name to look up)."""
	if not filename or not isinstance(filename, str):
		return None
	m = _SERVER_SCRIPT_FILENAME_RE.match(filename.strip())
	if not m:
		return None
	name = (m.group("name") or "").strip()
	return name or None


def is_server_script_filename(filename) -> bool:
	"""``True`` for any ``<serverscript>`` filename (named or bare), so callers can branch before extracting a name."""
	if not filename or not isinstance(filename, str):
		return False
	return filename.strip().startswith(_SERVER_SCRIPT_PREFIX)


def get_server_script_record(scrubbed_name: str, *, cache: dict | None = None) -> dict | None:
	"""Look up a Server Script row by its scrubbed name (the form stored in the
	synthetic ``safe_exec`` filename) and return ``{"name": <actual>, "script":
	<body>}`` or ``None``.

	Resolution is scrub-equivalent (Frappe's ``scrub`` is lossy: lowercases and
	replaces non-alphanumerics with ``_``), so the original-cased name the Desk URL
	needs round-trips. ``cache``, when given, memoizes the result per render.
	"""
	from optimus.ai_fix import _InterruptGuard
	from optimus.renderer.source import _may_read_server_script

	if not isinstance(scrubbed_name, str) or not scrubbed_name or len(scrubbed_name) > 140:
		return None
	if not _may_read_server_script():
		return None
	cache_key = ("server_script", scrubbed_name)
	if cache is not None and cache_key in cache:
		record = cache[cache_key]
		return record if record and _may_read_server_script(record["name"]) else None
	guard = _InterruptGuard(base=True)
	record = None
	try:
		with guard:
			import frappe

			# Resolve the lossy scrubbed name using names only. Never fetch every
			# script body, and require document permission for the matched row.
			matches = [row["name"] for row in frappe.get_all("Server Script", fields=["name"])
				if frappe.scrub(row["name"]) == scrubbed_name or row["name"].lower() == scrubbed_name.lower()]
			if len(matches) == 1 and _may_read_server_script(matches[0]):
				body = frappe.db.get_value("Server Script", matches[0], "script")
				if isinstance(body, str) and len(body) <= 4 * 1024 * 1024:
					record = {"name": matches[0], "script": body}
	except Exception:
		record = None
	if guard.pending():
		record = body = None
		raise guard.interrupt()
	if cache is not None:
		cache[cache_key] = record
	return record


def get_server_script_lines(scrubbed_name: str, *, cache: dict | None = None) -> list[str] | None:
	"""Return the Server Script's ``script`` field split into lines, or ``None`` if
	it can't be resolved. Reuses ``get_server_script_record`` (shares its cache)."""
	record = get_server_script_record(scrubbed_name, cache=cache)
	if not record:
		return None
	body = record.get("script") or ""
	if not body:
		return None
	return body.splitlines()


def desk_url(scrubbed_name: str, *, cache: dict | None = None) -> str:
	"""Build a Desk URL for the Server Script: the specific form
	(``/app/server-script/<actual-name>``) when ``scrubbed_name`` resolves, else
	the list page (``/app/server-script``). The actual name is passed through with
	its original casing / spaces (the browser handles escaping)."""
	record = get_server_script_record(scrubbed_name, cache=cache)
	if record and record.get("name"):
		return f"/app/server-script/{record['name']}"
	return "/app/server-script"
