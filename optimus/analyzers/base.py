# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Shared types for the analyzer pipeline.

Every analyzer is a pure function with this signature:

    analyze(recordings: list[dict], context: AnalyzeContext) -> AnalyzerResult

The analyzer reads the recording dicts (already enriched by analyze.py with
sqlparse-formatted queries, EXPLAIN output, normalized queries, and
exact/normalized copy counts) and returns:

    actions Optimus Action child rows (only per_action populates this)
    findings Optimus Finding child rows (each analyzer may emit findings)
    aggregate top-level dict-shaped data (e.g. top_queries, table_breakdown)
    warnings non-fatal issues to surface in the report

Pure means: no Frappe DB access, no Redis access, no I/O. Analyzers operate
only on the data passed in. Side-effects are limited to the AnalyzerResult
they return. The orchestrator (analyze.py) merges all results and persists
them once.

This makes analyzers trivially unit-testable from JSON fixtures and easy to
reason about: each one is a pure data transformation.
"""

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Shared constants and helpers (Round 2 fixes #19 + #20)
# ---------------------------------------------------------------------------
# Severity sort order lower number is higher severity. Used by every
# analyzer when sorting its findings list. Moved here from per-module
# copies to keep the ordering consistent across the pipeline.
SEVERITY_ORDER: dict[str, int] = {"High": 0, "Medium": 1, "Low": 2}


def humanize_duration_ms(ms, decimals: int = 0, threshold_ms: float = 1000.0) -> str:
	"""Format a duration for prose: ``"<n>ms"`` below ``threshold_ms``,
	``"<n.nn>s"`` at or above it. One second is 1000ms, so a duration that
	reaches a full second reads as seconds instead of a four-digit
	millisecond count (better to skim "1.50s" than "1500ms").

	Shared by every analyzer and by analyze.py so a duration reads the same
	way wherever it lands in a finding title or description. Plain text, no
	markup, no space before the unit so it drops into a sentence cleanly
	("took 1.23s"). ``decimals`` sets the millisecond precision only; the
	seconds branch always keeps two decimals. Defensive: ``None`` or a
	non-numeric value formats as zero.
	"""
	try:
		v = float(ms) if ms is not None else 0.0
	except (TypeError, ValueError):
		v = 0.0
	if threshold_ms and abs(v) >= threshold_ms:
		return f"{v / 1000:.2f}s"
	return f"{v:.{decimals}f}ms"


# Path prefixes we treat as "framework" when picking a representative
# callsite for a query. The goal is to blame the user's business logic,
# not the frappe helper the query was routed through (get_value,
# get_all, db.count etc.). See the detailed explanation in
# analyzers/n_plus_one.py this is just a shared constant now.
#
# Intentionally narrower than FRAMEWORK_APPS below: walk_callsite uses
# this to pick a BLAME frame (skip frappe helpers, surface the caller).
# We don't skip erpnext/hrms/etc. here because when a user's app calls
# into erpnext, the deepest erpnext frame is still a legitimate blame
# target (user can at least refactor their calling pattern). The
# is_framework_callsite() FILTER (below) routes those into Observations
# separately, which is the right layer for the noise filter.
FRAMEWORK_PREFIXES: tuple[str, ...] = (
	"frappe/",
	"optimus/",
)

# v0.5.2: official Frappe-maintained apps. When a finding's BLAME
# frame resolves inside one of these apps, the user can't practically
# act on it fixes live upstream, not in their bench. The renderer
# routes these into the collapsed Observations subsection (see the
# split in renderer.py + redundant_calls / explain_flags / n_plus_one
# filters).
#
# Production trigger: a raw session on a Sales Invoice Save+Submit
# surfaced 10 "Redundant cache lookup: <hash> (106 times)" findings
# all landing in apps/erpnext/.../sales_invoice.py:300-321 a loop
# inside ERPNext that the application developer can't patch.
FRAMEWORK_APPS: frozenset[str] = frozenset({
	"frappe",
	"optimus",
	"erpnext",
	"payments",
	"hrms",
	"lms",
	"helpdesk",
	"insights",
	"crm",
	"builder",
	"wiki",
	"drive",
})

# Well-known third-party libs to catch even when sys.path manipulation bypasses
# site-packages/ (pyinstrument strips the prefix, so a lib arrives as
# ``pandas/core/frame.py``). Checked by is_framework_callsite() by matching the
# resolved app ROOT (first segment) NOT a substring anywhere because frappe's
# recorder strips the ``apps/`` prefix, so a user callsite is ``<app>/<app>/…`` and
# a stripped lib is ``<lib>/…``; a lib name DEEPER in a relative path is therefore
# the user's own submodule (``myapp/myapp/requests/…``), not the library, and must
# stay actionable. (Bare names, matched like call_tree's _THIRD_PARTY_LIB_SEGMENTS,
# so the two surfaces agree. Out-of-bench absolute paths get a segment-anywhere
# fallback in is_framework_callsite for the top-segment-is-a-filesystem-prefix case.)
_THIRD_PARTY_LIB_NAMES: frozenset[str] = frozenset({
	# DB drivers / caches / queues / web servers
	"MySQLdb", "pymysql", "psycopg2", "redis", "celery", "rq",
	"werkzeug", "gunicorn", "urllib3", "requests", "httpx",
	# cloud / serialization / templating / sanitization
	"boto3", "botocore", "jinja2", "markupsafe", "bleach", "nh3",
	# data / imaging
	"pandas", "numpy", "openpyxl", "PIL",
	# parsing / crypto / dates / profiler
	"sqlparse", "cryptography", "pytz", "dateutil", "pyinstrument",
})

# v0.6.0: Frappe's framework-managed columns every `tab*` table has these.
# Frappe writes (most of) them on every save (`modified`, `modified_by`,
# `idx`), on insert (`creation`, `owner`), on submit/cancel (`docstatus`), or
# they're already auto-indexed (`name` is the PK; `parent` is auto-indexed on
# child tables). Suggesting an index on any of them is a write-cost trap the
# developer shouldn't be nudged into so every index-suggestion path
# (index_suggestions.py, table_breakdown.py's per-table candidates, and the
# AI "suggest a fix" prompt) skips them.
#
# Mirrors `frappe.model.default_fields` + `frappe.model.optional_fields`.
# Analyzers are pure (no `import frappe`), so this is a hardcoded snapshot
# update it if Frappe adds a standard column.
FRAPPE_METADATA_COLUMNS: frozenset[str] = frozenset({
	# frappe.model.default_fields
	"name", "owner", "creation", "modified", "modified_by",
	"docstatus", "parent", "parentfield", "parenttype", "idx", "doctype",
	# frappe.model.optional_fields
	"_user_tags", "_comments", "_assign", "_liked_by", "_seen",
})


def is_frappe_metadata_column(name) -> bool:
	"""Case-insensitive membership test for ``FRAPPE_METADATA_COLUMNS``."""
	return bool(name) and str(name).strip().lower() in FRAPPE_METADATA_COLUMNS


# v0.6.0: Frappe's framework "meta" tables the ones that store the schema
# itself (DocType / DocField / Custom Field / Property Setter), the Single-
# doctype value store, the naming-series counters, the global-search index,
# the migration log, and UI/dashboard/print configuration. `bench migrate`
# owns these tables' structure (including their indexes), they're tiny or
# write-on-every-customization, and indexing them by hand via raw SQL is
# pointless (and would be clobbered on the next migrate). So no index-
# suggestion path proposes an index on a table in this set; the table
# breakdown still lists it (you may still want to know "30ms in tabSingles"),
# it just won't get index candidates.
#
# Curated snapshot content / log / queue tables (`tabFile`, `tabVersion`,
# `tabEmail Queue`, `tabCommunication`, `tabError Log`, …) are deliberately
# NOT here: those grow large and DO legitimately want application-chosen
# indexes.
FRAPPE_META_TABLES: frozenset[str] = frozenset({
	# DocType / schema definition
	"tabDocType", "tabDocField", "tabDocPerm", "tabCustom DocPerm",
	"tabDocType Action", "tabDocType Link", "tabDocType State",
	"tabDocType Layout", "tabModule Def",
	# Customization
	"tabCustom Field", "tabProperty Setter", "tabClient Script",
	"tabServer Script", "tabCustom HTML Block",
	# Single-doctype value store, naming series, global search
	"tabSingles", "tabSeries", "tab__global_search",
	# UI / dashboards / print configuration
	"tabWorkspace", "tabWorkspace Link", "tabWorkspace Shortcut",
	"tabWorkspace Chart", "tabWorkspace Quick List",
	"tabWorkspace Number Card", "tabWorkspace Custom Block",
	"tabDashboard", "tabDashboard Chart", "tabDashboard Chart Source",
	"tabNumber Card", "tabNumber Card Link",
	"tabPrint Format", "tabLetter Head",
	# App / migration bookkeeping
	"tabPatch Log", "tabInstalled Application", "tabInstalled Applications",
	"tabPackage", "tabPackage Import",
	# Misc framework config
	"tabRole", "tabRole Profile", "tabModule Profile",
})
_FRAPPE_META_TABLES_LOWER: frozenset[str] = frozenset(t.lower() for t in FRAPPE_META_TABLES)


def is_frappe_meta_table(name) -> bool:
	"""Case-insensitive membership test for ``FRAPPE_META_TABLES`` (also
	tolerates a backtick-quoted name, though ``sql_metadata`` returns the
	bare name)."""
	return bool(name) and str(name).strip().strip("`").lower() in _FRAPPE_META_TABLES_LOWER


# v0.6.x: framework-internal tables user/session/auth bookkeeping that
# every Frappe request touches via session.get_user / get_roles / etc.,
# irrespective of the app code. Distinct from FRAPPE_META_TABLES (= "Frappe
# owns the schema, no custom indexes survive a migrate"): these *are* real
# data tables, but app developers can't really change how often they're
# queried because the queries come from framework machinery. Surfaced via
# the "Hide framework / internal database tables" setting (default on).
FRAMEWORK_INTERNAL_TABLES: frozenset[str] = frozenset({
	"tabHas Role",
	"tabDefaultValue",
	"tabUser Social Login",
	"tabUser Role Profile",
	"tabBlock Module",
	"tabUser Email",
})
_FRAMEWORK_INTERNAL_TABLES_LOWER: frozenset[str] = frozenset(
	t.lower() for t in FRAMEWORK_INTERNAL_TABLES
)


def is_framework_db_table(name) -> bool:
	"""True for tables that are noise in the "Time spent per database table"
	breakdown schema/meta (``FRAPPE_META_TABLES``), user/session bookkeeping
	(``FRAMEWORK_INTERNAL_TABLES``), or MySQL system tables
	(``information_schema.*``). Case-insensitive + backtick-tolerant."""
	if not name:
		return False
	norm = str(name).strip().strip("`").lower()
	if not norm:
		return False
	if norm in _FRAPPE_META_TABLES_LOWER:
		return True
	if norm in _FRAMEWORK_INTERNAL_TABLES_LOWER:
		return True
	if norm.startswith("information_schema."):
		return True
	return False


# Core Frappe/ERPNext tables that take many INSERT/UPDATE rows per business
# transaction (every submitted voucher, every stock move, …). An extra index
# on one of these costs write time across many flows even though a single
# profiling session may only show one write the report flags that so an
# index recommendation here is treated conservatively.
WRITE_HOT_TABLES: frozenset[str] = frozenset({
	# Accounting / stock ledgers written in bulk on every submit
	"tabGL Entry", "tabStock Ledger Entry", "tabPayment Ledger Entry",
	"tabSerial and Batch Bundle", "tabSerial and Batch Entry",
	"tabBin", "tabSerial No", "tabBatch", "tabRepost Item Valuation",
	# Framework write-on-save / high-churn log tables
	"tabVersion", "tabComment", "tabActivity Log", "tabNotification Log",
	"tabError Log", "tabScheduled Job Log", "tabEmail Queue", "tabEmail Queue Recipient",
	"tabDeleted Document", "tabAccess Log", "tabView Log",
})
_WRITE_HOT_TABLES_LOWER: frozenset[str] = frozenset(t.lower() for t in WRITE_HOT_TABLES)


def is_write_hot_table(name) -> bool:
	"""Case-insensitive membership test for ``WRITE_HOT_TABLES``."""
	return bool(name) and str(name).strip().strip("`").lower() in _WRITE_HOT_TABLES_LOWER


def _last_app_segment(norm: str) -> str | None:
	"""The ``<app>`` in a real ``apps/<app>/`` segment, or None.

	Boundary-anchored, so:
	- a bench nested under a folder that is itself named ``apps``
	  (``/opt/apps/frappe-bench/apps/erpnext/…``) resolves the REAL app
	  (``erpnext``), not the bench dir the LAST ``/apps/`` on an ABSOLUTE path
	  wins; and
	- an app whose own name merely ends in ``apps`` (``webapps/module.py``) is
	  NOT mistaken for the bench ``apps/`` dir.
	A mid-path ``/apps/`` in a RELATIVE path is a user subpackage, not the bench
	apps dir (the recorder strips the bench prefix, so bench code arrives as
	``apps/<app>/…`` or ``<app>/<app>/…``: never ``<app>/apps/…``); so
	``myapp/apps/foo.py`` resolves to None here, letting the caller fall back to the
	top segment ``myapp``. Returns None when there's no ``apps/`` boundary at all.
	"""
	if norm.startswith("apps/"):
		tail = norm[len("apps/"):]
	elif norm.startswith("/") and "/apps/" in norm:
		# Absolute bench path: the LAST '/apps/' boundary = the real bench apps dir
		# even when an ancestor directory is also called 'apps'.
		tail = norm.rsplit("/apps/", 1)[1]
	else:
		return None
	first = tail.split("/", 1)[0]
	return first or None


def _extract_app_segment(norm: str) -> str | None:
	"""Return the app name from a normalized filename, or None.

	Handles both path shapes we see in recorder stacks:
	- ``apps/<app>/<app>/foo.py`` (bench-relative)
	- ``<app>/foo.py`` (pyinstrument short form after path strip)
	- ``/abs/path/to/apps/<app>/<app>/foo.py`` (absolute)
	- ``/abs/path/<arbitrary>/foo.py`` (absolute without ``apps/``)

	For the bench-relative / absolute forms we return the segment that follows
	the real (boundary-anchored, last-wins) ``apps/``. For the short form we
	treat the first path segment as the app. When neither ``apps/`` is found nor
	the path has any non-slash segment, return ``None``.
	"""
	if not norm:
		return None
	app = _last_app_segment(norm)
	if app:
		return app
	# v0.7.x: strip any leading slashes so absolute paths without
	# ``apps/`` (e.g. ``/Users/.../foo.py`` test fixtures) still
	# produce a non-empty segment instead of falling through to
	# ``_OTHER_APP_LABEL``. The first non-slash segment is a
	# reasonable best-effort app label; if the segment ends up
	# being a meaningless prefix (``Users``, ``tmp``), the row
	# still buckets under that label rather than disappearing
	# from the rendered Findings section.
	stripped = norm.lstrip("/")
	first = stripped.split("/", 1)[0]
	return first or None


def installed_apps_allowlist() -> frozenset[str] | None:
	"""The site's installed Frappe apps, as the ground-truth allowlist for
	exclusion-mode classification or ``None`` when frappe isn't importable
	(off-bench unit tests), in which case callers fall back to the hardcoded
	third-party heuristic. Lazy frappe import mirrors ``call_tree._top_level_app``;
	never raises. Analyzers resolve this ONCE and thread it in, so a real site
	classifies application-vs-library from ground truth instead of guessing from a
	name an installed app named like a library (``redis``) is the user's code,
	and a real library that isn't an installed app is not."""
	try:
		import frappe
		apps = frappe.get_installed_apps()
		return frozenset(apps) if apps else None
	except Exception:
		return None


