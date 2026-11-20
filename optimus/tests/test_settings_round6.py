# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the Optimus Settings additions.

Pure-test path (no live Frappe site). Covers the multi-line skip-list parser
(strips padding) and each analyzer's resolver honouring the configured value.
"""

from types import SimpleNamespace
from unittest.mock import patch

from optimus import settings
from optimus.analyzers import call_tree
from optimus.line_profile import analyzer as lp_analyzer


class TestParseSkipList:
	def test_empty_input_yields_empty_tuple(self):
		assert settings._parse_skip_list(None) == ()
		assert settings._parse_skip_list("") == ()
		assert settings._parse_skip_list("   ") == ()

	def test_multi_line_strips_whitespace(self):
		raw = "  /a  \n/b\n   /c   "
		assert settings._parse_skip_list(raw) == ("/a", "/b", "/c")


class TestSlowQueryThresholdResolver:
	"""top_queries.py reads slow_query_threshold_ms from settings; falls
	back to the legacy 200ms constant when settings are unreachable."""

	def test_override_propagates(self):
		from optimus.analyzers import top_queries

		fake_cfg = settings.OptimusConfig(slow_query_threshold_ms=400.0)
		with patch("optimus.settings.get_config", return_value=fake_cfg):
			slow, high = top_queries._resolve_slow_query_threshold()
		assert slow == 400.0
		assert high == 1000.0  # 2.5x multiplier


class TestSlowHotPathThresholdResolver:
	def test_settings_pct_converted_to_fraction(self):
		# Settings store pct as 0-100 for UX; resolver divides by 100.
		fake_cfg = settings.OptimusConfig(
			slow_hot_path_pct_threshold=30.0,
			slow_hot_path_min_ms=300.0,
		)
		with patch("optimus.settings.get_config", return_value=fake_cfg):
			med_pct, med_ms, high_pct, high_ms = call_tree._resolve_hot_path_thresholds()
		assert med_pct == 0.30
		assert med_ms == 300.0
		# High = Medium * legacy multipliers.
		assert high_pct == 0.30 * call_tree.HOT_PATH_HIGH_PCT_MULTIPLIER
		assert high_ms == 300.0 * call_tree.HOT_PATH_HIGH_MS_MULTIPLIER

	def test_legacy_defaults_when_settings_unreachable(self):
		# Patch get_config to raise so the resolver falls back.
		with patch("optimus.settings.get_config", side_effect=RuntimeError):
			med_pct, med_ms, high_pct, high_ms = call_tree._resolve_hot_path_thresholds()
		assert med_pct == call_tree.DEFAULT_HOT_PATH_PCT
		assert med_ms == call_tree.DEFAULT_HOT_PATH_MS


class TestHotLineThresholdResolver:
	def test_settings_drive_classification(self):
		# Configure a higher bar so the legacy 50% threshold fails.
		fake_cfg = settings.OptimusConfig(
			hot_line_high_pct=80.0,
			hot_line_high_min_ms=200.0,
		)
		with patch("optimus.settings.get_config", return_value=fake_cfg):
			# 60% of 1000ms = 600ms under 80% threshold, so Medium
			# (since med_pct = 80% * 0.5 = 40%).
			result = lp_analyzer._classify_hot_line(600, 1000)
			assert result == "Medium"

			# 90% of 1000ms = 900ms above both threshold and min_ms.
			result = lp_analyzer._classify_hot_line(900, 1000)
			assert result == "High"

			# Below both bars.
			result = lp_analyzer._classify_hot_line(50, 1000)
			assert result is None

	def test_settings_unreachable_falls_back(self):
		# Resolver still produces classifiable thresholds.
		with patch("optimus.settings.get_config", side_effect=RuntimeError):
			# 60% of 1000ms = 600ms over the legacy 50%/100ms High bar.
			assert lp_analyzer._classify_hot_line(600, 1000) == "High"


class TestRendererMinActionFilter:
	"""renderer.render() drops actions below min_action_duration_ms from
	the per-action breakdown when the setting is non-zero."""

	def _doc(self, actions):
		return SimpleNamespace(
			name="PS-test",
			session_uuid="test-uuid",
			title="test",
			user="tester@example.com",
			status="Ready",
			started_at="2026-04-13T00:00:00",
			stopped_at="2026-04-13T00:00:05",
			total_duration_ms=5000,
			total_requests=len(actions),
			total_queries=0,
			total_query_time_ms=0,
			analyze_duration_ms=100,
			top_severity="None",
			summary_html="<p>summary</p>",
			top_queries_json="[]",
			table_breakdown_json="[]",
			analyzer_warnings=None,
			actions=actions,
			findings=[],
			hot_frames_json=None,
			session_time_breakdown_json=None,
			total_python_ms=None,
			total_sql_ms=None,
		)

	def _action(self, label, duration_ms):
		return SimpleNamespace(
			action_label=label,
			event_type="request",
			http_method="GET",
			path="/" + label,
			recording_uuid="",
			duration_ms=duration_ms,
			queries_count=0,
			query_time_ms=0,
			slowest_query_ms=0,
			call_tree_json=None,
		)

	def test_threshold_drops_short_actions(self):
		from optimus import renderer

		doc = self._doc([
			self._action("noise_poll", 2.0),
			self._action("real_action", 800.0),
		])

		fake_cfg = settings.OptimusConfig(min_action_duration_ms=10.0)
		with patch("optimus.settings.get_config", return_value=fake_cfg):
			html = renderer.render_raw(doc, recordings=[])

		assert "real_action" in html
		# noise_poll was below the 10ms cutoff not in the per-action
		# table.
		assert "noise_poll" not in html
