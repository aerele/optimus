# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Source-resolution helpers: dotted-path → ``(abs_file, lineno, func_name)`` and
adjacent display helpers.

Used by the renderer to resolve action entry-points (the ``def`` line of a Frappe
API method or RQ job target) and repeated-hot-frame keys (the ``short_path::func``
shape ``call_tree._redacted_module_key`` emits) to a source location + a ±1-line
snippet for the report's "Where this fired" callsite blocks.

Public surface (underscore-prefixed but exposed via the package ``__init__.py``
dir-walk so ``renderer.X`` resolves):

* ``_action_dotted_entry(action)``: an action's dotted entry-point path, or ``None``.
* ``_skip_decorators_to_def(abs_filename, start_lineno, fn_name)``: walk past
  ``@decorator`` lines to land on ``def <fn_name>``.
* ``_resolve_dotted_to_code(dotted)``: ``(abs_filename, lineno, func_name)`` from a
  dotted module path.
* ``_bench_relative_display(abs_path)``: display form (``apps/<app>/...``).
* ``_action_entry_callsite(action, *, cache)``: action → dotted → code →
  ``{filename, _abs, lineno, function, source_snippet}``.
* ``_resolve_frame_key_to_callsite(function_key, *, cache)``: same, from a
  repeated-hot-frame key (``short_path::func``).
"""

from __future__ import annotations

import os
import re
import sys

from optimus.renderer.source import _installed_apps, _read_source_snippet, _resolve_source_path, _source_lines


def _action_dotted_entry(action) -> str | None:
	"""Derive an action's dotted entry-point path, or ``None``.

	- RQ Job: ``action["path"]`` is already the job method (Frappe's
	  recorder stores ``frappe.job.method`` there e.g.
	  ``ugly_code.python.common.bg_recheck_users``).
	- HTTP Request whose path is ``/api/method/<dotted>``: the ``<dotted>``
	  segment, with any ``?query`` and trailing ``/...`` stripped.
	- anything else (non-``/api/method`` HTTP, empty/missing path, non-dict
	  input): ``None``.
	"""
	if not isinstance(action, dict):
		return None
	event_type = (action.get("event_type") or "").strip()
	path = (action.get("path") or "").strip()
	if not path:
		return None
	if event_type == "RQ Job":
		return path.split("?", 1)[0].strip() or None
	if event_type == "HTTP Request" and path.startswith("/api/method/"):
		rest = path[len("/api/method/"):]
		rest = rest.split("?", 1)[0].split("/", 1)[0].strip().strip(".")
		return rest or None
	return None


def _skip_decorators_to_def(
	abs_filename: str,
	start_lineno: int,
	fn_name: str,
	*,
	cache: dict | None = None,
) -> int:
	"""Return the lineno of ``def <fn_name>`` / ``async def <fn_name>`` at or
	after ``start_lineno`` in ``abs_filename``. Returns ``start_lineno`` unchanged
	when that line isn't a decorator or no matching def is found within 30 lines.

	On CPython 3.11+ ``code.co_firstlineno`` for a decorated function points at
	the first decorator line, not the ``def``; this skip lands the callsite
	snippet on the signature rather than ``@frappe.whitelist(...)``. Reads through
	``cache`` (the shared per-render file cache) when available.
	"""
	if not abs_filename or start_lineno <= 0 or not fn_name:
		return start_lineno
	# Read source through the shared primitive (cache-aware; also resolves
	# Server Script sentinels, which a bare open() here used to miss).
	lines = _source_lines(abs_filename, cache=cache)
	if not lines or start_lineno > len(lines):
		return start_lineno
	# Cheap early exit: the line at start_lineno isn't a decorator →
	# nothing to skip.
	first = lines[start_lineno - 1].lstrip()
	if not first.startswith("@"):
		return start_lineno
	# Scan forward (≤ 30 lines) for the def line.
	pat = re.compile(
		r"^\s*(?:async\s+)?def\s+" + re.escape(fn_name) + r"\b"
	)
	last = min(len(lines), start_lineno + 30)
	for i in range(start_lineno, last):
		if pat.match(lines[i]):
			return i + 1  # convert 0-indexed to 1-indexed lineno
	return start_lineno  # no def found fall back to original


def _resolve_dotted_to_code(
	dotted,
	*,
	file_cache: dict | None = None,
) -> tuple[str, int, str] | None:
	"""Resolve a dotted module path to ``(abs_filename, lineno, func_name)``, or
	``None`` on any failure (never raises).

	Uses ``importlib`` directly (not ``frappe.get_attr``, which needs a running
	site) via the longest importable leading prefix then ``getattr`` for the rest.
	``inspect.unwrap`` sees through ``functools.wraps`` decorators. When the
	resolved lineno points at a decorator line, it is advanced to the ``def`` line
	so the callsite snippet anchors on the signature.
	"""
	if not isinstance(dotted, str) or len(dotted) > 500 or "." not in dotted or not all(part.isidentifier() for part in dotted.split(".")):
		return None
	from optimus.ai_fix import _InterruptGuard

	guard = _InterruptGuard(base=True)
	try:
		with guard:
			import importlib
			import inspect

			parts = dotted.split(".")
			may_import = parts[0] in _installed_apps()
			module = None
			mod_parts = 0
			for i in range(len(parts), 0, -1):
				try:
					module = importlib.import_module(".".join(parts[:i])) if may_import else sys.modules.get(".".join(parts[:i]))
					if module is None:
						continue
					mod_parts = i
					break
				except ImportError:
					continue
			if module is None or mod_parts == len(parts):
				return None  # nothing imported, or it's a module not a callable
			obj = module
			for attr in parts[mod_parts:]:
				obj = getattr(obj, attr)
			obj = inspect.unwrap(obj)
			code = getattr(obj, "__code__", None)
			if code is None:
				return None  # builtin / C func / not a plain Python function
			filename = code.co_filename or ""
			lineno = code.co_firstlineno or 0
			if not filename or filename.startswith("<") or lineno <= 0:
				return None  # Server Script / eval'd code / bogus
			abs_path = os.path.abspath(filename)
			fn_name = getattr(obj, "__name__", "") or ""
			lineno = _skip_decorators_to_def(
				abs_path, int(lineno), fn_name, cache=file_cache,
			)
			return (abs_path, int(lineno), fn_name)
	except Exception:
		return None
	if guard.pending():
		raise guard.interrupt()


def _bench_relative_display(abs_path: str) -> str:
	"""Display form of an absolute source path: ``apps/<app>/.../file.py``
	(relative to the bench root). Falls back to the absolute path when the
	file is outside the bench or the bench path can't be determined."""
	try:
		from frappe.utils import get_bench_path

		rel = os.path.relpath(abs_path, get_bench_path())
		if rel and not rel.startswith(".."):
			return rel.replace("\\", "/")
	except Exception:
		pass
	return abs_path


