# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""One-off maintenance for sites that ran an AI-enabled release with the API
key leak (see the security advisory in CHANGELOG.md).

A failed AI call could store the provider API key in plain text in
``tabError Log`` (and, once such a row was deleted, in its
``tabDeleted Document`` copy). ``scrub_error_log_secrets`` masks those keys in
place; the ``v0_12.scrub_ai_keys_from_error_log`` patch runs it once on
``bench migrate``. ``purge_ai_error_logs`` is the opt-in stronger option: it
deletes every AI-surface row (they may also hold prompt text: source code and
SQL with literal values).

Both are safe to re-run and can be called by hand::

    bench --site <site> execute optimus.maintenance.scrub_error_log_secrets --kwargs "{'dry_run': True}"
    bench --site <site> execute optimus.maintenance.purge_ai_error_logs --kwargs "{'dry_run': False}"

Candidates are chosen by CONTENT, not title: rows Frappe itself wrote for a
500 or a failed background job carry no Optimus title but do carry an
``ai_fix.py`` frame. Rows whose text, title or request metadata contain the
key stored today are read as well. Plain ``LIKE`` filters and the ORM keep
this portable to Postgres. Rows are processed in chunks of ``batch_size``
names with a commit per chunk, so a large table never builds one huge
transaction, and a re-run after an interruption only rewrites what is left.

The stored-key pass sends the key as a bound ``LIKE`` parameter, so a site
with MariaDB ``general_log`` ON or Frappe ``logging: 2`` records it in that
log; turn those off before running the scrub.
"""

from __future__ import annotations

import json
import re

import frappe

from optimus import safe_commit
from optimus.redaction import SECRET_PLACEHOLDER, scrub_secrets

_BATCH = 200
_SAVEPOINT = "optimus_scrub_row"
# frappe.deferred_insert.queue_prefix + doctype: only this queue is flushed.
_ERROR_LOG_QUEUE = "insert_queue_for_Error Log"
_AI_FRAME = "%ai_fix.py%"
# Any of these next to an ai_fix.py frame means the row may hold a key.
_SECRET_MARKERS = ("%Bearer %", "%api_key%", "%x-api-key%")
_MIN_KEY_LEN = 8
# bench migrate runs the scrub only when it reads at most this many rows; a
# LIKE scan of a larger Error Log would stall the migrate, so the patch prints
# the command to run instead.
MIGRATE_SCAN_LIMIT = 200_000

# Frappe's with-context traceback prints the locals of the urllib3 /
# http.client frames that send a header (``value``, ``values``,
# ``one_value``): the raw header value, so an ``x-api-key`` header shows as a
# bare key that no scrub_secrets shape matches. Only string-like values
# (quoted, bytes, list, tuple) are masked, and only in rows already selected
# as AI rows. The second form is the same line inside Deleted Document data,
# which is JSON (newlines escaped as \n); it consumes escape pairs whole so
# the JSON stays valid.
_VALUE_LINE = re.compile(r"""(?m)^([ \t]+(?:value|values|one_value) = )(?:b?['"]|[\[(]).*$""")
_ESCAPED_VALUE_LINE = re.compile(
	r"""(\\n[ \t]+(?:value|values|one_value) = )(?:b?'|b?\\"|[\[(])(?:(?!\\n)(?:\\.|[^"\\]))*"""
)
# Independent residual check: provider key shapes wherever they appear
# (OpenAI / Anthropic sk-, sk-ant-, sk-proj-; Groq gsk_; Google AIza). It
# never drives the masking, so a row it still flags after the scrub holds a
# shape the masking misses. Other providers' key shapes are covered only by
# the stored-key literal and by the owner's count of the rotated keys.
_KEY_SHAPE = re.compile(
	r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_\-]{16,}|gsk_[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_\-]{30,})"
)


def _or_markers(field: str) -> list[list]:
	return [[field, "like", marker] for marker in _SECRET_MARKERS]


def _json_escaped(value: str) -> str:
	"""``value`` as it appears inside JSON text (Deleted Document data is
	``as_json`` with ``ensure_ascii``: a smart quote becomes ``\\u2019``)."""
	return json.dumps(value)[1:-1] if value else ""


def _like_literal(value: str) -> str:
	"""``value`` escaped for a ``LIKE`` pattern, because backslash is the
	default LIKE escape on MariaDB and Postgres, so an unescaped backslash (the
	``\\u2019`` of a JSON-escaped key) never matches itself and ``%`` / ``_``
	would act as wildcards.

	It assumes the pattern reaches the database as a bound parameter, as on
	Frappe v16, where ``frappe.get_all`` builds the query with the query
	builder. Frappe v15's ``db_query`` path doubles backslashes itself, so
	there the stored-key pass could miss a key containing ``_``, ``%`` or a
	JSON-escaped character; the passes over rows with an ``ai_fix.py`` frame
	and the masking of the key in Python are unaffected."""
	return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _mask(text: str, api_key: str) -> str:
	out = scrub_secrets(text, literals=(api_key, _json_escaped(api_key)))
	out = _VALUE_LINE.sub(lambda m: m.group(1) + SECRET_PLACEHOLDER, out)
	return _ESCAPED_VALUE_LINE.sub(lambda m: m.group(1) + SECRET_PLACEHOLDER, out)


