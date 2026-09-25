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

Run them with ``bench execute`` or ``bench console``, never enqueue them:
their frames hold the unmasked rows, and a background job that fails is
logged with every frame's local variables, so they refuse to run inside an
RQ job (``InsideBackgroundJobError``). Where they hold the key itself, the
local is named ``api_key`` or ``secret``, names Frappe's traceback
sanitizer and Sentry redact.

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


class InsideBackgroundJobError(RuntimeError):
	"""``scrub_error_log_secrets`` or ``purge_ai_error_logs`` was called
	inside an RQ job."""


_BATCH = 200
_SAVEPOINT = "optimus_scrub_row"
# frappe.deferred_insert.queue_prefix + doctype: only this queue is flushed.
_ERROR_LOG_QUEUE = "insert_queue_for_Error Log"
# The flush takes at most this many queue entries (each a record or a list of
# them) and commits after every _FLUSH_COMMIT_EVERY inserted rows. It stops
# after _FLUSH_MAX_FAILURES inserts in a row fail (a lost connection),
# instead of popping, and losing, every entry left, and pushes back, masked,
# the records it took and did not insert.
_FLUSH_MAX_POPS = 10_000
_FLUSH_COMMIT_EVERY = 100
_FLUSH_MAX_FAILURES = 3
# The fields of a queued Error Log record that are masked before it is
# inserted (the ones the scrub reads in a stored row).
_QUEUED_TEXT_FIELDS = ("error", "method", "metadata")
_AI_FRAME = "%ai_fix.py%"
# The purge's frame patterns: Optimus's own module only, under its name and
# under the app's name before its rename (frappe_profiler), so another app's
# openai_fix.py rows are never deleted (the package prefix excludes them).
# They hold no LIKE escape, so they work on Frappe v15, whose db_query
# doubles backslashes, as well as on v16; each "_" is a one-character
# wildcard.
_OPTIMUS_AI_FRAMES = ("%optimus/ai_fix.py%", "%frappe_profiler/ai_fix.py%")
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
# bench migrate runs the scrub only when measure_scan_size() finds at most
# this many rows; a scan of a larger table would stall the migrate, so the
# patch prints the command to run instead.
MIGRATE_SCAN_LIMIT = 200_000
# The row count measure_scan_size() reports when a part of the size cannot
# be read: an unmeasured table must not look small, so the migrate skips the
# scrub.
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
# longer one keeps its tail past _ESCAPED_VALUE_MAX_UNITS units (a character
# or an escape pair each), which the residual check still reads.
_ESCAPED_VALUE_MAX_UNITS = 2048
_VALUE_LINE = re.compile(r"""(?m)^([ \t]+(?:value|values|one_value) = )(?:b?['"]|[\[(]).*$""")
_ESCAPED_VALUE_LINE = re.compile(
	r"""(\\n[ \t]+(?:value|values|one_value) = )(?:b?'|b?\\"|[\[(])(?:(?!\\n)(?:\\.|[^"\\]))"""
	f"{{0,{_ESCAPED_VALUE_MAX_UNITS}}}"
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


def _json_escaped(secret: str) -> str:
	"""``secret`` as it appears inside JSON text (Deleted Document data is
	``as_json`` with ``ensure_ascii``: a smart quote becomes ``\\u2019``).
	The parameter is named ``secret`` because it is often the full key: a
	name Frappe's traceback sanitizer and Sentry redact."""
	return json.dumps(secret)[1:-1] if secret else ""


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
	text fields (the fragment the SQL matched is not enough). The escaped
	key is held as ``secret``, a name the sanitizers redact."""
	secret = _json_escaped(api_key)
	for field in fields:
		text = row.get(field)
		if isinstance(text, str) and (api_key in text or secret in text):
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


class _Flushed(NamedTuple):
	"""What ``_flush_deferred_error_logs`` did: ``failed``, the entries or
	rows it could not insert (see there), and ``queue_failed``, True when a
	read or a write of the queue failed. That failure is counted once in
	``failed``, so the scrub does not count its own failed read of the queue
	again: one Redis outage is one failure."""

	failed: int
	queue_failed: bool


class _FlushRun:
	"""The running counts of one flush. ``streak`` holds the MASKED records
	of the current run of failed inserts (never a raw record): the ones a
	stop pushes back."""

	__slots__ = ("failed", "inserted", "popped", "queue_failed", "streak")

	def __init__(self):
		self.failed = 0
		self.inserted = 0
		self.popped = 0
		self.queue_failed = False
		self.streak: list[dict] = []

	def queue_error(self) -> None:
		"""Count a failed read or write of the queue, once per flush."""
		if not self.queue_failed:
			self.queue_failed = True
			self.failed += 1


def _flush_deferred_error_logs(api_key: str) -> _Flushed:
	"""Insert the Error Log rows waiting in Frappe's deferred-insert queue in
	Redis, masked, so they neither land unmasked later nor escape the scrub.
	Frappe queues its own error snapshots there (a server error, and every
	error in developer mode). An AI failure row is queued there again only
	when a rollback removed it, which happens on a transactional engine
	(Postgres); on MariaDB the Error Log is a MyISAM table, so the row
	survives the rollback and nothing is queued. A site whose scheduler is
	paused never flushes that queue. Only the Error Log queue: other
	doctypes' queues stay the scheduler's.

	Each record's ``error``, ``method`` and ``metadata`` are masked with
	``_mask`` and ``api_key`` (the key the caller read before the first pop;
	``method`` cut to its column) BEFORE the record is inserted, so a queued
	pre-fix snapshot never reaches the database (the INSERT statement, the
	binlog, the query logs) with the key in it. A record that cannot be
	masked is counted and dropped, never inserted unmasked.

	Takes only the entries queued when it starts, at most ``_FLUSH_MAX_POPS``
	of them, so a busy producer cannot keep it running, and commits after
	every ``_FLUSH_COMMIT_EVERY`` inserted rows. An entry that is not JSON, or
	a record that cannot be inserted, is counted and skipped. A row the table
	kept although its insert failed counts as inserted (see
	``_insert_error_log``). After ``_FLUSH_MAX_FAILURES`` inserts in a row
	fail, or when a periodic commit fails, it stops and pushes back, as one
	entry, every record it took and did not insert: the run of failed
	inserts, across entries, and the untried rest of the current entry, all
	masked (``_push_back``). A record inserted before the stop is never
	pushed back, so none is inserted twice. A failure to read the queue
	stops the flush. Never raises; returns a
	``_Flushed``: how many entries or rows could not be inserted (a failed
	read or write of the queue counts as one, however often it fails), and
	whether the queue failed."""
	run = _FlushRun()
	try:
		snapshot = max(0, int(frappe.cache.llen(_ERROR_LOG_QUEUE) or 0))
	except Exception:
		run.queue_error()
		return _Flushed(run.failed, run.queue_failed)
	try:
		_insert_queued(run, api_key, min(snapshot, _FLUSH_MAX_POPS))
	except Exception:
		run.failed += 1
	finally:
		# Commit what was inserted even when the loop stopped early: the rows
		# are already gone from Redis, and a later rollback would drop them.
		_committed(run)
	return _Flushed(run.failed, run.queue_failed)


def _committed(run: _FlushRun) -> bool:
	"""``safe_commit()``; a failure is counted in ``run.failed``. Never
	raises."""
	try:
		safe_commit()
		return True
	except Exception:
		run.failed += 1
		return False


def _pop(run: _FlushRun):
	"""The next entry of the queue, or None when it is empty or cannot be
	read (``run.queue_error``)."""
	try:
		return frappe.cache.lpop(_ERROR_LOG_QUEUE)
	except Exception:
		run.queue_error()
		return None


def _push(run: _FlushRun, entry) -> None:
	"""Put ``entry`` back at the end of the queue (``run.queue_error`` when
	it cannot be written)."""
	try:
		frappe.cache.rpush(_ERROR_LOG_QUEUE, entry)
	except Exception:
		run.queue_error()


def _insert_queued(run: _FlushRun, api_key: str, pops: int) -> None:
	"""The flush's insert loop over at most ``pops`` entries (see
	``_flush_deferred_error_logs``)."""
	for _ in range(pops):
		raw = _pop(run)
		if raw is None:
			return
		run.popped += 1
		records = _queued_records(raw)
		if records is None:
			run.failed += 1
			continue
		for i, record in enumerate(records):
			if not _insert_one(run, record, api_key):
				_push_back(run, records[i + 1:], api_key)
				return


def _insert_one(run: _FlushRun, record, api_key: str) -> bool:
	"""Insert one queued record, masked (a record that cannot be masked is
	counted and dropped). False when the flush must stop: after
	``_FLUSH_MAX_FAILURES`` failed inserts in a row, or when a periodic
	commit failed."""
	masked = _masked_record(record, api_key)
	if masked is None:
		run.failed += 1
		return True
	if _insert_error_log(masked):
		run.streak.clear()
		run.inserted += 1
		return run.inserted % _FLUSH_COMMIT_EVERY != 0 or _committed(run)
	run.failed += 1
	run.streak.append(masked)
	return len(run.streak) < _FLUSH_MAX_FAILURES


def _push_back(run: _FlushRun, rest: list, api_key: str) -> None:
	"""After a stop, push back, as one entry at the end of the queue, every
	record the flush took and did not insert: the run of failed inserts
	(``run.streak``, across entries) and the untried ``rest`` of the current
	entry. All of them MASKED (a record of ``rest`` that cannot be masked is
	counted and dropped), never as popped: bench migrate's own flush inserts
	the queue as it is right after the patches."""
	left = list(run.streak)
	run.streak.clear()
	for record in rest:
		masked = _masked_record(record, api_key)
		if masked is None:
			run.failed += 1
		else:
			left.append(masked)
	if left:
		_push(run, json.dumps(left))


def _masked_record(record, api_key: str) -> dict | None:
	"""A queued ``record`` with its ``_QUEUED_TEXT_FIELDS`` masked (see
	``_mask_row``), or None when it is not a record or masking it failed."""
	if not isinstance(record, dict):
		return None
	masked = _mask_row(record, _QUEUED_TEXT_FIELDS, api_key)
	if masked is None:
		return None
	return {**record, **masked[0]}


def _error_log_queue_length() -> int | None:
	"""How many entries wait in Error Log's deferred-insert queue, or None
	when the queue cannot be read."""
	try:
		return max(0, int(frappe.cache.llen(_ERROR_LOG_QUEUE) or 0))
	except Exception:
		return None


def _under_savepoint(write) -> bool:
	"""Run ``write()`` under the savepoint ``_SAVEPOINT``. True when it and
	the savepoint's release succeeded. Otherwise the write is rolled back to
	the savepoint, so a failure undoes only itself, also on Postgres, where a
	failed statement aborts the transaction and every later statement with
	it; then it returns False. The savepoint is released after the write and
	also after the rollback, because every write reuses one savepoint name
	and re-issuing a savepoint of the same name nests a new subtransaction on
	Postgres instead of replacing it. Frappe's own ``savepoint()`` helper
	releases only after a success (it takes a fresh random name each time).
	Never raises."""
	ok = False
	try:
		frappe.db.savepoint(_SAVEPOINT)
		write()
		frappe.db.release_savepoint(_SAVEPOINT)
		ok = True
	except Exception:
		pass
	if not ok:
		try:
			frappe.db.rollback(save_point=_SAVEPOINT)
			frappe.db.release_savepoint(_SAVEPOINT)
		except Exception:
			pass
	return ok


def _insert_error_log(record: dict) -> bool:
	"""Insert one queued record under a savepoint (``_under_savepoint``),
	so a failed insert rolls back only itself. True when the row is in the
	table: inserted, or stored although the insert then failed. On MariaDB
	Error Log is a MyISAM table, so the rollback to the savepoint does not
	undo an INSERT: when something after it fails (a hook that runs after
	the INSERT), the row stays. Such a row counts as inserted, so it is
	never pushed back to the queue and inserted a second time. Never
	raises."""
	doc = None

	def _insert():
		nonlocal doc
		doc = frappe.get_doc({**record, "doctype": "Error Log"})
		doc.insert(ignore_permissions=True)
	return _under_savepoint(_insert) or _row_stored(doc)


def _row_stored(doc) -> bool:
	"""True when ``doc`` got a name and an Error Log row of that name exists,
	read after the rollback to the savepoint. A read that fails counts as
	not stored. Never raises."""
	name = getattr(doc, "name", None)
	if not name:
		return False
	try:
		return bool(frappe.db.exists("Error Log", name))
	except Exception:
		return False


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
	"""Write one masked row under a savepoint (``_under_savepoint``), so a
	failed write (a lock timeout, a row deleted meanwhile) rolls back only
	itself. True when it was written."""
	return _under_savepoint(lambda: frappe.db.set_value(doctype, name, changes, update_modified=False))


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
	chunk, so sparse matches do not mean a commit per window. Each statement
	asks only for the rows the chunk still lacks (``batch_size`` minus the
	rows gathered), so no more than ``batch_size`` rows are ever held, and a
	window is done when a statement returns fewer rows than it asked for."""
	last = ""
	pending: list[dict] = []
	while True:
		end = _window_end(doctype, last)
		after = last
		while True:
			window = [["name", ">", after]]
			if end is not None:
				window.append(["name", "<=", end])
			limit = batch_size - len(pending)
			rows = frappe.get_all(
				doctype,
				filters=[*filters, *window],
				or_filters=or_filters,
				fields=fields,
				order_by="name asc",
				limit_page_length=limit,
			)
			pending += rows
			if len(pending) >= batch_size:
				yield pending
				pending = []
			if len(rows) < limit:
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


