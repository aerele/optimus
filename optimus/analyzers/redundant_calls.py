# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Analyzer: detect redundant frappe.get_doc / cache.get_value / has_permission calls.

Reads the per-recording sidecar argument log, buckets entries by (fn_name,
identifier_safe hash) so equivalent values cluster regardless of literal text,
and emits one Redundant Call finding per bucket whose anchored callsite alone
reaches a configurable threshold within one action. Optimus's own calls are
skipped. technical_detail_json carries both identifier_safe and identifier_raw
(the renderer uses identifier_raw).
"""

import json
import re
from collections import Counter, defaultdict

from optimus.analyzers.base import (
	CALLSITE_WALK_FIXED,
	CALLSITE_WALK_KEY,
	AnalyzerResult,
	installed_apps_allowlist,
	is_framework_callsite,
	walk_callsite,
)

_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:/")
# ``<app>/<app>/``: what follows the bench's apps dir (apps/<app>/<app>/<module>/...).
_BENCH_APP_RE = re.compile(r"([^/]+)/\1/")

DEFAULT_REDUNDANT_HIGH_MULTIPLIER = 5


def _conf_int(key: str, default: int) -> int:
	"""Read an int knob from site_config.json (the high multiplier), returning
	``default`` when unset."""
	try:
		import frappe

		v = frappe.conf.get(key)
		if v is not None:
			return int(v)
	except Exception:
		pass
	return default


def _threshold_for(fn_name: str, cfg) -> int:
	"""Return the count threshold for a sidecar fn_name, from Optimus Settings
	(with site_config.json and hardcoded defaults as fallbacks)."""
	if fn_name == "get_doc":
		return cfg.redundant_doc_threshold
	if fn_name == "cache_get":
		return cfg.redundant_cache_threshold
	if fn_name == "has_permission":
		return cfg.redundant_perm_threshold
	return 999_999


def _title_for(fn_name: str, identifier_safe, count: int) -> str:
	if fn_name == "get_doc":
		doctype, name_hash = identifier_safe
		return f"Redundant doc fetch: {doctype} {name_hash} ({count} times)"
	if fn_name == "cache_get":
		return f"Redundant cache lookup: {identifier_safe} ({count} times)"
	if fn_name == "has_permission":
		doctype, name_hash, ptype = identifier_safe
		return f"Redundant permission check: {doctype} {name_hash} {ptype} ({count} times)"
	return f"Redundant call: {fn_name} ({count} times)"


def _customer_description_for(fn_name: str, count: int, callsite: dict | None = None) -> str:
	"""Build the customer description, appending the callsite (file:line) when
	available so the user can navigate to the loop."""
	site_hint = ""
	if callsite:
		fn_site = callsite.get("filename") or ""
		ln = callsite.get("lineno")
		if fn_site and ln:
			site_hint = f" The loop is at **{fn_site}:{ln}**."

	if fn_name == "get_doc":
		return (
			f"The same document was fetched **{count} times** from the same "
			"line of code. This is almost always a loop that reloads a "
			"document inside its body caching the result outside the loop "
			"would eliminate the redundant fetches."
			f"{site_hint}"
		)
	if fn_name == "cache_get":
		return (
			f"The same cache key was looked up **{count} times** from the "
			"same callsite. Cache lookups are cheap individually but add up "
			"in a hot loop; reading once and re-using is the fix."
			f"{site_hint}"
		)
	if fn_name == "has_permission":
		return (
			f"The same permission check ran **{count} times** from the same "
			"callsite. Permission checks involve role lookups and DocType "
			"validation caching the result for the duration of the action "
			"is the standard fix."
			f"{site_hint}"
		)
	return f"A function was called {count} times redundantly.{site_hint}"


def _to_hashable(value):
	"""Convert nested lists to nested tuples so the value can be a dict key."""
	if isinstance(value, list):
		return tuple(_to_hashable(v) for v in value)
	if isinstance(value, tuple):
		return tuple(_to_hashable(v) for v in value)
	return value


_LIBRARY_MARKERS = ("/site-packages/", "/dist-packages/", "/lib/python", "/Lib/")


def _is_library_path(filename: str) -> bool:
	"""True for installed libraries and the Python stdlib. Any other absolute frame
	(an editable-installed app outside the bench) is user code and is kept."""
	return any(marker in filename for marker in _LIBRARY_MARKERS)


def _cut_at_bench_apps(filename: str) -> str:
	"""The part of ``filename`` after the bench's ``/apps/`` dir: the last ``/apps/``
	followed by ``<app>/<app>/`` (the bench layout), else the last ``/apps/``. The plain
	last ``/apps/`` can be an ``apps`` package inside the app
	(``.../apps/myapp/myapp/apps/x.py`` would become ``x.py``, an unknown app root)."""
	tails = []
	at = filename.find("/apps/")
	while at != -1:
		tails.append(filename[at + len("/apps/"):])
		at = filename.find("/apps/", at + 1)  # overlapping: an app named ``apps``
	for tail in reversed(tails):
		if _BENCH_APP_RE.match(tail):
			return tail
	return tails[-1]


def _relative_frames(stack: list):
	"""Yield ``stack``'s frames as ``_apps_relative_stack`` keeps them, lazily."""
	for frame in stack or []:
		if not isinstance(frame, dict):
			continue
		filename = str(frame.get("filename") or "").replace("\\", "/")
		if "/apps/" in filename:
			yield dict(frame, filename=_cut_at_bench_apps(filename))
		elif (filename.startswith("/") or _WINDOWS_ABS_RE.match(filename)) and _is_library_path(filename):
			continue
		else:
			yield frame


