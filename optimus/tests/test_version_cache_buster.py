# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Test that hooks.py uses a cache-busting version suffix for static assets.
The suffix is ``<__version__>.<mtime>`` so file edits auto-invalidate the
browser cache even between releases. See hooks._asset_version for the
rationale."""

import re

from optimus import __version__, hooks


def _js_entries():
	"""Return app_include_js as a list regardless of string/list form."""
	val = hooks.app_include_js
	return val if isinstance(val, list) else [val]


def test_app_include_js_contains_version():
	"""Every JS entry must have a ?v=<version>.<anything> cache-buster
	starting with the current __version__. The suffix after the version
	is the mtime-based auto-invalidator we don't care about its exact
	value, just that the version is present."""
	pattern = re.compile(
		rf"\?v={re.escape(__version__)}(\.\d+)?$"
	)
	for entry in _js_entries():
		assert pattern.search(entry), (
			f"JS entry must have ?v={__version__}[.<mtime>] suffix; "
			f"got: {entry}"
		)


def test_app_include_css_contains_version():
	pattern = re.compile(rf"\?v={re.escape(__version__)}(\.\d+)?$")
	assert pattern.search(hooks.app_include_css), (
		f"CSS entry must have ?v={__version__}[.<mtime>] suffix; "
		f"got: {hooks.app_include_css}"
	)


def test_app_include_paths_are_correct():
	# hooks.py: the pill state logic must load before floating_widget.js, and the
	# frontend shim after it (it reads the widget's data-session-uuid attribute).
	entries = _js_entries()
	assert [e.split("?")[0] for e in entries] == [
		"/assets/optimus/js/optimus_pill_state.js",
		"/assets/optimus/js/floating_widget.js",
		"/assets/optimus/js/optimus_frontend.js",
	]
	assert hooks.app_include_css.startswith("/assets/optimus/css/floating_widget.css")


def test_mtime_component_auto_invalidates_on_file_edit(monkeypatch):
	"""The ?v= suffix includes the file's mtime so any edit to the JS/CSS
	auto-busts the browser cache even when __version__ hasn't been bumped.
	"""
	from optimus.hooks import _asset_version

	monkeypatch.setattr(hooks._os.path, "getmtime", lambda path: 1700000000.9)
	before = _asset_version("js/floating_widget.js")
	monkeypatch.setattr(hooks._os.path, "getmtime", lambda path: 1700000500.2)
	after = _asset_version("js/floating_widget.js")
	assert before == f"{__version__}.1700000000"
	assert after == f"{__version__}.1700000500"
	assert before != after


def test_asset_version_falls_back_when_file_missing(monkeypatch):
	"""If the file can't be stat'd (unusual install, permission
	issue), _asset_version must still return a non-empty version
	string so hooks.py doesn't crash the app load. Falls back to
	__version__ alone."""
	from optimus.hooks import _asset_version

	v = _asset_version("js/this_file_does_not_exist.js")
	# Falls back to just __version__ (no mtime suffix).
	assert v == __version__
