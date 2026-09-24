# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Mask AI provider API keys that failed AI calls left in plain text in Error
Log rows (and their Deleted Document copies). Idempotent; batched by name
with a commit per chunk. See optimus.maintenance.scrub_error_log_secrets and
the security advisory in CHANGELOG.md (rotate the keys: backups still hold
them).

It never blocks the migrate: a failed or skipped scrub prints the command to
run it by hand and returns, because a failing patch would stop the rest of
the upgrade, the key-leak fix included. Patch Log marks the patch done either
way, so a skipped or failed scrub also leaves one Error Log row titled
"Optimus: Error Log key scrub did not run", with the reason (an exception
type or a row count, never row text) and the command. Every outcome writes
one counts-only line to the ``optimus`` log. Advisory step 3 (re-run the
scrub, then a dry run reporting 0) is the guarantee.
"""

_BREADCRUMB_TITLE = "Optimus: Error Log key scrub did not run"
_COUNTS = ("candidates", "changed", "deleted_docs_changed", "residual", "failed")


def execute():
	import frappe

	site = getattr(frappe.local, "site", None) or "<site>"
	command = (
		f"bench --site {site} execute optimus.maintenance.scrub_error_log_secrets "
		"--kwargs \"{'dry_run': False}\""
	)
	run_it = f"Run it now (Error Log is locked while it is scanned, so on a busy site run it off-peak): {command}"
	failed = None
	out = None
	scan = 0
	try:
		# Imported inside the try, so a broken import is reported like any
		# other failure and never stops the migrate.
		from optimus import maintenance

		scan = maintenance.scrub_scan_size()
		if scan <= maintenance.MIGRATE_SCAN_LIMIT:
			out = maintenance.scrub_error_log_secrets(dry_run=False)
	except Exception as e:
		# Keep only the type name: the scrub's frames hold unmasked rows.
		failed = type(e).__name__
	if failed is not None:
		# Drop the failing chunk's uncommitted writes (earlier chunks are
		# committed), so the transaction the migrate commits next is usable.
		try:
			frappe.db.rollback()
		except Exception:
			pass
		_breadcrumb(frappe, failed, command)
		_log_summary(frappe, f"failed ({failed})")
		print(f"Optimus: the Error Log key scrub failed ({failed}); the migrate continues. {run_it}")
		return
	if out is None:
		unknown = scan == maintenance.SCAN_SIZE_UNKNOWN
		size = "its size could not be read" if unknown else f"{scan} rows to read"
		_breadcrumb(frappe, "skipped: size unknown" if unknown else f"skipped: {scan} rows", command)
		_log_summary(frappe, f"skipped, {size}, limit {maintenance.MIGRATE_SCAN_LIMIT}")
		print(
			f"Optimus: skipped the Error Log key scrub during migrate ({size}, "
			f"limit {maintenance.MIGRATE_SCAN_LIMIT}). {run_it}"
		)
		return
	_log_summary(frappe, "ran, " + " ".join(f"{k}={int(out.get(k) or 0)}" for k in _COUNTS))
	changed = int(out.get("changed") or 0) + int(out.get("deleted_docs_changed") or 0)
	if changed:
		print(
			f"Optimus: masked AI API keys in {changed} stored error row(s). "
			"Rotate those keys at the provider: backups taken before this upgrade still hold them."
		)
	elif not (out.get("failed") or out.get("residual")):
		print("Optimus: found no AI API keys in stored error rows.")
	if out.get("failed"):
		print(f"Optimus: {out['failed']} error row(s) could not be masked. Run it again: {command}")
	if out.get("residual"):
		purge = f"bench --site {site} execute optimus.maintenance.purge_ai_error_logs --kwargs"
		print(
			f"Optimus: {out['residual']} error row(s) still hold a key-shaped value. Count the AI error rows "
			f"first: {purge} \"{{'dry_run': True}}\", then delete them: {purge} \"{{'dry_run': False}}\""
		)


def _breadcrumb(frappe, reason: str, command: str) -> None:
	"""One Error Log row saying the scrub did not run and how to run it, so
	the skip or failure is still visible once the migrate's console output is
	gone. ``reason`` is an exception type or a row count, never row text. It
	is called after the rollback (so the row survives it) and outside any
	``except`` block (so Frappe's Sentry hook has no active exception to
	attach). Never raises."""
	try:
		frappe.log_error(title=_BREADCRUMB_TITLE, message=f"{reason}. Run it by hand, off-peak: {command}")
	except Exception:
		pass


def _log_summary(frappe, outcome: str) -> None:
	"""One counts-only line in the ``optimus`` log for every outcome. Never
	raises."""
	try:
		frappe.logger("optimus").info(f"optimus scrub_ai_keys_from_error_log: {outcome}")
	except Exception:
		pass
