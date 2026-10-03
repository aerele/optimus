# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Give previously saved Settings a bounded manual cap, preserving stored zero."""

import frappe

from optimus import redis_keys
from optimus.ai_fix import _InterruptGuard


def execute():
	guard = _InterruptGuard(base=True)
	with guard:
		_seed()
	if guard.pending():
		raise guard.interrupt()


def _seed():
	values = frappe.db.get_singles_dict("Optimus Settings")
	if not values or "ai_refresh_max_findings" in values:
		return
	frappe.db.set_single_value("Optimus Settings", "ai_refresh_max_findings", 20)
	guard = _InterruptGuard(base=True)
	cache_failed = False
	try:
		with guard:
			frappe.cache.delete_value(redis_keys.settings_cache())
			frappe.clear_document_cache("Optimus Settings", "Optimus Settings")
	except Exception:
		cache_failed = True
	if guard.pending():
		values = None
		raise guard.interrupt()
	if cache_failed:
		print("Optimus AI refresh limit seeded; clear Settings cache after restarting web and workers.")
