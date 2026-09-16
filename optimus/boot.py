# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Boot-session hook: attach ``optimus_enabled`` to ``frappe.boot`` once per
Desk session init. The floating widget reads it synchronously to decide whether
to mount, so toggling ``Profiler Enabled`` off hides the widget on the next
Desk load without a separate settings request.
"""


def boot_session(bootinfo):
	"""Attach profiler config to frappe.boot. Fails open (widget visible) on any
	error reading settings, so a misconfigured read never hides the widget
	entirely; the admin can still disable it via the DocType.
	"""
	# Resolve the (Redis-cached) config once, but keep the two values in separate
	# try/excepts: a shared one would let a threshold error flip a deliberately
	# DISABLED Optimus back on. The threshold (for the Desk hot-path picker) is
	# cfg.large_duration_threshold_ms, already resolved by get_config the same way
	# display_threshold_ms() returns it. Both fail open (widget visible, default 1000).
	cfg = None
	try:
		from optimus.settings import get_config
		cfg = get_config()
		bootinfo.optimus_enabled = bool(cfg.enabled)
	except Exception:
		bootinfo.optimus_enabled = True
	try:
		from optimus.analyzers.base import DEFAULT_DISPLAY_THRESHOLD_MS
		bootinfo.optimus_large_duration_threshold_ms = (
			float(cfg.large_duration_threshold_ms) if cfg is not None
			else DEFAULT_DISPLAY_THRESHOLD_MS
		)
	except Exception:
		from optimus.analyzers.base import DEFAULT_DISPLAY_THRESHOLD_MS
		bootinfo.optimus_large_duration_threshold_ms = DEFAULT_DISPLAY_THRESHOLD_MS
