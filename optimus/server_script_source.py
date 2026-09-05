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
	if not scrubbed_name:
		return None
	if cache is not None and scrubbed_name in cache:
		return cache[scrubbed_name]

	record: dict | None = None
	try:
		import frappe

		# Match the requested (already-scrubbed) name against every Server
		# Script by scrubbing each candidate the way Frappe canonically does
		# (frappe.scrub). Done in Python rather than SQL REPLACE chains so it's
		# portable across MariaDB/Postgres (no backticks) and exactly correct
		# the Server Script table is tiny, so the full scan is cheap.
		target = (scrubbed_name or "").lower()
		for cand in frappe.get_all("Server Script", fields=["name", "script"]):
			cname = cand.get("name") or ""
			if frappe.scrub(cname) == scrubbed_name or cname.lower() == target:
				record = {"name": cname, "script": cand.get("script") or ""}
				break
	except Exception:
		record = None

	if cache is not None:
		cache[scrubbed_name] = record
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
