"""Read-only upgrade notice for the stricter provider endpoint/key policy."""

import frappe

from optimus import ai_fix


def execute():
	message = ""
	guard = ai_fix._InterruptGuard(base=True)
	try:
		with guard:
			message = _warning()
	except Exception:
		message = "Optimus AI endpoint policy could not be checked. Review Optimus Settings and use Test AI connection."
	if guard.pending():
		raise guard.interrupt()
	if message:
		print(message)


def _warning():
	if not frappe.db.get_single_value("Optimus Settings", "ai_enabled"):
		return ""
	name = frappe.db.get_single_value("Optimus Settings", "ai_provider") or ai_fix._DEFAULT_PROVIDER
	if (ai_fix._PROVIDER_DEFAULTS.get(name) or {}).get("base_url"):
		return ""
	url = frappe.db.get_single_value("Optimus Settings", "ai_base_url")
	try:
		url = ai_fix.validate_base_url(url)
	except ai_fix.AiFixError:
		return "Optimus AI Base URL is not allowed by the new endpoint policy. Review Optimus Settings and use Test AI connection."
	if frappe.db.get_single_value("Optimus Settings", "ai_api_key") and ai_fix.key_over_http_blocked(
		url, allow=ai_fix._allow_key_over_http(),
	):
		return (
			"Optimus AI API key will not be sent over plain http:// to another machine. "
			"Use https:// or explicitly set optimus_ai_allow_key_over_http if this network is trusted. "
			"Keyless local providers continue to work."
		)
	return ""