def is_framework_callsite(
	filename: str | None,
	tracked_apps: tuple[str, ...] | None = None,
	installed_apps: frozenset[str] | None = None,
) -> bool:
	"""True if ``filename`` lives inside framework or third-party code
	that the application developer can't practically patch.

	Two modes, chosen by whether ``tracked_apps`` is provided:

	**Inclusion mode**: when ``tracked_apps`` is a non-empty tuple, the
	classifier flips: a callsite is framework *unless* its app matches
	one of the tracked apps. This is what ``Optimus Settings ▸ Tracked
	Apps`` configures it lets the site admin say "I only care about
	findings in myapp" and get everything else routed to Observations
	without having to enumerate every framework app.

	**Exclusion mode**: when ``tracked_apps`` is None or empty, the classifier
	uses the site's installed-apps allowlist as ground truth: an app root that is
	an installed Frappe app (and not a framework/stock app) is the developer's own
	code; everything else is library/framework. When ``installed_apps`` is None
	(off-bench unit tests), it falls back to the built-in ``FRAMEWORK_APPS`` set +
	hardcoded third-party heuristic. This is the default for sites that haven't
	configured the Single.

	Matching is on the resolved app ROOT (the ``apps/<app>/`` segment or the top
	path segment), never a mid-path substring so neither ``my_crm/`` nor a user
	submodule named ``crm/`` deep in a path is misread as the framework app.

	Used by redundant_calls, explain_flags, n_plus_one, and top_queries to route
	findings with framework-only callsites into the Observations bucket. Analyzers
	resolve ``tracked_apps`` (from ``settings.get_tracked_apps()``) and
	``installed_apps`` (from ``installed_apps_allowlist()``) ONCE and thread them in.
	"""
	if not filename:
		return False
	norm = filename.replace("\\", "/")

	# venv / system packages are always un-patchable library code checked in BOTH
	# modes (a vendored lib under a tracked app's own .venv is not that app's code,
	# so inclusion mode must not report it as an actionable user finding).
	if "site-packages/" in norm or "dist-packages/" in norm:
		return True

	# Server Scripts are the developer's own optimizable code and live in the
	# database, not in any app so they stay actionable in BOTH modes (and must
	# never be caught by the installed-apps allowlist below, whose set has no entry
	# for the synthetic ``<serverscript>`` filename).
	if norm.startswith("<serverscript") or norm.startswith("<server-script"):
		return False

	if tracked_apps:
		# Inclusion mode: framework UNLESS the app is in the allowlist.
		app = _extract_app_segment(norm)
		if app and app in tracked_apps:
			return False
		return True

	# Exclusion mode (default). Resolve the app ROOT (the boundary-anchored
	# ``apps/<app>/`` segment when present, else the top path segment) never a
	# mid-path substring, so a user submodule named like a framework app or library
	# (``mybiz/mybiz/crm/…``, ``myapp/myapp/requests/…``) is not misread.
	user_app = _last_app_segment(norm)
	app_root = user_app or norm.lstrip("/").split("/", 1)[0]

	# Framework / stock apps (frappe, erpnext, …) are never actionable, even though
	# they're installed so this check precedes the installed-apps allowlist.
	if app_root in FRAMEWORK_APPS:
		return True

	# Ground truth beats name-guessing: when the site's installed-apps allowlist is
	# available, an app root that IS an installed Frappe app is application code
	# (actionable) including an app deliberately named like a library (``redis``,
	# ``requests``). Anything else a real third-party library, an out-of-bench
	# checkout, a stray absolute/Windows path the developer can't patch → framework.
	if installed_apps:
		return app_root not in installed_apps

	# Off-bench fallback (frappe unavailable, e.g. pure-Python unit tests): the
	# hardcoded heuristic. A real ``apps/<app>/`` path whose app isn't a framework
	# app is the user's code; a known third-party lib name as the app root is
	# library; an out-of-bench absolute path matches a known lib as any segment.
	if user_app is not None:  # a real apps/<app>/ app, already known non-framework
		return False
	if app_root in _THIRD_PARTY_LIB_NAMES:
		return True
	if norm.startswith("/") and "/apps/" not in norm:
		return any(seg in _THIRD_PARTY_LIB_NAMES for seg in norm.split("/"))
	return False


