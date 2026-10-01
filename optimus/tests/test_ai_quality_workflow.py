# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The ai-quality workflow: its pins agree with scripts/ai_eval/semgrep_rule_map.json, it
keeps the shape that makes semgrep and rq tests fail instead of skipping, and every
rq-dependent test module is selectable by its -k "semgrep or rq" run."""

import json
import os
import re

import pytest

from optimus.tests.ai_eval_support import REPO_ROOT, SCRIPTS_DIR

WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "ai-quality.yml")


@pytest.fixture(scope="module")
def rule_map():
	with open(os.path.join(SCRIPTS_DIR, "semgrep_rule_map.json"), encoding="utf-8") as fh:
		return json.load(fh)


@pytest.fixture(scope="module")
def workflow():
	with open(WORKFLOW, encoding="utf-8") as fh:
		return fh.read()


def test_workflow_pins_match_rule_map(rule_map, workflow):
	assert f"semgrep=={rule_map['semgrep']}" in workflow
	assert rule_map["rules_commit"] in workflow
	assert workflow.count("semgrep==") == workflow.count(f"semgrep=={rule_map['semgrep']}")


def test_workflow_keeps_its_safety_properties(workflow):
	for needle in (
		"  semgrep:\n", "  ai-tests:\n", "--baseline-commit", "continue-on-error: true",
		"REQUIRE_SEMGREP: '1'", "rq==2.6.1", '-k "semgrep or rq"', "ruff check scripts",
		"0000000000000000000000000000000000000000", "git merge-base origin/develop HEAD",
		"permissions:\n  contents: read\n", "pull_request:", "workflow_dispatch:",
	):
		assert needle in workflow, needle


def test_rq_dependent_test_modules_carry_the_rq_marker():
	"""CI's ai-quality workflow installs rq and runs `-k "semgrep or rq"`; a module that
	needs rq must be selectable, or it stays a silent skip forever."""
	rq_use = re.compile(r"^\s*(?:import rq\b|from rq\b)|importorskip\(\s*[\"']rq\b", re.M)
	marked = re.compile(r"^pytestmark\s*=.*pytest\.mark\.rq\b", re.M)
	here = os.path.dirname(__file__)
	offenders = []
	for name in sorted(os.listdir(here)):
		if name.startswith("test_") and name.endswith(".py"):
			src = open(os.path.join(here, name), encoding="utf-8").read()
			if rq_use.search(src) and "rq" not in name and not marked.search(src):
				offenders.append(name)
	assert offenders == []
