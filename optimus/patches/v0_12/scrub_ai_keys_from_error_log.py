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

Patch Log marks the patch done either way, so a skipped or failed scrub also
leaves one Error Log row titled "Optimus: Error Log key scrub did not run",
with the reason (an exception type name or a row count, never row text) and
the command. A scrub that ran but could not process every row or queued
entry, or left a key-shaped value, leaves one row titled "Optimus: Error Log
key scrub did not finish", with its counts and the command. Entries still in
the Error Log's deferred-insert queue are printed and logged, but alone they
leave no such row: that count also holds Frappe's own new error snapshots
(a server error, any error in developer mode), and bench migrate inserts the
queue right after the patches (the scrub masked the entries that were
waiting when it started, unless the flush stopped early, which the failed
count shows). Every outcome writes one counts-only line to
the ``optimus`` log at ERROR level (Frappe's loggers drop lower levels unless
DEV_SERVER is set, as under ``bench start``), so it reaches
``logs/optimus.log`` on a production site. Advisory step 3 (re-run the
scrub, then a dry run reporting 0) is the guarantee.
"""

_BREADCRUMB_TITLE = "Optimus: Error Log key scrub did not run"
_PARTIAL_TITLE = "Optimus: Error Log key scrub did not finish"
_COUNTS = ("candidates", "changed", "deleted_docs_changed", "residual", "failed", "queued")
_OFF_PEAK = "Error Log is locked while it is scanned, so on a busy site prefer off-peak"


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
		# breadcrumb and execute_patch's Patch Log row can be written even
		# after a failed statement on Postgres.
		_rollback(frappe)
	if failed is not None:
		_breadcrumb(frappe, _BREADCRUMB_TITLE, failed, run_it)
		_log_summary(frappe, f"failed ({failed})")
		print(f"Optimus: the Error Log key scrub failed ({failed}); the migrate continues. {run_it}")
		return
	if out is None:
		if scan == maintenance.SCAN_SIZE_UNKNOWN:
			because = unknown_because or "unknown"
			size = f"its size could not be read ({because})"
			reason = f"skipped: size unknown: {because}"
		else:
			size = f"{scan} rows to read"
			reason = f"skipped: {scan} rows"
		_breadcrumb(frappe, _BREADCRUMB_TITLE, reason, run_it)
		_log_summary(frappe, f"skipped, {size}, limit {maintenance.MIGRATE_SCAN_LIMIT}")
		print(
			f"Optimus: skipped the Error Log key scrub during migrate ({size}, "
			f"limit {maintenance.MIGRATE_SCAN_LIMIT}). {run_it}"
		)
		return
	counts = {k: int(out.get(k) or 0) for k in _COUNTS}
	_log_summary(frappe, "ran, " + " ".join(f"{k}={v}" for k, v in counts.items()))
	changed = counts["changed"] + counts["deleted_docs_changed"]
	if changed:
		print(
			f"Optimus: masked AI API keys in {changed} stored error row(s). "
			"Rotate those keys at the provider: backups taken before this upgrade still hold them."
		)
	elif not (counts["failed"] or counts["residual"] or counts["queued"]):
		print("Optimus: found no AI API keys in stored error rows.")
	if counts["failed"]:
		print(f"Optimus: {counts['failed']} error row(s) or queued entries could not be processed. {run_it}")
	if counts["queued"]:
		print(
			f"Optimus: Error Log entries still in the deferred-insert queue: {counts['queued']}. The scrub "
			"masked the ones waiting when it started, unless the flush stopped early (see the failed count); "
			"bench migrate inserts them all right after the patches. Run the scrub again after the restart to "
			f"mask them in the table: {command}"
		)
	if counts["residual"]:
		print(f"Optimus: {counts['residual']} error row(s) still hold a key-shaped value. {purge_it}")
	# Not on queued alone: it also counts Frappe's own new error snapshots.
	if counts["failed"] or counts["residual"]:
		hint = f"{run_it} {purge_it}" if counts["residual"] else run_it
		_breadcrumb(
			frappe, _PARTIAL_TITLE,
			f"failed={counts['failed']} residual={counts['residual']} queued={counts['queued']}", hint,
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