def _action_entry_callsite(action, *, cache: dict | None = None) -> dict | None:
	"""Resolve an action's entry-point source location + a ±1-line snippet.

	Returns ``{"filename", "_abs", "lineno", "function", "source_snippet"}`` or
	``None`` when there's no clean dotted entry point, it can't be resolved or the
	callable has no real source. ``source_snippet`` may itself be ``None`` if the
	file can't be read. ``cache`` (shared across a render) is forwarded to
	``_read_source_snippet`` so a cluster of actions in one file reads it once.
	"""
	dotted = _action_dotted_entry(action)
	if not dotted:
		return None
	resolved = _resolve_dotted_to_code(dotted, file_cache=cache)
	if not resolved:
		return None
	abs_path, lineno, name = resolved
	return {
		"filename": _bench_relative_display(abs_path),
		"_abs": abs_path,
		"lineno": lineno,
		"function": name,
		"source_snippet": _read_source_snippet(abs_path, lineno, cache=cache),
	}


def _resolve_frame_key_to_callsite(function_key, *, cache: dict | None = None) -> dict | None:
	"""Resolve a recorded short_path::function through the same source boundary."""
	from optimus.ai_fix import _InterruptGuard

	if not isinstance(function_key, str) or len(function_key) > 4096 or "::" not in function_key:
		return None
	guard = _InterruptGuard(base=True)
	try:
		with guard:
			short_path, _, func = function_key.partition("::")
			short_path, func = short_path.strip(), func.strip()
			if not short_path or not func:
				return None
			norm = short_path.replace("\\", "/")
			dotted = norm.removesuffix(".py").replace("/", ".").strip(".")
			resolved = _resolve_dotted_to_code(f"{dotted}.{func}", file_cache=cache) if dotted else None
			if resolved:
				abs_path, lineno, name = resolved
			else:
				abs_path = _resolve_source_path(short_path)
				if not isinstance(abs_path, str):
					return None
				lines = _source_lines(short_path, cache=cache)
				pattern = re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+" + re.escape(func.rsplit(".", 1)[-1]) + r"\b")
				lineno = next((i for i, line in enumerate(lines or [], start=1) if pattern.match(line)), None)
				if lineno is None:
					return None
				name = func
			return {"filename": _bench_relative_display(abs_path), "_abs": abs_path, "lineno": lineno,
				"function": name or func, "source_snippet": _read_source_snippet(abs_path, lineno, cache=cache)}
	except Exception:
		return None
	if guard.pending():
		raise guard.interrupt()
	return None