def is_framework_callsite_str(
	callsite: str | None,
	tracked_apps: tuple[str, ...] | None = None,
	installed_apps: frozenset[str] | None = None,
) -> bool:
	"""``is_framework_callsite`` for the ``'filename:lineno'`` string form
	that ``walk_callsite_str`` produces (and that the ``top_queries``
	aggregate stores per row).

	A missing / empty callsite counts as framework: we can't attribute it
	to the user's app, so it doesn't belong in a "your app" leaderboard
	either.
	"""
	if not callsite:
		return True
	# The line number is always the trailing ':N' segment strip it to
	# recover the filename for the path classifier. Recorder stacks use
	# forward slashes, so a Windows drive-letter ':' isn't a concern.
	filename = callsite.rsplit(":", 1)[0] if ":" in callsite else callsite
	return is_framework_callsite(filename, tracked_apps, installed_apps)


def is_profiler_own_query(stack: list | None) -> bool:
	"""Return True if a SQL call's Python stack originates from the
	profiler's own instrumentation.

	Examples of queries that hit this path:

	- ``optimus/infra_capture.py:176``: the ``SHOW GLOBAL
	  STATUS`` snapshot run inside every ``before_request`` /
	  ``after_request`` hook. Fired ~2× per captured request.
	- ``optimus/infra_capture.py``: the one-shot ``SHOW
	  VARIABLES`` for ``max_connections`` (cached after first call).
	- Anything else the profiler queries as part of its own bookkeeping.

	These queries are real SQL that MariaDB executed, so they show up
	in the recorder's call list with stack traces. The user can't act
	on them, though they're profiler overhead, not application work.
	Before this helper, n_plus_one would surface them as:

	    "Same query ran 22× at optimus/infra_capture.py:176"

	and top_queries would include them in the slow-queries leaderboard,
	both with the profiler's own internal file path as the "blame
	frame." Filtering them out here keeps the findings user-actionable.

	The rule (walk innermost → outermost):

	- If we find a user frame (not in ``frappe/`` and not in
	  ``optimus/``) → return False. The query came from user
	  code routed through framework helpers keep it.
	- If we exhaust the stack seeing only ``frappe/`` and
	  ``optimus/`` frames AND at least one was
	  ``optimus/`` → return True. The deepest non-frappe frame
	  is inside the profiler, so the query originated there.
	- If we exhaust with only ``frappe/`` frames → return False. This
	  is a legitimate framework query (migration, fixture, internal
	  bg task) the ``walk_callsite`` fallback still surfaces it.
	"""
	if not stack:
		return False
	has_profiler_frame = False
	for frame in reversed(stack):
		if not isinstance(frame, dict):
			continue
		filename = (frame.get("filename") or "").replace("\\", "/")
		if not filename:
			continue
		# v0.5.1: substring (not startswith) so we match bench-relative
		# paths like ``apps/optimus/optimus/capture.py``
		# and absolute paths like ``/Users/.../apps/optimus/...``
		# in addition to pyinstrument's ``optimus/capture.py``
		# short form. startswith missed both the bench and absolute
		# shapes, letting profiler frames slip through to be blamed
		# as Framework N+1 findings.
		if "optimus/" in filename:
			has_profiler_frame = True
			continue
		if "frappe/" in filename:
			# Keep walking the profiler or user code may be further out.
			continue
		# Non-framework frame this is user code; the query's origin
		# is the user's business logic, not our instrumentation.
		return False
	return has_profiler_frame