def _refuse_inside_a_background_job() -> None:
	"""Raise ``InsideBackgroundJobError`` inside an RQ job. The scrub's frames
	hold unmasked rows (keys, prompts), and a job that fails is logged with
	Frappe's with-context traceback, which prints every frame's locals, so
	those rows would be written back to the Error Log. Where rq cannot be
	imported there is no RQ job."""
	try:
		from rq import get_current_job
	except Exception:
		return
	if get_current_job() is not None:
		raise InsideBackgroundJobError(
			"Run optimus.maintenance with bench execute or bench console, never in a background job: "
			"a failed job's log would store the unmasked rows it reads."
		)


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
	part cannot be read), a convenience for ``bench console`` and ``bench
	execute``. The migrate patch calls ``measure_scan_size``, which also
	says why a size is unknown. Never raises."""
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
	``purge_ai_error_logs`` to remove the rows entirely. It reads the stored
	key first. A real run then inserts the Error Log rows still waiting in
	the deferred-insert queue, each masked before its INSERT (see
	``_flush_deferred_error_logs``); a dry run does not, so it does not count
	them, and only reads the queue length. One row that cannot be masked or
	written is counted in ``failed`` and skipped; it never stops the others.
	``dry_run`` accepts only the values ``_dry_run_flag`` names (``None`` is
	a dry run) and raises ``ValueError`` for anything else. Refuses to run
	inside an RQ job (``InsideBackgroundJobError``).

	Returns ``{"candidates", "changed", "deleted_docs_changed", "residual",
	"failed", "queued"}``: Error Log rows read, Error Log rows and Deleted
	Document rows masked, rows that still hold a key-shaped value after
	masking (checked with a detector independent of the masking, only in the
	rows read), rows (or queued rows) that could not be processed, and the
	entries still waiting in the Error Log's deferred-insert queue when the
	scrub ends (a queue that cannot be read counts one in ``failed``
	instead, once, also when the flush could not read or write it either).
	After a real run ``queued`` should be 0: entries left there
	were not scrubbed, and the scheduler would insert them as they are. With
	``dry_run=True`` the counts say what WOULD change and nothing is written.
	"""
	from optimus.ai_fix import _current_key_or_empty

	_refuse_inside_a_background_job()
	dry_run = _dry_run_flag(dry_run)
	batch_size = max(1, int(batch_size or _BATCH))
	out = {"candidates": 0, "changed": 0, "deleted_docs_changed": 0, "residual": 0, "failed": 0, "queued": 0}
	# The key first: the flush masks each queued record with it.
	api_key = _current_key_or_empty()
	queue_failed = False
	if not dry_run:
		flushed = _flush_deferred_error_logs(api_key)
		out["failed"] += flushed.failed
		queue_failed = flushed.queue_failed
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
	queued = _error_log_queue_length()
	if queued is not None:
		out["queued"] = queued
	elif not queue_failed:
		# One Redis outage is one failure: not again when the flush counted it.
		out["failed"] += 1
	return out


