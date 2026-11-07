# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Every skip the Frappe-free unit CI is allowed to produce, with why.

``ci_skip_guard`` fails the run on any skip that no row here matches, so a test cannot
silently stop running. A row matches on the test file of the skipped TEST (the basename of
its node id, never the line that called ``skip``) and a regex over the skip reason.

``max_count`` is ``None`` only for a skip that depends on the machine (a CLI, a database
service, a platform); a gate on a tool that ci.yml installs or lacks has the measured count
(``0`` when CI skips none), so a new skip with that reason fails CI. The count is an int, or
``{"default": n, "3.10": m}`` keyed by ``major.minor`` Python. Counts are the ``-rs`` totals of ci.yml's exact install (no rq, no
codex, no Frappe), measured on Python 3.10 and 3.14; a lower count is fine, a higher one
fails.
"""

from typing import NamedTuple


class Row(NamedTuple):
	file: str | None
	reason: str
	max_count: int | dict | None
	why: str


_BELOW_312 = {"default": 0, "3.10": 15, "3.11": 15}

ALLOWED = (
	# Environment gates: the tool is absent here, the test runs in the job or on the machine that has it.
	Row(
		None,
		r"^could not import 'rq(\.timeouts)?': No module named 'rq'",
		113,
		"rq tests run in ai-quality.yml, which installs rq; ci.yml has no rq (113 on 3.10 and 3.14)",
	),
	Row(
		None,
		r"semgrep CLI is not on PATH|OPTIMUS_SEMGREP_RULES_DIR",
		None,
		"semgrep tests run in ai-quality.yml",
	),
	Row(
		None,
		r"^codex is not installed|codex debug prompt-input could not run|no usable `debug prompt-input`",
		None,
		"needs the operator's codex CLI",
	),
	Row("test_ai_subscription_cli.py", r"set OPTIMUS_LIVE_CODEX=1", None, "live Codex is opt-in"),
	Row(None, r"^node not installed", 0, "UI tests need node (CI runners have it, so none skip there)"),
	Row(
		None,
		r"disposable SQL validation service",
		None,
		"SQL tests run in ai-quality.yml against MariaDB and PostgreSQL",
	),
	Row(
		"test_session_jobs.py",
		r"No bench Redis available|Redis Lua eval not available",
		None,
		"the Lua merge needs a Lua engine; it runs in integration.yml",
	),
	Row(
		None,
		r"^line_profiler not installed",
		0,
		"ci.yml installs line_profiler through the project deps, so none skip there",
	),
	Row(None, r"^frappe\.recorder unavailable", 0, "bench-only: no Frappe-free test is expected to skip"),
	Row(
		None,
		r"^pyinstrument (not available|not installed)",
		0,
		"ci.yml's install skips none; a machine without pyinstrument must install it or list the skip",
	),
	Row("test_ai_subscription_cli.py", r"PR_SET_PDEATHSIG is Linux only", None, "Linux-only behaviour"),
	Row("test_index_catalog_sql.py", r"MariaDB prefix key parts", None, "PostgreSQL has no prefix key parts"),
	Row(
		"test_renderer_structure_snapshot.py",
		r"regenerated structural snapshot",
		None,
		"the deliberate snapshot regeneration run",
	),
	Row(
		None,
		r"sys\.monitoring requires Python 3\.12\+|needs sys\.monitoring \+ line_profiler",
		_BELOW_312,
		"sys.monitoring needs Python 3.12+",
	),
	# Real-Frappe checks without a Frappe-free twin; they run on a bench (bench run-tests / integration.yml).
	Row(
		"test_analyze_singleflight_redis.py",
		r"needs Frappe's real RedisWrapper",
		17,
		"the real-RedisWrapper parameter is the bench drift canary; the v16-shaped twin runs here",
	),
	Row(
		"test_worker_permission_cache.py",
		r"real Frappe role cache",
		2,
		"the v16 role-cache shape is already faked in the same file",
	),
	Row(
		"test_ai_log_failure.py",
		r"frappe\.utils\.logger",
		1,
		"pins Frappe's default log level; the ERROR breadcrumb is faked in the same file",
	),
	Row(
		"test_maintenance_scrub.py",
		r"frappe\.utils\.logger",
		1,
		"pins Frappe's default log level; the ERROR breadcrumb is asserted in the same file",
	),
	Row(
		"test_ai_spend.py",
		r"frappe\.query_builder\.utils",
		5,
		"pypika SQL quoting per dialect (MariaDB has a string twin, the health window has a recording twin)",
	),
	Row(
		"test_ai_spend.py",
		r"frappe\.model\.docstatus",
		2,
		"Frappe's own Document.save order; the hook is pinned by an AST test",
	),
	Row("test_error_log_mask.py", r"without Document\.hook", 1, "Frappe's hook composer"),
	Row(
		"test_index_recipe_executable.py",
		r"real Frappe is not installed",
		1,
		"a fact about Frappe's error_log schema",
	),
	Row(
		"test_permissions_download_boundary.py",
		r"Requires installed Frappe",
		6,
		"characterises Frappe's own download route",
	),
)