def walk_callsite(stack: list | None) -> dict | None:
	"""Return the deepest non-framework frame that issued a query, or None.

	Shared implementation of the "skip frappe frames" callsite walker.
	The recorder builds `stack` outermost-to-innermost (after stripping
	its own frames), so the LAST entry is the closest /apps/ frame to
	the SQL call but that's often a frappe framework helper. We walk
	from innermost toward outermost and return the first frame whose
	filename isn't inside a framework directory.

	Returns a dict with keys `filename`, `lineno`, `function`: or None
	if the stack is empty / malformed / belongs to profiler
	instrumentation. Falls back to the innermost frame if every frame
	is in ``frappe/`` (legitimate for queries issued from inside
	frappe migrations, fixtures, etc.) so we never silently drop a
	legitimate framework finding.

	v0.5.1: stacks whose deepest non-frappe frame is inside
	``optimus/`` (as detected by ``is_profiler_own_query``)
	return None instead of falling back to the profiler frame. The
	caller's ``if not callsite: continue`` guard then drops the query
	otherwise the profiler's own ``SHOW GLOBAL STATUS`` snapshots
	show up as "Same query ran 22× at optimus/infra_capture
	.py:176" findings, which are noise the user can't act on.
	"""
	if not stack:
		return None

	for frame in reversed(stack):
		if not isinstance(frame, dict):
			continue
		filename = (frame.get("filename") or "").replace("\\", "/")
		lineno = frame.get("lineno")
		if not filename or lineno is None:
			continue
		# v0.5.1: substring (not startswith) matches bench and absolute
		# path shapes in addition to pyinstrument's short form. See the
		# matching fix in is_profiler_own_query for context.
		if any(prefix in filename for prefix in FRAMEWORK_PREFIXES):
			continue
		return frame

	# Fallback: every frame was in the framework. If the profiler itself
	# is in the stack, this is our own instrumentation drop it.
	if is_profiler_own_query(stack):
		return None

	# Pure frappe/* fallback: return the deepest frame so legitimate
	# framework queries (migrations, fixtures, background tasks) still
	# produce a finding.
	last = stack[-1] if isinstance(stack[-1], dict) else None
	if last and last.get("filename") and last.get("lineno") is not None:
		return last
	return None


