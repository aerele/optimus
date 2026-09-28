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

It changes only the rows that are Optimus's to change: a record from Optimus's
AI code (an ``optimus/ai_fix.py`` or ``frappe_profiler/ai_fix.py`` frame in
``error``, ``method`` or ``metadata``) or holding the stored key (raw,
JSON-escaped or repr-escaped), as ``maintenance._is_ai_record`` decides. Every
other row, another app's included, is left byte-identical: the hook is
site-wide and permanent, so it applies no key shape and moves no title there,
and Frappe's own ``validate`` and length check treat it as they would without
Optimus. If that check itself fails, the record is treated as one of
Optimus's.

In such a record the masking is ``optimus.maintenance._masked_record``, the
scrub's own: the stored key (raw, JSON-escaped and repr-escaped), the key
shapes ``scrub_secrets`` knows and the bare header value lines are masked in
``error``, ``method`` and ``metadata`` (a field the doc does not hold, such as
``metadata`` on Frappe v15, is left alone); a title longer than its column is
moved in front of ``error``, as v16's ``ErrorLog.validate`` does, and the
joined text masked again. A truthy value that is not text is read as
``str(value)``, the text the row stores, both to recognise the key and to mask
it. Only the fields the masking changed are set.

The key is read once per insert, of every Error Log row, since a row
holding the key is one of Optimus's (``ai_fix._current_key_or_empty``,
never cached, held only as ``api_key``), with Frappe's messages muted, so an
undecryptable key does not add "Encryption key is invalid" to the reply of
every request that logs an error. It is a SELECT on ``__Auth``, a table
every Frappe site has, so it cannot fail in a healthy transaction. It is
read only when one is stored (``_key_stored``: the Password field's plain
Singles value, asterisks that Frappe caches per transaction, never
decrypted); on a site where AI was never enabled no key is read, and only
the frame check decides.

