# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Operator documentation must describe the shipped configuration and contracts."""

import ast
import json
import re
from pathlib import Path

import pytest

from optimus import ai_budget, ai_guardrails, ai_prompts

ROOT = Path(__file__).resolve().parents[2]


def read(path):
	return (ROOT / path).read_text()


def tree(path):
	return ast.parse(read(path))


def literal_assignment(path, name):
	for node in tree(path).body:
		targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
		if any(isinstance(target, ast.Name) and target.id == name for target in targets):
			return ast.literal_eval(node.value)
	raise AssertionError(f"Missing constant: {path}:{name}")


def table(document, heading):
	section = read(document).split(heading + "\n", 1)[1].split("\n##", 1)[0]
	return [tuple(cell.strip().strip("`") for cell in line.strip("|").split("|"))
		for line in section.splitlines() if line.startswith("| ")][2:]


@pytest.mark.parametrize("name", ["SYSTEM_PROMPT", "STEPS_SYSTEM_PROMPT"])
def test_prompt_measurements_match_shipped_text(name):
	rows = {row[0]: row[1:] for row in table("docs/AI-FIXING.md", "### Prompt measurements")}
	prompt = getattr(ai_prompts, name)
	assert rows[name] == (str(len(prompt.encode("utf-8"))), str(ai_budget.estimate_tokens(prompt)))


def test_every_guardrail_has_its_current_tier_and_explanation():
	rows = table("docs/AI-FIXING.md", "### Guardrail reference")
	assert len(rows) == len(ai_guardrails.CODE_ACTIONS)
	assert {row[0]: row[1] for row in rows} == ai_guardrails.CODE_ACTIONS
	assert all(len(row[2]) >= 25 for row in rows)


def test_api_rate_defaults_and_force_stop_alias_are_documented():
	expected = {}
	for name in ("_AI_LIMITS", "_ACTION_LIMITS"):
		for action, values in literal_assignment("optimus/api.py", name).items():
			expected[action] = (str(values["limit"]), str(values["seconds"]))
	expected["force_stop_phase2"] = expected["stop_line_profile_pass"]
	rows = table("README.md", "### Per-user request limits")
	assert {row[0]: row[1:3] for row in rows} == expected
	assert "separate bucket" in read("README.md")


def test_queue_defaults_and_ranges_are_documented():
	expected = {}
	for node in ast.walk(tree("optimus/ai_jobs.py")):
		if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
			continue
		if node.func.id == "_setting_int":
			key, default, low, high = (ast.literal_eval(arg) for arg in node.args)
			expected[key] = (str(default), f"{low} to {high}")
	rows = table("README.md", "### Background AI jobs")
	assert {row[0]: row[1:3] for row in rows if row[0] in expected} == expected
	queue = next(node for node in ast.walk(tree("optimus/ai_jobs.py"))
		if isinstance(node, ast.Call) and node.args
		and isinstance(node.args[0], ast.Constant) and node.args[0].value == "optimus_ai_queue")
	assert next(row[1] for row in rows if row[0] == "optimus_ai_queue") == ast.literal_eval(queue.args[1])


@pytest.mark.parametrize("name", ["ai_context_tokens", "ai_refresh_max_findings", "ai_send_raw_values"])
def test_settings_defaults_match_schema(name):
	schema = json.loads(read("optimus/optimus/doctype/optimus_settings/optimus_settings.json"))
	field = next(field for field in schema["fields"] if field["fieldname"] == name)
	rows = table("README.md", "#### Refresh and privacy settings")
	row = next(row for row in rows if row[0] == name)
	assert row[1] == str(field["default"])


def test_provider_table_lists_only_selectable_providers():
	schema = json.loads(read("optimus/optimus/doctype/optimus_settings/optimus_settings.json"))
	options = next(field["options"] for field in schema["fields"] if field["fieldname"] == "ai_provider")
	section = read("docs/AI-FIXING.md").split("## 4. Where the request goes", 1)[1].split("###", 1)[0]
	assert set(re.findall(r"^\| `([^`]+)` \|", section, re.M)) == set(options.splitlines())
	assert "Aerele is disabled" in read("docs/AI-FIXING.md")


