# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Mask AI provider API keys that failed AI calls left in plain text in Error
Log rows (and their Deleted Document copies), and in the Error Log records
waiting in Frappe's deferred-insert queue. Idempotent; batched by name with a
commit per chunk. See optimus.maintenance.scrub_error_log_secrets and the
security advisory in CHANGELOG.md (rotate the keys: backups still hold
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

The queue is masked in Redis on every path, and this patch never inserts a
queued record: the scrub masks it when it runs, and
``maintenance.mask_error_log_queue`` does when the scrub was skipped (it
takes no table lock) or failed. bench migrate then inserts about 500 (Frappe
v15) or about 10,000 (v16) queued records after the patches; the scheduler
inserts the rest every 15 minutes. Entries the old processes queue while the
migrate runs are not masked: advisory step 3 (run the scrub again after the
restart, then a dry run reporting the values it lists) masks them in the
table and is the guarantee.

Patch Log marks the patch done either way, so a skipped or failed scrub also
leaves one Error Log row titled "Optimus: Error Log key scrub did not run",
with the reason (an exception type name or a row count, and the queue's
counts, never row text) and the command. A scrub that ran but could not
process every row or queued entry, left a key-shaped value, held queued
entries back unmasked (Redis refused a write or stopped answering) or could
not read the stored key leaves one row titled "Optimus: Error Log key scrub
did not finish", with its counts and the command. Entries in the queue alone, masked or not
yet, are printed and logged, but leave no such row: that count also holds
Frappe's own new error snapshots (a server error, any error in developer
mode). Every outcome writes one counts-only line to the ``optimus`` log at
ERROR level (Frappe's loggers drop lower levels unless DEV_SERVER is set, as
under ``bench start``), so it reaches ``logs/optimus.log`` on a production
site.
"""

_BREADCRUMB_TITLE = "Optimus: Error Log key scrub did not run"
_PARTIAL_TITLE = "Optimus: Error Log key scrub did not finish"
_COUNTS = (
	"candidates", "changed", "deleted_docs_changed", "residual", "failed", "queued", "queue_masked", "queue_unmasked",
)
_OFF_PEAK = "on MariaDB, Error Log is locked while it is scanned, so on a busy site prefer off-peak"
_INSERTS = (
	"bench migrate then inserts about 500 (Frappe v15) or about 10,000 (v16) queued records; the scheduler "
	"inserts the rest every 15 minutes."
)
_KEY_HINT = (
	"The stored AI API key cannot be decrypted: restore the site's encryption_key, or enter the key again in "
	"Optimus Settings, then run the scrub again."
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
		# queue masking can read the key, and the breadcrumb and
		# execute_patch's Patch Log row can be written, even after a failed
		# statement on Postgres.
		_rollback(frappe)
		queue = _mask_queue(frappe, maintenance)
		note = _queue_note(queue)
		if failed is not None:
			_breadcrumb(frappe, _BREADCRUMB_TITLE, f"{failed}{note}", run_it)
			_log_summary(frappe, f"failed ({failed}){note}")
			print(f"Optimus: the Error Log key scrub failed ({failed}); the migrate continues. {run_it}")
		else:
			if scan == maintenance.SCAN_SIZE_UNKNOWN:
				because = unknown_because or "unknown"
				size = f"its size could not be read ({because})"
				reason = f"skipped: size unknown: {because}"
			else:
				size = f"{scan} Error Log and Deleted Document rows to read"
				reason = f"skipped: {scan} rows"
			_breadcrumb(frappe, _BREADCRUMB_TITLE, f"{reason}{note}", run_it)
			_log_summary(frappe, f"skipped, {size}, limit {maintenance.MIGRATE_SCAN_LIMIT}{note}")
			print(
				f"Optimus: skipped the Error Log key scrub during migrate ({size}, "
				f"limit {maintenance.MIGRATE_SCAN_LIMIT}). {run_it}"
			)
		_print_queue(queue, command, with_failed=True)
		return
	counts = {k: int(out.get(k) or 0) for k in _COUNTS}
	key_unreadable = bool(out.get("key_unreadable"))
	_log_summary(
		frappe, "ran, " + " ".join(f"{k}={v}" for k, v in counts.items()) + f" key_unreadable={int(key_unreadable)}",
	)
	changed = counts["changed"] + counts["deleted_docs_changed"]
	queue_seen = counts["queued"] or counts["queue_masked"] or counts["queue_unmasked"]
	if changed:
		print(
			f"Optimus: masked AI API keys in {changed} stored error row(s). "
			"Rotate those keys at the provider: backups taken before this upgrade still hold them."
		)
	elif not (counts["failed"] or counts["residual"] or queue_seen or key_unreadable):
		print("Optimus: found no AI API keys in stored error rows.")
	if counts["failed"]:
		print(f"Optimus: {counts['failed']} error row(s) or queued entries could not be processed. {run_it}")
	if key_unreadable:
		print(
			"Optimus: the AI API key stored in Optimus Settings cannot be decrypted, so the scrub could not "
			"search for it or mask it by value. Restore the site's encryption_key, or enter the key again in "
			f"Optimus Settings, then run the scrub again: {command}"
		)
	_print_queue(counts, command, with_failed=False)
	if counts["residual"]:
		print(f"Optimus: {counts['residual']} error row(s) still hold a key-shaped value. {purge_it}")
	# Not on queued or queue_masked alone: queued also counts Frappe's own
	# new error snapshots, and a masked entry is done.
	if counts["failed"] or counts["residual"] or counts["queue_unmasked"] or key_unreadable:
		hint = run_it
		if key_unreadable:
			hint = f"{_KEY_HINT} {hint}"
		if counts["residual"]:
			hint = f"{hint} {purge_it}"
		_breadcrumb(
			frappe, _PARTIAL_TITLE,
			" ".join(f"{k}={counts[k]}" for k in ("failed", "residual", "queued", "queue_masked", "queue_unmasked"))
			+ f" key_unreadable={int(key_unreadable)}",
			hint,
		)


def _mask_queue(frappe, maintenance) -> dict | None:
	"""``maintenance.mask_error_log_queue()`` for a scrub that did not run:
	it masks the queue in Redis and takes no table lock. None when the
	module could not be imported or the masking raised. Its key read is its
	only database statement, and the transaction is rolled back after it:
	the read cannot say that it failed, and on Postgres a failed read aborts
	the transaction the breadcrumb and the Patch Log row are written in.
	Never raises."""
	if maintenance is None:
		return None
	counts = None
	try:
		counts = maintenance.mask_error_log_queue()
	except Exception:
		counts = None
	_rollback(frappe)
	return counts


def _queue_note(counts: dict | None) -> str:
	"""The queue's counts for a breadcrumb or a log line (never row text)."""
	if counts is None:
		return "; queue: not masked"
	labels = (("masked", "queue_masked"), ("unmasked", "queue_unmasked"), ("queued", "queued"), ("failed", "failed"))
	return "; queue: " + " ".join(f"{label}={int(counts.get(key) or 0)}" for label, key in labels)


