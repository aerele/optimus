# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

from urllib.parse import urlsplit

import frappe
from frappe import _
from frappe.model.document import Document


def _text(value):
	return value.strip() if isinstance(value, str) else ""


def _effective_endpoint(provider, base_url):
	"""The effective provider and URL, with case-sensitive paths preserved."""
	from optimus import ai_fix

	name = _text(provider) or ai_fix._DEFAULT_PROVIDER
	raw = (ai_fix._PROVIDER_DEFAULTS.get(name) or {}).get("base_url") or _text(base_url)
	try:
		parts = urlsplit(raw)
		# Default ports and host/scheme spelling do not change the destination.
		return name, parts.scheme, (parts.hostname or "").lower(), parts.port or ai_fix._DEFAULT_PORTS.get(parts.scheme), parts.path.rstrip("/"), parts.query, parts.fragment
	except ValueError:
		return name, raw


class OptimusSettings(Document):
	"""Site-wide configuration for Optimus.

	A Single DocType. The cached reader in ``optimus.settings``
	is what analyzers and hooks actually call this controller only
	handles validation + cache invalidation when an admin saves.
	"""

	def validate(self):
		self._normalize_tracked_apps()
		self._clamp_numeric_floors()
		self._warn_on_framework_apps_in_tracked()
		self._check_ai_base_url()
		self._clear_api_key_on_endpoint_change()
		self._warn_key_not_sent_over_http()
		self._warn_on_incomplete_ai_config()
		self._refuse_an_unsendable_ai_key()

	def on_update(self):
		# Settings are read on every request (via the `enabled` gate in
		# hooks_callbacks), so the cache version bumps on every save.
		# The settings module's reader respects the cache version.
		from optimus import redis_keys

		frappe.cache.delete_value(redis_keys.settings_cache())

	# Numeric floors per field a value below the floor would either
	# break the analyzer at runtime (negative interval, zero retention)
	# or render a useless report (sub-microsecond thresholds). Floors
	# of 0 mean "0 is OK as a 'no filter' / 'always flag' sentinel".
	_NUMERIC_FLOORS = {
		# v0.13.x: floor lowered 1 → 0 to admit the Strict-as-unlimited
		# preset. ``_sweep_old_sessions`` in janitor.py treats <= 0 as
		# "never sweep" (forever retention), so 0 is now a meaningful
		# sentinel rather than a fatal typo. Pre-v0.13.x the janitor
		# used ``or DEFAULT_RETENTION_DAYS`` and silently fell back to
		# 90 on 0.
		"session_retention_days": 0,
		# v0.13.x: floor lowered 1 → 0. analyze.py's enrich loop now
		# treats cap == 0 as "no cap" (enrich every query) instead of
		# falling back to MAX_QUERIES_ENRICHED_PER_RECORDING.
		"max_queries_per_recording": 0,
		"pyinstrument_sampler_interval_ms": 0.1,
		"min_action_duration_ms": 0,
		"large_duration_threshold_ms": 0,
		"background_job_wait_seconds": 0,
		"slow_query_threshold_ms": 1,
		"slow_hot_path_pct_threshold": 0,
		"slow_hot_path_min_ms": 0,
		"hot_line_high_pct": 0,
		"hot_line_high_min_ms": 0,
		"redundant_doc_threshold": 1,
		"redundant_cache_threshold": 1,
		"redundant_perm_threshold": 1,
		"n_plus_one_min_occurrences": 1,
		"ai_auto_suggest_max": 0,
		"ai_refresh_max_findings": 0,
		"ai_context_tokens": 0,
		# v0.9.0: AI request timeout. Below 10s breaks the LLM round-trip
		# entirely; the ceiling 600s is applied in settings.py:_resolve
		# (we can't enforce it from a floor). Clamping below pairs with
		# the doc's "start at 180 for local LLMs" guidance.
		"ai_request_timeout_seconds": 10,
	}

	def _clamp_numeric_floors(self):
		"""Floor each numeric setting at a safe minimum so a typo
		(e.g. ``-1`` for a sampler interval, ``0`` for session
		retention) doesn't break the analyzer at runtime. Silently
		clamps the operator sees the corrected value on the form
		after save."""
		for fieldname, floor in self._NUMERIC_FLOORS.items():
			current = self.get(fieldname)
			if current is None:
				continue
			try:
				if float(current) < float(floor):
					# setattr instead of self.set so the helper works
					# against a test stub that doesn't subclass Frappe's
					# full Document. setattr matches Document.set's
					# behaviour for non-child-table scalar fields.
					setattr(self, fieldname, floor)
			except (TypeError, ValueError):
				# Non-numeric input let Frappe's field-type validation
				# handle it; nothing useful for us to do here.
				continue

	def _normalize_tracked_apps(self):
		"""Trim whitespace and deduplicate app names, preserving order.

		Saves the admin from pasting ``myapp`` and ``myapp `` (trailing
		space) and getting two rows that both fail to match.
		"""
		if not self.tracked_apps:
			return
		seen = set()
		normalized = []
		for row in self.tracked_apps:
			name = (row.app_name or "").strip()
			if not name or name in seen:
				continue
			seen.add(name)
			row.app_name = name
			normalized.append(row)
		# Rebuild the child-table list preserving order.
		self.tracked_apps = normalized

	def _warn_on_framework_apps_in_tracked(self):
		"""Flash a non-blocking warning when the admin adds a known-framework app
		(frappe / erpnext / hrms / ...) to Tracked Apps.

		Adding framework apps flips the classifier into inclusion mode (framework
		code becomes "user code"), flooding the findings list with noise. The save
		is not blocked (ERPNext contributors may want this); it just warns.
		"""
		if not self.tracked_apps:
			return
		# Local import to avoid a top-level dependency on analyzers/.
		from optimus.analyzers.base import FRAMEWORK_APPS
		offenders = sorted({
			(row.app_name or "").strip()
			for row in self.tracked_apps
			if (row.app_name or "").strip() in FRAMEWORK_APPS
			and (row.app_name or "").strip() != "optimus"
		})
		if not offenders:
			return
		msg = _(
			"<b>Heads up:</b> you added {0} to Tracked Apps. These are "
			"framework/first-party apps. Adding them here flips the filter "
			"into <i>inclusion mode</i>, so their findings will now show up "
			"as <b>actionable</b> instead of in the collapsed Framework "
			"observations section. <br><br>If you want the default behavior "
			"(frappe + erpnext + stock apps treated as framework), <b>remove "
			"these rows and leave the table empty</b>. Only add your own "
			"custom app here if you want to narrow the actionable list to "
			"just that app."
		).format(", ".join(f"<code>{a}</code>" for a in offenders))
		frappe.msgprint(
			msg,
			title=_("Tracked Apps possible misconfiguration"),
			indicator="orange",
		)

	def _refuse_an_unsendable_ai_key(self):
		"""Refuse, on save, an API key the AI calls would refuse to send: for a
		provider that needs a key, one holding a character that is not
		printable ASCII (a space, a smart quote, a no-break space, a control
		character) after its surrounding whitespace is stripped, with the same
		message (``ai_fix._unsendable_key_message``). ``validate`` runs before
		Frappe encrypts the field, so a newly typed key is here in plain text;
		a key left unchanged is Frappe's asterisks, which pass the check. A
		keyless provider ("OpenAI-compatible") never sends such a key, so it
		is not refused there. The key is held only as ``api_key``, a name the
		traceback sanitizers redact, and never put in the message."""
		from optimus.ai_fix import _PROVIDER_DEFAULTS, _key_is_sendable, _unsendable_key_message

		provider = _text(self.get("ai_provider")) or "Anthropic"
		if not _PROVIDER_DEFAULTS.get(provider, {}).get("needs_key", True):
			return
		api_key = self.get("ai_api_key")
		api_key = api_key.strip() if isinstance(api_key, str) else ""
		if not api_key or _key_is_sendable(api_key):
			return
		frappe.throw(_unsendable_key_message())

	def _check_ai_base_url(self):
		"""Warn on refused endpoints; call-time validation prevents the request.

		Throwing here can snapshot a newly typed key in developer mode. Strip
		authority credentials from both sides of the version diff even when AI
		is off or a hosted provider ignores this field.
		"""
		from optimus import ai_fix

		before = self.get_doc_before_save() if hasattr(self, "get_doc_before_save") else None
		if before is not None:
			before.ai_base_url = ai_fix.strip_url_userinfo(before.get("ai_base_url"))
		raw = self.get("ai_base_url")
		self.ai_base_url = ai_fix.strip_url_userinfo(raw)
		if isinstance(raw, str) and raw != self.ai_base_url:
			frappe.msgprint(_(
				"The AI Base URL contained a user name or password. Optimus removed it before "
				"saving so it is not kept in the settings history; use the API Key field."
			), title=_("Optimus"), indicator="orange")
		name = _text(self.get("ai_provider")) or ai_fix._DEFAULT_PROVIDER
		if not self.get("ai_enabled") or (ai_fix._PROVIDER_DEFAULTS.get(name) or {}).get("base_url"):
			return
		problem = ""
		guard = ai_fix._InterruptGuard(base=True)
		try:
			with guard:
				self.ai_base_url = ai_fix.validate_base_url(self.ai_base_url)
		except ai_fix.AiFixError as exc:
			problem = str(exc)
		except Exception:
			problem = _("The AI Base URL could not be checked.")
		if guard.pending():
			raw = None
			raise guard.interrupt()
		if problem:
			frappe.msgprint(_(
				"{0} AI suggestions stay off until this is corrected. Use Test AI connection after saving."
			).format(problem), title=_("Optimus"), indicator="orange")

	def _clear_api_key_on_endpoint_change(self):
		"""A masked password means unchanged. Clearing it also removes __Auth
		during Frappe's normal password save, in the same transaction."""
		before = self.get_doc_before_save() if hasattr(self, "get_doc_before_save") else None
		if before is None:
			return
		if _effective_endpoint(before.get("ai_provider"), before.get("ai_base_url")) == _effective_endpoint(
			self.get("ai_provider"), self.get("ai_base_url"),
		):
			return
		api_key = self.get("ai_api_key")
		if not isinstance(api_key, str) or not api_key or set(api_key) != {"*"}:
			return
		self.ai_api_key = ""
		frappe.msgprint(_(
			"The API key was cleared because the AI provider or Base URL changed. "
			"Enter the API key for the new endpoint if it needs one."
		), title=_("Optimus"), indicator="orange")

	def _warn_key_not_sent_over_http(self):
		from optimus import ai_fix

		if not self.get("ai_enabled") or not self.get("ai_api_key"):
			return
		name = _text(self.get("ai_provider")) or ai_fix._DEFAULT_PROVIDER
		url = (ai_fix._PROVIDER_DEFAULTS.get(name) or {}).get("base_url") or self.ai_base_url
		try:
			url = ai_fix.validate_base_url(url)
		except ai_fix.AiFixError:
			return  # The endpoint warning above already explains the refusal.
		if ai_fix.key_over_http_blocked(url, allow=ai_fix._allow_key_over_http()):
			frappe.msgprint(_(
				"The API key will not be sent over plain http:// to another machine. "
				"Local models such as Ollama need no key. If this endpoint needs a key, use https:// "
				"or explicitly set optimus_ai_allow_key_over_http in the site configuration."
			), title=_("Optimus"), indicator="orange")

	def _warn_on_incomplete_ai_config(self):
		"""Non-blocking warning when AI fix suggestions are enabled but
		the config is incomplete (no model, or no API key for a provider
		that needs one). The feature stays enabled the operator just
		sees a clear hint instead of a cryptic error on first use."""
		if not self.get("ai_enabled"):
			return
		provider = _text(self.get("ai_provider")) or "Anthropic"
		from optimus import ai_fix

		needs_key = ai_fix.provider_needs_key(provider)
		# ai_model can be blank when a hosted default exists for the
		# provider; only "OpenAI-compatible" truly requires it (no
		# default to fall back to). We still nudge if it's blank for the
		# custom provider.
		missing = []
		if provider == "OpenAI-compatible":
			if not _text(self.get("ai_base_url")):
				missing.append(_("Base URL"))
			if not _text(self.get("ai_model")):
				missing.append(_("Model"))
		if needs_key and not _text(self.get("ai_api_key")):
			missing.append(_("API Key"))
		if not missing:
			return
		names = ", ".join(missing)
		if len(missing) == 1:
			body = _(
				"AI Fix Suggestions are enabled but {0} is not set. The "
				"<b>Refresh AI suggestions</b> button will report a configuration "
				"error until you fill it in."
			).format(names)
		else:
			body = _(
				"AI Fix Suggestions are enabled but {0} are not set. The "
				"<b>Refresh AI suggestions</b> button will report a configuration "
				"error until you fill them in."
			).format(names)
		frappe.msgprint(
			body,
			title=_("AI Fix Suggestions incomplete config"),
			indicator="orange",
		)
