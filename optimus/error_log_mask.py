# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Mask AI provider API keys in the Error Log rows Frappe inserts.

``hooks.py`` registers ``mask_error_log`` as the ``before_insert`` doc event
of Error Log. Frappe runs it inside ``Document.insert``, before ``validate``,
the column length check and the INSERT, for every Error Log row, whichever
way it is written:

- ``frappe.log_error`` inserts the row at once;
- a server error's snapshot, or any error logged while the site is
  read-only, waits in Frappe's deferred-insert queue in Redis, and Frappe's
  ``save_to_db`` inserts it later (bench migrate right after the patches,
  and the scheduler every 15 minutes), again through ``Document.insert``.

So nothing reads or changes that queue: a queued record is handled when it
is inserted, by the code running at that moment.

It changes only the rows that are Optimus's to change: a record from
Optimus's AI code (an ``optimus/ai_fix.py`` or ``frappe_profiler/ai_fix.py``
frame in ``error``, ``method`` or ``metadata``) or holding the stored key
(raw or JSON-escaped), as ``maintenance._is_ai_record`` decides. Every other
row, another app's included, is left byte-identical: the hook is site-wide
and permanent, so it applies no key shape and moves no title there, and
Frappe's own ``validate`` and length check treat it as they would without
Optimus. If that check itself fails, the record is treated as one of
Optimus's.

In such a record the masking is ``optimus.maintenance._masked_record``, the
scrub's own: the stored key (raw and JSON-escaped), the key shapes
``scrub_secrets`` knows and the bare header value lines are masked in
``error``, ``method`` and ``metadata`` (a field the doc does not hold, such
as ``metadata`` on Frappe v15, is left alone); a title longer than its
column is moved in front of ``error``, as v16's ``ErrorLog.validate`` does,
and the joined text masked again. A truthy value that is not text is read
as ``str(value)``, the text the row stores, both to recognise the key and to
mask it. Only the fields the masking changed are set.

The key is read once per insert, of every Error Log row, since a row
holding the key is one of Optimus's (``ai_fix._current_key_or_empty``,
never cached, held only as ``api_key``), with Frappe's messages muted, so an
undecryptable key does not add "Encryption key is invalid" to the reply of
every request that logs an error. It is a SELECT on ``__Auth``, a table
every Frappe site has, so it cannot fail in a healthy transaction.

