# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Mask AI provider API keys that failed AI calls left in plain text in Error
Log rows (and their Deleted Document copies). Idempotent; batched by name
with a commit per chunk. See optimus.maintenance.scrub_error_log_secrets and
the security advisory in CHANGELOG.md (rotate the keys: backups still hold
them).

It never blocks the migrate: a failed or skipped scrub prints the command to
run it by hand and returns, because a failing patch would stop the rest of
the upgrade, the key-leak fix included. When the scrub did not run, the patch
rolls the transaction back before it writes anything: on Postgres a failed
statement (a size query, a scrub chunk) aborts the transaction, and every
later write, this patch's breadcrumb and the Patch Log row
``execute_patch`` writes next included, would fail until a rollback, which
would stop the migrate on every retry. ``execute_patch`` commits just before
the patch and the scrub commits every chunk, so the rollback drops at most
the failing chunk's writes.

It never reads or changes Frappe's deferred-insert queue. The Error Log
``before_insert`` hook (``optimus.error_log_mask``) masks every Error Log
row as Frappe inserts it, so the records queued in Redis are masked when
bench migrate (right after the patches) or the scheduler inserts them.
Every path but a failed import of ``optimus.maintenance`` prints one line
saying so; after a failed import it says rows may be stored unmasked until
the module imports, since the hook needs it too. Frappe caches the hooks,
and a process started before the upgrade can cache the old ones again
during the migrate; so, as a real scrub does first, the patch refreshes
that cache when the scrub did not run (``_refresh_hooks_cache``), and
this migrate's flush of the queue runs the hook.