def walk_callsite_str(stack: list | None) -> str | None:
	"""String-form convenience wrapper: 'filename:lineno' or None."""
	frame = walk_callsite(stack)
	if not frame:
		return None
	return f"{frame.get('filename', '?')}:{frame.get('lineno', '?')}"


# ---------------------------------------------------------------------------
# Filename display helper (v0.5.1)
# ---------------------------------------------------------------------------
# Used by analyzers that embed filenames in user-visible finding TITLES.
#
# Frappe's DocType Data field caps at 140 characters. Apps with deeply-
# nested module paths push titles over that limit and crash the analyze
# pipeline with CharacterLengthExceededError. A production session on
# jewellery_erpnext hit this with an N+1 title:
#
#   Same query ran 65× at jewellery_erpnext/jewellery_erpnext/jewellery_
#   erpnext/doctype/parent_manufacturing_order/parent_manufacturing_order
#   .py:503
#
# That's 144 chars just past the 140 limit. Shortening the filename to
# its last 2 path segments yields:
#
#   Same query ran 65× at parent_manufacturing_order/parent_manufacturing
#   _order.py:503
#
# ~90 chars well under the limit and still uniquely identifies the
# file for navigation. The full absolute path remains in the finding's
# technical_detail_json so the developer can jump to it directly.
#
# Analyzers should use this for TITLES only; customer_description and
# technical_detail_json can keep the full path for disambiguation.


