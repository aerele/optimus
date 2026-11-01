# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The shared bulk batch cap: a subscription run spends the operator's own plan usage, so a
bulk refresh is bounded (0 = "all" included). HTTP providers keep their configured cap."""

import pytest

from optimus import ai_fix, ai_jobs
from optimus.tests.test_ai_refresh_inputs import answer, config, row

MAX = ai_fix.SUBSCRIPTION_BULK_MAX_FINDINGS


@pytest.mark.parametrize(
	("cap", "expected"),
	[(0, MAX), (1, 1), (MAX, MAX), (MAX + 1, MAX), (10**6, MAX)],
)
def test_subscription_mode_bounds_every_cap_including_all(monkeypatch, cap, expected):
	monkeypatch.setattr(ai_fix, "subscription_mode_active", lambda: True)
	assert ai_fix.bulk_batch_cap(cap) == expected


@pytest.mark.parametrize("cap", [0, 1, 20, 10**6])
def test_http_providers_keep_their_configured_cap(monkeypatch, cap):
	monkeypatch.setattr(ai_fix, "subscription_mode_active", lambda: False)
	assert ai_fix.bulk_batch_cap(cap) == cap


def test_the_cap_is_a_small_positive_bound():
	assert type(MAX) is int and 1 <= MAX <= 25


def test_a_codex_usage_limit_is_a_fatal_quota_error_that_stops_a_bulk():
	label = ai_fix._classify_cli_stderr("You've hit your usage limit. Upgrade or try again later.")
	message, kind = ai_fix._CLI_EXEC_ERRORS[label]
	assert kind == "quota" and kind in ai_fix.AI_FATAL_KINDS


def test_the_refresh_plan_shows_the_bounded_cap_in_subscription_mode(monkeypatch):
	cfg = config("Slow Query")
	cfg.ai_enabled = cfg.ai_suggest_findings = cfg.ai_humanize_steps = True
	cfg.ai_refresh_max_findings = 0
	monkeypatch.setattr("optimus.settings.get_config", lambda: cfg)
	monkeypatch.setattr(ai_jobs, "_now", lambda: 1780000000)
	monkeypatch.setattr(ai_fix, "subscription_mode_active", lambda: True)
	findings = [row(f"missing-{i}") for i in range(MAX + 5)]
	plan = ai_jobs.refresh_plan({"findings": findings, "actions": []})
	assert plan["cap"] == MAX and plan["selected"] == MAX and plan["pending"] == MAX + 5


def test_the_cap_settings_say_subscription_mode_clamps_them_to_ten():
	import json
	import os

	path = os.path.join(
		os.path.dirname(__file__), "..", "optimus", "doctype", "optimus_settings", "optimus_settings.json"
	)
	with open(path) as f:
		fields = {d["fieldname"]: d for d in json.load(f)["fields"]}
	for name in ("ai_refresh_max_findings", "ai_auto_suggest_max"):
		text = fields[name]["description"]
		assert "Subscription (local CLI)" in text and str(MAX) in text, name
