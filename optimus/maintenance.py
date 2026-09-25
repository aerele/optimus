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

Error Log records still waiting in Frappe's deferred-insert queue in Redis
are masked there, and never inserted by this module: a real scrub (or
``mask_error_log_queue``, which the migrate patch runs when the scrub was
skipped or failed) claims the queue, masks each record and pushes it back,
and Frappe's own flush inserts it (see ``_remask_error_log_queue``).

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
	"""``scrub_error_log_secrets``, ``mask_error_log_queue`` or
	``purge_ai_error_logs`` was called inside an RQ job."""


_BATCH = 200
_SAVEPOINT = "optimus_scrub_row"
# Error Log's deferred-insert queue (frappe.deferred_insert.queue_prefix +
# doctype): the only queue the re-mask touches.
_ERROR_LOG_QUEUE = "insert_queue_for_Error Log"
# The key the re-mask moves that queue to before it masks it (_claim_queue).
# It is outside the "insert_queue_for_" prefix: Frappe's save_to_db finds its
# queues by that prefix and reads the doctype from the rest of the key, so it
# never inserts a claimed entry. hooks.py lists it in persistent_cache_keys,
# so the frappe.clear_cache() in bench migrate's setUp keeps the entries an
# interrupted run left there. frappe.cache.make_key() turns each name into the
# site's Redis key, as RedisWrapper does for its own list commands.
_QUEUE_CLAIM = "optimus_error_log_queue_claim"
# The fields of a queued Error Log record that are masked (the ones the scrub
# reads in a stored row).
_QUEUED_TEXT_FIELDS = ("error", "method", "metadata")
_AI_FRAME = "%ai_fix.py%"
# Optimus's own AI module, under its name and under the app's name before its
# rename (frappe_profiler): a queued record with such a frame has its value
# lines masked (_is_ai_record), and the purge deletes the rows with one.
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
# UPDATE of a stored row cuts a masked value to fit; a queued record's long
# title is moved in front of its error first (_long_title_into_error).
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
# as AI rows, or queued records from the AI code or holding the key
# (_is_ai_record): other snapshots keep their value lines. The second form is the same line inside Deleted Document data,
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


def _queued_records(raw) -> list | None:
	"""The records of one queue entry (Frappe queues one record or a list of
	them), or None when the entry is not JSON."""
	try:
		records = json.loads(raw)
	except Exception:
		return None
	return records if isinstance(records, list) else [records]


class _QueueMasked(NamedTuple):
	"""What ``_remask_error_log_queue`` did.

	- ``masked``: queue entries masked and pushed onto the queue. The entries
	  of a claim an earlier run left are pushed first and then claimed and
	  pushed again with the queue, so they count twice.
	- ``unmasked``: entries a stop left in the claim key, unmasked. Frappe
	  never inserts them there; the next run masks them.
	- ``failed``: entries or records dropped because they are not JSON, not
	  a record or cannot be masked, plus one for a failed Redis command.
	- ``queue_failed``: True when a Redis command failed. That failure is
	  counted once in ``failed``, so the scrub does not count its own failed
	  read of the queue again: one Redis outage is one failure."""

	masked: int
	unmasked: int
	failed: int
	queue_failed: bool


class _QueueRun:
	"""The running counts of one re-mask (see ``_QueueMasked``)."""

	__slots__ = ("failed", "masked", "queue_failed", "unmasked")

	def __init__(self):
		self.failed = 0
		self.masked = 0
		self.queue_failed = False
		self.unmasked = 0

	def queue_error(self) -> None:
		"""Count a failed Redis command, once per run."""
		if not self.queue_failed:
			self.queue_failed = True
			self.failed += 1

	def result(self) -> _QueueMasked:
		return _QueueMasked(self.masked, self.unmasked, self.failed, self.queue_failed)


def _redis(command: str, *args):
	"""One raw Redis command on ``frappe.cache``, for keys already made with
	``frappe.cache.make_key``. RedisWrapper's own ``llen``, ``lpop``,
	``rpush`` and ``exists`` make the key themselves (a made key would be
	prefixed twice) and it has no ``renamenx`` of its own, so the re-mask
	sends every command through the client's ``execute_command``."""
	return frappe.cache.execute_command(command, *args)