It never raises, except an RQ job timeout (the job must stop), which leaves
as a fresh instance raised after the ``try``, so the frames it interrupted
(the record's text, the key read) never travel with it. It fails open: any
other failure (the key read, the masking) leaves the doc as it was, with
two exceptions. When the masking fails (on a record of Optimus's, the only
ones it masks), the text is withheld (``_withhold``) instead of inserted
raw. When Optimus's other modules cannot be imported, the stored key alone
is masked with Frappe alone (``_mask_stored_key_only``, below). Each
failure leaves a line in the ``optimus`` log (an exception type name at
most, never row text; a repeated outcome only every 1000th time, ``_note``). It never writes an Error Log itself: that insert
would run this hook again.

Frappe caches every app's hooks ("app_hooks" in Redis). A process started
before the upgrade that misses that key after migrate's ``clear_cache``
caches its old hooks again, without this event, and every process reads
them until the key is deleted. The migrate patch, when the scrub did not
run, and every real scrub delete and reload that cache
(``maintenance._refresh_hooks_cache``); the one run after the restart
(advisory step 3) makes the event reach every process for good.

The module imports only the standard library at import time; every Optimus
import is inside the guarded ``try``. Frappe resolves the handler outside
any ``try``, in every process that reads the hooks, and a process started
before the upgrade still holds the old Optimus modules (``optimus.redaction``
without ``SECRET_PLACEHOLDER``): there the handler resolves and its import
of ``optimus.maintenance`` fails inside the guard, instead of every Error
Log insert of that process failing until it is restarted. That process
still runs the old AI code, the code that leaks the key, so the hook then
falls back to Frappe alone: it reads the stored key with
``frappe.utils.password.get_decrypted_password`` (messages muted, held as
``api_key``, its JSON-escaped form as ``secret``) and replaces both with
``********`` in ``error``, ``method`` and ``metadata``, wherever they hold
them. A row without the key is left byte-identical, and a key shorter than
8 characters is not replaced, as in the masking above. Key shapes and
value lines are not masked there: that needs the modules that failed to
import.
"""

import json

# The Error Log fields that are masked (the ones the scrub reads in a row).
_TEXT_FIELDS = ("error", "method", "metadata")
# What a withheld record stores instead of its text.
WITHHELD = "Optimus withheld this error text: it could not be masked."
WITHHELD_TITLE = "Optimus withheld this error title: it could not be masked."
# Error Log.method is Data (varchar(140)).
_TITLE_LIMIT = 140
# optimus.redaction.SECRET_PLACEHOLDER and maintenance._MIN_KEY_LEN, copied:
# the fallback runs where those modules cannot be imported.
_PLACEHOLDER = "********"
_MIN_KEY_LEN = 8
# How many times each outcome has been noted in this process (_note), and
# how often a repeated one is logged: its first time, then every
# _NOTE_EVERY-th time. The outcomes are a few fixed texts and exception
# type names, so this stays small.
_NOTED: dict[str, int] = {}
_NOTE_EVERY = 1000


def mask_error_log(doc, method=None) -> None:
	"""The Error Log ``before_insert`` doc event: in a record from Optimus's
	AI code or holding the stored key, mask the key and the key shapes;
	leave every other ``doc`` as it was (see the module docstring). Never
	raises, except an RQ job timeout, re-raised as a fresh instance."""
	timeout_types = _job_timeout_types()
	interrupt = None
	outcome = None
	try:
		outcome = _mask_doc(doc, timeout_types)
	except Exception as e:
		if isinstance(e, timeout_types):
			interrupt = (type(e), e.args)
		else:
			outcome = f"stored as it was: {type(e).__name__}"
	if interrupt is not None:
		raise interrupt[0](*interrupt[1])
	if outcome is not None:
		_note(outcome, timeout_types)


def _mask_doc(doc, timeout_types) -> str | None:
	"""Mask ``doc``'s text fields in place when it is a record of Optimus's
	(``_is_ai``), or, when Optimus's other modules cannot be imported, its
	stored key alone (``_mask_stored_key_only``). None when it is done
	(masked, not Optimus's, or nothing to mask); otherwise the outcome for
	the ``optimus`` log. Raises what it cannot handle; ``mask_error_log``
	catches it."""
	import frappe

	stale = None
	try:
		from optimus import maintenance
		from optimus.ai_fix import _current_key_or_empty
	except Exception as e:
		if isinstance(e, timeout_types):
			raise
		stale = type(e).__name__

	record = {}
	for field in _TEXT_FIELDS:
		value = doc.get(field)
		if value is None:
			continue
		record[field] = str(value) if value and not isinstance(value, str) else value
	if not record:
		return None
	if stale is not None:
		return _mask_stored_key_only(frappe, doc, stale)
	api_key = _read_key(frappe, _current_key_or_empty)
	# Only a record from Optimus's AI code or holding the key is Optimus's to
	# change; every other row is left exactly as it was.
	if not _is_ai(maintenance, record, api_key, timeout_types):
		return None
	masked = _masked(maintenance, record, api_key, timeout_types)
	if masked is None:
		_withhold(doc, maintenance, record, api_key, timeout_types)
		return "withheld: its masking failed"
	for field in _TEXT_FIELDS:
		if field in masked and masked[field] != record.get(field):
			doc.set(field, masked[field])
	return None


def _mask_stored_key_only(frappe, doc, stale: str) -> str:
	"""The fallback of a process whose Optimus modules cannot be imported
	(``stale``, the import's exception type name): replace the stored key,
	raw and JSON-escaped, with ``_PLACEHOLDER`` in the text fields that hold
	it (a truthy value that is not text is read as ``str(value)``), using
	Frappe alone. A key shorter than ``_MIN_KEY_LEN`` characters is not
	replaced; a row without the key is left as it was. Returns the outcome
	for the ``optimus`` log. Raises what it cannot handle;
	``mask_error_log`` catches it."""
	api_key = _read_key(frappe, _stored_key)
	if len(api_key) >= _MIN_KEY_LEN:
		secret = json.dumps(api_key)[1:-1]
		for field in _TEXT_FIELDS:
			value = doc.get(field)
			if not value:
				continue
			text = value if isinstance(value, str) else str(value)
			if api_key in text or secret in text:
				doc.set(field, text.replace(secret, _PLACEHOLDER).replace(api_key, _PLACEHOLDER))
	return f"checked for the stored key alone (Optimus's modules could not be imported: {stale})"


def _stored_key() -> str:
	"""The stored ``Optimus Settings.ai_api_key``, stripped, or ``""``, read
	with Frappe alone (``ai_fix._current_key_or_empty`` without Optimus):
	the same ``__Auth`` SELECT, ``raise_exception=False``, so an
	undecryptable key answers ``""``. Held only as ``api_key``."""
	from frappe.utils.password import get_decrypted_password

	api_key = get_decrypted_password("Optimus Settings", "Optimus Settings", "ai_api_key", raise_exception=False)
	return api_key.strip() if isinstance(api_key, str) else ""


def _read_key(frappe, read) -> str:
	"""``read()`` (``_current_key_or_empty``, or ``_stored_key``) with
	Frappe's messages muted: for an undecryptable key Frappe's ``decrypt``
	calls ``frappe.throw``, which would add "Encryption key is invalid" to
	the reply of every request that logs an error. The flag is restored
	whatever happens."""
	flags = frappe.flags
	muted = getattr(flags, "mute_messages", None)
	flags.mute_messages = True
	try:
		api_key = read()
	finally:
		flags.mute_messages = muted
	return api_key


def _masked(maintenance, record: dict, api_key: str, timeout_types) -> dict | None:
	"""``maintenance._masked_record(record, api_key)``, or None when the
	masking failed (it answered None or raised). An RQ job timeout goes
	through."""
	try:
		return maintenance._masked_record(record, api_key)
	except Exception as e:
		if isinstance(e, timeout_types):
			raise
		return None


def _is_ai(maintenance, fields: dict, api_key: str, timeout_types) -> bool:
	"""True when ``fields`` hold a frame of Optimus's AI code or the key
	(``maintenance._is_ai_record``). True also when that check fails:
	nothing then shows the record is not Optimus's, and, once its masking
	failed, nothing shows the text is safe."""
	try:
		return bool(maintenance._is_ai_record(fields, api_key))
	except Exception as e:
		if isinstance(e, timeout_types):
			raise
		return True


def _withhold(doc, maintenance, record: dict, api_key: str, timeout_types) -> None:
	"""Store a fixed, key-free note instead of a record whose masking failed:
	``error`` always gets ``WITHHELD``. The title (``method``) gets
	``WITHHELD_TITLE`` when it holds a frame of Optimus's AI code or the key;
	otherwise it is kept, cut to its column as v16's ``validate`` cuts it
	(the full title would have gone in front of the withheld error).
	``metadata`` gets ``WITHHELD`` when it holds a frame or the key, and is
	kept otherwise."""
	doc.set("error", WITHHELD)
	title = record.get("method")
	if isinstance(title, str):
		if _is_ai(maintenance, {"method": title}, api_key, timeout_types):
			doc.set("method", WITHHELD_TITLE)
		elif len(title) > _TITLE_LIMIT:
			doc.set("method", title[:_TITLE_LIMIT])
	metadata = record.get("metadata")
	if metadata is not None and _is_ai(maintenance, {"metadata": metadata}, api_key, timeout_types):
		doc.set("metadata", WITHHELD)


def _note(outcome: str, timeout_types) -> None:
	"""A line in the ``optimus`` log saying what happened to the row
	(``outcome`` holds an exception type name at most, never row text), at
	ERROR level: Frappe's loggers drop lower levels on a production site.
	Each outcome is logged the first time it happens in this process, then
	once every ``_NOTE_EVERY`` times with its count, so a storm of failing
	Error Log inserts (a process started before the upgrade fails on every
	one) cannot fill the log, which Frappe rotates, and push out the
	migrate's summary lines. Never raises, except an RQ job timeout,
	re-raised as a fresh instance."""
	count = _NOTED.get(outcome, 0) + 1
	_NOTED[outcome] = count
	if count > 1 and count % _NOTE_EVERY:
		return
	line = f"optimus error_log_mask: an Error Log row was {outcome}"
	if count > 1:
		line = f"{line} ({count} times so far in this process)"
	interrupt = None
	try:
		import frappe

		frappe.logger("optimus").error(line)
	except Exception as e:
		if isinstance(e, timeout_types):
			interrupt = (type(e), e.args)
	if interrupt is not None:
		raise interrupt[0](*interrupt[1])


def _job_timeout_types() -> tuple[type[BaseException], ...]:
	"""RQ's job-timeout exception classes (subclasses of ``Exception``), or
	``()`` where rq cannot be imported. The same as
	``ai_fix._job_timeout_types``, kept here because this module must not
	need another Optimus module to let a job timeout stop the job: in a
	process holding stale Optimus modules, importing them is what fails."""
	try:
		from rq.timeouts import BaseTimeoutException
	except Exception:
		return ()
	return (BaseTimeoutException,)