An RQ job timeout (the job must stop) leaves as a fresh instance raised after
the ``try``, so the frames it interrupted (the record's text, the key read)
never travel with it. Non-Exception interrupts during the key read keep their
identity with decrypt frames and exception chains cleared. Ordinary failures
leave another app's doc as it was, and fail closed for a record of the AI
code. When the masking fails (on a record of Optimus's, the only ones it
masks), the text is withheld (``_withhold``) instead of inserted raw. When
anything else fails unexpectedly, a record holding an ``ai_fix.py`` frame
(a plain substring check, standard library alone) is withheld the same way
and every other record is left as it was (``_fail_closed``). When Optimus's
other modules cannot be imported, the fallback works with Frappe alone
(``_mask_stored_key_only``, below). Each failure leaves a line in the
``optimus`` log (an exception type name at most, never row text; an outcome
the first time it happens in the process, then every 1000th time, ``_note``).
It never writes an Error Log itself: that insert would run this hook again.

Frappe caches every app's hooks ("app_hooks" in Redis). A process started
before the upgrade that misses that key after migrate's ``clear_cache``
caches its old hooks again, without this event, and every process reads
them until the key is deleted. The migrate patch, when the scrub did not
run, and every real scrub delete and reload that cache
(``maintenance._refresh_hooks_cache``); the one run after the restart
(advisory step 3) makes the event reach every process for good.

The module imports only the standard library at import time; every Optimus
import is inside the guarded ``try``. Frappe resolves the handler outside
any ``try``, in every process that reads the hooks. On an in-place upgrade
(the new code on the filesystem the running processes use), a process
started before the upgrade still holds the old Optimus modules
(``optimus.redaction`` without ``SECRET_PLACEHOLDER``): there the handler
resolves and its import of ``optimus.maintenance`` fails inside the guard,
instead of every Error Log insert of that process failing until it is
restarted. That process still runs the old AI code, the code that leaks the
key, so the hook then falls back to Frappe alone. A record holding an
``ai_fix.py`` frame is withheld (``_withhold``: it cannot be masked there,
and its key may be one no longer stored). In every other record it reads
the stored key with ``frappe.utils.password.get_decrypted_password``
(messages muted, held as ``api_key``, its escaped forms as ``secret``) and
replaces them with ``********`` in ``error``, ``method`` and ``metadata``,
wherever they hold them. A row without the key is left byte-identical, and a
key shorter than 8 characters is not replaced, as in the masking above. Key
shapes and value lines are not masked there: that needs the modules that
failed to import. On
an image-based or rolling deployment a process of the old image has no such
module: once it reads the new hooks, every Error Log insert there fails
until the process is replaced, and no code here can prevent it (SECURITY.md,
"Known limitations").
"""


import json

# The path suffix of this module's frames ("optimus/error_log_mask.py"), built
# from its own name so a rename or a move cannot leave a stale copy: the
# analyzers (optimus.analyzers.base) recognise the hook's own stored-key read
# by it.
HOOK_FRAME_SUFFIX = __name__.replace(".", "/") + ".py"
# The Error Log fields that are masked (the ones the scrub reads in a row).
_TEXT_FIELDS = ("error", "method", "metadata")
# What a withheld record stores instead of its text: stored Error Log
# content, so untranslated (a translation at write time would bake in the
# failing request's language), key-free, with the next step.
WITHHELD = "Optimus withheld this error text: it could not be masked. See logs/optimus.log for the reason."
WITHHELD_TITLE = "Optimus withheld this error title: it could not be masked."
# Error Log.method is Data (varchar(140)).
_TITLE_LIMIT = 140
# optimus.redaction.SECRET_PLACEHOLDER and MIN_KEY_LEN, copied: the fallback
# runs where those modules cannot be imported (a test pins them equal).
_PLACEHOLDER = "********"
_MIN_KEY_LEN = 8
# maintenance._OPTIMUS_AI_FRAME_PATHS, copied for the same reason: the frame
# paths of Optimus's AI module (under its name and under the app's name
# before the rename). A record holding one is withheld whenever it cannot be
# masked (_fail_closed, _mask_stored_key_only).
_AI_FRAME_PATHS = ("optimus/ai_fix.py", "frappe_profiler/ai_fix.py")
# How many times each outcome has been noted in this process (_note), and
# how often a repeated one is logged: its first time, then every
# _NOTE_EVERY-th time. The outcomes are a few fixed texts and exception
# type names, so this stays small.
_NOTED: dict[str, int] = {}
_NOTE_EVERY = 1000


def mask_error_log(doc, method=None) -> None:
	"""The Error Log ``before_insert`` doc event: in a record from Optimus's AI
	code or holding the stored key, mask the key and the key shapes; leave every
	other ``doc`` as it was (see the module docstring). An RQ job timeout leaves
	as a fresh instance. Non-Exception interrupts during the key read keep their
	identity, with decrypt frames and exception chains cleared."""
	timeout_types = _job_timeout_types()
	interrupt = None
	outcome = None
	failure = None
	try:
		outcome = _mask_doc(doc, timeout_types)
	except Exception as e:
		if isinstance(e, timeout_types):
			interrupt = (type(e), e.args)
		else:
			failure = type(e).__name__
	if interrupt is not None:
		raise interrupt[0](*interrupt[1])
	if failure is not None:
		outcome = _fail_closed(doc, failure, timeout_types)
	if outcome is not None:
		_note(outcome, timeout_types)


def _fail_closed(doc, failure: str, timeout_types) -> str:
	"""After an unexpected failure (``failure``, its exception type name):
	a record holding a frame of Optimus's AI module (``_holds_ai_frame``, a
	plain substring check with the standard library alone) has its text
	withheld (``_withhold``, the same rule as a failed masking); every other
	record is left as it was (fail-open: the hook is site-wide). Returns the
	outcome for the ``optimus`` log. Never raises, except an RQ job timeout,
	raised again as a fresh instance; a failure here leaves the outcome
	"stored as it was"."""
	outcome = f"stored as it was: {failure}"
	interrupt = None
	try:
		record = _record(doc)
		if _holds_ai_frame(record):
			_withhold(doc, record, _holds_ai_frame)
			outcome = f"withheld: its masking failed unexpectedly ({failure})"
	except Exception as e:
		if isinstance(e, timeout_types):
			interrupt = (type(e), e.args)
	if interrupt is not None:
		raise interrupt[0](*interrupt[1])
	return outcome


def _record(doc) -> dict:
	"""``doc``'s text fields that are set, a truthy value that is not text read
	as ``str(value)`` (the text the row stores)."""
	record = {}
	for field in _TEXT_FIELDS:
		value = doc.get(field)
		if value is None:
			continue
		record[field] = str(value) if value and not isinstance(value, str) else value
	return record


def _holds_ai_frame(fields: dict) -> bool:
	"""True when a text field of ``fields`` holds a frame of Optimus's AI
	module (``_AI_FRAME_PATHS``): a plain substring check, standard library
	alone, as ``maintenance._is_ai_record`` makes it."""
	return any(isinstance(text, str) and any(path in text for path in _AI_FRAME_PATHS) for text in fields.values())


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

	record = _record(doc)
	if not record:
		return None
	if stale is not None:
		return _mask_stored_key_only(frappe, doc, stale, record)
	api_key = _read_key(frappe, _current_key_or_empty) if _key_stored(frappe, timeout_types) else ""
	# Only a record from Optimus's AI code or holding the key is Optimus's to
	# change; every other row is left exactly as it was.
	failures = []
	if not _is_ai(maintenance, record, api_key, timeout_types, failures=failures):
		return None
	masked = _masked(maintenance, record, api_key, timeout_types, failures=failures)
	if masked is None:
		_withhold(doc, record, lambda fields: _is_ai(maintenance, fields, api_key, timeout_types))
		reason = ", ".join(dict.fromkeys(failures)) or "no masked record"
		return f"withheld: its masking failed ({reason})"
	for field in _TEXT_FIELDS:
		if field in masked and masked[field] != record.get(field):
			doc.set(field, masked[field])
	return None


def _mask_stored_key_only(frappe, doc, stale: str, record: dict) -> str:
	"""The fallback of a process whose Optimus modules cannot be imported
	(``stale``, the import's exception type name), with Frappe and the
	standard library alone. It fails closed for a record of Optimus's AI code
	(a frame of it in ``record``, ``_holds_ai_frame``): that process runs the
	old AI code and the key shapes cannot be masked here, so its text is
	withheld (``_withhold``, the same rule as a failed masking: the title and
	the metadata too when they hold a frame or the stored key). In every
	other record the stored key, raw, JSON-escaped and repr-escaped, is
	replaced with ``_PLACEHOLDER`` in the text fields that hold it (a truthy
	value that is not text is read as ``str(value)``); a key shorter than
	``_MIN_KEY_LEN`` characters is not replaced, and a row without the key is
	left as it was. Returns the outcome for the ``optimus`` log. Raises what
	it cannot handle; ``mask_error_log`` catches it."""
	api_key = _read_key(frappe, _stored_key) if _key_stored(frappe, _job_timeout_types()) else ""
	if _holds_ai_frame(record):
		_withhold(doc, record, lambda fields: _holds_ai_frame(fields) or _holds_key(fields, api_key))
		return f"withheld: a record of the AI code (Optimus's modules could not be imported: {stale})"
	if len(api_key) >= _MIN_KEY_LEN:
		for field in _TEXT_FIELDS:
			value = doc.get(field)
			if not value:
				continue
			text = value if isinstance(value, str) else str(value)
			masked = text
			# Escaped forms first: replacing raw backslashes first could
			# leave an escape from the representation beside the mask.
			for secret in sorted(_key_literals(api_key), key=len, reverse=True):
				masked = masked.replace(secret, _PLACEHOLDER)
			if masked != text:
				doc.set(field, masked)
	return f"checked for the stored key alone (Optimus's modules could not be imported: {stale})"


def _holds_key(fields: dict, api_key: str) -> bool:
	"""True when a text field of ``fields`` holds ``api_key`` (at least
	``_MIN_KEY_LEN`` characters), raw, JSON-escaped or repr-escaped: the
	standard-library check of the fallback."""
	if not isinstance(api_key, str) or len(api_key) < _MIN_KEY_LEN:
		return False
	return any(
		isinstance(text, str) and secret in text for secret in _key_literals(api_key) for text in fields.values()
	)


def _key_stored(frappe, timeout_types) -> bool:
	"""False only when Optimus Settings holds no API key (none was ever stored:
	AI never enabled), so the key is not read at all: no SELECT on ``__Auth``,
	no decrypt, on every Error Log insert of such a site. It reads the plain
	Singles value of the Password field (``frappe.db.get_single_value``),
	which holds one asterisk per character of a stored key, never the key,
	and which Frappe caches per transaction (``Database.value_cache`` on v15
	and v16, cleared at commit and rollback); a key written there by hand as
	plain text is held as ``secret``, a name the sanitizers redact. As in
	``maintenance._key_unreadable``: no Optimus Settings DocType
	(``DoesNotExistError``) means no key; any other failure answers True, so
	the key is read as before. An RQ job timeout goes through
	(``mask_error_log`` raises it again, fresh)."""
	missing = getattr(frappe, "DoesNotExistError", ())
	try:
		secret = frappe.db.get_single_value("Optimus Settings", "ai_api_key")
	except Exception as e:
		if isinstance(e, timeout_types):
			raise
		return not isinstance(e, missing)
	return isinstance(secret, str) and bool(secret.strip())


def _key_literals(api_key) -> tuple[str, ...]:
	"""``optimus.redaction.key_literals``, copied with the standard library
	alone for the fallback (a test pins the two equal): ``api_key`` raw,
	JSON-escaped and repr-escaped, without duplicates, raw first; ``()`` for
	no key."""
	if not isinstance(api_key, str) or not api_key:
		return ()
	return tuple(dict.fromkeys((api_key, json.dumps(api_key)[1:-1], repr(api_key)[1:-1])))


def _stored_key() -> str:
	"""The stored ``Optimus Settings.ai_api_key``, stripped, or ``""``, read
	with Frappe alone (``ai_fix._current_key_or_empty`` without Optimus):
	the same ``__Auth`` SELECT, ``raise_exception=False``, so an
	undecryptable key answers ``""``. Held only as ``api_key``. Ordinary
	exceptions answer ``""``; RQ timeouts leave fresh. Other interrupts keep
	their identity with traceback, context and cause cleared, so decrypt
	frames never escape. This guard cannot import another Optimus module."""
	job_timeout_types = _job_timeout_types()
	interrupt = None
	escaping: BaseException | None = None
	try:
		from frappe.utils.password import get_decrypted_password

		api_key = get_decrypted_password(
			"Optimus Settings", "Optimus Settings", "ai_api_key",
			raise_exception=False,
		) or ""
	except BaseException as e:
		if isinstance(e, job_timeout_types):
			interrupt = (type(e), e.args)
		elif not isinstance(e, Exception):
			escaping = e
		else:
			return ""
	if escaping is not None:
		escaping.__traceback__ = None
		escaping.__context__ = None
		escaping.__cause__ = None
		escaping.__suppress_context__ = True
		raise escaping
	if interrupt is not None:
		raise interrupt[0](*interrupt[1])
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


def _masked(maintenance, record: dict, api_key: str, timeout_types, *, failures: list[str]) -> dict | None:
	"""``maintenance._masked_record(record, api_key)``, or None when the
	masking failed (it answered None or raised). Only exception type names
	go into ``failures``. An RQ job timeout goes through."""
	try:
		return maintenance._masked_record(record, api_key, failures=failures)
	except Exception as e:
		if isinstance(e, timeout_types):
			raise
		failures.append(type(e).__name__)
		return None


def _is_ai(maintenance, fields: dict, api_key: str, timeout_types, *, failures: list[str] | None = None) -> bool:
	"""True when ``fields`` hold a frame of Optimus's AI code or the key
	(``maintenance._is_ai_record``). True also when that check fails:
	nothing then shows the record is not Optimus's, and, once its masking
	failed, nothing shows the text is safe. When supplied, ``failures``
	receives only the exception type name."""
	try:
		return bool(maintenance._is_ai_record(fields, api_key))
	except Exception as e:
		if isinstance(e, timeout_types):
			raise
		if failures is not None:
			failures.append(type(e).__name__)
		return True


def _withhold(doc, record: dict, is_ai) -> None:
	"""Store a fixed, key-free note instead of a record that could not be
	masked: ``error`` always gets ``WITHHELD``. The title (``method``) gets
	``WITHHELD_TITLE`` when ``is_ai({"method": title})`` (it holds a frame of
	Optimus's AI code or the key); otherwise it is kept, cut to its column as
	v16's ``validate`` cuts it (the full title would have gone in front of
	the withheld error). ``metadata`` gets ``WITHHELD`` when
	``is_ai({"metadata": metadata})``, and is kept otherwise. ``is_ai`` is
	the masking's own check (``_is_ai``) or, where Optimus's modules are not
	used, the standard-library one."""
	doc.set("error", WITHHELD)
	title = record.get("method")
	if isinstance(title, str):
		if is_ai({"method": title}):
			doc.set("method", WITHHELD_TITLE)
		elif len(title) > _TITLE_LIMIT:
			doc.set("method", title[:_TITLE_LIMIT])
	metadata = record.get("metadata")
	if metadata is not None and is_ai({"metadata": metadata}):
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
