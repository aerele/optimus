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

The scrub reads only the tables. Error Log records still waiting in
Frappe's deferred-insert queue in Redis are never read or changed here:
the Error Log ``before_insert`` hook (``optimus.error_log_mask``) masks
every Error Log row as Frappe inserts it, with ``_masked_record`` below,
the queued ones included when Frappe's ``save_to_db`` inserts them. A real
scrub first refreshes the hooks Frappe caches (``_refresh_hooks_cache``),
so that hook reaches every process once all of them run the new code.

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
# The fields of an Error Log record that _masked_record masks (the ones the
# scrub reads in a stored row).
_RECORD_TEXT_FIELDS = ("error", "method", "metadata")
_AI_FRAME = "%ai_fix.py%"
# Optimus's own AI module, under its name and under the app's name before its
# rename (frappe_profiler): a record with such a frame has its value lines
# masked (_is_ai_record), and the purge deletes the rows with one.
_OPTIMUS_AI_FRAME_PATHS = ("optimus/ai_fix.py", "frappe_profiler/ai_fix.py")
# The purge's frame patterns, so another app's openai_fix.py rows are never
# deleted (the package prefix excludes them). They hold no LIKE escape, so
# they work on Frappe v15, whose db_query doubles backslashes, as well as on
# v16; each "_" is a one-character wildcard.
_OPTIMUS_AI_FRAMES = tuple(f"%{path}%" for path in _OPTIMUS_AI_FRAME_PATHS)
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
# the column fails the whole row's write, error text included. The scrub's
# UPDATE of a stored row cuts a masked value to fit; _masked_record moves a
# record's long title in front of its error first (_long_title_into_error).
_FIELD_LIMITS = {"method": 140}
# bench migrate runs the scrub only when measure_scan_size() finds at most
# this many rows; a scan of a larger table would stall the migrate, so the
# patch prints the command to run instead.
MIGRATE_SCAN_LIMIT = 200_000
# The row count measure_scan_size() reports when a part of the size cannot
# be read: an unmeasured table must not look small, so the migrate skips the
# scrub.
SCAN_SIZE_UNKNOWN = sys.maxsize
# Deleted Document rows up to a bound (the parameter): exact and portable.
# The LIMIT bounds the cost: on MariaDB it reads at most that many index
# entries; Postgres scans the table and stops at that many rows.
_DELETED_DOCUMENT_COUNT = "SELECT COUNT(*) FROM (SELECT 1 FROM `tabDeleted Document` LIMIT %s) t"
# The values dry_run accepts as text (stripped, any case), besides True /
# False and 1 / 0.
_DRY_RUN_WORDS = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}

# Frappe's with-context traceback prints the locals of the urllib3 /
# http.client frames that send a header (``value``, ``values``,
# ``one_value``): the raw header value, so an ``x-api-key`` header shows as a
# bare key that no scrub_secrets shape matches. Only string-like values
# (quoted, bytes, list, tuple) are masked, and only in rows already selected
# as AI rows, or in records from the AI code or holding the key
# (_is_ai_record): other snapshots keep their value lines. The second form
# is the same line inside Deleted Document data, which is JSON (newlines
# escaped as \n); it consumes escape pairs whole so the JSON stays valid. Its repeat is bounded: an unbounded one keeps a
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


def _mask(text: str, api_key: str, value_lines: bool = True) -> str:
	"""``text`` through ``scrub_secrets`` with the key (raw and JSON-escaped)
	as literals, then, when ``value_lines``, with the bare header value
	lines masked (``_VALUE_LINE``, ``_ESCAPED_VALUE_LINE``)."""
	out = scrub_secrets(text, literals=(api_key, _json_escaped(api_key)))
	if not value_lines:
		return out
	out = _VALUE_LINE.sub(lambda m: m.group(1) + SECRET_PLACEHOLDER, out)
	return _ESCAPED_VALUE_LINE.sub(lambda m: m.group(1) + SECRET_PLACEHOLDER, out)