def purge_ai_error_logs(dry_run: bool = True) -> dict:
	"""Delete every Error Log row with a frame in Optimus's ``ai_fix.py``
	(``optimus/ai_fix.py``, or ``frappe_profiler/ai_fix.py`` from a release
	before the app was renamed to optimus), and every Deleted Document copy of
	such a row. Opt-in: run it by hand when the stored prompt text (source
	code, SQL literals) must go too. Rows with another app's ``*ai_fix.py``
	frame are left alone.

	Uses ``frappe.db.delete`` so no new Deleted Document copies are made.
	Returns ``{"error_logs": int, "deleted_documents": int}`` (with
	``dry_run=True`` the counts say what WOULD be deleted). ``dry_run``
	accepts only the values ``_dry_run_flag`` names (``None`` is a dry run)
	and raises ``ValueError`` for anything else. Refuses to run inside an RQ
	job (``InsideBackgroundJobError``).
	"""
	_refuse_inside_a_background_job()
	dry_run = _dry_run_flag(dry_run)
	out = {"error_logs": 0, "deleted_documents": 0}
	targets = (
		("Error Log", [], [["error", "like", p] for p in _OPTIMUS_AI_FRAMES], "error_logs"),
		(
			"Deleted Document",
			[["deleted_doctype", "=", "Error Log"]],
			[["data", "like", p] for p in _OPTIMUS_AI_FRAMES],
			"deleted_documents",
		),
	)
	for doctype, filters, or_filters, key in targets:
		for rows in _chunks(doctype, filters, or_filters, ["name"], _BATCH):
			out[key] += len(rows)
			if not dry_run:
				frappe.db.delete(doctype, {"name": ("in", [r["name"] for r in rows])})
				safe_commit()
	return out
