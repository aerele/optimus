"""Desk error messages must not interpret supplied identifiers as HTML."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from optimus import api, pdf_export


@pytest.mark.parametrize("load", [api._require_session_permission, pdf_export._load_session])
def test_unknown_identifier_is_escaped(load, monkeypatch):
	def throw(message, *args, **kwargs):
		raise RuntimeError(message)
	monkeypatch.setattr(api.frappe, "db", SimpleNamespace(get_value=lambda *a, **k: None), raising=False)
	monkeypatch.setattr(api.frappe, "throw", throw, raising=False)
	with pytest.raises(RuntimeError) as caught:
		load('<img src=x onerror="alert(1)">')
	assert "<img" not in str(caught.value) and "&lt;img" in str(caught.value)


def test_translated_errors_do_not_interpolate_raw_identifiers():
	offenders = []
	for module in ("api.py", "pdf_export.py", "permissions.py", "ai_jobs.py"):
		for node in ast.walk(ast.parse((Path(api.__file__).parent / module).read_text())):
			if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
				and node.func.attr == "format" and isinstance(node.func.value, ast.Call)
				and getattr(node.func.value.func, "id", None) == "_"):
				if any(isinstance(arg, ast.Name) and arg.id in {"session_uuid", "run_uuid"} for arg in node.args):
					offenders.append((module, node.lineno))
	assert not offenders, offenders