def _print_queue(counts: dict | None, command: str, with_failed: bool) -> None:
	"""The console lines about the Error Log queue. ``with_failed`` adds the
	queue's own failed count (when the scrub ran, its failed line counts
	them already)."""
	if counts is None:
		print(
			"Optimus: the Error Log deferred-insert queue was not masked; bench migrate inserts it as it is. "
			f"Run the scrub after the restart (step 3): {command}"
		)
		return
	masked, unmasked, queued = (int(counts.get(k) or 0) for k in ("queue_masked", "queue_unmasked", "queued"))
	if masked or unmasked or queued:
		print(
			f"Optimus: queued Error Log entries masked in Redis: {masked}, unmasked: {unmasked}; entries now in "
			f"the deferred-insert queue: {queued}. {_INSERTS} Entries queued while the migrate runs are not "
			f"masked: run the scrub again after the restart (step 3): {command}"
		)
	if unmasked:
		print(
			"Optimus: queued Error Log entries that could not be masked because Redis refused writes or stopped "
			f"answering: {unmasked}. They are held back and not inserted; run the scrub again (step 3): {command}"
		)
	failed = int(counts.get("failed") or 0) if with_failed else 0
	if failed:
		print(
			f"Optimus: queued Error Log entries or records that could not be processed: {failed}. Run the scrub "
			f"again after the restart (step 3): {command}"
		)


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