def test_code_map_symbols_exist():
	rows = table("docs/AI-FIXING.md", "## 9. Where the code lives")
	for _, path, symbols in rows:
		names = {node.name for node in ast.walk(tree(path)) if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
		names |= {node.id for node in ast.walk(tree(path)) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
		for name in re.findall(r"`([A-Za-z_]\w*)`", "`" + symbols + "`"):
			assert name in names, (path, name)


def shared_ai_helpers():
	"""Resolve module aliases and direct imports, without importing Frappe."""
	result = set()
	for path in (ROOT / "optimus").rglob("*.py"):
		if {"tests", "tests_integration"} & set(path.parts) or path.name == "ai_fix.py":
			continue
		module = ast.parse(path.read_text())
		aliases = set()
		for node in ast.walk(module):
			if isinstance(node, ast.ImportFrom) and node.module == "optimus":
				aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "ai_fix")
			elif isinstance(node, ast.ImportFrom) and node.module == "optimus.ai_fix":
				result.update(alias.name for alias in node.names if alias.name.startswith("_"))
			elif isinstance(node, ast.Import):
				aliases.update(alias.asname or "optimus.ai_fix" for alias in node.names if alias.name == "optimus.ai_fix")
		for node in ast.walk(module):
			if isinstance(node, ast.Attribute) and ast.unparse(node.value) in aliases and node.attr.startswith("_"):
				result.add(node.attr)
	return result


def test_shared_ai_helpers_are_documented_as_internal_contracts():
	section = read("CLAUDE.md").split("### Shared AI internals", 1)[1].split("\n##", 1)[0]
	assert set(re.findall(r"`(_\w+)`", section)) == shared_ai_helpers()


def log_titles():
	paths = ("ai_fix.py", "analyze.py", "api.py", "ai_jobs.py", "line_profile/jobs.py")
	modules = [tree("optimus/" + path) for path in paths]
	loggers = {"log_ai_failure", "_run_ai_step"}
	while True:
		previous = len(loggers)
		for module in modules:
			for fn in ast.walk(module):
				if isinstance(fn, ast.FunctionDef) and "title" in {arg.arg for arg in fn.args.args + fn.args.kwonlyargs}:
					if any(isinstance(n, ast.Call) and ast.unparse(n.func).split(".")[-1] in loggers for n in ast.walk(fn)):
						loggers.add(fn.name)
		if len(loggers) == previous:
			break
	result = set()
	for module in modules:
		for call in ast.walk(module):
			if not isinstance(call, ast.Call) or ast.unparse(call.func).split(".")[-1] not in loggers:
				continue
			title = next((kw.value for kw in call.keywords if kw.arg == "title"), call.args[0] if call.args else None)
			if isinstance(title, ast.Constant) and isinstance(title.value, str):
				result.add(title.value)
			else:
				assert isinstance(title, ast.Name) and title.id == "title", (
					"Document a fixed log title or explicitly handle the new wrapper", ast.unparse(call)
				)
	return result


def test_scrubbed_log_titles_have_operator_guidance():
	rows = table("docs/AI-FIXING.md", "### Error Log reference")
	assert {row[0] for row in rows} == log_titles()
	assert all(len(row[1]) >= 30 for row in rows)


def test_log_inventory_follows_nested_title_forwarding(monkeypatch):
	source = "\n".join(
		f"def wrapper_{n}(title, exc): wrapper_{n + 1}(title, exc)" for n in range(5)
	) + '\ndef wrapper_5(title, exc): log_ai_failure(title, exc)\nwrapper_0("optimus nested", None)\n'
	monkeypatch.setitem(log_titles.__globals__, "tree", lambda path: ast.parse(source if path == "optimus/api.py" else ""))
	assert log_titles() == {"optimus nested"}


def test_integration_inventory_matches_workflow_modules():
	workflow = read(".github/workflows/integration.yml")
	modules = set(re.findall(r"^\s+(test_\w+)\s*\\?\s*$", workflow, re.M))
	assert modules
	rows = table("optimus/tests_integration/README.md", "## Current CI coverage")
	assert {row[0] for row in rows} == modules
	assert all((ROOT / "optimus/tests_integration" / (name + ".py")).is_file() for name in modules)


def test_install_smoke_includes_every_declared_doctype():
	declared = {json.loads(path.read_text())["name"]
		for path in (ROOT / "optimus/optimus/doctype").glob("*/*.json")}
	checked = literal_assignment("optimus/tests_integration/test_install_smoke.py", "_OPTIMUS_DOCTYPES")
	assert set(checked) == declared


def test_architecture_references_existing_modules():
	section = read("CLAUDE.md").split("## Architecture", 1)[1].split("## Conventions", 1)[0]
	paths = re.findall(r"`([\w/.-]+\.py)`", section)
	assert paths
	assert all((ROOT / "optimus" / path).is_file() for path in paths)


def test_acceptance_does_not_turn_missing_model_evidence_into_a_pass():
	text = read("docs/AI-ACCEPTANCE.md")
	for pending in ("Owner-confirmed labels", "BEFORE comparison", "Final context 0", "Final context 4096",
		"Recipe schema-sync durability", "End-user permission matrix", "Upgrade and rollback"):
		assert re.search(r"\| " + re.escape(pending) + r" \| PENDING \|", text)
	assert "Release acceptance: INCOMPLETE" in text