def _has_residual_secret(text: str, api_key: str) -> bool:
	if _KEY_SHAPE.search(text):
		return True
	return len(api_key) >= _MIN_KEY_LEN and (api_key in text or _json_escaped(api_key) in text)


def _flush_deferred_error_logs() -> int:
	"""Insert the Error Log rows ``defer_insert`` queued in Redis (Frappe's own
	error snapshots are deferred, and a site whose scheduler is paused never
	flushes them), so the scrub sees them instead of them landing unmasked
	later. Only the Error Log queue: other doctypes' queues stay the
	scheduler's. Never raises; returns how many queued rows could not be
	inserted (a queue that cannot be read counts as one)."""
	failed = 0
	try:
		while raw := frappe.cache.lpop(_ERROR_LOG_QUEUE):
			records = json.loads(raw)
			for record in records if isinstance(records, list) else [records]:
				failed += not _insert_error_log(record)
	except Exception:
		failed += 1
	finally:
		# Commit what was inserted even when the loop stopped early: the rows
		# are already gone from Redis, and a later rollback would drop them.
		try:
			safe_commit()
		except Exception:
			failed += 1
	return failed


def _insert_error_log(record: dict) -> bool:
	"""Insert one queued record under a savepoint, so a failed insert rolls
	back only itself, also on Postgres, where a failed statement aborts the
	transaction and every later queued insert with it. The savepoint is
	released either way (see ``_write_row``)."""
	ok = True
	try:
		frappe.db.savepoint(_SAVEPOINT)
		frappe.get_doc({**record, "doctype": "Error Log"}).insert(ignore_permissions=True)
		frappe.db.release_savepoint(_SAVEPOINT)
	except Exception:
		ok = False
	if not ok:
		try:
			frappe.db.rollback(save_point=_SAVEPOINT)
			frappe.db.release_savepoint(_SAVEPOINT)
		except Exception:
			pass
	return ok


def _mask_row(row: dict, text_fields: tuple[str, ...], api_key: str) -> tuple[dict, bool] | None:
	"""``(changes, residual)`` for one row, or None when masking it failed."""
	result = None
	try:
		changes = {}
		residual = False
		for field in text_fields:
			old = row.get(field) or ""
			new = _mask(old, api_key)
			if new != old:
				changes[field] = new
			residual = residual or _has_residual_secret(new, api_key)
		result = (changes, residual)
	except Exception:
		result = None
	return result


def _write_row(doctype: str, name: str, changes: dict) -> bool:
	"""Write one masked row under a savepoint, so a failed write (a lock
	timeout, a row deleted meanwhile) rolls back only itself, also on
	Postgres, where a failed statement aborts the transaction. The savepoint
	is released after the write and also after the rollback, because this
	function reuses one savepoint name and re-issuing a savepoint of the same
	name nests a new subtransaction on Postgres instead of replacing it.
	Frappe's own ``savepoint()`` helper releases only after a success (it
	takes a fresh random name each time)."""
	failed = False
	try:
		frappe.db.savepoint(_SAVEPOINT)
		frappe.db.set_value(doctype, name, changes, update_modified=False)
		frappe.db.release_savepoint(_SAVEPOINT)
	except Exception:
		failed = True
	if failed:
		try:
			frappe.db.rollback(save_point=_SAVEPOINT)
			frappe.db.release_savepoint(_SAVEPOINT)
		except Exception:
			pass
	return not failed


def _chunks(doctype: str, filters: list[list], or_filters: list[list] | None, fields: list[str], batch_size: int):
	"""Yield successive lists of rows ordered by ``name`` (keyset pagination, so
	rows edited in an earlier chunk never shift a later one)."""
	last = ""
	while True:
		rows = frappe.get_all(
			doctype,
			filters=[*filters, ["name", ">", last]],
			or_filters=or_filters,
			fields=fields,
			order_by="name asc",
			limit_page_length=batch_size,
		)
		if not rows:
			return
		yield rows
		last = rows[-1]["name"]