# ---------------------------------------------------------------------------
# v0.5.3: "Projected after fix" timing heuristics
# ---------------------------------------------------------------------------
# Per-finding-type speedup factors. Applied to the CURRENT average per-query
# time to estimate what the same query would cost after the recommended
# fix. These are ceiling estimates a real fix could do better or worse,
# but they give the developer a rough sense of "is this worth my afternoon".
#
# Derivations:
#   Full Table Scan: scan O(N) → index lookup O(log N). For N=10k-10M the
#                    ratio is ~20×. Use 0.05.
#   Missing Index:   same the suggestion IS to add an index.
#   Filesort:        sort cost is O(N log N); with an index-ordered read,
#                    the sort disappears but the read cost remains. Typical
#                    observed speedup on Frappe DocTypes is ~3×. Use 0.30.
#   Temporary Table: materialization cost goes away when a covering index
#                    supports the GROUP BY / DISTINCT. ~2× speedup. Use 0.50.
#   Low Filter Ratio: the fix is selectivity, so projected_time ≈ current ×
#                    (filtered% / 100). Special-cased in explain_flags
#                    not a simple factor.
#   N+1 Query:       N queries × avg → 1 batched query ≈ 2 × avg. Computed
#                    directly in n_plus_one, not via this table.
_POST_FIX_SPEEDUP: dict[str, float] = {
	"Full Table Scan": 0.05,
	"Missing Index": 0.05,
	"Filesort": 0.30,
	"Temporary Table": 0.50,
}

