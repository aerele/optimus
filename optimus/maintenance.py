# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""One-off maintenance for sites that ran an AI-enabled release with the API
key leak (see the security advisory in CHANGELOG.md).

A failed AI call could store the provider API key in plain text in
``tabError Log`` (and, once such a row was deleted, in its
``tabDeleted Document`` copy). ``scrub_error_log_secrets`` masks those keys in
place; the ``v0_12.scrub_ai_keys_from_error_log`` patch runs it once on
``bench migrate``. ``purge_ai_error_logs`` is the opt-in stronger option: it
deletes every Optimus AI row (they may also hold prompt text: source code and
SQL with literal values).

Both are safe to re-run and can be called by hand::

    bench --site <site> execute optimus.maintenance.scrub_error_log_secrets --kwargs "{'dry_run': True}"
    bench --site <site> execute optimus.maintenance.purge_ai_error_logs --kwargs "{'dry_run': False}"

Candidates are chosen by CONTENT, not title: rows Frappe itself wrote for a
500 or a failed background job carry no Optimus title but do carry an
``ai_fix.py`` frame. Rows whose text, title or request metadata contain the
key stored today are read as well. Plain ``LIKE`` filters and the ORM keep
this portable to Postgres. Matching rows are processed in chunks of
``batch_size`` with a commit per chunk, so a large table never builds one
huge transaction, and a re-run after an interruption only rewrites what is
left.

``tabError Log`` is MyISAM on MariaDB, so each statement holds a table read
lock while it runs and blocks every Error Log insert meanwhile. Each ``LIKE``
statement therefore reads one window of at most ``_WINDOW`` rows by primary
key (``name > last AND name <= bound``, the bound read from the primary key
alone), instead of scanning the rest of the table for a few sparse matches.
On a large Error Log, run it off-peak anyway.

