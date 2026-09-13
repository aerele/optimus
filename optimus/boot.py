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
	# Read the two values independently: a shared try/except let a threshold error
	# flip a deliberately DISABLED Optimus back on. The threshold (for the Desk
	# hot-path picker) comes from the single display_threshold_ms resolver. Both
	# fail open (widget visible, default 1000).
	try:
		from optimus.settings import get_config
		bootinfo.optimus_enabled = bool(get_config().enabled)
	except Exception:
		bootinfo.optimus_enabled = True
	try:
		from optimus.settings import display_threshold_ms
		bootinfo.optimus_large_duration_threshold_ms = display_threshold_ms()
	except Exception:
		bootinfo.optimus_large_duration_threshold_ms = 1000.0
