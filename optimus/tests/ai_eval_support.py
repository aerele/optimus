# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Helpers for tests of scripts/ai_eval/ (the AI eval kit) and for semgrep tests.

scripts/ai_eval/ sits outside the optimus package and has no package markers, so no test
runner collects or imports it on its own. Tests load its modules explicitly from here.
Not a test module (no test_ prefix), so pytest never collects it.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts", "ai_eval")
REQUIRE_ENV = "REQUIRE_SEMGREP"


def load(name: str):
	"""Import scripts/ai_eval/<name>.py. Helper modules (``_corpus``, ``_answer``,
	``_semgrep``) import under their own names because the scripts import them that way;
	``live`` and ``report`` load under a private name so no top-level ``report`` module
	appears in sys.modules."""
	if SCRIPTS_DIR not in sys.path:
		sys.path.insert(0, SCRIPTS_DIR)
	if name.startswith("_"):
		return importlib.import_module(name)
	key = f"optimus_ai_eval_{name}"
	if key in sys.modules:
		return sys.modules[key]
	spec = importlib.util.spec_from_file_location(key, os.path.join(SCRIPTS_DIR, f"{name}.py"))
	module = importlib.util.module_from_spec(spec)
	sys.modules[key] = module
	spec.loader.exec_module(module)
	return module


def semgrep_rules_dir() -> str:
	"""The one semgrep configuration every test uses: OPTIMUS_SEMGREP_RULES_DIR."""
	return os.environ.get("OPTIMUS_SEMGREP_RULES_DIR", "")


def require_semgrep() -> str:
	"""Return the rules dir, or skip; with REQUIRE_SEMGREP=1 (CI's ai-tests job) fail instead,
	so a missing semgrep can never turn a semgrep test into a silent skip."""
	rules = semgrep_rules_dir()
	missing = []
	if shutil.which("semgrep") is None:
		missing.append("the semgrep CLI is not on PATH")
	if not rules or not os.path.isdir(rules):
		missing.append("OPTIMUS_SEMGREP_RULES_DIR does not name a frappe/semgrep-rules 'rules' directory")
	if missing:
		message = "; ".join(missing)
		if os.environ.get(REQUIRE_ENV) == "1":
			pytest.fail(f"{REQUIRE_ENV}=1 but {message}")
		pytest.skip(message)
	return rules