The stored-key pass never sends the key to the database. Its ``LIKE`` value
is an 8-character fragment of the key: the window made only of letters,
digits and ``-`` nearest the middle of the key, which needs no escaping
and so works the same on Frappe v15 and v16 (a key with no such window uses
its escaped middle window instead, see ``_key_fragment``). A row the pass
returns is used only when the FULL key, raw or JSON-escaped, is in the row's
text, checked in Python. So MariaDB's general log and slow log, the
processlist, the Postgres statement log and Frappe's ``logging: 2`` can
record at most that fragment. A key shorter than 16 characters is not
searched by value at all (the fragment would be half of it); the other
passes still run and still mask it by value in every row they read, if it
has at least 8 characters.
"""

from __future__ import annotations

import json
import re
import sys
from typing import NamedTuple

import frappe

from optimus import safe_commit
from optimus.redaction import SECRET_PLACEHOLDER, scrub_secrets

_BATCH = 200
_SAVEPOINT = "optimus_scrub_row"
# frappe.deferred_insert.queue_prefix + doctype: only this queue is flushed.
_ERROR_LOG_QUEUE = "insert_queue_for_Error Log"
# The flush takes at most this many queue entries (each a record or a list of
# them) and commits after every _FLUSH_COMMIT_EVERY inserted rows.
_FLUSH_MAX_POPS = 10_000
_FLUSH_COMMIT_EVERY = 100
_AI_FRAME = "%ai_fix.py%"
# The purge's frame pattern: Optimus's own module only, so another app's
# openai_fix.py rows are never deleted (the "optimus/" prefix excludes them).
# It holds no LIKE escape, so it works on Frappe v15, whose db_query doubles
# backslashes, as well as on v16; its "_" is a one-character wildcard.
_OPTIMUS_AI_FRAME = "%optimus/ai_fix.py%"
# Any of these next to an ai_fix.py frame means the row may hold a key.
_SECRET_MARKERS = ("%Bearer %", "%api_key%", "%x-api-key%")
_MIN_KEY_LEN = 8
# The stored-key pass runs only for a key of at least this many characters,
# and sends only a fragment of _FRAGMENT_LEN characters of it: a window made
# only of _CLEAN_FRAGMENT characters when one exists (no LIKE metacharacter,
# no JSON escape, so it needs no escaping and is the same on v15 and v16).
_MIN_SEARCH_KEY_LEN = 16
_FRAGMENT_LEN = 8
_CLEAN_FRAGMENT = re.compile(r"[A-Za-z0-9-]+")
# Each LIKE statement reads at most this many rows by primary key.
_WINDOW = 1000
# Error Log.method is Data (varchar(140)). Masking can lengthen a value (a
# URL's "u:p" becomes "********"), and in strict mode a value longer than
# the column fails the whole row's write, error text included.
_FIELD_LIMITS = {"method": 140}
# bench migrate runs the scrub only when scrub_scan_size() is at most this; a
# scan of a larger table would stall the migrate, so the patch prints the
# command to run instead.
MIGRATE_SCAN_LIMIT = 200_000
# What scrub_scan_size() returns when a part of the size cannot be read: an
# unmeasured table must not look small, so the migrate skips the scrub.
SCAN_SIZE_UNKNOWN = sys.maxsize
# Deleted Document rows up to a bound (the parameter): exact, portable, and
# it reads at most that many index entries.
_DELETED_DOCUMENT_COUNT = "SELECT COUNT(*) FROM (SELECT 1 FROM `tabDeleted Document` LIMIT %s) t"
# The values dry_run accepts as text (stripped, any case), besides True /
# False and 1 / 0.
_DRY_RUN_WORDS = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}

# Frappe's with-context traceback prints the locals of the urllib3 /
# http.client frames that send a header (``value``, ``values``,
# ``one_value``): the raw header value, so an ``x-api-key`` header shows as a
# bare key that no scrub_secrets shape matches. Only string-like values
# (quoted, bytes, list, tuple) are masked, and only in rows already selected
# as AI rows. The second form is the same line inside Deleted Document data,
# which is JSON (newlines escaped as \n); it consumes escape pairs whole so
# the JSON stays valid. Its repeat is bounded: an unbounded one keeps a
# backtracking frame per character (about 150 bytes each), so a planted
# multi-megabyte row could exhaust memory and kill bench migrate. Frappe
# prints at most 1000 characters of a local, so a real value line fits; a
# longer one keeps its tail past 2048 units, which the residual check still
# reads.
_VALUE_LINE = re.compile(r"""(?m)^([ \t]+(?:value|values|one_value) = )(?:b?['"]|[\[(]).*$""")
_ESCAPED_VALUE_LINE = re.compile(
	r"""(\\n[ \t]+(?:value|values|one_value) = )(?:b?'|b?\\"|[\[(])(?:(?!\\n)(?:\\.|[^"\\])){0,2048}"""
)
# Independent residual check: provider key shapes wherever they appear
# (OpenAI / Anthropic sk-, sk-ant-, sk-proj-; Groq gsk_; Google AIza). It
# never drives the masking, so a row it still flags after the scrub holds a
# shape the masking misses. Other providers' key shapes are covered only by
# the stored-key literal and by the owner's count of the rotated keys.
_KEY_SHAPE = re.compile(
	r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_\-]{16,}|gsk_[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_\-]{30,})"
)