# Minimum projected time per query. Even a perfect index lookup costs
# client/server round-trip + plan time, which is typically ~0.3-0.5ms on
# a warm MariaDB connection. Don't project below this floor otherwise
# the report claims "projected 0.0ms" which is nonsense.
POST_FIX_FLOOR_MS = 0.3


def project_post_fix_ms(
	finding_type: str,
	current_avg_ms: float,
	filtered_pct: float | None = None,
) -> float | None:
	"""Return the projected per-query time after applying the finding's
	suggested fix, or None if the finding type isn't one we project.

	``filtered_pct`` is only used for "Low Filter Ratio" findings
	(MariaDB's EXPLAIN ``filtered`` column, 0-100 representing what %
	of examined rows survive the WHERE).
	"""
	if current_avg_ms <= 0:
		return None

	if finding_type == "Low Filter Ratio":
		if filtered_pct is None or filtered_pct <= 0:
			return None
		factor = max(0.01, filtered_pct / 100.0)
		return max(POST_FIX_FLOOR_MS, round(current_avg_ms * factor, 2))

	factor = _POST_FIX_SPEEDUP.get(finding_type)
	if factor is None:
		return None
	return max(POST_FIX_FLOOR_MS, round(current_avg_ms * factor, 2))


def percentile(values: list[float], pct: int) -> float:
	"""Linear-interpolated percentile of ``values``. Returns 0.0 for an
	empty list. ``pct`` is in [0, 100]. Used by repetition-heavy
	analyzers (N+1, redundant calls) to surface the tail of the per-hit
	duration distribution alongside the consolidated total.

	No numpy dependency Optimus already ships pure-Python analyzers,
	and this is exact enough for finding-card P95 readouts.
	"""
	if not values:
		return 0.0
	s = sorted(values)
	k = (len(s) - 1) * pct / 100.0
	f = int(k)
	c = min(f + 1, len(s) - 1)
	return s[f] + (s[c] - s[f]) * (k - f)


