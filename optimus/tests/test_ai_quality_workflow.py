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


def test_sql_journal_is_exercised_on_both_database_engines(workflow):
	"""MariaDB 10.6 (snapshot isolation off), MariaDB 11.8 (``innodb_snapshot_isolation`` on
	by default, as on Frappe v16's CI: 1020 conflicts) and Postgres, each a leg of its own."""
	job = workflow.split('  sql-journal:\n', 1)[1]
	for needle in (
		'image: mariadb:${{ matrix.mariadb }}', 'image: postgres:16',
		"- leg: mariadb 10.6\n            database: mariadb\n            mariadb: '10.6'\n            snapshot: '0'",
		"- leg: mariadb 11.8\n            database: mariadb\n            mariadb: '11.8'\n            snapshot: '1'",
		"- leg: postgres 16\n            database: postgres",
		'OPTIMUS_TEST_SQL: ${{ matrix.database }}', 'OPTIMUS_TEST_SQL_SNAPSHOT: ${{ matrix.snapshot }}',
		'PyMySQL==1.1.2', 'psycopg2-binary==2.9.12', 'python -m pytest optimus/tests/test_ai_refresh_sql.py',
	):
		assert needle in job, needle
	assert 'continue-on-error' not in job


def test_the_header_lists_every_job(workflow):
	"""D6: the comment block at the top names each job, so a reader of the file finds them all."""
	header = workflow.split("\non:\n", 1)[0]
	jobs = re.findall(r"^  ([a-z][a-z0-9-]*):\n    name:", workflow.split("\njobs:\n", 1)[1], re.M)
	assert jobs == ["semgrep", "ai-tests", "sql-journal"]
	for job in jobs:
		assert re.search(rf"^#   {re.escape(job)}\s", header, re.M), job
	listed = header.split("sql-journal", 1)[1]
	assert "MariaDB 10.6" in listed and "MariaDB 11.8" in listed and "PostgreSQL 16" in listed
