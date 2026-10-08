# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""On-demand LLM-suggested fixes for Optimus Findings.

Turns a finding's callsite / source snippet / normalized SQL + EXPLAIN into a
concrete fix by asking a configured LLM. Called from analyze-time
auto-suggest in ``optimus.analyze`` and from the "Refresh AI suggestions"
endpoint ``optimus.api.refill_ai_suggestions``, never from an analyzer, so
the pure-analyzer / frozen-capture invariants are untouched.

Provider-agnostic: two wire formats (Anthropic Messages, OpenAI Chat
Completions) chosen by the ``ai_provider`` Select; ``ai_base_url`` /
``ai_model`` / ``ai_api_key`` are overridable in Optimus Settings so a local
model (Ollama / LM Studio / vLLM) can be used with nothing leaving the box.
``frappe`` is lazy-imported inside each function so the pure prompt / HTTP
helpers are unit-testable without a bench.
"""

from __future__ import annotations

import json
import re
import time
import traceback
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

from optimus import ai_budget, ai_guardrails, ai_prompts, safe_call
from optimus.analyzers.base import humanize_duration_ms


class AiFixError(Exception):
	"""User-facing error from the AI-fix path. The API endpoint converts
	this into ``frappe.throw`` so the message is shown to the operator.
	``status_code`` carries the provider's HTTP status when the error came
	from an HTTP response, so callers can react to it (the temperature retry
	fires only on a 400 or 422). ``kind`` classifies the failure
	(``"config"``, ``"transport"``, ``"timeout"``, ``"bad_response"`` here;
	later releases fill the rest). ``usage`` carries token usage already billed before an empty-response failure.

	The message must never contain the API key: it is shown to the operator
	and written to the Error Log. An HTTP-status error from ``_http_post``
	is the exception: its message carries the provider's reply, so its row
	shows a body-free log text instead (``_LOG_TEXT_ATTR``)."""

	def __init__(
		self,
		message: str = "",
		*,
		status_code: int | None = None,
		kind: str = "unknown",
		usage: dict | None = None,
	):
		super().__init__(message)
		self.status_code = status_code
		self.kind = kind
		# Only billed counts belong on an exception that may reach a log.
		self.usage = {
			key: value for key, value in (usage or {}).items()
			if key in ("prompt_tokens", "completion_tokens", "total_tokens")
			and type(value) is int and value >= 0
		} if usage is not None else None


# Findings that carry enough code / SQL context for the LLM to reason about
# a concrete fix. Infra / frontend / "function not invoked" findings are
# excluded the LLM would only get a title + a couple of numbers.
#
# v0.7.x: Slow Hot Path / Hook Bottleneck / Repeated Hot Frame removed.
# Their AI suggestions are structurally generic (the LLM only sees a
# function name + percentage + line range) and the actionable insight
# already lives on the embedded N+1 / Hot Line / Redundant Call that
# shares the same chain leaf. Skipping these types saves tokens without
# losing diagnostic signal the broader hot-path findings still appear
# in the Findings section with their smoking-gun + drill-down; they
# just no longer carry an LLM-rendered "Suggested fix" block.

# Index-family findings (Missing Index, Full Table Scan, Filesort, Temporary
# Table, Low Filter Ratio) get a deterministic recipe from
# optimus.renderer.fix_recipes instead of an LLM answer, and Framework N+1
# findings point at a loop inside framework code the app cannot change, so
# none of them reaches the LLM. A Hot Line and a Redundant Call are further
# gated per finding by ``llm_gate_note``.
AI_ELIGIBLE_FINDING_TYPES: frozenset[str] = frozenset({
	"N+1 Query",
	"Slow Query",
	"Redundant Call",
	"Hot Line",
})


# Per-provider protocol + sensible defaults. ``ai_base_url`` / ``ai_model``
# from Optimus Settings override these; the "OpenAI-compatible" provider
# REQUIRES both (no hosted default to fall back to).
_PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
	"Anthropic": {
		"context_tokens": 200000,
		"protocol": "anthropic",
		"base_url": "https://api.anthropic.com",
		"model": "claude-sonnet-4-6",
		"needs_key": True,
	},
	"OpenAI": {
		"context_tokens": 128000,
		"protocol": "openai",
		"base_url": "https://api.openai.com/v1",
		"model": "gpt-4.1-mini",
		"needs_key": True,
	},
	"Kimi (Moonshot)": {
		"context_tokens": 128000,
		"protocol": "openai",
		"base_url": "https://api.moonshot.ai/v1",
		"model": "kimi-k2-0905-preview",
		"needs_key": True,
	},
	"DeepSeek": {
		"context_tokens": 64000,
		# DeepSeek's API is OpenAI-compatible, so it reuses the OpenAI wire
		# path. Default to deepseek-chat (V3). deepseek-reasoner (R1) also works
		# and ignores a custom temperature instead of rejecting it, so the
		# temperature retry in _call_openai_chat never needs to fire for it.
		"protocol": "openai",
		"base_url": "https://api.deepseek.com/v1",
		"model": "deepseek-chat",
		"needs_key": True,
	},
	"OpenAI-compatible": {
		"context_tokens": 4096,
		"protocol": "openai",
		"base_url": "",
		"model": "",
		# Local endpoints (Ollama / LM Studio / vLLM) usually need no key.
		"needs_key": False,
	},
	# v0.14.x: Aerele-managed AI provider. Architecturally identical to
	# the Anthropic / OpenAI entries just a hosted endpoint + an API
	# key the customer pastes into ``ai_api_key``. The token balance,
	# pre-call validation and metering all live on Aerele's separate
	# Frappe site (the URL below); Optimus is a dumb client. Aerele's
	# proxy fronts an OpenAI-shaped wire so ``_call_openai_chat`` routes
	# correctly without a new protocol handler. See
	# ``docs/AI-FIXING.md`` §10.
	#
	# TEMPORARILY DISABLED until Aerele billing + the managed LLM gateway
	# are production-ready. To re-enable: uncomment this entry AND add
	# "Aerele" back to the ai_provider Select options (plus its two
	# descriptions) in optimus_settings.json. The _aerele_call_metadata
	# wiring further down is left intact, ready to use.
	# "Aerele": {
	# 	"context_tokens": 200000,
	# 	"protocol": "openai",
	# 	"base_url": "https://api.aerele.in/optimus/v1",
	# 	"model": "claude-sonnet-4-6",  # Aerele picks the upstream model
	# 	"needs_key": True,
	# },
}
_DEFAULT_PROVIDER = "Anthropic"
_DEFAULT_CONTEXT_TOKENS = 4096  # a provider dict without context_tokens gets the tightest window
# Silent prompt truncation is a small-window local-server failure (Ollama num_ctx);
# hosted windows are far larger than any Optimus prompt, so they are not checked.
_TRUNCATION_CHECK_MAX_CONTEXT = 32768
_WINDOW_STEPS = (80, 48, 32, 20, 12, 8)  # source-window sizes tried, largest first

def provider_needs_key(name: str) -> bool:
	"""True when the provider named ``name`` (an ``ai_provider`` Select value) needs an API key,
	read from ``_PROVIDER_DEFAULTS[name]["needs_key"]`` so adding a provider stays data-only. An
	unknown or blank name returns True, so a settings check errs toward asking for a key."""
	defaults = _PROVIDER_DEFAULTS.get((name or "").strip())
	if defaults is None:
		return True
	return bool(defaults.get("needs_key", True))


# Fallback timeout when settings can't be read (pure-pytest path with no
# bench, or a settings cache miss during early bootstrap). v0.9.0+ the live
# value comes from cfg.ai_request_timeout_seconds (clamped 10–600s).
_HTTP_TIMEOUT = 60            # seconds; one shot, no retries
_MAX_OUTPUT_TOKENS = 2000
_SOURCE_LINES_BEFORE = 24     # how much code window the caller should gather
_SOURCE_LINES_AFTER = 24
_MAX_SOURCE_WINDOW_LINES = 80
_MAX_QUERY_CHARS = 2400
_MAX_USER_CONTENT_CHARS = 18000
# Low temperature we want the model to stick to the code it was shown, not
# get "creative". OpenAI's o-series reasoning models reject a non-default
# temperature, so it's omitted for those (see `_is_reasoning_model`).
_TEMPERATURE = 0.1

_ANTHROPIC_VERSION = "2023-06-01"

# Compact "what this finding type means + how it's usually fixed in Frappe"
# line, injected into the user message. Keeps the system prompt general and
# gives the model a strong, type-specific starting point.
_FINDING_TYPE_HINTS = ai_prompts.FINDING_TYPE_HINTS


def _finding_type_hint(ftype):
	"""Per-finding-type hint for the LLM prompt (dialect-neutral: the EXPLAIN
	types whose hints differed per dialect no longer reach the LLM)."""
	return ai_prompts.FINDING_TYPE_HINTS.get(ftype)


_MAX_STEPS_ACTIONS = 60
_MAX_STEPS_USER_CHARS = 8000





# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# v0.6.x: per-section "use the LLM for X" toggle → the config attribute.
_AI_SECTION_FLAGS = {
	"findings": "ai_suggest_findings",
	"humanize": "ai_humanize_steps",
}


def is_finding_type_excluded(finding_type: str | None) -> bool:
	"""Return True when ``finding_type`` is in ``cfg.ai_excluded_finding_types``.

	Exact case-sensitive match. Empty / unknown type, or any read error (no
	bench, settings cache wedged), returns False so an inert exclude never
	blocks by accident.
	"""
	if not finding_type or not isinstance(finding_type, str):
		return False
	try:
		from optimus.settings import get_config
		excluded = get_config().ai_excluded_finding_types
	except Exception:
		return False
	return finding_type in (excluded or ())


_INDEX_TYPE_NOTE = (
	"Index advice is built by Optimus from the DocType metadata, without the AI. "
	"See the recipe on this finding in the report."
)
_FRAMEWORK_N1_NOTE = (
	"A Framework N+1 finding points at a loop inside framework code, which your app "
	"cannot change, so Optimus does not ask the AI about it."
)
_NOT_ELIGIBLE_NOTE = "This finding type does not carry enough code or SQL context for an AI suggestion."


def _app_scope() -> tuple[tuple[str, ...], frozenset[str] | None]:
	"""The site's Tracked Apps and installed apps, the same inputs the report
	uses to tell your code from framework code. Ordinary settings failures fall
	back to the default scope; job timeouts propagate."""
	from optimus.analyzers.base import installed_apps_allowlist
	from optimus.settings import get_config

	tracked = safe_call.best_effort(lambda: tuple(getattr(get_config(), "tracked_apps", ()) or ()), ())
	return tracked, safe_call.best_effort(installed_apps_allowlist, None)


def gate_input(row) -> dict:
	"""The dict ``llm_gate_note`` reads, built from an Optimus Finding row (or
	any object with its attributes)."""
	return {
		"finding_type": getattr(row, "finding_type", "") or "",
		"technical_detail_json": getattr(row, "technical_detail_json", None) or "{}",
	}


def llm_gate_note(finding: dict) -> str | None:
	"""None when the LLM may be asked about ``finding``; otherwise why not, as a
	sentence the report can show. The single eligibility chokepoint for
	analyze, the refresh backfill and ``suggest_fix``. Accepts a render dict
	(``technical_detail``), a row-shaped dict (``technical_detail_json``) or
	a ``gate_input(row)`` dict."""
	ftype = finding.get("finding_type") or ""
	from optimus.renderer import fix_recipes

	if ftype not in AI_ELIGIBLE_FINDING_TYPES:
		if ftype in fix_recipes.INDEX_FINDING_TYPES:
			return _INDEX_TYPE_NOTE
		if ftype == "Framework N+1":
			return _FRAMEWORK_N1_NOTE
		return _NOT_ELIGIBLE_NOTE
	if ftype == "Redundant Call":
		if fix_recipes.analyzed_before_callsite_fix(finding):
			return fix_recipes.PRE_L5_REDUNDANT_CALL_NOTE
		return None
	if ftype != "Hot Line":
		return None
	tracked, installed = _app_scope()
	return fix_recipes.hot_line_gate(finding, tracked_apps=tracked, installed_apps=installed)


def _resolve_timeout_seconds() -> int:
	"""Return the configured HTTP timeout (seconds) for outbound LLM calls,
	clamped to ``[10, 600]`` and falling back to :data:`_HTTP_TIMEOUT` when
	settings can't be read.
	"""
	try:
		from optimus.settings import get_config
		v = get_config().ai_request_timeout_seconds
		return max(10, min(600, int(v or _HTTP_TIMEOUT)))
	except Exception:
		return _HTTP_TIMEOUT


def is_available(section: str | None = None) -> bool:
	"""True when AI fix suggestions are on and minimally configured:
	``ai_enabled`` set, a model resolvable for the chosen provider and an API
	key present unless the provider needs none (local endpoints).

	When ``section`` is ``"findings"`` / ``"humanize"``, also
	requires the matching per-section toggle. Fails soft: an unknown ``section``
	or an unreadable config attr does not block once ``ai_enabled`` has passed."""
	try:
		from optimus.settings import get_config
		cfg = get_config()
	except Exception:
		return False
	if not getattr(cfg, "ai_enabled", False):
		return False
	try:
		provider = _resolve_provider()
	except AiFixError:
		return False
	if not provider.get("model") or not provider.get("base_url"):
		return False
	if provider.get("needs_key") and not provider.get("has_key"):
		return False
	if section:
		flag = _AI_SECTION_FLAGS.get(section)
		if flag and not getattr(cfg, flag, True):
			return False
	return True


def _resolve_display_threshold_ms() -> float:
	"""The configured "render durations in seconds above (ms)" threshold, so the
	durations in AI-fix context read in the same unit as the report. Delegates to
	the single resolver in settings (lazy import keeps this module's pure prompt /
	HTTP layer importable without frappe)."""
	from optimus.settings import display_threshold_ms
	return display_threshold_ms()


def suggest_fix(finding: dict, *, timeout: int | None = None) -> dict:
	"""Ask the configured LLM for a fix for ``finding``.

	Returns ``{suggestion, model, provider, generated_at, source_available,
	prompt_version, guardrail, finish_reason}`` plus ``tokens`` when the provider
	reported usage. ``timeout`` caps the whole first-call-plus-re-ask budget
	(default: the configured request timeout). Raises ``AiFixError``."""
	if is_finding_type_excluded(finding.get("finding_type")):
		raise AiFixError("excluded by ai_excluded_finding_types", kind="config")
	gate_note = llm_gate_note(finding)
	if gate_note:
		from frappe import _

		raise AiFixError(
			_("No AI suggestion for this finding: {0}").format(gate_note), kind="not_eligible",
		)
	provider = _provider_config()
	_require_configured(provider)
	ctx = _context_tokens(provider)
	system, messages, shown = _build_fix_request(
		finding, threshold_ms=_resolve_display_threshold_ms(), context_tokens=ctx, out_tokens=_output_tokens(provider)
	)
	_check_context_fits(system, ctx, messages=messages, out_tokens=_output_tokens(provider))
	usage: dict = {}
	text, guardrail, finish = _complete_with_guardrails(
		provider,
		system,
		messages,
		shown_lines=shown,
		usage=usage,
		metadata=_aerele_call_metadata(provider, finding.get("finding_type")),
		started_at=time.monotonic(),
		timeout=int(timeout or _resolve_timeout_seconds()),
	)
	result = {
		"suggestion": text,
		"model": provider["model"],
		"provider": provider["name"],
		"generated_at": datetime.now(timezone.utc).isoformat(),
		"source_available": _had_concrete_context(finding),
		"prompt_version": ai_prompts.PROMPT_VERSION,
		"guardrail": guardrail,
		"finish_reason": finish,
	}
	if usage.get("total_tokens"):
		result["tokens"] = usage
	return result


def humanize_steps(actions: list[dict], *, session_title: str | None = None, usage_out: dict | None = None) -> str:
	from frappe import _

	if not actions:
		raise AiFixError(_("There are no recorded actions to summarise."), kind="config")
	provider = _provider_config()
	_require_configured(provider)
	system, messages = _build_steps_messages(
		actions, session_title, threshold_ms=_resolve_display_threshold_ms(), context_tokens=_context_tokens(provider)
	)
	_check_context_fits(system, _context_tokens(provider), messages=messages, out_tokens=_output_tokens(provider))
	text = _dispatch_call(
		provider, system, messages, usage_out=usage_out,
		metadata=_aerele_call_metadata(provider, "Steps to Reproduce"),
	)
	text = (text or "").strip()
	if not text:
		raise AiFixError(_("The AI provider returned an empty response."), kind="bad_response")
	return text




def _had_concrete_context(finding: dict) -> bool:
	"""True when the LLM was given something concrete to reason about a
	source window / snippet, or a SQL statement. ``False`` means it only had
	the finding's title + numbers, so the suggestion is necessarily
	directional (and the UI should say so)."""
	detail = (finding.get("technical_detail") or {})
	callsite = detail.get("callsite") or {}
	if finding.get("source_window") or callsite.get("source_snippet"):
		return True
	if finding.get("phase2_hotline"):
		return True  # has the per-line numbers even if the source couldn't be read
	return bool(detail.get("normalized_query") or detail.get("example_queries"))


def test_connection() -> dict:
	try:
		provider = _provider_config()
	except AiFixError as e:
		return {"ok": False, "message": str(e), "model": ""}
	if not provider.get("model") or not provider.get("base_url"):
		return {"ok": False, "message": "Provider/model/base URL not fully configured.", "model": provider.get("model") or ""}
	messages = [{"role": "user", "content": "Reply with exactly: OK"}]
	usage: dict = {}
	try:
		text = _dispatch_call(
			provider, "You are a connectivity probe. Reply with exactly: OK", messages, usage_out=usage, max_tokens=16
		)
	except AiFixError as e:
		return {"ok": False, "message": str(e), "model": provider["model"]}
	_toks = usage.get("total_tokens") or 0
	return {
		"ok": True,
		"message": f"Reachable. Model replied: {(text or '').strip()[:60]!r}" + (f" ({_toks} tokens)" if _toks else ""),
		"model": provider["model"],
	}


# ---------------------------------------------------------------------------
# Config / provider resolution
# ---------------------------------------------------------------------------

def _current_key_or_empty() -> str:
	"""The stored ``Optimus Settings.ai_api_key``, stripped, or ``""`` when it
	is unset or cannot be decrypted. Never validates, so ``log_ai_failure``
	can scrub an echoed key even when ``_get_api_key`` would reject it.

	SECURITY: the value only ever lives in a local named ``api_key`` (Frappe's
	traceback sanitizer and Sentry's denylist both redact that name) and in
	an ``_ApiKeyAuth``. Never put it in a dict, a header dict, a request body
	or an exception message.

	Any ``Exception`` from the decryption answers ``""``. Two kinds of
	interrupt still leave, both raised after the ``try`` so the decrypt
	frames they interrupted (Fernet's and ``cstr``'s locals hold the key
	bytes) never travel with them:

	- an RQ job timeout (the job must stop, and answering "" would send the
	  request unauthenticated) leaves as a fresh instance of its type, so
	  those frames never reach ``execute_job``'s log;
	- an interrupt that is not an ``Exception`` (``SystemExit`` from a
	  gunicorn worker timeout, ``KeyboardInterrupt``, a gevent ``Timeout``)
	  leaves as the SAME instance (gevent matches its timeout by identity),
	  with its traceback, ``__context__`` and ``__cause__`` cleared, as in
	  ``_http_post``: Sentry's WSGI middleware would ship those frames'
	  locals. It leaves unchained when this function is not itself called
	  while an exception is being handled."""
	guard = _InterruptGuard(base=True)
	try:
		with guard:
			from frappe.utils.password import get_decrypted_password

			api_key = get_decrypted_password(
				"Optimus Settings", "Optimus Settings", "ai_api_key",
				raise_exception=False,
			) or ""
	except Exception:
		return ""
	if guard.pending():
		raise guard.interrupt()
	return api_key.strip() if isinstance(api_key, str) else ""


def _get_api_key(needs_key: bool = True) -> str:
	"""The API key to send, stripped of surrounding whitespace (a pasted
	trailing newline), or ``""`` when none is stored.

	For a provider that needs a key (``needs_key``), raises
	``AiFixError(kind="config")`` before any HTTP call when the key holds a
	character that cannot be sent in an HTTP header or is not plain ASCII:
	every character must be printable ASCII (``!`` to ``~``). For a provider
	that needs none (Ollama, LM Studio, vLLM behind "OpenAI-compatible"),
	such a key is not an error: ``""`` is returned and the request goes
	without a key. A sendable key is still returned for it (a router such as
	OpenRouter needs one). That
	rejects a pasted smart quote, a no-break space or soft hyphen, a C1
	control character, an internal space, and a control character such as a
	newline, tab or NUL (which would otherwise reach ``requests`` /
	``http.client`` and surface the key in a ``ValueError`` message or in
	``putheader`` locals).

	The check runs outside any ``try``, so the ``AiFixError`` is raised with
	no exception being handled and has no ``__context__``; ``from None`` also
	keeps ``__suppress_context__`` explicit."""
	api_key = _current_key_or_empty()
	if not api_key:
		return ""
	if not _key_is_sendable(api_key):
		if not needs_key:
			return ""
		raise AiFixError(_unsendable_key_message(), kind="config") from None
	return api_key


def _key_is_sendable(api_key: str) -> bool:
	"""True when every character of ``api_key`` (already stripped) is
	printable ASCII (``!`` to ``~``): what ``_get_api_key`` sends, and what
	Optimus Settings accepts on save for a provider that needs a key."""
	return all("\x21" <= ch <= "\x7e" for ch in api_key)


def _unsendable_key_message() -> str:
	"""The one message for a key ``_key_is_sendable`` refuses, raised by
	``_get_api_key`` and by Optimus Settings' save validation. It never
	holds the key."""
	from frappe import _

	return _("The AI API key in Optimus Settings contains a character that cannot be sent in an HTTP header or is not plain ASCII (often a pasted smart quote, a stray space, a no-break space, or a control character such as a newline or tab). Paste the key again.")


class _ApiKeyAuth(requests.auth.AuthBase):
	"""Attaches the API key header at send time, so no header dict of
	Optimus's ever holds the key: only the HTTP library's own prepared
	request does, while it is sent. ``repr``/``str`` are masked because
	Frappe's with-context tracebacks, RQ failure logs and Sentry all print
	frame locals by repr."""

	__slots__ = ("_header", "_value", "_prefix")

	def __init__(self, header: str, api_key: str, prefix: str = ""):
		# The parameter holds the key while this runs: named api_key, a name
		# Frappe's traceback sanitizer and Sentry's denylist redact.
		self._header = header
		self._value = api_key
		self._prefix = prefix

	def __call__(self, r):
		r.headers[self._header] = self._prefix + self._value
		return r

	def _scrub_literals(self) -> tuple[str, ...]:
		"""The key this object sends, raw, JSON-escaped and repr-escaped (``_key_literals``):
		the key the request really carried, even if Optimus Settings holds a
		new one by the time the reply is read. Pass the result straight into
		``scrub_secrets(..., literals=...)``; never bind it to a local."""
		return _key_literals(self._value)

	def __repr__(self) -> str:
		return f"<_ApiKeyAuth {self._header}: ********>"

	__str__ = __repr__


def _key_literals(api_key) -> tuple[str, ...]:
	"""``api_key`` as it can appear in text, for ``scrub_secrets(...,
	literals=...)``: raw, JSON-escaped (how a JSON body or a JSON-encoded
	message holds it) and repr-escaped (a frame local printed by repr), the
	one helper ``redaction.key_literals``. ``()`` when there is no key. Pass
	the result straight into that call: the key may only sit in a local named
	``api_key`` (or ``secret`` inside ``scrub_secrets``)."""
	from optimus.redaction import key_literals

	return key_literals(api_key)


def _in_flight_literals(auth) -> tuple[str, ...]:
	"""The literals of the key a request was sent with (``auth`` is the
	``_ApiKeyAuth`` it used), or ``()`` when it carried none."""
	return auth._scrub_literals() if isinstance(auth, _ApiKeyAuth) else ()


def _scrub_literals_for(auth) -> tuple[str, ...]:
	"""What a provider reply (or a failure's log text) is scrubbed of, for
	``scrub_secrets(..., literals=...)``, raw, JSON-escaped and repr-escaped: the key the
	request was sent with when ``auth`` is the ``_ApiKeyAuth`` it used (the
	only key the provider ever received, even if Optimus Settings holds a new
	one by now), with no database read; otherwise the key stored in Optimus
	Settings, read again (a SELECT on ``__Auth``). So a call reads the stored
	key once, at its entry point, unless its request carried no key. Nothing
	is cached across calls. Pass the result straight into that call, or bind
	it only to a local named ``api_key``. Call this BEFORE binding the reply
	(see ``_response_detail``): the read is a database query."""
	if isinstance(auth, _ApiKeyAuth):
		return _in_flight_literals(auth)
	return _key_literals(_current_key_or_empty())


def _resolve_provider() -> dict:
	"""``_provider_config()`` plus ``has_key``: whether a key is stored (one
	read of its cached, non-decrypting Singles value). For ``is_available``; a call that
	sends a request uses ``_provider_config()`` and reads the key once
	itself (``_get_api_key``).

	SECURITY: the dict carries ``has_key`` (bool), never the key itself: it is
	a local in most AI frames, and Frappe's traceback sanitizer
	(``frappe.utils._get_traceback_sanitizer``) only redacts a dict key named
	exactly ``password``, ``passwd``, ``secret``, ``token``, ``key`` or
	``pwd``; ``api_key`` is not one of them.
	"""
	provider = _provider_config()
	provider["has_key"] = False
	guard = _InterruptGuard(base=True)
	try:
		with guard:
			import frappe

			secret = frappe.db.get_single_value("Optimus Settings", "ai_api_key")
			provider["has_key"] = isinstance(secret, str) and bool(secret.strip())
	except Exception:
		pass
	if guard.pending():
		raise guard.interrupt()
	return provider


def _provider_config() -> dict:
	"""Resolve the active provider config: protocol, base_url, model,
	needs_key and the provider display name, without reading the key. Raises
	``AiFixError`` on an unknown provider or a custom provider missing its
	required base_url/model."""
	from optimus.settings import get_config
	cfg = get_config()
	name = (getattr(cfg, "ai_provider", "") or _DEFAULT_PROVIDER).strip()
	if name not in _PROVIDER_DEFAULTS:
		raise AiFixError(f"Unknown AI provider {name!r}. Pick one in Optimus Settings.")

	defaults = _PROVIDER_DEFAULTS[name]
	# The Base URL override applies ONLY to bring-your-own providers (those
	# with no built-in default endpoint i.e. "OpenAI-compatible"). Hosted
	# providers (Anthropic / OpenAI / Kimi / DeepSeek) ALWAYS use their default: the
	# Settings field is hidden for them, so a previously-stored value must not
	# silently override and route calls to a dead host (that stale-value trap
	# caused a ConnectionError after the field was hidden for hosted providers).
	if defaults["base_url"]:
		base_url = defaults["base_url"]
	else:
		base_url = (getattr(cfg, "ai_base_url", "") or "").strip().rstrip("/")
	model = (getattr(cfg, "ai_model", "") or "").strip() or defaults["model"]
	# The Settings override applies only to bring-your-own endpoints (no built-in
	# base_url), like the Base URL: the field is hidden for hosted providers.
	ctx_override = 0 if defaults["base_url"] else int(getattr(cfg, "ai_context_tokens", 0) or 0)

	# A key may be set for any provider: some OpenAI-compatible routers
	# (OpenRouter, Together, Groq) need one even though local endpoints don't.
	return {
		"name": name,
		"protocol": defaults["protocol"],
		"base_url": base_url,
		"model": model,
		"needs_key": bool(defaults["needs_key"]),
		"context_tokens": ctx_override if ctx_override > 0 else int(defaults["context_tokens"]),
		"max_output_tokens": defaults.get("max_output_tokens"),
	}


# ---------------------------------------------------------------------------
# Output guardrail never let an "index a metadata column" recommendation
# through, even if the model ignored the system prompt. Frappe metadata
# columns (`name`, `idx`, `parent`, `creation`, `modified`, `docstatus`, …)
# are written on every save (or already indexed), so indexing them is a
# write-cost trap; the profiler never suggests it anywhere including here.
# ---------------------------------------------------------------------------

# "add an index on <col>", "Search Index … <col>", "index the <col> column",
# "ADD INDEX (`<col>`)" captures the column token that follows the
# index-action phrase, skipping connector words ("on", "the", …). The hit is
# discarded if it's negated ("do NOT index …") see `_NEGATION_RE`.
_INDEX_ADVICE_RE = re.compile(
	r"(?:add\s+(?:an?\s+)?index|search\s+index|index)\b"
	r"[\s(]*(?:(?:on|the|a|an|for|to|of|column|field)\s+)*"
	r"[`'\"]?(?P<col>[A-Za-z_][\w]*)",
	re.IGNORECASE,
)
_NEGATION_RE = re.compile(r"(?:not|n['’]t|never|avoid|without|no need to|don['’]t)\W*$", re.IGNORECASE)


def _metadata_columns() -> frozenset:
	"""The Frappe standard-metadata column set, from the analyzer base
	module (single source of truth). Empty set if unimportable the
	guardrail then simply does nothing."""
	try:
		from optimus.analyzers.base import FRAPPE_METADATA_COLUMNS
		return FRAPPE_METADATA_COLUMNS
	except Exception:
		return frozenset()


# ---------------------------------------------------------------------------
# Output guardrail: raw `frappe.db.sql(...)` in suggested fix code
# ---------------------------------------------------------------------------
# The system prompt at the top of this module tells the LLM "never hand-built
# SQL strings" and lists ``frappe.get_all`` / ``frappe.get_list`` /
# ``frappe.db.get_value`` / ``frappe.db.get_values`` / ``frappe.qb`` as the
# idiomatic alternatives. The few-shot examples reinforce that. But a
# sufficiently confident model still occasionally leaks raw SQL into its
# proposed fix code and the system-prompt instruction alone is a soft
# nudge with no backstop.
#
# This guardrail mirrors ``_flag_metadata_column_index_advice``: detect the
# anti-pattern in the LLM's output, append a clearly-marked profiler note,
# never rewrite (markdown is fragile). The note is advisory, not blocking
# a fix that legitimately needs raw SQL (DDL, vendor-specific MariaDB
# extensions) can be acted on with the operator's judgement.

# ``frappe.db.sql(…, "SELECT …"…)``. The literal can be a regular string,
# f-string, or raw string; the verb that follows is case-insensitive. The
# verbs covered are the ones a model is most likely to suggest as a "fix"
# (DDL like CREATE / ALTER is intentionally outside the scope those are
# legit administrative paths and the prompt already rarely produces them).
_RAW_SQL_IN_FIX_RE = re.compile(
	# ``[a-z]{0,2}`` allows any string prefix (f / r / b / rb / br / fr …);
	# ``["\']{1,3}`` covers single- AND triple-quoted literals (the common shape
	# for a multi-line "fix" query). ``WITH`` catches CTE-led SELECTs.
	r'frappe\.db\.sql\s*\(\s*[a-z]{0,2}["\']{1,3}\s*'
	r'(?:WITH|SELECT|INSERT|UPDATE|DELETE|REPLACE)\b',
	re.IGNORECASE,
)

# Multi-line opener: ``frappe.db.sql("""`` (triple-quoted query whose verb is on
# a later line). A multi-line frappe.db.sql is essentially always a hand-built
# query, so flag the opener regardless of the (off-line) verb.
_RAW_SQL_OPENER_RE = re.compile(
	r'frappe\.db\.sql\s*\(\s*[a-z]{0,2}(?:"""|\'\'\')',
	re.IGNORECASE,
)

# Markdown code-fence detector. Group 1 captures the info-string
# (``diff`` / ``python`` / ``py`` / empty for un-tagged fences).
_CODE_FENCE_RE = re.compile(r'^```(\w*)\s*$')


def _flag_raw_sql_in_fix(text: str) -> str:
	"""If the model's proposed fix contains a raw ``frappe.db.sql(...)`` with a
	SELECT / INSERT / UPDATE / DELETE / REPLACE literal, append a correction
	note (never rewrites); returns the text unchanged when clean.

	Scope: only inside markdown code fences (prose mentions are ignored); inside
	a ``diff`` block only addition (``+``) lines count (removal lines are the
	before-code). Only ``frappe.db.sql`` is detected; DDL verbs (CREATE / ALTER /
	DROP) are excluded since raw DDL is sometimes the right answer.
	"""
	if not text:
		return text

	flagged = False
	in_fence = False
	fence_kind = ""
	for line in text.splitlines():
		fence_match = _CODE_FENCE_RE.match(line.strip())
		if fence_match:
			if not in_fence:
				in_fence = True
				fence_kind = (fence_match.group(1) or "").lower()
			else:
				in_fence = False
				fence_kind = ""
			continue
		if not in_fence:
			continue

		# Inside a code block. Diff blocks restrict scanning to addition
		# lines; non-diff blocks scan every line.
		if fence_kind == "diff":
			if not line.startswith("+") or line.startswith("+++"):
				continue
			# Strip the leading "+" so the regex sees actual code, not
			# the diff-marker prefix.
			line_to_scan = line[1:]
		else:
			line_to_scan = line

		# Two detectors: the verb-anchored one (single-line ``frappe.db.sql("SELECT
		# …")``) and a multi-line OPENER (``frappe.db.sql("""`` with the SQL verb
		# on a following line the common multi-line shape this line-by-line scan
		# would otherwise miss).
		if _RAW_SQL_IN_FIX_RE.search(line_to_scan) or _RAW_SQL_OPENER_RE.search(line_to_scan):
			flagged = True
			break

	if not flagged:
		return text

	return text.rstrip() + (
		"\n\n> **Profiler note:** the fix above includes a raw "
		"`frappe.db.sql(\"SELECT …\")` call. The recommended Frappe pattern "
		"is `frappe.get_all` / `frappe.get_list` / `frappe.db.get_value` / "
		"`frappe.db.get_values` (Document API for typical reads) or "
		"`frappe.qb` (query builder for joins / aggregations / dynamic "
		"conditions). Use raw SQL only when none of those API surfaces fit "
		"(rare e.g. DDL, vendor-specific MariaDB extensions)."
	)


def _flag_metadata_column_index_advice(text: str) -> str:
	"""If the model recommended indexing a Frappe metadata column, append a
	correction note. We don't rewrite the body (markdown is fragile) we
	add a clearly-marked profiler note so the reader doesn't act on it."""
	meta = _metadata_columns()
	if not meta or not text:
		return text
	hits = []
	for m in _INDEX_ADVICE_RE.finditer(text):
		col = m.group("col").strip("`'\"() ").lower()
		if col not in meta or col in hits:
			continue
		# Skip negated mentions ("do NOT index `modified`") no correction needed.
		if _NEGATION_RE.search(text[max(0, m.start() - 16):m.start()]):
			continue
		hits.append(col)
	if not hits:
		return text
	cols = ", ".join(f"`{c}`" for c in hits)
	plural = len(hits) > 1
	return text.rstrip() + (
		"\n\n> **Profiler note:** disregard any suggestion above to index "
		+ cols
		+ (" these are Frappe framework-managed columns" if plural
		   else " that is a Frappe framework-managed column")
		+ " (Frappe writes "
		+ ("them" if plural else "it")
		+ " on every save, or "
		+ ("they're" if plural else "it's")
		+ " already indexed). Index a business column from the WHERE / JOIN "
		"instead, or change the query shape."
	)


# ---------------------------------------------------------------------------
# Prompt construction (pure)
# ---------------------------------------------------------------------------

def _truncate(text: Any, limit: int) -> str:
	s = "" if text is None else str(text)
	if len(s) <= limit:
		return s
	return s[:limit] + "\n…(truncated)"


def _build_steps_messages(
	actions: list[dict], session_title: str | None, *, threshold_ms: float = 1000.0,
	context_tokens: int = _DEFAULT_CONTEXT_TOKENS,
) -> tuple[str, list[dict]]:
	lines: list[str] = []
	title = (str(session_title).strip() if session_title else "")
	if title:
		lines.append(f"Session title: {title}")
		lines.append("")
	lines.append("Recorded actions, in order:")
	for i, a in enumerate(actions[:_MAX_STEPS_ACTIONS], 1):
		label = (a.get("label") or "").strip() or "(unnamed action)"
		bits: list[str] = []
		cmd = (a.get("cmd") or "").strip()
		if cmd:
			bits.append(f"cmd={cmd}")
		else:
			endpoint = " ".join(p for p in ((a.get("method") or "").strip(), (a.get("path") or "").strip()) if p)
			if endpoint:
				bits.append(endpoint)
		doctype = (a.get("doctype") or "").strip()
		if doctype:
			bits.append(f"doctype={doctype}")
		dur = a.get("duration_ms")
		if dur:
			try:
				bits.append(humanize_duration_ms(float(dur), threshold_ms=threshold_ms))
			except (TypeError, ValueError):
				pass
		suffix = f"  ({'; '.join(bits)})" if bits else ""
		lines.append(f"{i}. {label}{suffix}")
	extra = len(actions) - _MAX_STEPS_ACTIONS
	if extra > 0:
		lines.append(f"... and {extra} more action(s).")
	system = ai_prompts.STEPS_SYSTEM_PROMPT
	limit = min(
		_MAX_STEPS_USER_CHARS,
		ai_budget.user_char_budget(context_tokens, system, out_tokens=ai_budget.output_tokens(context_tokens)),
	)
	text = "\n".join(lines)
	if ai_budget.text_size(text) > limit - 60:
		text = ai_budget.clip(text, limit - 80) + "\n…(truncated)"
	content = ai_budget.data_block("actions", text)
	return system, [{"role": "user", "content": content}]




def _build_messages(
	finding: dict, *, threshold_ms: float = 1000.0, context_tokens: int = 200000
) -> tuple[str, list[dict]]:
	"""``(system, [user_message])`` for ``finding``; see ``_build_fix_request``."""
	system, messages, _shown = _build_fix_request(finding, threshold_ms=threshold_ms, context_tokens=context_tokens)
	return system, messages


_REASONING_MODEL_RE = re.compile(r"^o[0-9]")  # OpenAI o1/o3/o4… reject `temperature`


def _is_reasoning_model(model: str) -> bool:
	return bool(_REASONING_MODEL_RE.match((model or "").strip().lower()))


# ---------------------------------------------------------------------------
# HTTP layer (uses `requests`; `frappe` only for best-effort logging)
# ---------------------------------------------------------------------------

_LOGGED_ATTR = "_optimus_ai_logged"
# The name of the Error Log row an exception was logged in (_mark_logged), so
# a later log_ai_failure for it adds its context to that row.
_LOGGED_ROW_ATTR = "_optimus_ai_log_row"
# The body-free text an HTTP-status AiFixError from _http_post is logged with
# (see _exception_text): its message carries the provider's reply.
_LOG_TEXT_ATTR = "_optimus_log_text"


def log_ai_failure(
	title: str,
	exc: BaseException | None = None,
	*,
	session_uuid: str | None = None,
	docname: str | None = None,
	auth: requests.auth.AuthBase | None = None,
	marks: BaseException | None = None,
	**context,
) -> bool:
	"""Write one Error Log row for an AI-surface failure. This is the ONLY
	function on the AI surface allowed to call ``frappe.log_error``
	(``test_ai_log_audit.py`` enforces it).

	Call it OUTSIDE any ``except`` block: record the exception in the
	handler and log after the ``try``. ``frappe.log_error`` calls Sentry's
	``capture_exception``, which ships the ACTIVE exception's frame locals
	even when a message is passed (the audit enforces this too).

	The message is explicit: ``title``, the session, ``context`` as ``k=v``
	lines and the plain traceback of ``exc`` (code lines only: no frame
	locals, no exception chain; for an HTTP-status error from ``_http_post``
	the exception line holds its body-free log text, not its message: see
	``_exception_text``), passed through
	``redaction.scrub_secrets`` with the key as a literal (``auth``, the
	``_ApiKeyAuth`` a failed request was sent with, when the HTTP layer logs
	it; else the stored key, read again: ``_scrub_literals_for``). Frappe's own
	with-context traceback prints every frame's locals, which is how the API
	key and the prompt reached the Error Log before this fix.

	- ``reference_doctype`` / ``reference_name`` point at the Optimus Session:
	  ``docname`` when the caller has it (no lookup), else the session that
	  ``session_uuid`` resolves to.
	- The row is inserted directly, in the current transaction. On MariaDB
	  Error Log is a MyISAM table, so the row survives any rollback. On a
	  transactional engine (Postgres), if that transaction is rolled back
	  later (a ``frappe.throw`` in a web request, a failing background job),
	  a ``frappe.db.after_rollback`` callback queues the same scrubbed row
	  again (see ``_requeue_if_rolled_back``).
	- If scrubbing fails, the row keeps only the title and the error type:
	  an unscrubbed message is never written.
	- An exception is logged at most once: the HTTP layer logs its own
	  failures, so a caller that logs the same ``AiFixError`` again writes
	  no second row. Its title and ``k=v`` lines (and the session), scrubbed
	  like the message, are appended to the row already written instead
	  (``_append_context``: ``frappe.db.set_value`` on that row's ``error``),
	  so the caller's context (the finding, the step) is not lost. Only a
	  row that was written marks the exception, with the row's name.
	  ``marks`` is another exception the written row stands for (the HTTP
	  layer's ``AiFixError``, logged before it is raised).
	- Returns True once ``frappe.log_error`` has returned, else False
	  (already logged, or the write raised). A write that raised leaves one
	  error-level line with the error type in the ``optimus`` log
	  (``_note_unwritten_row``). It raises also when a hook after the insert
	  fails: the row is then written but ``exc`` stays unmarked, so a caller
	  that logs it again writes a second row.
	- Never raises, except an RQ job timeout (the job must still stop),
	  which leaves as a fresh instance with no chain. That includes a
	  timeout passed in as ``exc``: callers catch it with ``except
	  Exception`` (rq's ``JobTimeoutException`` is one), so its row is
	  written and it is then raised again, fresh, stopping the AI loop.
	"""
	logged = False
	failure_type = None
	guard = _InterruptGuard()
	guard.note(exc)
	try:
		with guard:
			if exc is not None and getattr(exc, _LOGGED_ATTR, False):
				_append_context(exc, title, session_uuid, context, auth)
			else:
				import frappe

				lines = [title]
				try:
					if session_uuid:
						lines.append(f"session_uuid={session_uuid}")
					for k in sorted(context):
						lines.append(f"{k}={context[k]}")
					if exc is not None:
						lines.append(_exception_text(exc))
					message = _scrubbed_message(title, lines, exc, auth)
				finally:
					# Sentry (attach_stacktrace) serialises this frame's locals
					# with the event, and an interrupt that is not an Exception
					# leaves with this frame: only the scrubbed message may be
					# bound, on every path.
					del lines

				if not docname and session_uuid:
					try:
						docname = frappe.db.get_value("Optimus Session", {"session_uuid": session_uuid}, "name")
					except _job_timeout_types():
						raise
					except Exception:
						docname = None
				reference_doctype = "Optimus Session" if docname else None
				reference_name = docname or None
				row = frappe.log_error(
					title=title,
					message=message,
					reference_doctype=reference_doctype,
					reference_name=reference_name,
				)
				logged = True
				row_name = getattr(row, "name", None)
				_mark_logged(exc, row_name)
				_mark_logged(marks, row_name)
				_requeue_if_rolled_back(
					{
						"error": message, "method": title,
						"reference_doctype": reference_doctype, "reference_name": reference_name,
					},
					row,
				)
	except Exception as e:
		failure_type = type(e).__name__
	# The breadcrumb comes first: a write that failed while a timeout was
	# passed in still leaves its line before the timeout is raised again.
	if failure_type is not None:
		_note_unwritten_row(failure_type)
	if guard.pending():
		interrupt = guard.interrupt()
		# The fresh timeout stands for the passed-in one: when that one's row
		# was written, mark it too, so run()'s outer handler does not log the
		# same timeout a second time. It carries no traceback or chain.
		if exc is not None and getattr(exc, _LOGGED_ATTR, False) and type(interrupt) is type(exc):
			_mark_logged(interrupt, getattr(exc, _LOGGED_ROW_ATTR, None))
		raise interrupt
	return logged


def _append_context(exc: BaseException, title: str, session_uuid: str | None, context: dict, auth=None) -> None:
	"""Append a later caller's ``title``, session and ``k=v`` lines, scrubbed
	like the message (``_scrubbed_message``), to the Error Log row ``exc``
	was logged in (its ``_LOGGED_ROW_ATTR``): a blank line, then those lines,
	after the row's ``error``, with ``frappe.db.set_value`` (no second row,
	``modified`` kept). Nothing when ``exc`` carries no row name or the row is
	gone. A failure leaves one line with its error type in the ``optimus`` log
	and is never raised; an RQ job timeout leaves as a fresh instance. Runs
	outside any ``except`` block."""
	name = getattr(exc, _LOGGED_ROW_ATTR, None)
	if not isinstance(name, str) or not name:
		return
	lines = [title]
	try:
		if session_uuid:
			lines.append(f"session_uuid={session_uuid}")
		for k in sorted(context):
			lines.append(f"{k}={context[k]}")
		addition = _scrubbed_message(title, lines, exc, auth)
	finally:
		# only the scrubbed lines may be bound (see log_ai_failure)
		del lines
	failure_type = None
	guard = _InterruptGuard()
	try:
		with guard:
			import frappe

			existing = frappe.db.get_value("Error Log", name, "error")
			if existing is not None:
				frappe.db.set_value("Error Log", name, "error", f"{existing}\n\n{addition}", update_modified=False)
	except Exception as e:
		failure_type = type(e).__name__
	if guard.pending():
		raise guard.interrupt()
	if failure_type is not None:
		_note_line(f"optimus ai_fix: the caller's context could not be added to an AI Error Log row: {failure_type}")


def _exception_text(exc: BaseException) -> str:
	"""The plain traceback of ``exc`` for its Error Log row: the code lines
	of its frames (no frame locals, no exception chain), then the exception
	line.

	An HTTP-status ``AiFixError`` from ``_http_post`` carries a body-free log
	text (``_LOG_TEXT_ATTR``: the status, the call site and the provider's
	error code). Its message holds the provider's reply for the operator,
	and the reply can echo the prompt, so its exception line shows that text
	instead of the message. The HTTP layer logs such an error itself; this
	matters when that row could not be written and the caller logs it.

	Both are ``traceback.format_exception`` output: for such an error it
	formats a stand-in of the same type whose only argument is the log text,
	with the error's own traceback, so the stdlib still names the type and
	formats the frames and only the message differs. The stand-in is made
	with ``__new__`` alone (no ``__init__``), so it carries nothing else of
	the error: no notes, no chain."""
	shown = exc
	log_text = getattr(exc, _LOG_TEXT_ATTR, None)
	if isinstance(log_text, str):
		shown = type(exc).__new__(type(exc))
		shown.args = (log_text,)
	return "".join(traceback.format_exception(type(shown), shown, exc.__traceback__, chain=False)).rstrip()


def _scrubbed_message(title: str, lines: list[str], exc: BaseException | None, auth=None) -> str:
	"""``lines`` joined and passed through ``redaction.scrub_secrets`` with
	the key as a literal, raw, JSON-escaped and repr-escaped (``_scrub_literals_for(auth)``:
	the in-flight key when ``auth`` is the ``_ApiKeyAuth`` the request used,
	else the stored key). If scrubbing fails, the
	message keeps only the title and the error type, never the unscrubbed
	text. An RQ job timeout leaves as a fresh instance (no scrubber frame, no
	chain). An interrupt that is not an ``Exception`` (``SystemExit`` from a
	gunicorn worker timeout) gets the reply readers' guard
	(``_InterruptGuard(base=True)``): the same instance leaves with its
	traceback, context and cause cleared, so the scrubber's frame (the joined
	text, the literals) never travels with it. ``lines`` and the literals
	(``api_key``) are unbound on every path, so this frame, which does
	travel with what it raises, holds neither."""
	failed = ""
	api_key = None
	guard = _InterruptGuard(base=True)
	try:
		with guard:
			from optimus.redaction import scrub_secrets

			api_key = _scrub_literals_for(auth)
			return scrub_secrets("\n".join(lines), literals=api_key)
	except Exception as e:
		failed = type(e).__name__
	finally:
		# Never ride unscrubbed on an interrupt: a timeout or a non-Exception
		# interrupt is raised below, with this frame.
		lines = None
		api_key = None
	if guard.pending():
		raise guard.interrupt()
	kind = type(exc).__name__ if exc is not None else "none"
	return f"{title}\n(details withheld: scrubbing the message failed with {failed}; error type {kind})"


def _requeue_if_rolled_back(record: dict, row=None) -> None:
	"""Queue the Error Log row ``log_ai_failure`` just inserted (``row``, the
	document ``frappe.log_error`` returned) again if a rollback removed it.

	``frappe.log_error`` inserts the row in the current transaction, and
	``frappe.app`` rolls the request back after an exception (a
	``frappe.throw`` after the log), as ``execute_job`` does for a failing
	job. Whether that removes the row depends on the table: on MariaDB Error
	Log is a MyISAM table (``error_log.json``: ``"engine": "MyISAM"``), so the
	row survives any rollback; on a transactional engine (Postgres) it is
	gone. A ``frappe.db.after_rollback`` callback, which runs after the
	ROLLBACK, therefore queues the same fields (``record``: the scrubbed
	message, the title, the references, plus the ``trace_id`` and
	``metadata`` of ``row``) through ``frappe.deferred_insert`` only when
	``row`` no longer exists. ``commit()`` drops the callbacks, so a committed
	row is never queued; a savepoint rollback runs none. Nothing is
	registered in read-only mode (``log_error`` has queued the row itself) or
	when ``log_error`` returned no named document (nothing was inserted).

	The callback never calls ``frappe.log_error`` (it may run inside an
	``except`` block, and it must not reach Sentry), never raises
	(``CallbackManager.run`` would pass the error to the rollback's caller)
	except an RQ job timeout, and holds only the row name and the scrubbed
	fields. If the existence check fails, nothing is queued: a queued copy of
	a row that survived would be a duplicate.

	A failed existence check, a failed queue or a failed registration leaves
	the error type in the ``optimus`` log (``_note_unwritten_row``), recorded
	in the handler and logged after the ``try``: on Postgres the row may be
	lost otherwise without a trace."""
	guard = _InterruptGuard()
	failure_type = None
	try:
		with guard:
			import frappe

			name = getattr(row, "name", None)
			if not isinstance(name, str) or not name:
				return  # nothing was inserted (no database, or queued in read-only mode)
			if getattr(frappe.flags, "read_only", False):
				return
			for field in ("trace_id", "metadata"):
				value = getattr(row, field, None)
				if isinstance(value, str) and value:
					record[field] = value

			def _requeue() -> None:
				requeue_guard = _InterruptGuard()
				requeue_failure = None
				try:
					with requeue_guard:
						import frappe

						if frappe.db.exists("Error Log", name):
							return  # MyISAM: the ROLLBACK left the row in place
						from frappe.deferred_insert import deferred_insert

						deferred_insert("Error Log", [dict(record)])
				except Exception as e:
					requeue_failure = type(e).__name__
				if requeue_guard.pending():
					raise requeue_guard.interrupt()
				if requeue_failure is not None:
					_note_unwritten_row(requeue_failure)

			frappe.db.after_rollback.add(_requeue)
	except Exception as e:
		failure_type = type(e).__name__
	if guard.pending():
		raise guard.interrupt()
	if failure_type is not None:
		_note_unwritten_row(failure_type)


def _note_unwritten_row(error_type: str) -> None:
	"""Leave a trace when an AI failure row may be missing from the Error Log:
	one line in the ``optimus`` log naming the error TYPE only (its message
	could hold anything). It says the row "may not have been written or
	re-queued, or a hook after the insert failed", because that is all that
	is known: the write failed; or a hook that runs after the insert (a
	broken Error Log notification, say) failed, so the row is there but
	the write raised (the error then stays unmarked, and a caller that logs
	it again writes a second row); or, after a rollback, the existence check
	failed (on MariaDB the row may well have survived) or the queue failed;
	or the rollback callback could not be registered (the row was written,
	but a later rollback on Postgres could remove it without queuing it
	again).

	It is logged at ERROR: Frappe's loggers drop anything below ERROR unless
	DEV_SERVER is set (``bench start``; ``frappe/utils/logger.py``), so a
	warning would never reach the log on a production site. Never raises,
	except an RQ job timeout."""
	_note_line(
		"optimus ai_fix: an AI Error Log row may not have been written or re-queued, "
		f"or a hook after the insert failed: {error_type}"
	)


def _note_line(line: str) -> None:
	"""``line`` (fixed text and an error type, never row text) in the
	``optimus`` log at ERROR. Never raises, except an RQ job timeout."""
	guard = _InterruptGuard()
	try:
		with guard:
			import frappe

			frappe.logger("optimus").error(line)
	except Exception:
		pass
	if guard.pending():
		raise guard.interrupt()


def _mark_logged(exc: BaseException | None, row_name: str | None = None) -> None:
	"""Flag ``exc`` so a later ``log_ai_failure(..., exc)`` writes no second
	row, and keep the name of the row it was logged in (``row_name``), so
	that call appends its context there (``_append_context``)."""
	if exc is None:
		return
	guard = _InterruptGuard()
	try:
		with guard:
			setattr(exc, _LOGGED_ATTR, True)
			if isinstance(row_name, str) and row_name:
				setattr(exc, _LOGGED_ROW_ATTR, row_name)
	except Exception:
		pass
	if guard.pending():
		raise guard.interrupt()


def _log_http_error(
	provider: str, where: str, status: int | None, detail: str = "",
	*, exc: BaseException | None = None, provider_error: str = "", auth=None,
) -> None:
	"""Log one HTTP-layer failure through ``log_ai_failure``: provider, call
	site, HTTP status, the provider's own error identifier when it sent one
	(``provider_error``, see ``_provider_error_code``) and a short detail
	(for a transport error, its type and message, scrubbed; for an
	unexpected error, its type and plain frames). Never the prompt, the
	source code, the headers or the response body. The session reference
	comes from the per-worker spend marker the caller set
	(``analyze._mark_ai_spend_session``), the same one
	``_record_session_spend`` reads. ``exc`` (the ``AiFixError`` about to be
	raised) is then marked logged, but only if the row was written, so the
	caller's own ``log_ai_failure`` for it writes no second row and a failed
	write still leaves the caller's. ``auth`` (the ``_ApiKeyAuth`` the request
	was sent with) is what the row is scrubbed of, so logging it reads no key
	from the database."""
	session_uuid = None
	guard = _InterruptGuard()
	try:
		with guard:
			import frappe

			session_uuid = getattr(frappe.local, "_optimus_spend_session", None)
	except Exception:
		session_uuid = None
	if guard.pending():
		raise guard.interrupt()
	context = {"provider": provider, "where": where, "status": status, "detail": detail}
	if provider_error:
		context["provider_error"] = provider_error
	log_ai_failure("optimus ai_fix", session_uuid=session_uuid, auth=auth, marks=exc, **context)


def _job_timeout_types() -> tuple[type[BaseException], ...]:
	"""``safe_call.job_timeout_types`` under the name this module's call sites use
	(tests patch it here)."""
	return safe_call.job_timeout_types()


class _InterruptGuard(safe_call.InterruptGuard):
	"""``safe_call.InterruptGuard`` reading the RQ timeout types through this module's
	``_job_timeout_types``, so ai_fix's call sites and the tests that patch that name
	stay in step. ``__slots__ = ()`` keeps instances without a ``__dict__``
	(``test_ai_interrupt_guard.py`` checks that the guard holds no traceback)."""

	__slots__ = ()

	def _timeout_types(self) -> tuple[type[BaseException], ...]:
		return _job_timeout_types()


def _response_detail(resp, auth=None) -> str:
	"""The provider's own error body (capped), as a ': ...' suffix or '' when
	there is no readable body. Surfaces the specific reason ("model not found",
	"context too long", ...) so it reaches the operator.

	SECURITY: a provider can echo the API key in its error body, and this text
	reaches toasts, API responses and the title of Frappe's own error
	snapshot, so the body is scrubbed BEFORE it is cut to 300 characters
	(cutting first can split the key, and a partial key no longer matches the
	literal). The literals are the key the request was sent with (``auth``,
	the ``_ApiKeyAuth`` it used), or the key stored in Optimus Settings when
	it carried none (``_scrub_literals_for``), raw, JSON-escaped and repr-escaped. Any failure returns
	''; an RQ job timeout leaves as a fresh instance, with the raw body
	unbound.

	The literals are read BEFORE the body is bound: reading the stored key is
	a database query (only when no key was sent). An interrupt that is not an ``Exception`` (``SystemExit``
	from a gunicorn worker timeout) gets ``_http_post``'s guard
	(``_InterruptGuard(base=True)``): the body is unbound, then the same
	instance leaves with its traceback, context and cause cleared, so neither
	the scrubber's frames nor this one carry the body."""
	body_text = ""
	guard = _InterruptGuard(base=True)
	try:
		with guard:
			from optimus.redaction import SCRUB_TEXT_CAP, scrub_secrets

			api_key = _scrub_literals_for(auth)
			body_text = (resp.text or "").strip()
			if not body_text:
				return ""
			return ": " + scrub_secrets(body_text[:SCRUB_TEXT_CAP], literals=api_key)[:300]
	except Exception:
		return ""
	body_text = ""  # the raw body may echo the key: never on the interrupt's traceback
	if guard.pending():
		raise guard.interrupt()


# What a 404 message names instead of the request URL when the URL cannot be
# scrubbed (_shown_url).
_UNSHOWN_URL = "(the configured Base URL)"


def _shown_url(url: str, auth=None) -> str:
	"""``url`` as a 404 message names it: scrubbed of the key the request was
	sent with (``auth``), or the stored key when it carried none
	(``_scrub_literals_for``), raw, JSON-escaped and repr-escaped, and of credentials in it (a custom Base URL typed
	as ``user:password@host``). A ``url`` that is not a str, or is empty, is
	returned as it is: there is nothing to scrub. Any failure returns
	``_UNSHOWN_URL``, never the unscrubbed URL; an RQ job timeout leaves as
	a fresh instance, raised after the ``try``, so the frames it interrupted
	(``json.dumps`` holds the key under the names ``obj`` and ``o`` while
	the literals are built) never travel with it. An interrupt that is not an
	``Exception`` leaves as the same instance with its traceback, context and
	cause cleared, and ``url`` unbound (``_InterruptGuard(base=True)``)."""
	guard = _InterruptGuard(base=True)
	try:
		with guard:
			from optimus.redaction import scrub_secrets

			api_key = _scrub_literals_for(auth)
			return scrub_secrets(url, literals=api_key)
	except Exception:
		return _UNSHOWN_URL
	url = None  # it may hold credentials: never on the interrupt's traceback
	if guard.pending():
		raise guard.interrupt()


# A provider's machine-readable error code: lowercase-letter words joined by
# "_", ".", ":" or "-" (invalid_request_error, rate_limit_exceeded,
# overloaded_error, ...), at most 64 characters. Nothing that could be prose,
# a prompt fragment, an address or a URL. The shape also rules out most API
# keys (they carry a digit or an upper-case letter), but not every key: one
# made only of lowercase words matches it. What keeps such a key out is the
# literal check in _provider_error_code, which drops a value holding the
# in-flight key, or the stored key when no key was sent (8 characters or
# more, raw, JSON-escaped or repr-escaped).
_PROVIDER_ERROR_RE = re.compile(r"^[a-z]+(?:[_.:-][a-z]+)*$")
_PROVIDER_ERROR_MAX_LEN = 64


def _provider_error_code(resp, auth=None) -> str:
	"""The provider's machine-readable reason for an HTTP error, for the Error
	Log row (whose detail never holds the body: it can echo the prompt), or
	'' when there is none.

	Read from the JSON body's ``error`` object: ``type`` and ``code`` (OpenAI
	and compatible servers) or ``type`` (Anthropic). A value is kept only when
	it is a string of at most 64 characters made of lowercase-letter words
	joined by ``_ . : -`` (``_PROVIDER_ERROR_RE``: no digits, no upper case)
	and does not contain the key the request was sent with (``auth``, the
	``_ApiKeyAuth`` it used), or the stored key when it carried none, raw,
	JSON-escaped or repr-escaped; both kept values are joined as
	``type:code`` when that still fits 64 characters, else the first one is
	used. Any failure returns ''; an RQ job timeout leaves as a fresh
	instance, with the parsed body unbound, and an interrupt that is not an
	``Exception`` as the same instance with its traceback, context and cause
	cleared and the parsed body unbound (``_InterruptGuard(base=True)``). As
	in ``_response_detail``, the literals are read BEFORE the body is parsed
	and bound."""
	guard = _InterruptGuard(base=True)
	try:
		with guard:
			from optimus.redaction import scrub_secrets

			api_key = _scrub_literals_for(auth)
			data = resp.json()
			error = data.get("error") if isinstance(data, dict) else None
			if not isinstance(error, dict):
				return ""
			parts: list[str] = []
			for field in ("type", "code"):
				value = error.get(field)
				if (
					isinstance(value, str)
					and len(value) <= _PROVIDER_ERROR_MAX_LEN
					and _PROVIDER_ERROR_RE.fullmatch(value)
					and value not in parts
					and scrub_secrets(value, literals=api_key) == value
				):
					parts.append(value)
			if not parts:
				return ""
			joined = ":".join(parts)
			return joined if len(joined) <= _PROVIDER_ERROR_MAX_LEN else parts[0]
	except Exception:
		return ""
	data = error = value = None  # the body may echo the key: never on the interrupt's traceback
	if guard.pending():
		raise guard.interrupt()


def _http_post(
	url: str,
	headers: dict,
	body: dict,
	*,
	provider: str,
	where: str,
	timeout: int | None = None,
	auth: requests.auth.AuthBase | None = None,
) -> dict:
	"""POST JSON, return the parsed response dict. Maps transport / HTTP /
	decode errors to ``AiFixError`` with operator-friendly messages and logs
	each failure once (``_log_http_error``).

	``requests`` never follows a redirect here (``allow_redirects=False``):
	it drops only a header named ``Authorization`` when it follows one to
	another host, so Anthropic's ``x-api-key`` header would be sent on to the
	redirect target. A 307 or 308 whose Location stays on the same host and
	port, with the same scheme or an http to https upgrade on that host
	(``_same_origin_redirect``), is followed by hand, at most
	``_MAX_REDIRECTS`` times: a new POST of the same body to that address,
	the key attached again by the same ``auth``. Every other 3xx (301, 302
	and 303 would turn the POST into a GET; another host or port; a
	downgrade; one redirect too many) is ``kind="bad_response"``. The
	Location is never logged or shown. A 4xx / 5xx reply is an HTTP error;
	both are logged with a body-free log text (``_LOG_TEXT_ATTR``), never
	their body.

	SECURITY: ``auth`` (an ``_ApiKeyAuth``) attaches the key at send time, so
	``headers`` never holds it. Every failure is logged and raised OUTSIDE the
	``except`` blocks: while an ``except`` block runs, the original exception
	is the active one, Frappe's Sentry hook captures it with its
	requests / urllib3 frames (whose locals hold the prepared headers), and
	a ``raise`` there would chain it as ``__context__``. The catch-all only
	records plain values (the exception's type name and its frames as
	``file:line:function`` strings read off the traceback; for an RQ job
	timeout, its type and args): a ``UnicodeEncodeError`` from http.client
	carries the header value, so the error to raise is built after the
	``try``, where a failure while building it can neither chain that
	exception nor find it still bound in this frame.

	The catch-all takes ``BaseException``. An interrupt that is not an
	``Exception`` (``SystemExit`` from a gunicorn worker timeout,
	``KeyboardInterrupt``, a gevent ``Timeout``) is re-raised after the
	``try`` as the SAME instance (gevent matches its timeout by identity),
	with its traceback, ``__context__`` and ``__cause__`` cleared, and is not
	logged: otherwise it would leave with the requests / urllib3 frames,
	which Sentry's WSGI middleware ships with their locals. It leaves
	unchained when ``_http_post`` is not itself called while an exception is
	being handled (a raise inside a handler sets ``__context__`` again), so no
	request is sent from inside an ``except`` block (``test_ai_log_audit.py``
	rule 4)."""
	timeout = timeout or _resolve_timeout_seconds()
	failure: AiFixError | None = None
	detail = ""
	resp = None
	target = url
	redirects = 0
	while True:
		unexpected_name: str | None = None
		unexpected_frames: list[str] = []
		guard = _InterruptGuard(base=True)
		try:
			with guard:
				resp = requests.post(target, headers=headers, json=body, timeout=timeout, auth=auth, allow_redirects=False)
		except requests.exceptions.Timeout:
			failure = AiFixError(f"The AI provider didn't respond within {timeout}s.", kind="timeout")
			detail = "timeout"
		except requests.exceptions.RequestException as e:
			failure = AiFixError(f"Couldn't reach the AI provider: {type(e).__name__}.", kind="transport")
			detail = f"{type(e).__name__}: {e}"
		except Exception as e:
			unexpected_name = type(e).__name__
			# file:line:function per frame, read straight off the traceback:
			# no source lookup (no I/O while this handler runs), no locals,
			# no message.
			unexpected_frames = [
				f"{frame.f_code.co_filename}:{lineno}:{frame.f_code.co_name}"
				for frame, lineno in traceback.walk_tb(e.__traceback__)
			]
		# A non-Exception interrupt is not ours to handle: it leaves unlogged,
		# as the same instance, without the frames below this one (their locals
		# hold the prepared headers), and unchained when _http_post is not
		# itself called while an exception is being handled. An RQ job timeout
		# must still stop the job: the same type, raised fresh, with no
		# requests / urllib3 frames and no chain.
		if guard.pending():
			raise guard.interrupt()
		if unexpected_name is not None:
			from frappe import _

			failure = AiFixError(_("The AI request failed ({0}).").format(unexpected_name), kind="transport")
			# Where it happened, never what it said: plain frames, no message, no locals.
			detail = unexpected_name + "".join(f"\n  {frame}" for frame in unexpected_frames)
		if failure is not None:
			_log_http_error(provider, where, None, detail, exc=failure, auth=auth)
			raise failure
		if resp.status_code not in (307, 308) or redirects >= _MAX_REDIRECTS:
			break
		# 307 / 308 keep the method and the body. The key goes again only to
		# the same host and port (_same_origin_redirect); anything else is the
		# bad_response below. The Location is never logged or shown.
		follow = _same_origin_redirect(target, resp)
		if follow is None:
			break
		target = follow
		redirects += 1

	status = resp.status_code
	if 300 <= status < 400:
		# Not followed (see the docstring): it is not the provider's reply,
		# whatever its body holds, and the key never goes to its target.
		from frappe import _

		failure = AiFixError(
			_("The AI provider answered with a redirect (HTTP {0}) instead of a reply. Optimus follows only a 307 or 308 redirect to the same host: a Base URL that answers 301, 302 or 303, or redirects to another host, must be set to the final URL it redirects to. Change the Base URL in Optimus Settings to that final URL (its https:// address, for example).").format(status),
			status_code=status, kind="bad_response",
		)
	elif status in (401, 403):
		failure = AiFixError("The AI provider rejected the API key. Check it in Optimus Settings.", status_code=status)
	elif status == 404:
		# A 404 means the endpoint path or the model was not found. The Model
		# field is editable for every provider and a wrong model name returns
		# 404, so the message leads with that. It also always mentions a custom
		# ('OpenAI-compatible') Base URL missing the '/v1' segment, phrased as
		# "if you set a custom Base URL" so a hosted-provider operator (whose
		# Base URL is fixed and hidden) reads it as not their case. The
		# provider's own error body is surfaced either way. The URL is shown
		# scrubbed (_shown_url): a custom Base URL can be typed as
		# user:password@host.
		detail = f"url={url}"
		shown_url = _shown_url(url, auth)
		failure = AiFixError(
			f"The AI provider returned 404 (Not Found) for {shown_url}. Check that the Model "
			"in Optimus Settings is a valid model name for this provider: a wrong model "
			"returns 404. If you set a custom Base URL, make sure it includes the '/v1' "
			"path segment (for example http://localhost:11434/v1 for Ollama)."
			+ _response_detail(resp, auth),
			status_code=status,
		)
	elif status == 429:
		failure = AiFixError("The AI provider is rate-limiting requests. Try again shortly.", status_code=status)
	elif status >= 400:
		response_detail = _response_detail(resp, auth)
		context_error = status == 400 and _CONTEXT_LIMIT_RE.search(response_detail)
		failure = AiFixError(
			f"The AI provider returned an error (HTTP {status}){response_detail}"
			+ (" " + _context_advice() if context_error else ""),
			status_code=status, kind="config" if context_error else "unknown",
		)
	if failure is not None:
		provider_error = _provider_error_code(resp, auth)
		# If the row below cannot be written, the caller logs this error: with
		# this text, never the reply its message carries (_exception_text).
		code = f", provider_error={provider_error}" if provider_error else ""
		setattr(failure, _LOG_TEXT_ATTR, f"HTTP {status} from the AI provider (where={where}{code})")
		_log_http_error(provider, where, status, detail, exc=failure, provider_error=provider_error, auth=auth)
		raise failure

	data = None
	try:
		data = resp.json()
	except Exception:
		detail = "non-JSON body"
		failure = AiFixError(
			"The AI provider returned an unexpected (non-JSON) response.",
			status_code=status, kind="bad_response",
		)
	if failure is None and not isinstance(data, dict):
		from frappe import _

		detail = f"JSON {type(data).__name__}, not an object"
		failure = AiFixError(
			_("The AI provider returned an unexpected response (not a JSON object)."),
			status_code=status, kind="bad_response",
		)
	if failure is not None:
		_log_http_error(provider, where, status, detail, exc=failure, auth=auth)
		raise failure
	return data


# How many same-origin 307 / 308 redirects _http_post follows for one request.
_MAX_REDIRECTS = 3
# The port a URL scheme means when the URL names none.
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _same_origin_redirect(current: str, resp) -> str | None:
	"""The absolute URL a redirect reply (``resp``) points to, when the key may
	be sent there: the same host and the same port, with the same scheme, or
	an upgrade from http to https on that host (port 80 to 443, or the same
	port), and no credentials of its own in the URL. None otherwise, for a
	missing or unparsable Location, and on any failure (never raises, except
	an RQ job timeout, raised fresh, and an interrupt that is not an
	``Exception``, which leaves as the same instance with its traceback,
	context and cause cleared: ``_InterruptGuard(base=True)``). Neither the
	Location nor the result is ever logged or shown: it comes from the reply,
	not from Optimus Settings. Before an interrupt is raised, the URLs
	(``current`` can carry the Base URL's ``user:password@``) and the reply are unbound, so
	this frame, which travels with it, holds no credential."""
	location = follow = old = new = None
	guard = _InterruptGuard(base=True)
	try:
		with guard:
			location = resp.headers.get("location")
			if not isinstance(location, str) or not location.strip():
				return None
			follow = urljoin(current, location.strip())
			old, new = urlsplit(current), urlsplit(follow)
			if not old.hostname or new.hostname != old.hostname:
				return None
			if (new.username, new.password) != (old.username, old.password):
				return None
			old_port = old.port or _DEFAULT_PORTS.get(old.scheme)
			new_port = new.port or _DEFAULT_PORTS.get(new.scheme)
			if old_port is None or new_port is None:
				return None
			if new.scheme == old.scheme:
				same_origin = new_port == old_port
			else:
				same_origin = (old.scheme, new.scheme) == ("http", "https") and (
					new_port == old_port or (old_port, new_port) == (80, 443)
				)
			return follow if same_origin else None
	except Exception:
		return None
	# the URLs may carry credentials: never on the interrupt's traceback
	current = resp = location = follow = old = new = None
	if guard.pending():
		raise guard.interrupt()
	return None


# Token counts land in Int columns (Optimus Session.ai_tokens_spent,
# ai_steps_tokens); a larger reported count is not a real one.
_MAX_TOKEN_COUNT = 2**31 - 1


def _token_count(value) -> int:
	"""A provider-reported token count as a non-negative int, or 0 when it is
	not one: not a number (``"abc"``, a container, NaN, infinity), a bool, a
	negative or an absurdly large value. Usage is informational, so an odd
	usage block must never fail a reply that already carries a suggestion.
	Never raises, except an RQ job timeout (re-raised fresh)."""
	if isinstance(value, bool):
		return 0
	guard = _InterruptGuard()
	try:
		with guard:
			count = int(value)
	except Exception:
		return 0
	if guard.pending():
		raise guard.interrupt()
	return count if 0 <= count <= _MAX_TOKEN_COUNT else 0


def _usage_block(data) -> dict:
	"""``data["usage"]`` when it is a dict, else ``{}``."""
	usage = data.get("usage") if isinstance(data, dict) else None
	return usage if isinstance(usage, dict) else {}


def _usage_from_openai(data: dict | None) -> dict:
	"""Normalised token usage from an OpenAI-shaped response (also what the
	Aerele managed proxy + Ollama/LM Studio/vLLM return). Missing or
	malformed fields → 0 (see ``_token_count``); ``total`` falls back to
	prompt+completion when the upstream omits it. Never raises, except an RQ
	job timeout (``_token_count`` lets it through as a fresh instance)."""
	u = _usage_block(data)
	prompt = _token_count(u.get("prompt_tokens"))
	completion = _token_count(u.get("completion_tokens"))
	total = _token_count(u.get("total_tokens")) or (prompt + completion)
	return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def _usage_from_anthropic(data: dict | None) -> dict:
	"""Normalised token usage from an Anthropic Messages response
	(``usage.input_tokens`` / ``usage.output_tokens``). Missing or malformed
	fields → 0 (see ``_token_count``). Never raises, except an RQ job timeout
	(``_token_count`` lets it through as a fresh instance)."""
	u = _usage_block(data)
	prompt = sum(_token_count(u.get(k)) for k in (
		"input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
	))
	completion = _token_count(u.get("output_tokens"))
	return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def _record_session_spend(total_tokens) -> None:
	"""Best-effort: add this call's tokens to the active session's cumulative
	``Optimus Session.ai_tokens_spent``. The session uuid comes from
	``frappe.local._optimus_spend_session`` (set by the caller before any AI
	call); ``None`` (e.g. the settings probe) is a no-op."""
	try:
		import frappe

		su = getattr(frappe.local, "_optimus_spend_session", None)
		n = int(total_tokens or 0)
		if su and n > 0:
			frappe.db.sql(
				"update `tabOptimus Session` "
				"set ai_tokens_spent = coalesce(ai_tokens_spent, 0) + %s "
				"where session_uuid = %s",
				(n, su),
			)
	except Exception:
		pass


def _aerele_call_metadata(provider, finding_type=None) -> dict | None:
	"""Metadata attaching an Aerele managed-proxy request to the originating
	Optimus Session, so the Aerele billing portal can attribute each AI call.

	Returns ``None`` for every non-Aerele provider (so no unknown body fields
	reach OpenAI / Anthropic) and on any failure (the call then proceeds
	unattributed). The session uuid comes from
	``frappe.local._optimus_spend_session``; the docname is resolved from it."""
	if not provider or provider.get("name") != "Aerele":
		return None
	try:
		import frappe

		uuid = getattr(frappe.local, "_optimus_spend_session", None)
		if not uuid:
			return None
		meta = {"optimus_session_uuid": uuid}
		docname = frappe.db.get_value("Optimus Session", {"session_uuid": uuid}, "name")
		if docname:
			meta["optimus_session"] = docname
		if finding_type:
			meta["optimus_finding_type"] = finding_type
		return meta
	except Exception:
		return None


def _call_anthropic(
	base_url: str, api_key: str, model: str, system: str, messages: list[dict],
	*, max_tokens: int = _MAX_OUTPUT_TOKENS, usage_out: dict | None = None,
	timeout: int | None = None, meta_out: dict | None = None,
) -> str:
	url = base_url.rstrip("/") + "/v1/messages"
	headers = {
		"content-type": "application/json",
		"anthropic-version": _ANTHROPIC_VERSION,
	}
	auth = _ApiKeyAuth("x-api-key", api_key) if api_key else None
	body = {
		"model": model,
		"max_tokens": max_tokens,
		"temperature": _TEMPERATURE,
		"system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
		"messages": messages,
	}
	data = _http_post(url, headers, body, provider="anthropic", where="messages", auth=auth, timeout=timeout)
	if usage_out is not None:
		usage_out.update(_usage_from_anthropic(data))
		_record_session_spend(usage_out.get("total_tokens"))
	if meta_out is not None:
		meta_out["prompt_tokens_reported"] = any(_usage_block(data).get(k) is not None for k in (
			"input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
		))
		meta_out["finish_reason"] = _ANTHROPIC_FINISH.get(_text_or_empty(data.get("stop_reason")))
	try:
		blocks = data.get("content") or []
		for b in blocks:
			if isinstance(b, dict) and b.get("type") == "text":
				return _text_or_empty(b.get("text"))
		# Fall back to the first block's text if no explicit type.
		if blocks and isinstance(blocks[0], dict):
			return _text_or_empty(blocks[0].get("text"))
	except Exception:
		pass
	raise AiFixError("The AI provider's response didn't contain any text.")


def _text_or_empty(text) -> str:
	"""A text block's ``text`` when it is a string, else ``""``: a text that
	is not a string (a dict, a list) counts as no text, so the callers report
	an empty response instead of failing on ``.strip()`` (an AttributeError
	would leave the endpoint as a 500 whose snapshot holds the prompt)."""
	return text if isinstance(text, str) else ""


def _call_openai_chat(
	base_url: str, api_key: str, model: str, system: str, messages: list[dict],
	*, max_tokens: int = _MAX_OUTPUT_TOKENS, usage_out: dict | None = None,
	metadata: dict | None = None, timeout: int | None = None, meta_out: dict | None = None,
) -> str:
	url = base_url.rstrip("/") + "/chat/completions"
	headers = {"content-type": "application/json"}
	auth = _ApiKeyAuth("authorization", api_key, prefix="Bearer ") if api_key else None
	body = {
		"model": model,
		"max_tokens": max_tokens,
		"messages": [{"role": "system", "content": system}, *messages],
	}
	sent_temperature = not _is_reasoning_model(model)
	if sent_temperature:
		body["temperature"] = _TEMPERATURE
	# Aerele-only: attribute this call to the originating Optimus Session.
	if metadata:
		body["metadata"] = metadata
	retry_without_temperature = False
	try:
		data = _http_post(url, headers, body, provider="openai", where="chat/completions", auth=auth, timeout=timeout)
	except AiFixError as e:
		# Some reasoning models reject a non-default `temperature` with a
		# request-validation error. OpenAI o-series are pre-filtered by
		# `_is_reasoning_model`, but others e.g. Moonshot/Kimi "thinking"
		# variants only allow the default and say so ("invalid temperature:
		# only 1 is allowed for this model"). We can't enumerate every such
		# model, so retry once without `temperature` (letting the model use its
		# own default). Gate on a request-validation status (400 or 422; some
		# OpenAI-compatible gateways use 422) so a body that mentions the word
		# for another reason (e.g. a 404 listing valid params) can't trigger a
		# needless second call.
		# The retry runs after the try: a request sent inside this block would
		# log its own failure while this error is the active exception.
		if sent_temperature and getattr(e, "status_code", None) in (400, 422) and "temperature" in str(e).lower():
			retry_without_temperature = True
		else:
			raise
	if retry_without_temperature:
		body.pop("temperature", None)
		data = _http_post(url, headers, body, provider="openai", where="chat/completions", auth=auth, timeout=timeout)
	if usage_out is not None:
		usage_out.update(_usage_from_openai(data))
		_record_session_spend(usage_out.get("total_tokens"))
	choices = data.get("choices") or []
	if meta_out is not None:
		first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
		meta_out["prompt_tokens_reported"] = _usage_block(data).get("prompt_tokens") is not None
		meta_out["finish_reason"] = _OPENAI_FINISH.get(_text_or_empty(first.get("finish_reason")))
	try:
		if choices:
			msg = choices[0].get("message") or {}
			content = msg.get("content")
			if isinstance(content, str):
				return content
			# Some servers return content as a list of parts. A part whose
			# text is None or not a string counts as no text (_text_or_empty),
			# so one odd part never loses the whole reply.
			if isinstance(content, list):
				return "".join(
					_text_or_empty(p.get("text")) for p in content if isinstance(p, dict)
				)
	except Exception:
		pass
	raise AiFixError("The AI provider's response didn't contain any text.")


def _reask_enabled() -> bool:
	"""Operator knob in site_config; an ordinary read failure defaults to enabled."""
	failure = None
	try:
		import frappe

		return bool(frappe.conf.get("optimus_ai_reask", True))
	except Exception as exc:
		failure = exc
	if isinstance(failure, _job_timeout_types()):
		raise type(failure)(*failure.args)
	return True


def _log_reask(outcome: str, codes: list[str] | None = None) -> None:
	"""Log only the outcome and rule codes; an RQ timeout must still stop the job."""
	failure = None
	try:
		import frappe

		frappe.logger("optimus").info(f"ai_fix guardrail re-ask: {outcome} {','.join(codes or [])}".strip())
	except Exception as exc:
		failure = exc
	if isinstance(failure, _job_timeout_types()):
		raise type(failure)(*failure.args)


def _context_tokens(provider: dict) -> int:
	return int(provider.get("context_tokens") or _DEFAULT_CONTEXT_TOKENS)


def _output_tokens(provider: dict) -> int:
	return int(provider.get("max_output_tokens") or ai_budget.output_tokens(_context_tokens(provider)))


def _context_advice() -> str:
	from frappe import _

	return _(
		"Raise the context window on the model server (Ollama: OLLAMA_CONTEXT_LENGTH or a Modelfile "
		"PARAMETER num_ctx) and set the same value in Optimus Settings > AI > Context window (tokens)."
	)


def _check_context_fits(
	system: str, context_tokens: int, *, messages=(), out_tokens: int = 512,
) -> None:
	"""Refuse before any HTTP call when the window cannot hold the prompt."""
	need = (ai_budget.estimate_tokens(system)
		+ sum(ai_budget.estimate_tokens(m.get("content") or "") for m in messages)
		+ out_tokens + ai_budget.TEMPLATE_TOKENS)
	if context_tokens >= max(ai_budget.min_context_tokens(system), need):
		return
	from frappe import _

	raise AiFixError(
		_("The model's context window ({0} tokens) is too small for the Optimus prompt.").format(context_tokens)
		+ " "
		+ _context_advice(),
		kind="config",
	)


def _dispatch_call(
	provider: dict,
	system: str,
	messages: list[dict],
	*,
	usage_out: dict | None,
	metadata: dict | None = None,
	timeout: int | None = None,
	max_tokens: int | None = None,
	meta_out: dict | None = None,
) -> str:
	"""Send one chat completion through the provider's protocol handler. The API
	key is fetched here into a local named ``api_key`` (never into ``provider``)."""
	api_key = _get_api_key(provider.get("needs_key", True))
	if provider.get("needs_key") and not api_key:
		from frappe import _

		raise AiFixError(_("No API key is configured for this AI provider. Set it under Optimus Settings > AI Fix Suggestions."), kind="config")
	out = int(max_tokens or _output_tokens(provider))
	if provider["protocol"] == "anthropic":
		return _call_anthropic(
			provider["base_url"], api_key, provider["model"], system, messages,
			max_tokens=out, usage_out=usage_out, timeout=timeout, meta_out=meta_out,
		)
	return _call_openai_chat(
		provider["base_url"], api_key, provider["model"], system, messages,
		max_tokens=out, usage_out=usage_out, metadata=metadata, timeout=timeout, meta_out=meta_out,
	)


def _add_usage(total: dict, part: dict) -> None:
	for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
		if part.get(k):
			total[k] = total.get(k, 0) + part[k]


def _complete_with_guardrails(
	provider: dict,
	system: str,
	messages: list[dict],
	*,
	shown_lines: list[str],
	usage: dict,
	metadata: dict | None = None,
	started_at: float,
	timeout: int,
) -> tuple[str, dict, str | None]:
	"""First call, verification, at most one re-ask, fallback.

	Returns ``(suggestion, guardrail, finish_reason)`` where ``guardrail`` is
	``{"violations": [codes], "reasked": bool, "fallback": bool}`` and ``fallback``
	means the code was removed. Every call's tokens land in ``usage`` even when a
	later step fails. The re-ask is sent only when the knob is on, the answer was
	not cut off, a block rule is broken (advise and note rules never re-ask), less
	than half the time budget is used and the re-ask fits the context window. The
	rewrite is adopted only when it keeps the four headings, is not cut off and
	breaks strictly fewer block rules."""
	ctx = _context_tokens(provider)
	out = _output_tokens(provider)
	meta: dict = {}
	text = _dispatch_call(
		provider, system, messages, usage_out=usage, metadata=metadata, timeout=timeout, max_tokens=out, meta_out=meta
	)
	text = (text or "").strip()
	if not text:
		from frappe import _

		raise AiFixError(_("The AI provider returned an empty response."), kind="bad_response", usage=dict(usage))
	first_usage = dict(usage)
	if meta.get("prompt_tokens_reported") is False:
		first_usage.pop("prompt_tokens", None)  # normalized zero is not a reported zero
	text, finish = ai_budget.cap_reply(text, meta.get("finish_reason"))
	violations = ai_guardrails.verify_fix(text, source_lines=shown_lines, finish_reason=finish)
	to_fix = ai_guardrails.reaskable(violations)
	reasked = False
	if to_fix:
		reask_text = ai_guardrails.reask_message(violations)
		fit_usage = {
			"prompt_tokens": first_usage.get("prompt_tokens")
			or ai_budget.estimate_tokens(system)
			+ sum(ai_budget.estimate_tokens(m.get("content") or "") for m in messages)
			+ ai_budget.TEMPLATE_TOKENS,
			"completion_tokens": first_usage.get("completion_tokens") or ai_budget.estimate_tokens(text),
		}
		elapsed = time.monotonic() - started_at
		if not _reask_enabled():
			_log_reask("skipped-knob")
		elif elapsed >= timeout * 0.5:
			_log_reask("skipped-budget")
		elif not ai_budget.reask_fits(ctx, fit_usage, reask_text, out_tokens=out):
			_log_reask("skipped-fit")
		else:
			reasked = True
			reask_usage: dict = {}
			reask_meta: dict = {}
			rewritten = None
			reask_error: Exception | None = None
			try:
				rewritten = _dispatch_call(
					provider,
					system,
					[*messages, {"role": "assistant", "content": text}, {"role": "user", "content": reask_text}],
					usage_out=reask_usage,
					metadata=metadata,
					timeout=max(1, int(timeout - elapsed)),
					max_tokens=ai_budget.reask_output_tokens(out, fit_usage["completion_tokens"]),
					meta_out=reask_meta,
				)
			except Exception as e:
				reask_error = e  # record only; act after the try (no log or raise while it is active)
			finally:
				# A billed re-ask counts even when its text is unusable.
				_add_usage(usage, reask_usage)
			if isinstance(reask_error, _job_timeout_types()):
				guard = _InterruptGuard()
				guard.note(reask_error)
				logged = getattr(reask_error, _LOGGED_ATTR, False)
				row_name = getattr(reask_error, _LOGGED_ROW_ATTR, None)
				reask_error = None  # no failed request frames in the escaping frame
				interrupt = guard.interrupt()
				if logged:
					_mark_logged(interrupt, row_name)
				raise interrupt
			if reask_error is not None:
				error_type = type(reask_error).__name__
				reask_error = None
				_log_reask("failed", [error_type])
			rewritten = (rewritten or "").strip()
			if rewritten:
				rewritten, rewritten_finish = ai_budget.cap_reply(rewritten, reask_meta.get("finish_reason"))
				new = ai_guardrails.verify_fix(rewritten, source_lines=shown_lines, finish_reason=rewritten_finish)
				adopt = (
					not any(v.action == ai_guardrails.TRUNCATED for v in new)
					and not ai_guardrails.check_headings(rewritten)
					and len(ai_guardrails.reaskable(new)) < len(to_fix)
				)
				_log_reask("adopted" if adopt else "kept-original", [v.code for v in to_fix])
				if adopt:
					text, violations, finish = rewritten, new, rewritten_finish
	sent_size = ai_budget.text_size(system) + sum(ai_budget.text_size(m.get("content") or "") for m in messages)
	if ctx <= _TRUNCATION_CHECK_MAX_CONTEXT and ai_budget.context_truncated(first_usage, sent_size):
		violations = [*violations, ai_guardrails.Violation("context-truncated")]
	final = ai_guardrails.apply_fallback(text, violations)
	# fallback = the code was removed (a block rule still holds or the answer was cut off).
	guardrail = {
		"violations": [v.code for v in violations],
		"reasked": reasked,
		"fallback": ai_guardrails.strips_code(violations),
	}
	return final, guardrail, finish


def _require_configured(provider: dict) -> None:
	from frappe import _

	if not provider.get("model"):
		raise AiFixError(_("No AI model is configured. Set Model under Optimus Settings > AI Fix Suggestions."), kind="config")
	if not provider.get("base_url"):
		raise AiFixError(_("No AI base URL is configured. Set Base URL under Optimus Settings > AI Fix Suggestions."), kind="config")




def _window_lines(window: list[dict]) -> str:
	return "\n".join(
		f"{'>> ' if row.get('is_target') else '   '}{row.get('lineno')}: {row.get('content', '')}" for row in window
	)


_LOOP_FACT_TYPES: frozenset[str] = frozenset({"N+1 Query", "Redundant Call", "Hot Line"})


def _loop_facts_text(finding: dict) -> str:
	"""Profiler-computed loop facts for the user message (fix_recipes.loop_facts
	over the source window), or "" when the type is not loop-shaped or the
	window is missing, gapped or has no target row. Identifiers only, no values."""
	if (finding.get("finding_type") or "") not in _LOOP_FACT_TYPES:
		return ""
	detail = finding.get("technical_detail") or {}
	callsite = detail.get("callsite") or {}
	window = finding.get("source_window") or callsite.get("source_snippet") or []
	rows = [r for r in window if isinstance(r, dict) and isinstance(r.get("lineno"), int)]
	if not rows or any(b["lineno"] != a["lineno"] + 1 for a, b in zip(rows, rows[1:], strict=False)):
		return ""
	target = next((i for i, r in enumerate(rows) if r.get("is_target")), None)
	if target is None:
		target = next((i for i, r in enumerate(rows) if r["lineno"] == callsite.get("lineno")), None)
	if target is None:
		return ""
	from optimus.renderer import fix_recipes

	facts = safe_call.best_effort(
		lambda: fix_recipes.loop_facts([str(r.get("content") or "") for r in rows], target + 1), {},
	)
	return fix_recipes.format_loop_facts(facts, line_offset=rows[0]["lineno"] - 1)


def _build_fix_request(
	finding: dict, *, threshold_ms: float, context_tokens: int, out_tokens: int | None = None
) -> tuple[str, list[dict], list[str]]:
	"""Build ``(system, [user_message], shown_lines)`` for one finding. Pure.

	The user message is assembled from priority parts inside the context budget:
	priority 0 parts are always sent (the source window shrinks around the target
	line until they fit), the rest are dropped whole, largest number first. Every
	piece of captured text sits in a ``<data-NONCE>`` block with a fence it cannot
	break. ``shown_lines`` are the source lines actually sent, for the verbatim
	check. The analyzer's ``customer_description`` and ``fix_hint`` are not sent
	(the hint steered models to ``get_all``), and ``suggested_ddl`` is sent as prose."""
	from optimus.analyzers.base import format_durations

	system = ai_prompts.SYSTEM_PROMPT
	out = int(out_tokens or ai_budget.output_tokens(context_tokens))
	budget = ai_budget.user_char_budget(context_tokens, system, out_tokens=out)
	nonce = ai_budget.new_nonce()
	detail = finding.get("technical_detail") or {}
	callsite = detail.get("callsite") or {}

	def block(kind: str, text: str, lang: str = "") -> str:
		return ai_budget.data_block(kind, text, lang=lang, nonce=nonce)

	ftype = finding.get("finding_type") or "Unknown"
	head = [f"Finding type: {ftype}"]
	hint = _finding_type_hint(ftype)
	if hint:
		head.append(f"How this type is usually fixed: {hint}")
	head.append(f"Severity: {finding.get('severity') or 'Unknown'}")
	impact = finding.get("estimated_impact_ms")
	if impact:
		head.append(f"Estimated impact: ~{humanize_duration_ms(float(impact), threshold_ms=threshold_ms)}")
	if finding.get("affected_count"):
		head.append(f"Affected occurrences: {finding['affected_count']}")

	fixed: list[tuple[int, str]] = [(0, "\n".join(head))]
	if finding.get("title"):
		fixed.append((0, "Title:\n" + block("title", ai_budget.clip(format_durations(finding["title"], threshold_ms), 400))))
	had_callsite = bool(callsite.get("filename") and callsite.get("lineno") is not None)
	if had_callsite:
		fn = f" ({callsite['function']})" if callsite.get("function") else ""
		fixed.append((
			0,
			"Callsite (the closest non-framework frame to the cost; the loop or call may be in a function it calls):\n"
			+ block("callsite", ai_budget.clip(f"{callsite['filename']}:{callsite['lineno']}{fn}", 400)),
		))
	hot_fn = (detail.get("function") or "").strip()
	cum_ms = detail.get("cumulative_ms")
	wall_ms = detail.get("action_wall_time_ms")
	if hot_fn and cum_ms:
		share = ""
		try:
			if wall_ms:
				share = (
					f", {round(float(cum_ms) / float(wall_ms) * 100)}% of this action's "
					f"{humanize_duration_ms(float(wall_ms), threshold_ms=threshold_ms)} wall time"
				)
		except (TypeError, ValueError, ZeroDivisionError):
			share = ""
		fixed.append((
			1,
			f"Hot function (dominates this action): ~{humanize_duration_ms(float(cum_ms), threshold_ms=threshold_ms)}"
			f"{share}. Point at the lines inside it that cost the time.\n" + block("function", hot_fn),
		))

	tail: list[tuple[int, str]] = []
	hot = finding.get("phase2_hotline") or {}
	hot_content = ""
	if isinstance(hot, dict) and hot.get("lineno") is not None:
		hot_content = str(hot.get("content") or "").strip()
		hl_ms = hot.get("total_ms") or 0
		hl_hits = hot.get("hits") or 0
		timing = ""
		if hl_ms:
			timing = f" ({humanize_duration_ms(float(hl_ms), threshold_ms=threshold_ms)}"
			timing += f" over {int(hl_hits)} call(s))" if hl_hits else ")"
		tail.append((
			1,
			f"Line profile: the hottest line is line {hot['lineno']}{timing}. Start your fix there."
			+ ("\n" + block("hot-line", hot_content) if hot_content else ""),
		))
	if detail.get("normalized_query"):
		tail.append((2, "Query (normalized):\n" + block("sql", _truncate(detail["normalized_query"], _MAX_QUERY_CHARS), "sql")))
	if detail.get("explain_row"):
		tail.append((4, "EXPLAIN row:\n" + block("explain", _truncate(detail["explain_row"], 800))))
	examples = detail.get("example_queries") or []
	if examples:
		shown_q = [_truncate(q, _MAX_QUERY_CHARS) for q in examples[:2]]
		tail.append((5, "Example affected queries:\n" + block("sql", "\n---\n".join(shown_q), "sql")))
	if detail.get("validation_note"):
		tail.append((6, "Note:\n" + block("validation", str(detail["validation_note"]))))

	window = finding.get("source_window") or callsite.get("source_snippet") or []
	loop_text = _loop_facts_text(finding)
	if loop_text:
		tail.append((1, loop_text))
	content, shown = "", []
	for max_lines in (*_WINDOW_STEPS, 4, 2, 1, 0):
		trimmed = ai_budget.trim_window(window, max_lines=max_lines)
		if trimmed:
			source = (
				"Source (the only code you have; copy `-` and context lines verbatim; `>>` marks the callsite "
				"line; it is a window, so if the code this finding is about is not in it, write no diff):\n"
				+ block("source", _window_lines(trimmed), "python")
			)
		elif had_callsite or window:
			source = (
				"Source: NOT AVAILABLE within this request. Write no diff; start **Fix** "
				"with \"Without seeing the code, the likely fix is\"."
			)
		else:
			source = ""
		content = ai_budget.assemble([*fixed, (0, source), *tail], budget)
		shown = [str(row.get("content", "")) for row in trimmed]
		if ai_budget.text_size(content) <= budget or not trimmed:
			break
	if hot_content and block("hot-line", hot_content) in content:
		shown.append(hot_content)
	return system, [{"role": "user", "content": content}], shown


_OPENAI_FINISH = {"stop": "stop", "length": "length"}
_ANTHROPIC_FINISH = {
	"end_turn": "stop",
	"stop_sequence": "stop",
	"max_tokens": "length",
	"model_context_window_exceeded": "length",
}
_CONTEXT_LIMIT_RE = re.compile(r"context[ _-]?(?:length|window|size)|maximum context|too many tokens|num_ctx", re.I)