def _has_residual_secret(text: str, api_key: str) -> bool:
	if _KEY_SHAPE.search(text):
		return True
	return len(api_key) >= _MIN_KEY_LEN and (api_key in text or _json_escaped(api_key) in text)


def _masked_record(record, api_key: str) -> dict | None:
	"""An Error Log ``record`` as the Error Log hook
	(``optimus.error_log_mask``) stores it, before Frappe's ``validate``,
	length check and INSERT: its ``_RECORD_TEXT_FIELDS`` masked
	(``_mask_row``), with a title longer than its column moved in front of
	``error`` (``_long_title_into_error``) and the joined ``error`` masked
	again, so it is idempotent.

	Its bare header value lines are masked only when ``_is_ai_record``: any
	other snapshot (an ERPNext error with a ``value = ...`` local, say) goes
	through ``scrub_secrets`` alone and keeps them. The residual check is
	skipped: its answer is never used here, and it costs about a third of
	the masking's time. None when it is not a record or masking it failed,
	the joined pass included: the hook then withholds a record from the AI
	code or holding the key, and never stores the joined text unmasked. An
	RQ job timeout is raised, not swallowed (``_reraise_job_timeout``)."""
	if not isinstance(record, dict):
		return None
	try:
		value_lines = _is_ai_record(record, api_key)
	except Exception as e:
		_reraise_job_timeout(e)
		return None
	masked = _mask_row(
		record, _RECORD_TEXT_FIELDS, api_key, value_lines=value_lines, cut=False, check_residual=False,
	)
	if masked is None:
		return None
	merged = {**record, **masked[0]}
	moved = _long_title_into_error(merged)
	if moved is merged:
		return merged
	# The joined "<title>\n<error>" is masked again as a whole: a shape the
	# join completes (a title ending in "Bearer", an error starting with the
	# token) is masked now, so a second pass, or the scrub of the row once
	# Frappe has inserted it, changes nothing.
	joined = _mask_row(moved, ("error",), api_key, value_lines=value_lines, cut=False, check_residual=False)
	if joined is None:
		return None
	return {**moved, **joined[0]}


def _long_title_into_error(record: dict) -> dict:
	"""``record`` as Frappe v16's ``ErrorLog.validate`` leaves it: a title
	(``method``) longer than its column goes, in full, in front of
	``error``, then is cut to fit. The Error Log hook does it before
	``validate`` and the length check, so the row is the same on Frappe v15,
	whose Error Log has no such ``validate``: there the insert would fail
	(``CharacterLengthExceededError``), and ``save_to_db`` would log that
	failure and drop the record. On v16 ``validate`` then has nothing left
	to do."""
	method = record.get("method")
	limit = _FIELD_LIMITS["method"]
	if not isinstance(method, str) or len(method) <= limit:
		return record
	error = record.get("error")
	return {**record, "error": f"{method}\n{'' if error is None else error}", "method": method[:limit]}


def _is_ai_record(record: dict, api_key: str) -> bool:
	"""True when an Error Log record comes from Optimus's AI code (a frame in
	``optimus/ai_fix.py`` or ``frappe_profiler/ai_fix.py`` in any of its text
	fields: its ``error``, its title, whose text a 500 snapshot takes from the
	exception, or its request ``metadata``) or holds the stored key (of at
	least ``_MIN_KEY_LEN`` characters, raw or JSON-escaped, in any of its text
	fields)."""
	for field in _RECORD_TEXT_FIELDS:
		text = record.get(field)
		if isinstance(text, str) and any(path in text for path in _OPTIMUS_AI_FRAME_PATHS):
			return True
	return len(api_key) >= _MIN_KEY_LEN and _holds_key(record, _RECORD_TEXT_FIELDS, api_key)


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


