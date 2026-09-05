# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Drop the session-comparison / baseline-pinning feature.

Removes ``compared_to_session`` and ``is_baseline`` from Optimus Session:
reloading the DocType lets Frappe's auto-DDL drop the columns during
migrate. Also clears lingering ``profiler:baseline:<label>`` cache keys.
Idempotent: each step is wrapped in try/except.
"""

import frappe


def execute():
	# Reload the DocType so Frappe's column-drop migration picks up the
	# field removal from optimus_session.json. After this, the underlying
	# tabOptimus Session table no longer has compared_to_session /
	# is_baseline columns.
	try:
		frappe.reload_doc("optimus", "doctype", "optimus_session")
	except Exception:
		frappe.log_error(title="v0.6.0 patch: reload optimus_session (drop comparison)")

	# Best-effort: clear baseline-pinning cache keys. These were stored as
	# `profiler:baseline:<label>` -> docname. Redis SCAN-style deletion
	# isn't available through frappe.cache's portable API, so we just drop
	# the keys we can derive from existing session titles anything else
	# expires on its own (cache, not durable state).
	try:
		titles = frappe.get_all("Optimus Session", pluck="title") or []
		for title in set(titles):
			try:
				frappe.cache.delete_value(f"profiler:baseline:{title or ''}")
			except Exception:
				pass
	except Exception:
		frappe.log_error(title="v0.6.0 patch: clear baseline cache keys")

	frappe.db.commit()