def _remask_error_log_queue(api_key: str) -> _QueueMasked:
	"""Mask, in Redis, the Error Log records waiting in Frappe's deferred-insert
	queue, and leave inserting them to Frappe. Frappe queues its own error
	snapshots there (a server error, and every error in developer mode). An
	AI failure row is queued there again only when a rollback removed it,
	which happens on a transactional engine (Postgres); on MariaDB the Error
	Log is a MyISAM table, so the row survives the rollback and nothing is
	queued. Only the Error Log queue: other doctypes' queues are never
	touched.

	1. A claim an earlier run left (it was interrupted, or Redis refused its
	   writes) is drained first (``_drain_claim``), so the claim key is free.
	2. The whole queue is claimed at once (``_claim_queue``): RENAMENX moves
	   it to the claim key, where Frappe's ``save_to_db`` never looks, so from
	   that instant no consumer can pop an entry this run has not masked.
	3. The claim is drained, whatever the rename answered: each entry is
	   popped from the claim, its records masked (``_masked_records``), and
	   the masked records pushed as one entry onto the queue. The run only
	   ever adds to the queue; it never pops from it.

	Frappe's ``save_to_db`` then inserts the masked records: bench migrate
	runs it right after the patches, and the scheduler every 15 minutes. Each
	run takes entries until it has inserted about 500 records (Frappe v15) or
	10,000 (v16), so a larger queue is inserted over several scheduler runs.
	Entries queued after the claim are not masked: those the old processes
	queue while bench migrate runs are inserted as they are, which is why the
	advisory's step 3 runs the scrub again after the restart (it then masks
	them in the table).

	It never touches the database (no insert, no read, no commit), so no
	queued record can kill a database connection or fail the migrate. An
	entry that is not JSON (``save_to_db`` would fail on it), or a record
	that is not a dict or cannot be masked, is counted and dropped, never
	pushed back raw. A failed push or read stops it: popping on after a
	refused push (Redis past ``maxmemory`` with ``noeviction`` refuses the
	push and still allows the pop) would lose every entry after it. The
	entry whose push failed is lost; the rest stay in the claim key, where
	Frappe never inserts them, and the next run masks them.

	Never raises; returns a ``_QueueMasked``."""
	run = _QueueRun()
	try:
		queue = frappe.cache.make_key(_ERROR_LOG_QUEUE)
		claim = frappe.cache.make_key(_QUEUE_CLAIM)
		left = _redis("EXISTS", claim)
	except Exception:
		run.queue_error()
		return run.result()
	if left and not _drain_claim(run, queue, claim, api_key):
		return run.result()
	_claim_queue(run, queue, claim)
	_drain_claim(run, queue, claim, api_key)
	return run.result()


def _claim_queue(run: _QueueRun, queue, claim) -> None:
	"""Move the whole queue to the claim key at once: RENAMENX, which never
	replaces a claim key that exists (it then answers False and changes
	nothing, and the caller drains that claim). RENAMENX of a queue that is
	not there fails ("no such key"): the queue is empty, or ``save_to_db``
	emptied it meanwhile, which is not a failure. A rename that fails while
	the queue is still there, or a queue that cannot be read, is one.
	Never raises."""
	claimed = False
	try:
		_redis("RENAMENX", queue, claim)
		claimed = True
	except Exception:
		pass
	if claimed:
		return
	try:
		if _redis("EXISTS", queue):
			run.queue_error()
	except Exception:
		run.queue_error()


def _drain_claim(run: _QueueRun, queue, claim, api_key: str) -> bool:
	"""Pop each entry of the claim key, mask its records
	(``_masked_records``) and push them as one entry at the end of the
	queue, up to the claim's length when it starts. True when it took them
	all (or the claim ran empty early: another run took the rest). False
	when a read or a push failed: it stops there, and the entries it did not
	take stay in the claim key (``run.unmasked``). Never raises."""
	try:
		size = int(_redis("LLEN", claim) or 0)
	except Exception:
		run.queue_error()
		return False
	for taken in range(size):
		try:
			raw = _redis("LPOP", claim)
		except Exception:
			run.queue_error()
			run.unmasked += size - taken
			return False
		if raw is None:
			return True
		records = _queued_records(raw)
		if records is None:
			run.failed += 1
			continue
		masked = _masked_records(run, records, api_key)
		if not masked:
			continue
		if not _push(queue, masked):
			# Stop: another pop could lose one more entry.
			run.queue_error()
			run.unmasked += size - taken - 1
			return False
		run.masked += 1
	return True


