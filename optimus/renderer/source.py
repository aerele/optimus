# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""On-disk source-file access plus a bounded LRU cache for the renderer.

Finding-card snippets, AI-fix source windows and Phase-2 drilldowns all read
source lines through here. Three responsibilities:

  * Path resolution (:func:`_resolve_source_path`): turn analyzer-recorded
    app-relative paths into absolute paths via ``frappe.get_app_path`` / bench
    fallback. Server Script callsites resolve to a tuple sentinel that
    downstream branches load from the ``tabServer Script`` DocType, not disk.
  * Source boundary (:func:`_path_within_bench`): allow canonical app/library
    .py/.js/.html source, including soft-linked apps; deny site data, config,
    logs and archives. Tests may relax only the root allowlist.
  * Per-render file cache (:class:`_BoundedFileCache`): a 50-entry
    move-to-end LRU dict passed as ``file_cache=`` to cap memory on big codebases.

Both readers (:func:`_read_source_snippet`, :func:`_read_source_window`) share
the per-line truncation constant (:data:`_SNIPPET_TRUNCATE_CHARS`, 200 chars)
and go through :func:`_resolve_source_path`. ``frappe`` is lazy-imported so the
pure-pytest tests don't need a bench.
"""

from __future__ import annotations

import os
import stat
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar

# Per-line truncation for source snippets/windows keeps a single
# multi-kilobyte minified line out of technical_detail_json / the LLM
# prompt. Kept here (with the readers) rather than imported from
# analyze.py so the readers don't pull in analyze.py, which imports
# frappe.recorder.
_SNIPPET_TRUNCATE_CHARS = 200

_FILE_CACHE_MAX_ENTRIES = 50


class _BoundedFileCache:
	"""Bounded LRU dict for the per-render file cache, capping memory on
	sessions that touch many source files. Supports the dict-style protocol the
	read sites use: ``filename in cache``, ``cache[filename]``,
	``cache[filename] = lines``.
	"""

	__slots__ = ("_data", "_max")

	def __init__(self, max_entries: int = _FILE_CACHE_MAX_ENTRIES):
		self._data: OrderedDict = OrderedDict()
		self._max = max_entries

	def __contains__(self, key) -> bool:
		return key in self._data

	def __getitem__(self, key):
		# Touch on read so true LRU semantics apply (recently-read
		# files stay in the cache longer than ones read once and
		# forgotten).
		value = self._data[key]
		self._data.move_to_end(key)
		return value

	def __setitem__(self, key, value) -> None:
		if key in self._data:
			self._data.move_to_end(key)
		self._data[key] = value
		while len(self._data) > self._max:
			self._data.popitem(last=False)


_SOURCE_EXTENSIONS = frozenset({".py", ".js", ".html"})
_DENIED_BENCH_DIRS = ("sites", "config", "logs", "archived")
_SOURCE_MAX_BYTES = 4 * 1024 * 1024
_SCRIPT_READERS = ContextVar("optimus_script_readers", default=())
_APPS_WARNING_SENT = False


def _is_inside(path: str, root: str) -> bool:
	return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _bench_paths():
	import frappe

	try:
		sites_path = frappe.local.sites_path
	except (AttributeError, RuntimeError):
		return None, None
	if not isinstance(sites_path, str) or not sites_path:
		return None, None
	sites = os.path.abspath(sites_path)
	return os.path.realpath(os.path.dirname(sites)), os.path.realpath(sites)


def _in_test_context():
	import frappe

	try:
		return getattr(frappe.flags, "in_test", False) is True
	except (AttributeError, RuntimeError):
		return False


def _installed_apps():
	from optimus.ai_fix import _InterruptGuard

	global _APPS_WARNING_SENT
	site_bound = False
	problem = ""
	guard = _InterruptGuard(base=True)
	try:
		with guard:
			import frappe

			if not getattr(frappe.local, "site", None):
				return frozenset()
			site_bound = True
			return frozenset(app for app in frappe.get_installed_apps() or () if isinstance(app, str) and app.isidentifier())
	except Exception as exc:
		problem = type(exc).__name__
	if guard.pending():
		raise guard.interrupt()
	if site_bound and problem and not _APPS_WARNING_SENT:
		_APPS_WARNING_SENT = True
		guard = _InterruptGuard(base=True)
		try:
			with guard:
				frappe.logger("optimus").warning("optimus source access: installed apps unavailable (%s)", problem)
		except Exception:
			pass  # The source boundary remains closed if logging also fails.
		if guard.pending():
			raise guard.interrupt()
	return frozenset()


def _path_within_bench(path: str) -> bool:
	"""Canonical source allowlist; site data stays denied even in tests."""
	if not isinstance(path, str) or not path or "\x00" in path:
		return False
	real = os.path.realpath(path)
	if os.path.splitext(real)[1].lower() not in _SOURCE_EXTENSIONS:
		return False
	bench, sites = _bench_paths()
	denied = [os.path.join(bench, name) for name in _DENIED_BENCH_DIRS] if bench else []
	if sites:
		denied.append(sites)
	if any(_is_inside(real.casefold(), os.path.realpath(root).casefold()) for root in denied):
		return False
	if _in_test_context():
		return True
	if not bench:
		return False
	roots = [os.path.realpath(os.path.join(bench, name)) for name in ("apps", "env")]
	try:
		with os.scandir(os.path.join(bench, "apps")) as entries:
			roots.extend(os.path.realpath(entry.path) for entry in entries if entry.is_symlink() and entry.is_dir())
	except OSError:
		pass
	return any(_is_inside(real, root) for root in roots)


@contextmanager
def server_script_readers(*users):
	"""Intersect nested principals with the acting user; always reset context."""
	token = _SCRIPT_READERS.set(tuple(dict.fromkeys((*_SCRIPT_READERS.get(), *(u for u in users if u)))))
	try:
		yield
	finally:
		_SCRIPT_READERS.reset(token)


def _may_read_server_script(name=None):
	from optimus.ai_fix import _InterruptGuard

	guard = _InterruptGuard(base=True)
	try:
		with guard:
			import frappe

			actor = getattr(frappe.session, "user", None)
			users = {*_SCRIPT_READERS.get(), actor}
			if not actor or any(not isinstance(user, str) or not user or user == "Guest" for user in users):
				return False
			return all(frappe.has_permission("Server Script", "read", user=user, **({"doc": name} if name else {})) for user in users)
	except Exception:
		return False
	if guard.pending():
		raise guard.interrupt()


def _resolve_source_path(filename, *, must_exist=True):
	"""Resolve a stored source path without importing an uninstalled app."""
	from optimus.ai_fix import _InterruptGuard

	if not isinstance(filename, str) or not filename.strip() or len(filename) > 4096 or "\x00" in filename:
		return None
	name = filename.strip()
	if name.startswith("<"):
		from optimus.server_script_source import extract_script_name

		script = extract_script_name(name)
		return ("server_script", script) if script else None
	guard = _InterruptGuard(base=True)
	try:
		with guard:
			candidates = [name]
			if not os.path.isabs(name):
				import frappe

				parts = name.replace("\\", "/").split("/")
				if parts[0] in _installed_apps():
					candidates.append(frappe.get_app_path(parts[0], *parts[1:]))
				bench, _sites = _bench_paths()
				if bench:
					candidates.extend((os.path.join(bench, name), os.path.join(bench, "apps", name)))
			for candidate in candidates:
				real = os.path.realpath(candidate)
				if _path_within_bench(real) and (not must_exist or os.path.isfile(real)):
					return real
	except Exception:
		return None
	if guard.pending():
		raise guard.interrupt()
	return None


def _source_lines(filename: str, *, cache: dict | None = None) -> list[str] | None:
	"""Validate access before cache lookup, including Server Script revocation."""
	from optimus.ai_fix import _InterruptGuard

	guard = _InterruptGuard(base=True)
	lines = None
	try:
		with guard:
			resolved = _resolve_source_path(filename, must_exist=not (cache is not None and filename in cache))
			if isinstance(resolved, tuple):
				from optimus.server_script_source import get_server_script_lines

				return get_server_script_lines(resolved[1], cache=cache)
			if not resolved:
				return None
			if cache is not None and filename in cache:
				return cache[filename]
			fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
			try:
				with os.fdopen(fd, "rb", closefd=False) as fh:
					info = os.fstat(fh.fileno())
					if not stat.S_ISREG(info.st_mode) or info.st_size > _SOURCE_MAX_BYTES:
						return None
					data = fh.read(_SOURCE_MAX_BYTES + 1)
					if len(data) <= _SOURCE_MAX_BYTES:
						lines = data.decode("utf-8").splitlines()
			finally:
				os.close(fd)
	except Exception:
		lines = None
	if guard.pending():
		lines = data = None
		raise guard.interrupt()
	if cache is not None:
		cache[filename] = lines
	return lines


def _read_source_snippet(
	filename: str,
	lineno,
	*,
	cache: dict | None = None,
) -> list[dict] | None:
	"""Return a small source snippet around ``(filename, lineno)``, or ``None``
	when the file isn't readable / lineno is out of range. ``filename`` is
	resolved via ``_resolve_source_path`` (Server Script filenames read from the
	``tabServer Script`` DocType)."""
	try:
		ln = int(lineno)
	except (TypeError, ValueError):
		return None
	if ln <= 0 or not filename:
		return None

	lines = _source_lines(filename, cache=cache)
	if not lines:
		return None

	limit = _SNIPPET_TRUNCATE_CHARS
	snippet: list[dict] = []
	# v0.7.x: read a ±2-line window around the anchor (compromise
	# between ±1 too tight, body invisible and ±4 included
	# preceding-function noise). The template's blank-line filter
	# drops empties (except the callsite itself), so the visible
	# snippet ends up at ~3-4 lines: the anchor `def` + a couple of
	# body lines. For the exact hot line inside the function, the
	# Slow-Hot-Path description points to the Line-Level Drilldown.
	for n in range(max(1, ln - 2), ln + 3):
		if 1 <= n <= len(lines):
			content = lines[n - 1]
			if len(content) > limit:
				content = content[:limit] + "..."
			snippet.append({"lineno": n, "content": content})
	return snippet or None


def _read_source_window(
	filename: str,
	lineno,
	*,
	before: int = 12,
	after: int = 12,
	cache: dict | None = None,
	max_line_chars: int | None = None,
) -> list[dict] | None:
	"""Return a wider source window around ``(filename, lineno)`` for the AI-fix
	prompt: a list of ``{lineno, content, is_target}`` covering
	``lineno - before`` … ``lineno + after`` (clamped). Per-line truncation
	matches ``_read_source_snippet`` unless ``max_line_chars`` overrides it.
	Returns ``None`` when unreadable / lineno out of range. ``filename`` is
	resolved via ``_resolve_source_path`` (Server Script sentinels read from the DocType).
	"""
	try:
		ln = int(lineno)
	except (TypeError, ValueError):
		return None
	if ln <= 0 or not filename:
		return None

	lines = _source_lines(filename, cache=cache)
	if not lines:
		return None

	limit = max_line_chars or _SNIPPET_TRUNCATE_CHARS
	start = max(1, ln - max(0, before))
	end = min(len(lines), ln + max(0, after))
	window: list[dict] = []
	for n in range(start, end + 1):
		content = lines[n - 1]
		if len(content) > limit:
			content = content[:limit] + "..."
		window.append({"lineno": n, "content": content, "is_target": n == ln})
	return window or None