def _mask_row(
	row: dict, text_fields: tuple[str, ...], api_key: str, value_lines: bool = True, cut: bool = True,
	check_residual: bool = True,
) -> tuple[dict, bool] | None:
	"""``(changes, residual)`` for one row (``_mask`` with ``value_lines``),
	or None when masking it failed. With ``cut``, a masked value longer than
	its column (``_FIELD_LIMITS``) is cut to fit. Without
	``check_residual``, ``residual`` is always False: the independent
	detector (``_has_residual_secret``) is not run."""
	result = None
	try:
		changes = {}
		residual = False
		for field in text_fields:
			old = row.get(field) or ""
			new = _mask(old, api_key, value_lines=value_lines)
			if new != old:
				if cut:
					new = new[:_FIELD_LIMITS.get(field, len(new))]
				changes[field] = new
			if check_residual:
				residual = residual or _has_residual_secret(new, api_key)
		result = (changes, residual)
	except Exception as e:
		_reraise_job_timeout(e)
		result = None
	return result


def _reraise_job_timeout(e: Exception) -> None:
	"""Raise ``e`` again when it is an RQ job timeout, so the masking's own
	fail-safes never swallow it: the Error Log hook (``error_log_mask``) runs
	this masking inside RQ jobs, and the job must stop. The hook re-raises it
	as a fresh instance, so these frames never travel with it. Any other
	exception returns."""
	try:
		from optimus.ai_fix import _job_timeout_types

		timeout_types = _job_timeout_types()
	except Exception:
		timeout_types = ()
	if isinstance(e, timeout_types):
		raise e


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


def _key_unreadable(api_key: str) -> bool | None:
	"""True when Optimus Settings holds an API key that
	``_current_key_or_empty`` could not read (``api_key`` is ""): the site's
	``encryption_key`` changed (a backup restored onto another site, say) or
	the encrypted copy in ``__Auth`` is gone. The scrub then cannot search
	for the key or mask it by value. Frappe keeps a Password field's value
	encrypted in ``__Auth`` and only asterisks, one per character, in the
	field itself, so this reads that field plainly (``get_single_value``),
	never the decrypted key. The value read is held as ``secret``, a name
	the sanitizers redact, in case a key was ever written into the field
	as plain text.

	False when there is no Optimus Settings at all (``DoesNotExistError``:
	a site Optimus was uninstalled from, where no key is stored), and None
	when the read failed for another reason, which the scrub counts in
	``failed``. Never raises."""
	if api_key:
		return False
	missing = getattr(frappe, "DoesNotExistError", ())
	try:
		secret = frappe.db.get_single_value("Optimus Settings", "ai_api_key")
	except Exception as e:
		return False if isinstance(e, missing) else None
	return isinstance(secret, str) and bool(secret.strip())


def _refresh_hooks_cache() -> bool:
	"""Make the hooks every process reads carry the Error Log hook
	(``optimus.error_log_mask``). Frappe caches all apps' hooks under
	"app_hooks" (in Redis, through ``frappe.client_cache`` on v16 and
	``frappe.cache`` on v15), and a process started before the upgrade that
	misses that key, after migrate's ``clear_cache``, loads its own old
	modules and caches the old hooks again; every process then reads them
	until the key is deleted. This deletes the key, reloads the hooks in
	this process, whose modules are the new ones, so the key holds the new
	hooks at once, and drops this process's own copy of the doc events
	(``frappe.local.doc_events_hooks``, kept for a whole request, or a whole
	bench migrate). It helps only once every process runs the new code: a
	process still running the old code can cache the old hooks again. True
	when it did all three; never raises."""
	try:
		cache = getattr(frappe, "client_cache", None) or frappe.cache
		cache.delete_value("app_hooks")
		frappe.get_hooks()
		frappe.local.doc_events_hooks = None
		return True
	except Exception:
		return False


