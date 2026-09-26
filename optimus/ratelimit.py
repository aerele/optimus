# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Per-user rate limits for Optimus endpoints.

Frappe's ``@rate_limit(key=...)`` reads ``key`` as the name of a request form field, so its bucket
is per IP (plus that field's value): users behind one NAT share a bucket and a caller can open a
fresh bucket by sending the field with a new value. This module keys the bucket on the action, the
window length and the logged-in user only. Frappe #42815 adds ``@rate_limit(user_based=True)``
upstream; it is not in v16.18 (``test_ratelimit.py::test_frappe_rate_limit_user_based_canary``
fails once the installed Frappe has it), so switch to it once Optimus requires such a release.

Limits are fixed windows: every call ``INCR``s the counter and the call that creates it arms the
window with ``EXPIRE``; a counter found without a TTL (a crash between the two, or an eviction
race) is re-armed on the next call, so nobody is locked out for good. Only ``incr``, ``ttl`` and
``expire`` are used on ``frappe.cache`` (plain ``set``/``get`` bypass Frappe's per-site key prefix;
the key goes through ``frappe.cache.make_key``). The caller passes defaults; a site can override
any action in
site_config, for example ``"optimus_rate_limits": {"refill_ai_suggestions": [12, 3600]}``.

Call ``enforce_user_rate_limit`` only after the caller passed its permission check, so a denied
call never uses up a user's budget. A Redis error propagates, as it does for Frappe's own limiter.
"""

from __future__ import annotations

import frappe
from frappe import _

from optimus import redis_keys


def effective_limits(action: str, limit: int, seconds: int) -> tuple[int, int]:
	"""Return ``(limit, seconds)`` for ``action``: the site_config override when it is a pair of
	positive integers, else the caller's defaults. A malformed override is ignored, never fatal."""
	try:
		overrides = frappe.conf.get("optimus_rate_limits") or {}
		pair = overrides.get(action) if isinstance(overrides, dict) else None
		if pair is not None and not isinstance(pair, (str, bytes)) and len(pair) == 2:
			new_limit, new_seconds = int(pair[0]), int(pair[1])
			if new_limit >= 1 and new_seconds >= 1:
				return new_limit, new_seconds
	except Exception:
		pass
	return limit, seconds


def enforce_user_rate_limit(action: str, *, limit: int, seconds: int) -> None:
	"""Count one ``action`` call for the current user and raise ``frappe.RateLimitExceededError``
	(HTTP 429) once the user passes ``limit`` calls in the current ``seconds`` window.

	The bucket depends only on the action, the window length and the user; nothing in the request
	(form fields, IP) can move a caller into a fresh bucket."""
	limit, seconds = effective_limits(action, limit, seconds)
	user = frappe.session.user
	key = frappe.cache.make_key(redis_keys.user_rate_limit(action, user, seconds))
	count = frappe.cache.incr(key)
	if count == 1 or frappe.cache.ttl(key) < 0:
		# This call opened the window, or the counter lost its TTL (a crash between INCR and
		# EXPIRE): arm it, or the counter would never reset.
		frappe.cache.expire(key, seconds)
	if count > limit:
		frappe.throw(
			_("You have reached the limit of {0} {1} requests in {2} seconds. Please try again later.").format(
				limit, action, seconds
			),
			frappe.RateLimitExceededError,
			title=_("Optimus"),
		)