Patch Log marks the patch done either way, so a skipped or failed scrub also
leaves one Error Log row titled "Optimus: Error Log key scrub did not run",
with the reason (an exception type name or a row count, never row text) and
the command. A scrub that ran but could not process every row, left a
key-shaped value or could not read the stored key leaves one row titled
"Optimus: Error Log key scrub did not finish", with its counts and the
command. Every outcome writes one counts-only line to the ``optimus`` log at
ERROR level (Frappe's loggers drop lower levels unless DEV_SERVER is set, as
under ``bench start``), so it reaches ``logs/optimus.log`` on a production
site.
"""

_BREADCRUMB_TITLE = "Optimus: Error Log key scrub did not run"
_PARTIAL_TITLE = "Optimus: Error Log key scrub did not finish"
_COUNTS = ("candidates", "changed", "deleted_docs_changed", "residual", "failed")
_OFF_PEAK = "on MariaDB, Error Log is locked while it is scanned, so on a busy site prefer off-peak"
_KEY_HINT = (
	"The stored AI API key cannot be decrypted: restore the site's encryption_key, or enter the OLD key again in "
	"Optimus Settings, then run the scrub again."
)
# Printed last on every path but a failed import. It repeats no command: the
# one instruction line above it has it.
_QUEUE_LINE = (
	"Optimus: Error Log entries still queued in Redis are masked by Optimus when Frappe inserts them; nothing "
	"needs doing for the queue."
)
# Printed instead after a failed import: the Error Log hook imports the same
# module, so it cannot mask anything either until the module imports.
_NOT_MASKED_LINE = (
	"Optimus: Error Log rows, queued ones included, may be stored unmasked while optimus.maintenance cannot be "
	"imported; run the command above once it can."
)


def execute():
	import frappe

	site = getattr(frappe.local, "site", None) or "<site>"
	command = (
		f"bench --site {site} execute optimus.maintenance.scrub_error_log_secrets "
		"--kwargs \"{'dry_run': False}\""
	)
	purge = f"bench --site {site} execute optimus.maintenance.purge_ai_error_logs --kwargs"
	run_it = f"Run it by hand ({_OFF_PEAK}): {command}"
	purge_it = (
		f"Count the AI error rows first: {purge} \"{{'dry_run': True}}\", then delete them "
		f"({_OFF_PEAK}): {purge} \"{{'dry_run': False}}\""
	)
	failed = None
	out = None
	scan = 0
	unknown_because = None
	maintenance = None
	try:
		# Imported inside the try, so a broken import is reported like any
		# other failure and never stops the migrate.
		from optimus import maintenance

		scan, unknown_because = maintenance.measure_scan_size()
		if scan <= maintenance.MIGRATE_SCAN_LIMIT:
			out = maintenance.scrub_error_log_secrets(dry_run=False)
	except Exception as e:
		# Keep only the type name: the scrub's frames hold unmasked rows.
		failed = type(e).__name__
	if out is None:
		# The scrub did not run (skipped or failed): roll back first, so the
		# breadcrumb and execute_patch's Patch Log row can be written, even
		# after a failed statement on Postgres. Then refresh the hooks Frappe
		# caches, as a real scrub does first, so this process's flush of the
		# deferred-insert queue, right after the patches, runs the Error Log
		# hook; its reload reads the installed apps, so a refresh that failed
		# is rolled back too.
		_rollback(frappe)
		if maintenance is not None and not _refresh_hooks(maintenance):
			_rollback(frappe)
		if failed is not None:
			_breadcrumb(frappe, _BREADCRUMB_TITLE, failed, run_it)
			_log_summary(frappe, f"failed ({failed})")
			print(f"Optimus: the Error Log key scrub failed ({failed}); the migrate continues. {run_it}")
		else:
			if scan == maintenance.SCAN_SIZE_UNKNOWN:
				because = unknown_because or "unknown"
				size = f"its size could not be read ({because})"
				reason = f"skipped: size unknown: {because}"
			else:
				size = f"{scan} Error Log and Deleted Document rows to read"
				reason = f"skipped: {scan} rows"
			_breadcrumb(frappe, _BREADCRUMB_TITLE, reason, run_it)
			_log_summary(frappe, f"skipped, {size}, limit {maintenance.MIGRATE_SCAN_LIMIT}")
			print(
				f"Optimus: skipped the Error Log key scrub during migrate ({size}, "
				f"limit {maintenance.MIGRATE_SCAN_LIMIT}). {run_it}"
			)
		print(_QUEUE_LINE if maintenance is not None else _NOT_MASKED_LINE)
		return
	counts = {k: int(out.get(k) or 0) for k in _COUNTS}
	key_unreadable = bool(out.get("key_unreadable"))
	_log_summary(
		frappe, "ran, " + " ".join(f"{k}={v}" for k, v in counts.items()) + f" key_unreadable={int(key_unreadable)}",
	)
	changed = counts["changed"] + counts["deleted_docs_changed"]
	if changed:
		print(
			f"Optimus: masked AI API keys in {changed} stored error row(s). "
			"Rotate those keys at the provider: backups taken before this upgrade still hold them."
		)
	elif not (counts["failed"] or counts["residual"] or key_unreadable):
		print("Optimus: found no AI API keys in stored error rows.")
	if counts["failed"]:
		print(f"Optimus: {counts['failed']} error row(s) could not be processed. {run_it}")
	if key_unreadable:
		print(
			"Optimus: the AI API key stored in Optimus Settings cannot be decrypted, so the scrub could not "
			"search for it or mask it by value. Restore the site's encryption_key, or enter the OLD key again in "
			f"Optimus Settings, then run the scrub again: {command}"
		)
	if counts["residual"]:
		print(f"Optimus: {counts['residual']} error row(s) still hold a key-shaped value. {purge_it}")
	print(_QUEUE_LINE)
	if counts["failed"] or counts["residual"] or key_unreadable:
		hint = run_it
		if key_unreadable:
			hint = f"{_KEY_HINT} {hint}"
		if counts["residual"]:
			hint = f"{hint} {purge_it}"
		_breadcrumb(
			frappe, _PARTIAL_TITLE,
			" ".join(f"{k}={counts[k]}" for k in ("failed", "residual")) + f" key_unreadable={int(key_unreadable)}",
			hint,
		)


def _refresh_hooks(maintenance) -> bool:
	"""``maintenance._refresh_hooks_cache()``: False when it failed or
	raised. Never raises."""
	try:
		return bool(maintenance._refresh_hooks_cache())
	except Exception:
		return False


def _rollback(frappe) -> None:
	"""Roll the current transaction back. Never raises."""
	try:
		frappe.db.rollback()
	except Exception:
		pass


def _breadcrumb(frappe, title: str, reason: str, hint: str) -> None:
	"""One Error Log row saying the scrub did not run (or did not finish)
	and how to run it, so it is still visible once the migrate's console
	output is gone. ``reason`` is an exception type name or counts, never
	row text. It is written outside any ``except`` block (so Frappe's Sentry
	hook has no active exception to attach). If the write itself fails, the
	transaction is rolled back: a failed INSERT aborts it on Postgres, and
	execute_patch's Patch Log row comes next. Never raises."""
	failed = False
	try:
		frappe.log_error(title=title, message=f"{reason}. {hint}")
	except Exception:
		failed = True
	if failed:
		_rollback(frappe)


def _log_summary(frappe, outcome: str) -> None:
	"""One counts-only line in the ``optimus`` log for every outcome, at
	ERROR level: Frappe's loggers drop lower levels unless DEV_SERVER is
	set, so an info line would never reach ``logs/optimus.log`` on a
	production site. Never raises."""
	try:
		frappe.logger("optimus").error(f"optimus scrub_ai_keys_from_error_log: {outcome}")
	except Exception:
		pass