def _refuse_inside_a_background_job() -> None:
	"""Raise ``InsideBackgroundJobError`` inside an RQ job. The scrub's frames
	hold unmasked rows (keys, prompts), and a job that fails is logged with
	Frappe's with-context traceback, which prints every frame's locals, so
	those rows would be written back to the Error Log. Where rq cannot be
	imported there is no RQ job. It fails open: if ``get_current_job()``
	itself raises, that counts as "no job", so a broken rq never stops a
	scrub run by hand or by the migrate patch (which never runs in a job)."""
	job = None
	try:
		from rq import get_current_job

		job = get_current_job()
	except Exception:
		job = None
	if job is not None:
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
	its LIMIT, which bounds the cost: on MariaDB it reads at most that many
	index entries, and Postgres, which scans the table, stops after that
	many rows. It is plain SQL that runs the same on both. An estimate is
	not used: Frappe v15's is not scoped to the site's database, Postgres
	answers -1 for a table never analysed, and InnoDB's can be a fifth
	low."""
	rows = frappe.db.sql(_DELETED_DOCUMENT_COUNT, (MIGRATE_SCAN_LIMIT + 1,))
	return rows[0][0] if rows else None


def measure_scan_size() -> ScanSize:
	"""How many rows ``scrub_error_log_secrets`` may read, so ``bench
	migrate`` can decide whether to run it inline: every Error Log row and
	every Deleted Document row (not just copies of Error Log rows:
	``deleted_doctype`` is not indexed, so the passes read the whole table;
	see ``_deleted_document_count``). The Error Log records waiting in the
	deferred-insert queue are not counted: the scrub never reads them (the
	Error Log hook masks them as Frappe inserts them). Cheap (no LIKE
	scan). Never raises: a part that raises, returns no value or
	returns a negative one makes the size ``SCAN_SIZE_UNKNOWN``, which is
	above ``MIGRATE_SCAN_LIMIT``, so the migrate skips the scrub and prints
	the command instead of scanning a table of unknown size. It stops at the
	first such part (on Postgres a failed statement makes every later one
	fail too) and names it in ``reason``."""
	parts = (
		lambda: frappe.db.count("Error Log"),
		_deleted_document_count,
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
	``purge_ai_error_logs`` to remove the rows entirely. One row that cannot
	be masked or written is counted in ``failed`` and skipped; it never stops
	the others. ``dry_run`` accepts only the values ``_dry_run_flag`` names
	(``None`` is a dry run) and raises ``ValueError`` for anything else.
	Refuses to run inside an RQ job (``InsideBackgroundJobError``).

	It reads the stored key first. It never reads or changes Frappe's
	deferred-insert queue: the Error Log hook (``optimus.error_log_mask``)
	masks every queued record as Frappe inserts it. A real run first
	refreshes the hooks Frappe caches (``_refresh_hooks_cache``), so that
	hook reaches every process once all of them run the new code (run it
	after the restart); a failed refresh is not counted and never stops the
	scrub. A dry run does not touch Redis at all.

	Returns a dict:

	- ``candidates``: Error Log rows read;
	- ``changed`` / ``deleted_docs_changed``: Error Log rows and Deleted
	  Document rows masked;
	- ``residual``: rows that still hold a key-shaped value after masking
	  (checked with a detector independent of the masking, only in the rows
	  read);
	- ``failed``: rows that could not be processed, plus one when the check
	  for an unreadable key could not read Optimus Settings;
	- ``key_unreadable``: True when a key is stored but cannot be read
	  (``_key_unreadable``); it also counts one in ``failed``. Restore the
	  site's ``encryption_key``, or enter the OLD key again in Optimus
	  Settings (the scrub searches for the key that leaked), then run the
	  scrub again. On a site Optimus was uninstalled from no key is stored,
	  so it is False.

	With ``dry_run=True`` the counts say what WOULD change and nothing is
	written.
	"""
	from optimus.ai_fix import _current_key_or_empty

	_refuse_inside_a_background_job()
	dry_run = _dry_run_flag(dry_run)
	batch_size = max(1, int(batch_size or _BATCH))
	out = {
		"candidates": 0, "changed": 0, "deleted_docs_changed": 0, "residual": 0, "failed": 0,
		"key_unreadable": False,
	}
	api_key = _current_key_or_empty()
	if not dry_run:
		_refresh_hooks_cache()
	unreadable = _key_unreadable(api_key)
	if unreadable is None:
		out["failed"] += 1
	elif unreadable:
		out["key_unreadable"] = True
		out["failed"] += 1
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