class _Scan(NamedTuple):
	doctype: str
	filters: list[list]
	or_filters: list[list]
	fields: tuple[str, ...]
	counter: str
	# True for the stored-key pass: SQL matched only a fragment, so a row is
	# used only if the full key is in its text.
	confirm_key: bool = False


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

	It is used only for the fallback fragment of a key that has no clean
	window (see ``_key_fragment``). It assumes the pattern reaches the
	database as a bound parameter, as on Frappe v16, where ``frappe.get_all``
	builds the query with the query builder. Frappe v15's ``db_query`` path
	doubles backslashes itself, so on v15 an escaped fallback pattern matches
	nothing and the stored-key pass misses such a key. Every other pattern
	holds no escape and works on v15 and v16 alike, the masking happens in
	Python, and the scrub reads ``metadata`` only where Error Log has that
	column (v15 has none)."""
	return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _key_fragment(api_key: str) -> tuple[str, bool]:
	"""``(fragment, clean)``: the only part of the key the stored-key pass
	sends to the database, ``_FRAGMENT_LEN`` characters long.

	It is the window of the key made only of letters, digits and ``-``
	(``_CLEAN_FRAGMENT``) that lies nearest the middle of the key. Such a
	window has no LIKE metacharacter and no JSON escape, so it is sent as is
	and matches the key and its JSON-escaped copy on Frappe v15 and v16.
	When the key has no such window, the window in the middle is returned
	with ``clean=False``, to be LIKE-escaped (v16 only)."""
	middle = (len(api_key) - _FRAGMENT_LEN) // 2
	starts = sorted(range(len(api_key) - _FRAGMENT_LEN + 1), key=lambda s: (abs(s - middle), s))
	for start in starts:
		window = api_key[start:start + _FRAGMENT_LEN]
		if _CLEAN_FRAGMENT.fullmatch(window):
			return window, True
	return api_key[middle:middle + _FRAGMENT_LEN], False


def _holds_key(row: dict, fields: tuple[str, ...], api_key: str) -> bool:
	"""True when the full key, raw or JSON-escaped, is in one of the row's
	text fields (the fragment the SQL matched is not enough)."""
	escaped = _json_escaped(api_key)
	for field in fields:
		text = row.get(field)
		if isinstance(text, str) and (api_key in text or escaped in text):
			return True
	return False


def _mask(text: str, api_key: str) -> str:
	out = scrub_secrets(text, literals=(api_key, _json_escaped(api_key)))
	out = _VALUE_LINE.sub(lambda m: m.group(1) + SECRET_PLACEHOLDER, out)
	return _ESCAPED_VALUE_LINE.sub(lambda m: m.group(1) + SECRET_PLACEHOLDER, out)


def _has_residual_secret(text: str, api_key: str) -> bool:
	if _KEY_SHAPE.search(text):
		return True
	return len(api_key) >= _MIN_KEY_LEN and (api_key in text or _json_escaped(api_key) in text)


def _queued_records(raw) -> list | None:
	"""The records of one queue entry (Frappe queues one record or a list of
	them), or None when the entry is not JSON."""
	try:
		records = json.loads(raw)
	except Exception:
		return None
	return records if isinstance(records, list) else [records]


def _flush_deferred_error_logs() -> int:
	"""Insert the Error Log rows waiting in Frappe's deferred-insert queue in
	Redis, so the scrub sees them instead of them landing unmasked later.
	Frappe queues its own error snapshots there (a server error, and every
	error in developer mode), and an AI failure row is queued there again when
	the transaction that inserted it rolls back. A site whose scheduler is
	paused never flushes that queue. Only the Error Log queue: other
	doctypes' queues stay the scheduler's.

	Takes only the entries queued when it starts, at most ``_FLUSH_MAX_POPS``
	of them, so a busy producer cannot keep it running, and commits after
	every ``_FLUSH_COMMIT_EVERY`` inserted rows. An entry that is not JSON, or
	a record that cannot be inserted, is counted and skipped; a failure to
	read the queue stops the flush. Never raises; returns how many entries or
	rows could not be inserted (a queue that cannot be read counts as one)."""
	failed = 0
	inserted = 0
	try:
		pops = min(int(frappe.cache.llen(_ERROR_LOG_QUEUE) or 0), _FLUSH_MAX_POPS)
		for _ in range(pops):
			raw = frappe.cache.lpop(_ERROR_LOG_QUEUE)
			if raw is None:
				break
			records = _queued_records(raw)
			if records is None:
				failed += 1
				continue
			for record in records:
				if not _insert_error_log(record):
					failed += 1
					continue
				inserted += 1
				if inserted % _FLUSH_COMMIT_EVERY == 0:
					safe_commit()
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
	"""``(changes, residual)`` for one row, or None when masking it failed. A
	masked value longer than its column (``_FIELD_LIMITS``) is cut to fit."""
	result = None
	try:
		changes = {}
		residual = False
		for field in text_fields:
			old = row.get(field) or ""
			new = _mask(old, api_key)
			if new != old:
				new = new[:_FIELD_LIMITS.get(field, len(new))]
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


def _window_end(doctype: str, last: str) -> str | None:
	"""The ``_WINDOW``-th name after ``last``, read from the primary key alone
	(no row is read), or None when fewer names are left."""
	rows = frappe.get_all(
		doctype,
		filters=[["name", ">", last]],
		fields=["name"],
		order_by="name asc",
		limit_start=_WINDOW - 1,
		limit_page_length=1,
	)
	return rows[0]["name"] if rows else None


def _chunks(doctype: str, filters: list[list], or_filters: list[list] | None, fields: list[str], batch_size: int):
	"""Yield successive lists of at most ``batch_size`` matching rows, ordered
	by ``name`` (keyset pagination, so rows edited or deleted in an earlier
	chunk never shift a later one). Each statement reads one window of at
	most ``_WINDOW`` names, ``name > last AND name <= bound``; the last window
	has no upper bound. Matches from several windows are gathered into one
	chunk, so sparse matches do not mean a commit per window."""
	last = ""
	pending: list[dict] = []
	while True:
		end = _window_end(doctype, last)
		after = last
		while True:
			window = [["name", ">", after]]
			if end is not None:
				window.append(["name", "<=", end])
			rows = frappe.get_all(
				doctype,
				filters=[*filters, *window],
				or_filters=or_filters,
				fields=fields,
				order_by="name asc",
				limit_page_length=batch_size,
			)
			pending += rows
			while len(pending) >= batch_size:
				yield pending[:batch_size]
				pending = pending[batch_size:]
			if len(rows) < batch_size:
				break
			after = rows[-1]["name"]
		if end is None:
			break
		last = end
	if pending:
		yield pending


def _error_log_fields() -> tuple[str, ...]:
	"""The Error Log text fields to read: ``metadata`` only where the column
	exists (Frappe v15's Error Log has none)."""
	if frappe.db.has_column("Error Log", "metadata"):
		return ("error", "method", "metadata")
	return ("error", "method")


def _scans(api_key: str, error_fields: tuple[str, ...]) -> list[_Scan]:
	"""The passes, in order: rows with an ai_fix.py frame and a secret
	marker, then (for a key of at least 16 characters) rows matching the
	stored key's fragment."""
	deleted = [["deleted_doctype", "=", "Error Log"]]
	scans = [
		_Scan("Error Log", [["error", "like", _AI_FRAME]], _or_markers("error"), error_fields, "changed"),
		_Scan(
			"Deleted Document", [*deleted, ["data", "like", _AI_FRAME]], _or_markers("data"), ("data",),
			"deleted_docs_changed",
		),
	]
	if len(api_key) >= _MIN_SEARCH_KEY_LEN:
		# The key stored today, anywhere: a 500 snapshot's title is the
		# exception message and a request's metadata holds its form data.
		fragment, clean = _key_fragment(api_key)
		if clean:
			likes = [f"%{fragment}%"]
		else:
			likes = sorted({f"%{_like_literal(fragment)}%", f"%{_like_literal(_json_escaped(fragment))}%"})
		scans += [
			_Scan(
				"Error Log", [], [[f, "like", p] for f in error_fields for p in likes], error_fields, "changed",
				confirm_key=True,
			),
			_Scan(
				"Deleted Document", deleted, [["data", "like", p] for p in likes], ("data",),
				"deleted_docs_changed", confirm_key=True,
			),
		]
	return scans


def _dry_run_flag(value) -> bool:
	"""``dry_run`` as a bool. ``None`` means the default, a dry run. Accepted:
	``True`` / ``False``, ``1`` / ``0``, and the strings "true", "false",
	"1", "0", "yes", "no" (surrounding spaces ignored, any case). Anything
	else raises ``ValueError``, so a typo such as "off" neither runs for real
	nor silently does nothing."""
	if value is None:
		return True
	if isinstance(value, bool):
		return value
	if isinstance(value, int) and value in (0, 1):
		return bool(value)
	if isinstance(value, str) and value.strip().lower() in _DRY_RUN_WORDS:
		return _DRY_RUN_WORDS[value.strip().lower()]
	raise ValueError(
		'dry_run must be True or False (also accepted: 1, 0, or the strings "true", "false", "1", "0", '
		'"yes", "no"; leave it out for a dry run)'
	)


class ScanSize(NamedTuple):
	"""What ``measure_scan_size`` found. ``reason`` is None when every part
	was read; otherwise it says why ``rows`` is ``SCAN_SIZE_UNKNOWN``: the
	failing part's exception TYPE name (never its message, which could quote
	a row), "no value" or "negative value"."""

	rows: int
	reason: str | None


def _deleted_document_count() -> int | None:
	"""Deleted Document rows, counted exactly up to ``MIGRATE_SCAN_LIMIT + 1``
	(then the migrate skips the scrub anyway). The subquery stops reading at
	its LIMIT, so it costs at most that many index entries, and it is plain
	SQL that runs the same on MariaDB and Postgres. An estimate is not used:
	Frappe v15's is not scoped to the site's database, Postgres answers -1
	for a table never analysed, and InnoDB's can be a fifth low."""
	rows = frappe.db.sql(_DELETED_DOCUMENT_COUNT, (MIGRATE_SCAN_LIMIT + 1,))
	return rows[0][0] if rows else None


def measure_scan_size() -> ScanSize:
	"""How many rows ``scrub_error_log_secrets`` may read, so ``bench
	migrate`` can decide whether to run it inline: every Error Log row,
	every Deleted Document row (not just copies of Error Log rows:
	``deleted_doctype`` is not indexed, so the passes read the whole table;
	see ``_deleted_document_count``), and the entries waiting in Error Log's
	deferred-insert queue. Cheap (no LIKE scan). Never raises: a part that
	raises, returns no value or returns a negative one makes the size
	``SCAN_SIZE_UNKNOWN``, which is above ``MIGRATE_SCAN_LIMIT``, so the
	migrate skips the scrub and prints the command instead of scanning a
	table of unknown size. It stops at the first such part (on Postgres a
	failed statement makes every later one fail too) and names it in
	``reason``."""
	parts = (
		lambda: frappe.db.count("Error Log"),
		_deleted_document_count,
		lambda: frappe.cache.llen(_ERROR_LOG_QUEUE),
	)
	total = 0
	for part in parts:
		reason = None
		try:
			value = part()
			if value is None:
				reason = "no value"
			elif int(value) < 0:
				reason = "negative value"
			else:
				total += int(value)
		except Exception as e:
			reason = type(e).__name__
		if reason is not None:
			return ScanSize(SCAN_SIZE_UNKNOWN, reason)
	return ScanSize(total, None)


def scrub_scan_size() -> int:
	"""The row count of ``measure_scan_size`` (``SCAN_SIZE_UNKNOWN`` when a
	part cannot be read). Never raises."""
	return measure_scan_size().rows


def scrub_error_log_secrets(dry_run: bool = True, batch_size: int = _BATCH) -> dict:
	"""Mask API keys left in Error Log rows (and Deleted Document copies of
	Error Log rows) by AI failures before the fix.

	Reads rows whose text contains an ``ai_fix.py`` frame AND a secret
	marker, plus rows whose text, title or metadata contain the key stored
	today (found by a fragment of it, then confirmed in Python; see the
	module docstring). A row is written only when the masking changes it,
	with ``update_modified=False``. Idempotent: a second run changes nothing.
	Prompt text (source code, SQL) in those rows is left as is; use
	``purge_ai_error_logs`` to remove the rows entirely. A real run first
	inserts the Error Log rows still waiting in the deferred-insert queue
	(see ``_flush_deferred_error_logs``); a dry run does not, so it does not
	count them. One row that cannot be masked or written is counted in
	``failed`` and skipped; it never stops the others. ``dry_run`` accepts
	only the values ``_dry_run_flag`` names (``None`` is a dry run) and
	raises ``ValueError`` for anything else.

	Returns ``{"candidates", "changed", "deleted_docs_changed", "residual",
	"failed"}``: Error Log rows read, Error Log rows and Deleted Document rows
	masked, rows that still hold a key-shaped value after masking (checked
	with a detector independent of the masking, only in the rows read), and
	rows (or queued rows) that could not be processed. With ``dry_run=True``
	the counts say what WOULD change and nothing is written.
	"""
	from optimus.ai_fix import _current_key_or_empty

	dry_run = _dry_run_flag(dry_run)
	batch_size = max(1, int(batch_size or _BATCH))
	out = {"candidates": 0, "changed": 0, "deleted_docs_changed": 0, "residual": 0, "failed": 0}
	if not dry_run:
		out["failed"] += _flush_deferred_error_logs()
	api_key = _current_key_or_empty()
	seen: dict[str, set[str]] = {"Error Log": set(), "Deleted Document": set()}

	for scan in _scans(api_key, _error_log_fields()):
		doctype = scan.doctype
		for rows in _chunks(doctype, scan.filters, scan.or_filters, ["name", *scan.fields], batch_size):
			for row in rows:
				if scan.confirm_key and not _holds_key(row, scan.fields, api_key):
					continue  # the fragment matched, the key is not there
				if row["name"] in seen[doctype]:
					continue
				seen[doctype].add(row["name"])
				if doctype == "Error Log":
					out["candidates"] += 1
				masked = _mask_row(row, scan.fields, api_key)
				if masked is None:
					out["failed"] += 1
					continue
				changes, residual = masked
				out["residual"] += int(residual)
				if not changes:
					continue
				if dry_run or _write_row(doctype, row["name"], changes):
					out[scan.counter] += 1
				else:
					out["failed"] += 1
			if not dry_run:
				safe_commit()
	return out


def purge_ai_error_logs(dry_run: bool = True) -> dict:
	"""Delete every Error Log row with a frame in Optimus's ``ai_fix.py``, and
	every Deleted Document copy of such a row. Opt-in: run it by hand when the
	stored prompt text (source code, SQL literals) must go too. Rows with
	another app's ``*ai_fix.py`` frame are left alone.

	Uses ``frappe.db.delete`` so no new Deleted Document copies are made.
	Returns ``{"error_logs": int, "deleted_documents": int}`` (with
	``dry_run=True`` the counts say what WOULD be deleted). ``dry_run``
	accepts only the values ``_dry_run_flag`` names (``None`` is a dry run)
	and raises ``ValueError`` for anything else.
	"""
	dry_run = _dry_run_flag(dry_run)
	out = {"error_logs": 0, "deleted_documents": 0}
	targets = (
		("Error Log", [["error", "like", _OPTIMUS_AI_FRAME]], "error_logs"),
		(
			"Deleted Document",
			[["deleted_doctype", "=", "Error Log"], ["data", "like", _OPTIMUS_AI_FRAME]],
			"deleted_documents",
		),
	)
	for doctype, filters, key in targets:
		for rows in _chunks(doctype, filters, None, ["name"], _BATCH):
			out[key] += len(rows)
			if not dry_run:
				frappe.db.delete(doctype, {"name": ("in", [r["name"] for r in rows])})
				safe_commit()
	return out