def _apps_relative_stack(stack: list) -> list:
	"""``stack`` with absolute bench paths cut to ``<app>/...`` and absolute frames outside
	the bench apps dropped, as frappe/recorder.py:97 does for SQL stacks
	(``TRACEBACK_PATH_PATTERN = ".*/apps/"``). Without it a bench under
	``/home/frappe/frappe-bench`` puts ``frappe/`` in every path, walk_callsite skips every
	frame and the finding is lost (P4). Relative and Server Script frames pass through,
	as do absolute frames that are neither under /apps/ nor library or stdlib code."""
	return list(_relative_frames(stack))


def _app_root(filename: str) -> str:
	"""First segment of an apps-relative filename (``apps/<app>/...`` or ``<app>/...``)."""
	if filename.startswith("apps/"):
		filename = filename[len("apps/"):]
	return filename.split("/", 1)[0]


def _is_optimus_own(stack: list) -> bool:
	"""True when the innermost frame outside ``frappe/`` (``stack`` is innermost-first, as
	capture records it) is Optimus's own code. Optimus reads its settings from the cache
	on every recorded query (``_profiler_register`` -> ``_read_extras`` ->
	``settings.get_config``), so the sidecar logs a cache lookup per query; the walk skips
	Optimus's frames and would blame that lookup on the user's query line. Matched on the
	app root after the bench cut, so an app merely named like Optimus is not skipped."""
	for frame in _relative_frames(stack):
		filename = str(frame.get("filename") or "").replace("\\", "/")
		if not filename:
			continue
		root = _app_root(filename)
		if root == "frappe":
			continue
		return root == "optimus"
	return False


def _callsite_key(callsite: dict | None) -> tuple | None:
	if not callsite:
		return None
	return (callsite.get("filename"), callsite.get("lineno"))


def _anchored(votes: list) -> list:
	"""The occurrences at the most frequent callsite among ``votes`` (ties: the first
	seen); [] when there are no votes."""
	if not votes:
		return []
	top_key = Counter(_callsite_key(cs) for _action, _raw, cs in votes).most_common(1)[0][0]
	return [w for w in votes if _callsite_key(w[2]) == top_key]


def _max_in_any_action(occurrences: list) -> int:
	"""How many of ``occurrences`` the busiest action holds (0 for none)."""
	counts = Counter(action_idx for action_idx, _raw, _cs in occurrences)
	return counts.most_common(1)[0][1] if counts else 0