def short_filename(filename: str, keep_segments: int = 2) -> str:
	"""Return the last ``keep_segments`` path components of ``filename``.

	Examples::

	    short_filename("frappe/model/document.py")                    → "model/document.py"
	    short_filename("a/b/c/d/e.py")                                → "d/e.py"
	    short_filename("erpnext.py")                                  → "erpnext.py"
	    short_filename("/Users/.../apps/frappe/frappe/handler.py")    → "frappe/handler.py"
	    short_filename("")                                            → ""

	The returned value is always <=  sum of the last N segment lengths
	plus (N - 1) slashes, which for typical Python files is 40-60 chars.
	"""
	if not filename:
		return ""
	norm = filename.replace("\\", "/")
	parts = [p for p in norm.split("/") if p]
	if not parts:
		return ""
	if len(parts) <= keep_segments:
		return "/".join(parts)
	return "/".join(parts[-keep_segments:])


@dataclass
class AnalyzerResult:
	"""Output from a single analyzer."""

	actions: list[dict] = field(default_factory=list)
	findings: list[dict] = field(default_factory=list)
	aggregate: dict[str, Any] = field(default_factory=dict)
	warnings: list[str] = field(default_factory=list)


@dataclass
class AnalyzeContext:
	"""Shared state across the analyzer pipeline.

	Holds the accumulated outputs from each analyzer as the orchestrator
	walks through them. The orchestrator calls `merge()` after each
	analyzer to fold its result into the context.
	"""

	session_uuid: str
	docname: str

	actions: list[dict] = field(default_factory=list)
	findings: list[dict] = field(default_factory=list)
	aggregate: dict[str, Any] = field(default_factory=dict)
	warnings: list[str] = field(default_factory=list)

	def merge(self, result: AnalyzerResult) -> None:
		"""Fold an analyzer's output into the context."""
		if result.actions:
			self.actions.extend(result.actions)
		if result.findings:
			self.findings.extend(result.findings)
		if result.aggregate:
			self.aggregate.update(result.aggregate)
		if result.warnings:
			self.warnings.extend(result.warnings)