def _scans(api_key: str) -> list[tuple[str, list[list], list[list], tuple[str, ...], str]]:
	"""(doctype, filters, or_filters, text fields, counter) for each pass."""
	deleted = [["deleted_doctype", "=", "Error Log"]]
	scans = [
		("Error Log", [["error", "like", _AI_FRAME]], _or_markers("error"), ("error", "method", "metadata"), "changed"),
		("Deleted Document", [*deleted, ["data", "like", _AI_FRAME]], _or_markers("data"), ("data",), "deleted_docs_changed"),
	]
	if len(api_key) >= _MIN_KEY_LEN:
		# The key stored today, anywhere: a 500 snapshot's title is the
		# exception message and a request's metadata holds its form data.
		likes = sorted({f"%{_like_literal(api_key)}%", f"%{_like_literal(_json_escaped(api_key))}%"})
		scans += [
			("Error Log", [], [[f, "like", p] for f in ("error", "method", "metadata") for p in likes],
				("error", "method", "metadata"), "changed"),
			("Deleted Document", deleted, [["data", "like", p] for p in likes], ("data",), "deleted_docs_changed"),
		]
	return scans


def scrub_scan_size() -> int:
	"""How many rows ``scrub_error_log_secrets`` may read: every Error Log row
	plus every Deleted Document copy of one. Cheap (no LIKE scan), so
	``bench migrate`` can decide whether to run the scrub inline."""
	return int(frappe.db.count("Error Log") or 0) + int(
		frappe.db.count("Deleted Document", {"deleted_doctype": "Error Log"}) or 0
	)


def scrub_error_log_secrets(dry_run: bool = True, batch_size: int = 200) -> dict:
	"""Mask API keys left in Error Log rows (and Deleted Document copies of
	Error Log rows) by AI failures before the fix.

	Reads rows whose text contains an ``ai_fix.py`` frame AND a secret
	marker, plus rows whose text, title or metadata contain the key stored
	today. A row is written only when the masking changes it, with
	``update_modified=False``. Idempotent: a second run changes nothing.
	Prompt text (source code, SQL) in those rows is left as is; use
	``purge_ai_error_logs`` to remove the rows entirely. A real run first
	inserts the Error Log rows still queued by ``defer_insert``. One row that
	cannot be masked or written is counted in ``failed`` and skipped; it never
	stops the others.

	Returns ``{"candidates", "changed", "deleted_docs_changed", "residual",
	"failed"}``: Error Log rows read, Error Log rows and Deleted Document rows
	masked, rows that still hold a key-shaped value after masking (checked
	with a detector independent of the masking), and rows (or queued rows)
	that could not be processed. With ``dry_run=True`` the counts say what
	WOULD change and nothing is written.
	"""
	from optimus.ai_fix import _current_key_or_empty

	batch_size = max(1, int(batch_size or _BATCH))
	out = {"candidates": 0, "changed": 0, "deleted_docs_changed": 0, "residual": 0, "failed": 0}
	if not dry_run:
		out["failed"] += _flush_deferred_error_logs()
	api_key = _current_key_or_empty()
	seen: dict[str, set[str]] = {"Error Log": set(), "Deleted Document": set()}

	for doctype, filters, or_filters, text_fields, counter in _scans(api_key):
		for rows in _chunks(doctype, filters, or_filters, ["name", *text_fields], batch_size):
			for row in rows:
				if row["name"] in seen[doctype]:
					continue
				seen[doctype].add(row["name"])
				if doctype == "Error Log":
					out["candidates"] += 1
				masked = _mask_row(row, text_fields, api_key)
				if masked is None:
					out["failed"] += 1
					continue
				changes, residual = masked
				out["residual"] += int(residual)
				if not changes:
					continue
				if dry_run or _write_row(doctype, row["name"], changes):
					out[counter] += 1
				else:
					out["failed"] += 1
			if not dry_run:
				safe_commit()
	return out


def purge_ai_error_logs(dry_run: bool = True) -> dict:
	"""Delete every Error Log row with an ``ai_fix.py`` frame, and every
	Deleted Document copy of such a row. Opt-in: run it by hand when the
	stored prompt text (source code, SQL literals) must go too.

	Uses ``frappe.db.delete`` so no new Deleted Document copies are made.
	Returns ``{"error_logs": int, "deleted_documents": int}`` (with
	``dry_run=True`` the counts say what WOULD be deleted).
	"""
	out = {"error_logs": 0, "deleted_documents": 0}
	targets = (
		("Error Log", [["error", "like", _AI_FRAME]], "error_logs"),
		("Deleted Document", [["deleted_doctype", "=", "Error Log"], ["data", "like", _AI_FRAME]], "deleted_documents"),
	)
	for doctype, filters, key in targets:
		for rows in _chunks(doctype, filters, None, ["name"], _BATCH):
			out[key] += len(rows)
			if not dry_run:
				frappe.db.delete(doctype, {"name": ("in", [r["name"] for r in rows])})
				safe_commit()
	return out