def _push(queue, records: list[dict]) -> bool:
	"""Push ``records`` as one entry at the end of ``queue``. False when
	Redis refuses it. Never raises."""
	try:
		_redis("RPUSH", queue, json.dumps(records))
		return True
	except Exception:
		return False


def _masked_records(run: _QueueRun, records: list, api_key: str) -> list[dict]:
	"""``records`` masked (``_masked_record``); one that cannot be masked is
	counted in ``run.failed`` and dropped."""
	out = []
	for record in records:
		masked = _masked_record(record, api_key)
		if masked is None:
			run.failed += 1
		else:
			out.append(masked)
	return out


def _masked_record(record, api_key: str) -> dict | None:
	"""A queued ``record`` as the re-mask pushes it back for Frappe's
	``save_to_db`` to insert (see ``_remask_error_log_queue``): its
	``_QUEUED_TEXT_FIELDS`` masked (``_mask_row``), with a title longer than
	its column moved in front of ``error`` (``_long_title_into_error``).
	The residual check is skipped: its answer is never used here, and it
	costs about a third of the masking's time. Its bare header value lines are masked only when ``_is_ai_record``: any
	other snapshot (an ERPNext error with a ``value = ...`` local, say) goes
	through ``scrub_secrets`` alone and keeps them. None when it is not a
	record or masking it failed: the re-mask counts it and drops it, and
	never pushes it back raw. Nothing here inserts it: ``save_to_db`` does,
	right after the patches in bench migrate and every 15 minutes from the
	scheduler, about 500 records a run on Frappe v15 and 10,000 on v16."""
	if not isinstance(record, dict):
		return None
	try:
		value_lines = _is_ai_record(record, api_key)
	except Exception:
		return None
	masked = _mask_row(
		record, _QUEUED_TEXT_FIELDS, api_key, value_lines=value_lines, cut=False, check_residual=False,
	)
	if masked is None:
		return None
	return _long_title_into_error({**record, **masked[0]})


def _long_title_into_error(record: dict) -> dict:
	"""``record`` as Frappe v16's ``ErrorLog.validate`` leaves it: a title
	(``method``) longer than its column goes, in full, in front of
	``error``, then is cut to fit. Done in Redis, before Frappe's
	``save_to_db`` inserts the record, so the row is the same on Frappe v15,
	whose Error Log has no such ``validate``: its insert would fail
	(``CharacterLengthExceededError``), and ``save_to_db`` logs such a
	failure and drops the record. On v16 ``validate`` then has nothing left
	to do."""
	method = record.get("method")
	limit = _FIELD_LIMITS["method"]
	if not isinstance(method, str) or len(method) <= limit:
		return record
	error = record.get("error")
	return {**record, "error": f"{method}\n{'' if error is None else error}", "method": method[:limit]}


def _is_ai_record(record: dict, api_key: str) -> bool:
	"""True when a queued record comes from Optimus's AI code (a frame in
	``optimus/ai_fix.py`` or ``frappe_profiler/ai_fix.py`` in any of its text
	fields: its ``error``, its title, whose text a 500 snapshot takes from the
	exception, or its request ``metadata``) or holds the stored key (of at
	least ``_MIN_KEY_LEN`` characters, raw or JSON-escaped, in any of its text
	fields)."""
	for field in _QUEUED_TEXT_FIELDS:
		text = record.get(field)
		if isinstance(text, str) and any(path in text for path in _OPTIMUS_AI_FRAME_PATHS):
			return True
	return len(api_key) >= _MIN_KEY_LEN and _holds_key(record, _QUEUED_TEXT_FIELDS, api_key)


def _error_log_queue_length() -> int | None:
	"""How many entries wait in Error Log's deferred-insert queue, or None
	when the queue cannot be read."""
	try:
		return max(0, int(frappe.cache.llen(_ERROR_LOG_QUEUE) or 0))
	except Exception:
		return None