def analyze(recordings: list, context) -> AnalyzerResult:
	# Read settings once for this analyze pass avoids N cache
	# lookups for an N-bucket analysis.
	from optimus.settings import get_config
	cfg = get_config()
	tracked_apps = cfg.tracked_apps  # may be empty (→ exclusion mode)
	installed_apps = installed_apps_allowlist()

	# Bucket: (fn_name, identifier_safe_tuple) → list of
	# (action_idx, raw, caller_stack)
	buckets: dict = defaultdict(list)
	truncation_seen = False
	skipped_unhashable = 0

	for action_idx, recording in enumerate(recordings):
		sidecar = recording.get("sidecar") or []
		for entry in sidecar:
			if not isinstance(entry, dict):
				continue
			if entry.get("_truncated"):
				truncation_seen = True
				continue
			fn_name = entry.get("fn_name")
			safe = entry.get("identifier_safe")
			raw = entry.get("identifier_raw")
			caller_stack = entry.get("caller_stack") or []
			if fn_name is None or safe is None:
				continue
			if _is_optimus_own(caller_stack):
				# Optimus's own call (its per-query settings read), not the user's.
				continue
			try:
				key = (fn_name, _to_hashable(safe))
				buckets[key].append((action_idx, raw, caller_stack))
			except TypeError:
				skipped_unhashable += 1
				continue

	if skipped_unhashable:
		context.warnings.append(
			f"redundant_calls: skipped {skipped_unhashable} sidecar entries "
			"with unhashable identifiers (likely dict-arg get_doc on unsaved docs)."
		)

	if truncation_seen:
		context.warnings.append(
			"Sidecar argument log was truncated for at least one recording "
			"redundant call detection may be incomplete."
		)

	findings: list = []
	# v0.5.2: track how many buckets we dropped as framework-only so we
	# can surface a soft warning (same pattern as index_suggestions'
	# drop counts).
	drop_framework_callsite = 0
	# And how many had no caller stack at all (sidecars captured before
	# v0.5.2 when caller_stack wasn't recorded).
	drop_no_caller_stack = 0
	# v0.5.2 round 2: buckets whose count threshold was only reached by
	# summing ACROSS many actions (e.g. "25 calls" that turned out to
	# be 1 call in each of 25 requests not a loop, just a call that
	# naturally fires once per request).
	drop_cross_request_spread = 0

	for (fn_name, safe_key), occurrences in buckets.items():
		threshold = _threshold_for(fn_name, cfg)
		if len(occurrences) < threshold:
			continue

		# v0.5.2 round 2: a "redundant call" is a LOOP, meaning the
		# threshold must be reached WITHIN a single action. Cross-
		# request aggregation (25 separate requests each calling cache
		# once) isn't a loop it's a framework call that naturally
		# fires once per request. Production report had 3 "Redundant
		# cache lookup: … (25 times)" / "(36 times)" findings from
		# werkzeug/serving.py:370 each was 1 call per request across
		# 25/36 requests, not a repeated in-loop lookup.
		if _max_in_any_action(occurrences) < threshold:
			drop_cross_request_spread += 1
			continue

		# P14: walk every occurrence's own stack (cut to apps-relative paths, P4) and
		# anchor the bucket on the callsite most of them share (ties: the first seen),
		# with the action and the identifier of the occurrences there. The first
		# occurrence alone could point at a non-loop line, the wrong action, or a
		# framework frame. Cache sidecars store innermost-first stacks; the walker
		# expects outermost-first, so the selected frame is the caller of the lookup.
		walked = [
			(action_idx, raw, walk_callsite(list(reversed(_apps_relative_stack(stack)))))
			for action_idx, raw, stack in occurrences
			if stack
		]
		if not walked:
			# Recording captured before v0.5.2 OR stack capture failed.
			drop_no_caller_stack += 1
			continue
		# A user loop is never outvoted by a more frequent framework callsite: when any
		# occurrence resolves to non-framework code, only those vote. But a user callsite
		# that is no loop itself (one user call beside 30 from an ERPNext loop) must not
		# take the bucket either: the bucket is then anchored on its most frequent
		# callsite of any kind, so a framework loop is suppressed as one (C3).
		actionable = [
			w for w in walked
			if w[2] is not None
			and not is_framework_callsite(
				w[2].get("filename") or "", tracked_apps=tracked_apps, installed_apps=installed_apps
			)
		]
		anchored = _anchored(actionable)
		if _max_in_any_action(anchored) < threshold:
			anchored = _anchored(walked)
		callsite = anchored[0][2]
		if callsite is None or is_framework_callsite(
			callsite.get("filename") or "", tracked_apps=tracked_apps, installed_apps=installed_apps
		):
			# Pure framework stack. walk_callsite returns None for
			# profiler-own stacks; for pure frappe/* stacks it falls
			# back to the deepest frame (so legitimate migration /
			# background-task findings don't disappear). Here we
			# ADDITIONALLY filter any callsite that resolves to an
			# official Frappe-maintained app (frappe, erpnext, hrms,
			# …) or a pip-installed third-party lib the loop inside
			# those isn't actionable for application developers.
			# Same rationale as the Framework N+1 filter.
			drop_framework_callsite += 1
			continue

		# C3: the finding is the anchored callsite's loop, so its count, loop size,
		# severity and title come from the anchored occurrences alone, and they must
		# reach the threshold on their own, within one action, as the bucket did.
		count = len(anchored)
		if count < threshold:
			continue
		action_counts = Counter(a for a, _raw, _cs in anchored)
		max_in_any_action = action_counts.most_common(1)[0][1]
		if max_in_any_action < threshold:
			drop_cross_request_spread += 1
			continue

		# Callsite IS user code (or at least contains a user frame).
		# Emit the finding with the callsite in the detail so users
		# can navigate to the loop.

		high_multiplier = _conf_int(
			"optimus_redundant_high_multiplier", DEFAULT_REDUNDANT_HIGH_MULTIPLIER
		)
		# v0.5.2 round 2: severity based on max-in-any-action, not
		# total count. Because count was established above to reflect
		# loop density within a single action, using it for severity
		# misleads ("100 cross-request cache calls" looks worse than
		# "100 cache calls in one loop in one request"). Use
		# max_in_any_action instead.
		severity = (
			"High"
			if max_in_any_action >= threshold * high_multiplier
			else "Medium"
		)

		# Action ref = the action containing the most anchored occurrences
		# (ties: the first seen).
		top_action_idx = action_counts.most_common(1)[0][0]

		identifier_safe = safe_key
		identifier_raw = anchored[0][1]

		findings.append({
			"finding_type": "Redundant Call",
			# Title/description report the per-action LOOP magnitude
			# (max_in_any_action), not the cross-action total (count) the
			# loop ran max_in_any_action times in its hottest request and
			# saying "(50 times)" when 50 = 10×5 requests overstates the loop
			# ("almost always a loop" reads as 50-in-a-row). Severity already
			# uses max_in_any_action; the total + distinct_actions stay in
			# technical_detail_json.
			"severity": severity,
			"title": _title_for(fn_name, identifier_safe, max_in_any_action),
			"customer_description": _customer_description_for(
				fn_name, max_in_any_action, callsite=callsite
			),
			"technical_detail_json": json.dumps({
				CALLSITE_WALK_KEY: CALLSITE_WALK_FIXED,
				"fn_name": fn_name,
				"identifier_safe": (
					list(identifier_safe) if isinstance(identifier_safe, tuple) else identifier_safe
				),
				"identifier_raw": (
					list(identifier_raw) if isinstance(identifier_raw, tuple) else identifier_raw
				),
				"count": count,
				"distinct_actions": len(action_counts),
				# v0.5.2: surface the callsite so developers can
				# actually navigate to the loop. Pre-v0.5.2 the only
				# identifier was a sha256 hash of the cache key
				# useless for finding the offending code.
				"callsite": {
					"filename": callsite.get("filename"),
					"lineno": callsite.get("lineno"),
					"function": callsite.get("function"),
				},
			}, default=str),
			"estimated_impact_ms": 0,
			"affected_count": count,
			"action_ref": str(top_action_idx),
		})

	if drop_cross_request_spread:
		context.warnings.append(
			f"Suppressed {drop_cross_request_spread} Redundant Call "
			"candidate(s) where the threshold was reached only by "
			"summing across multiple requests (e.g. one cache lookup "
			"per request × 25 requests). That's not a loop it's a "
			"call that naturally fires once per request. A real "
			"redundant loop has the threshold met WITHIN a single "
			"action."
		)
	if drop_framework_callsite:
		context.warnings.append(
			f"Suppressed {drop_framework_callsite} Redundant Call "
			"finding(s) whose loop lives inside Frappe framework code "
			"or a third-party library (users can't act on those). "
			"The hot ones still show up in the Repeated Hot Frame "
			"leaderboard if they represent significant time."
		)

	if drop_no_caller_stack:
		context.warnings.append(
			f"Skipped {drop_no_caller_stack} Redundant Call candidate(s) "
			"with no captured caller stack. Re-run the session on the "
			"v0.5.2+ profiler to enable callsite-based filtering."
		)

	return AnalyzerResult(findings=findings)
