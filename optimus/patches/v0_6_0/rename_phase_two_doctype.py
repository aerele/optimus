# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Rename the ``Profiler Phase 2 Run`` DocType to ``Profiler Phase Two Run``
(Title-Case convention: no digits in the name).

The Profiler-prefixed names are intentional: this predates the app rename to
optimus; a later patch renames ``Profiler Phase Two Run`` to ``Optimus
Phase Two Run``. Idempotent and defensive: no-op when the old DocType is gone
(fresh install) or when both names already exist (partial migration: bail and
let the operator resolve), otherwise ``frappe.rename_doc`` renames the row,
table, child ``parenttype`` values and clears the schema plus settings caches.
"""

import frappe

OLD_NAME = "Profiler Phase 2 Run"
NEW_NAME = "Profiler Phase Two Run"


def execute():
	try:
		old_exists = frappe.db.exists("DocType", OLD_NAME)
	except Exception:
		return
	if not old_exists:
		return  # fresh install or already renamed

	# Guard: if BOTH names exist, renaming would clash. Bail out loud.
	try:
		new_exists = frappe.db.exists("DocType", NEW_NAME)
	except Exception:
		return
	if new_exists:
		frappe.logger().warning(
			"optimus: skipping Phase-Two DocType rename both "
			f"{OLD_NAME!r} AND {NEW_NAME!r} exist. Resolve manually before "
			"re-running migrate (e.g. `bench --site <s> delete-doc DocType "
			f"'{NEW_NAME}'` if the empty one is the duplicate)."
		)
		return

	try:
		frappe.rename_doc("DocType", OLD_NAME, NEW_NAME, force=True)
	except Exception:
		frappe.log_error(title="optimus patch: rename phase-two doctype")
		return

	# Belt + braces: clear any cached doctype meta so the next request
	# resolves the new name cleanly.
	try:
		frappe.clear_cache(doctype=NEW_NAME)
	except Exception:
		pass

	# Drop our own settings cache too paranoid but cheap. Both the legacy
	# (``profiler_settings_cached``) and the v0.7.0-renamed
	# (``optimus_settings_cached``) keys are cleared since either could
	# be lingering depending on which version the site is upgrading from.
	for key in ("profiler_settings_cached", "optimus_settings_cached"):
		try:
			frappe.cache.delete_value(key)
		except Exception:
			pass

	frappe.db.commit()