def _claim_length() -> int | None:
	"""How many entries a stopped or interrupted run left in the claim key
	(see ``_remask_error_log_queue``), or None when it cannot be read."""
	try:
		return max(0, int(_redis("LLEN", frappe.cache.make_key(_QUEUE_CLAIM)) or 0))
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
	Error Log rows) by AI failures before the fix, and in the Error Log
	records waiting in Frappe's deferred-insert queue.

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

	It reads the stored key first. A real run then masks the queue in Redis,
	before it reads any row (``_remask_error_log_queue``): it never inserts
	a queued record, Frappe's own ``save_to_db`` does, right after the
	patches in bench migrate and every 15 minutes from the scheduler. A dry
	run only reads the queue's length and the claim key's.

	Returns a dict:

	- ``candidates``: Error Log rows read;
	- ``changed`` / ``deleted_docs_changed``: Error Log rows and Deleted
	  Document rows masked;
	- ``residual``: rows that still hold a key-shaped value after masking
	  (checked with a detector independent of the masking, only in the rows
	  read);
	- ``failed``: rows, queue entries or queued records that could not be
	  processed; a Redis failure counts one, once;
	- ``queued``: the entries in the Error Log's deferred-insert queue when
	  the scrub ends: after a real run, the ones it masked and the ones
	  queued meanwhile, which include Frappe's own new error snapshots;
	- ``queue_masked``: the queue entries it masked and pushed back (0 in a
	  dry run);
	- ``queue_unmasked``: the entries left unmasked in the claim key, where
	  Frappe never inserts them: after a stop (Redis refused a push or a
	  read), or, in a dry run, what an earlier run left there. The next real
	  run masks them.

	With ``dry_run=True`` the counts say what WOULD change and nothing is
	written.
	"""
	from optimus.ai_fix import _current_key_or_empty

	_refuse_inside_a_background_job()
	dry_run = _dry_run_flag(dry_run)
	batch_size = max(1, int(batch_size or _BATCH))
	out = {
		"candidates": 0, "changed": 0, "deleted_docs_changed": 0, "residual": 0, "failed": 0, "queued": 0,
		"queue_masked": 0, "queue_unmasked": 0,
	}
	# The key first: the re-mask masks each queued record with it.
	api_key = _current_key_or_empty()
	queue_failed = False
	if not dry_run:
		# Before any row is read, so a failing statement cannot stop it.
		queue = _remask_error_log_queue(api_key)
		out["queue_masked"], out["queue_unmasked"] = queue.masked, queue.unmasked
		out["failed"] += queue.failed
		queue_failed = queue.queue_failed
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
	_read_queue(out, queue_failed, claim=dry_run)
	return out


def mask_error_log_queue() -> dict:
	"""The queue half of ``scrub_error_log_secrets`` alone: read the stored
	key, then mask, in Redis, the Error Log records waiting in the
	deferred-insert queue (``_remask_error_log_queue``), with no other
	database access. The migrate patch runs it when the scrub was skipped
	(the tables are too large to scan during the migrate) or failed.

	Returns ``{"queue_masked", "queue_unmasked", "queued", "failed"}``, as
	``scrub_error_log_secrets`` counts them. Refuses to run inside an RQ job
	(``InsideBackgroundJobError``): its frames hold the unmasked entries."""
	from optimus.ai_fix import _current_key_or_empty

	_refuse_inside_a_background_job()
	api_key = _current_key_or_empty()
	queue = _remask_error_log_queue(api_key)
	out = {"queue_masked": queue.masked, "queue_unmasked": queue.unmasked, "queued": 0, "failed": queue.failed}
	_read_queue(out, queue.queue_failed, claim=False)
	return out


def _read_queue(out: dict, queue_failed: bool, claim: bool) -> None:
	"""Set ``out["queued"]`` to the queue's length now and, with ``claim``,
	``out["queue_unmasked"]`` to the claim key's. A read that fails counts
	one in ``out["failed"]``, unless ``queue_failed`` says a Redis failure
	was already counted: one Redis outage is one failure."""
	queued = _error_log_queue_length()
	held = _claim_length() if claim else out["queue_unmasked"]
	if queued is not None:
		out["queued"] = queued
	if held is not None:
		out["queue_unmasked"] = held
	if (queued is None or held is None) and not queue_failed:
		out["failed"] += 1


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
