# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Mask AI provider API keys in every Error Log row as Frappe inserts it.

``hooks.py`` registers ``mask_error_log`` as the ``before_insert`` doc event
of Error Log. Frappe runs it inside ``Document.insert``, before ``validate``,
the column length check and the INSERT, for every Error Log row, whichever
way it is written:

- ``frappe.log_error`` inserts the row at once;
- a server error's snapshot, or any error logged while the site is
  read-only, waits in Frappe's deferred-insert queue in Redis, and Frappe's
  ``save_to_db`` inserts it later (bench migrate right after the patches,
  and the scheduler every 15 minutes), again through ``Document.insert``.

So nothing reads or changes that queue: a queued record is masked when it
is inserted, by the code running at that moment. The masking is
``optimus.maintenance._masked_record``, the scrub's own: the stored key
(raw and JSON-escaped) and the key shapes ``scrub_secrets`` knows are masked
in ``error``, ``method`` and ``metadata`` (a field the doc does not hold,
such as ``metadata`` on Frappe v15, is left alone); the bare header value
lines only in a record from Optimus's AI code or holding the key; a title
longer than its column is moved in front of ``error``, as v16's
``ErrorLog.validate`` does, and the joined text masked again. A truthy value
that is not text is masked as ``str(value)``, the text the row stores. Only
the fields the masking changed are set.

The key is read once per insert (``ai_fix._current_key_or_empty``, never
cached, held only as ``api_key``), with Frappe's messages muted, so an
undecryptable key does not add "Encryption key is invalid" to the reply of
every request that logs an error. It is a SELECT on ``__Auth``, a table
every Frappe site has, so it cannot fail in a healthy transaction.

It never raises, except an RQ job timeout (the job must stop), which leaves
as a fresh instance raised after the ``try``, so the frames it interrupted
(the record's text, the key read) never travel with it. It fails open: any
other failure (an import, the key read, the masking) leaves the doc as it
was, with one exception. When the masking fails on a record that is
recognisably from Optimus's AI code (an ``ai_fix.py`` frame in any field) or
holds the key, the text is withheld (``_withhold``) instead of inserted
raw. Each failure leaves one line in the ``optimus`` log (an exception type
name at most, never row text). It never writes an Error Log itself: that
insert would run this hook again.

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
without ``SECRET_PLACEHOLDER``): there the handler resolves, its import of
``optimus.maintenance`` fails inside the guard, and the row is stored as it
was, instead of every Error Log insert of that process failing until it is
restarted.
"""

# The Error Log fields that are masked (the ones the scrub reads in a row).
_TEXT_FIELDS = ("error", "method", "metadata")
# What a withheld record stores instead of its text.
WITHHELD = "Optimus withheld this error text: it could not be masked."
WITHHELD_TITLE = "Optimus withheld this error title: it could not be masked."
# Error Log.method is Data (varchar(140)).
_TITLE_LIMIT = 140


def mask_error_log(doc, method=None) -> None:
	"""The Error Log ``before_insert`` doc event: mask the stored AI key and
	the key shapes in ``doc`` (see the module docstring). Never raises,
	except an RQ job timeout, re-raised as a fresh instance."""
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
	"""Mask ``doc``'s text fields in place. None when it is done (masked, or
	nothing to mask); otherwise the outcome for the ``optimus`` log. Raises
	what it cannot handle; ``mask_error_log`` catches it."""
	import frappe

	from optimus import maintenance
	from optimus.ai_fix import _current_key_or_empty

	record = {}
	for field in _TEXT_FIELDS:
		value = doc.get(field)
		if value is None:
			continue
		record[field] = str(value) if value and not isinstance(value, str) else value
	if not record:
		return None
	api_key = _read_key(frappe, _current_key_or_empty)
	masked = _masked(maintenance, record, api_key, timeout_types)
	if masked is None:
		if _is_ai(maintenance, record, api_key, timeout_types):
			_withhold(doc, maintenance, record, api_key, timeout_types)
			return "withheld: its masking failed"
		return "stored as it was: its masking failed"
	for field in _TEXT_FIELDS:
		if field in masked and masked[field] != record.get(field):
			doc.set(field, masked[field])
	return None


def _read_key(frappe, read) -> str:
	"""``read()`` (``_current_key_or_empty``) with Frappe's messages muted:
	for an undecryptable key Frappe's ``decrypt`` calls ``frappe.throw``,
	which would add "Encryption key is invalid" to the reply of every request
	that logs an error. The flag is restored whatever happens."""
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
	(``maintenance._is_ai_record``). True also when that check fails: the
	masking already failed, and nothing then shows the text is safe."""
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
	"""One line in the ``optimus`` log saying what happened to the row
	(``outcome`` holds an exception type name at most, never row text), at
	ERROR level: Frappe's loggers drop lower levels on a production site.
	Never raises, except an RQ job timeout, re-raised as a fresh instance."""
	interrupt = None
	try:
		import frappe

		frappe.logger("optimus").error(f"optimus error_log_mask: an Error Log row was {outcome}")
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
