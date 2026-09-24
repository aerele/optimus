# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Mask AI provider API keys that failed AI calls left in plain text in Error
Log rows (and their Deleted Document copies). Idempotent; batched by name
with a commit per chunk. See optimus.maintenance.scrub_error_log_secrets and
the security advisory in CHANGELOG.md (rotate the keys: backups still hold
them).

It never blocks the migrate: a failed or skipped scrub prints the command to
run it by hand and returns, because a failing patch would stop the rest of
the upgrade, the key-leak fix included. Advisory step 3 (re-run the scrub,
then a dry run reporting 0) is the guarantee.
"""


def execute():
	import frappe

	from optimus import maintenance

	site = getattr(frappe.local, "site", None) or "<site>"
	command = (
		f"bench --site {site} execute optimus.maintenance.scrub_error_log_secrets "
		"--kwargs \"{'dry_run': False}\""
	)
	failed = None
	out = None
	scan = 0
	try:
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
		print(f"Optimus: the Error Log key scrub failed ({failed}); the migrate continues. Run it now: {command}")
		return
	if out is None:
		print(
			f"Optimus: skipped the Error Log key scrub during migrate ({scan} rows to read, "
			f"limit {maintenance.MIGRATE_SCAN_LIMIT}). Run it now: {command}"
		)
		return
	changed = int(out.get("changed") or 0) + int(out.get("deleted_docs_changed") or 0)
	print(
		f"Optimus: masked AI API keys in {changed} stored error row(s). "
		"Rotate those keys at the provider: backups taken before this upgrade still hold them."
	)
	if out.get("failed"):
		print(f"Optimus: {out['failed']} error row(s) could not be masked. Run it again: {command}")
	if out.get("residual"):
		print(
			f"Optimus: {out['residual']} error row(s) still hold a key-shaped value. Delete the AI error rows: "
			f"bench --site {site} execute optimus.maintenance.purge_ai_error_logs --kwargs \"{{'dry_run': False}}\""
		)
