# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for analyze._enrich_findings_with_source_snippets runs without
Frappe; takes a list of finding dicts (with technical_detail_json) and a
real file on disk, then asserts the source_snippet shape after enrichment."""

import json
import os
import tempfile

from optimus import analyze


def _finding(filename, lineno, function="fn"):
	return {
		"finding_type": "Slow Hot Path",
		"severity": "Medium",
		"title": "test",
		"customer_description": "test",
		"estimated_impact_ms": 100,
		"affected_count": 1,
		"action_ref": "0",
		"technical_detail_json": json.dumps({
			"callsite": {
				"filename": filename,
				"lineno": lineno,
				"function": function,
			},
		}),
	}


def _read_snippet(finding):
	detail = json.loads(finding["technical_detail_json"])
	return detail.get("callsite", {}).get("source_snippet")


class TestEnrichFindingsWithSourceSnippets:
	def test_attaches_lines_centered_on_lineno(self, tmp_path):
		# v0.7.x: snippet window widened from ±1 to ±4 for a 5-line file
		# anchored on line 3, the window covers lines 1..5 (clipped at
		# file boundaries).
		src = tmp_path / "fake.py"
		src.write_text("line one\nline two\nline three\nline four\nline five\n")
		findings = [_finding(str(src), 3)]

		analyze._enrich_findings_with_source_snippets(findings)

		snippet = _read_snippet(findings[0])
		assert snippet == [
			{"lineno": 1, "content": "line one"},
			{"lineno": 2, "content": "line two"},
			{"lineno": 3, "content": "line three"},
			{"lineno": 4, "content": "line four"},
			{"lineno": 5, "content": "line five"},
		]

	def test_lineno_at_first_line_skips_below(self, tmp_path):
		# Anchored on line 1 with a ±4 window → range(1, 6); a 3-line
		# file → all 3 lines returned, none below line 1.
		src = tmp_path / "fake.py"
		src.write_text("alpha\nbeta\ngamma\n")
		findings = [_finding(str(src), 1)]

		analyze._enrich_findings_with_source_snippets(findings)

		snippet = _read_snippet(findings[0])
		assert snippet == [
			{"lineno": 1, "content": "alpha"},
			{"lineno": 2, "content": "beta"},
			{"lineno": 3, "content": "gamma"},
		]

	def test_lineno_at_last_line_skips_above(self, tmp_path):
		# Anchored on line 3 (file end) with ±4 window → range(1, 8) but
		# only 3 lines exist; window clipped to lines 1..3.
		src = tmp_path / "fake.py"
		src.write_text("alpha\nbeta\ngamma\n")
		findings = [_finding(str(src), 3)]

		analyze._enrich_findings_with_source_snippets(findings)

		snippet = _read_snippet(findings[0])
		assert snippet == [
			{"lineno": 1, "content": "alpha"},
			{"lineno": 2, "content": "beta"},
			{"lineno": 3, "content": "gamma"},
		]

	def test_long_line_truncated_with_ellipsis(self, tmp_path):
		long = "x" * 500
		src = tmp_path / "fake.py"
		src.write_text(f"a\n{long}\nb\n")
		findings = [_finding(str(src), 2)]

		analyze._enrich_findings_with_source_snippets(findings)

		snippet = _read_snippet(findings[0])
		# Line 2 is the long one; expect truncation at 200 chars + "..."
		hot = next(s for s in snippet if s["lineno"] == 2)
		assert hot["content"].endswith("...")
		# The truncated body itself is 200 chars; total = 203
		assert len(hot["content"]) == 203

	def test_missing_file_silent_skip(self, tmp_path):
		findings = [_finding(str(tmp_path / "does_not_exist.py"), 5)]

		analyze._enrich_findings_with_source_snippets(findings)

		assert _read_snippet(findings[0]) is None

	def test_lineno_out_of_range_silent_skip(self, tmp_path):
		src = tmp_path / "tiny.py"
		src.write_text("only one line\n")
		findings = [_finding(str(src), 999)]

		analyze._enrich_findings_with_source_snippets(findings)

		# 999 is past EOF; no in-range neighbors → no snippet attached.
		assert _read_snippet(findings[0]) is None

	def test_invalid_lineno_string_silent_skip(self, tmp_path):
		src = tmp_path / "fake.py"
		src.write_text("a\nb\nc\n")
		f = _finding(str(src), 2)
		# Mangle the lineno to a non-numeric string.
		detail = json.loads(f["technical_detail_json"])
		detail["callsite"]["lineno"] = "not-a-number"
		f["technical_detail_json"] = json.dumps(detail)

		analyze._enrich_findings_with_source_snippets([f])

		assert _read_snippet(f) is None

	def test_callsite_without_filename_silent_skip(self):
		f = {
			"technical_detail_json": json.dumps({
				"callsite": {"function": "foo"},  # no filename, no lineno
			}),
		}

		analyze._enrich_findings_with_source_snippets([f])

		assert _read_snippet(f) is None

	def test_finding_without_technical_detail_silent_skip(self):
		f = {"technical_detail_json": ""}

		analyze._enrich_findings_with_source_snippets([f])

		# Function should not crash on empty detail.
		assert f["technical_detail_json"] == ""

	def test_malformed_json_silent_skip(self):
		f = {"technical_detail_json": "{not valid json"}

		analyze._enrich_findings_with_source_snippets([f])

		# Function should not crash on malformed JSON.
		assert f["technical_detail_json"] == "{not valid json"

	def test_non_dict_finding_skipped_cleanly(self, tmp_path):
		# The function is typed as ``list[dict]`` but defence-in-depth:
		# a corrupted upstream payload could plant a non-dict entry
		# (None, scalar, list, string). The whole analyze job must not
		# crash because of one bad row silent skip per the function's
		# best-effort contract, valid neighbours still get enriched.
		src = tmp_path / "fake.py"
		src.write_text("a\nb\nc\n")
		good = _finding(str(src), 2)
		findings = [None, "garbage", 42, [1, 2], good]

		# Must not raise on any non-dict entry.
		analyze._enrich_findings_with_source_snippets(findings)

		# The valid neighbour still got its snippet attached.
		snippet = _read_snippet(good)
		assert snippet == [
			{"lineno": 1, "content": "a"},
			{"lineno": 2, "content": "b"},
			{"lineno": 3, "content": "c"},
		]

	def test_non_dict_detail_skipped_cleanly(self, tmp_path):
		# ``json.loads("null") -> None``, ``json.loads('"x"') -> "x"``,
		# ``json.loads("[1,2]") -> [1, 2]``: all valid JSON, none are
		# the dict shape the rest of the loop assumes. Each must silent-
		# skip; the valid neighbour still gets enriched.
		src = tmp_path / "fake.py"
		src.write_text("a\nb\nc\n")
		good = _finding(str(src), 2)
		findings = [
			{"technical_detail_json": "null"},
			{"technical_detail_json": '"a string"'},
			{"technical_detail_json": "[1, 2, 3]"},
			{"technical_detail_json": "42"},
			good,
		]

		# Must not raise on any non-dict detail shape.
		analyze._enrich_findings_with_source_snippets(findings)

		# The valid neighbour still got its snippet attached.
		snippet = _read_snippet(good)
		assert snippet == [
			{"lineno": 1, "content": "a"},
			{"lineno": 2, "content": "b"},
			{"lineno": 3, "content": "c"},
		]

	def test_string_form_callsite_skipped_cleanly(self, tmp_path):
		# Slow Query findings (optimus/analyzers/top_queries.py:129) store
		# callsite as a "path:lineno" string, not the canonical
		# {filename, lineno, function} dict every other finding type uses.
		# The enrichment loop must not crash on that shape renderer
		# handles snippets for these findings at render time via
		# _normalize_callsite + lazy attach in _finding_to_dict.
		#
		# Regression for the production traceback on session
		# 523567a82bce0342: ``builtins.AttributeError: 'str' object has
		# no attribute 'get'`` at analyze.py:1391.
		src = tmp_path / "fake.py"
		src.write_text("line\n")
		f = {
			"finding_type": "Slow Query",
			"severity": "High",
			"title": "Slow query: 1020ms",
			"customer_description": "...",
			"estimated_impact_ms": 1020,
			"affected_count": 1,
			"action_ref": "0",
			"technical_detail_json": json.dumps({
				"normalized_query": "SELECT 1",
				"callsite": f"{src}:1",  # STRING form, not dict
				"recording_uuid": "abc",
				"fix_hint": "",
			}),
		}

		# Must not raise.
		analyze._enrich_findings_with_source_snippets([f])

		# Slow Query callsite shape preserved (string, not mutated to
		# dict) renderer normalizes it lazily at render time.
		detail = json.loads(f["technical_detail_json"])
		assert detail["callsite"] == f"{src}:1"

	def test_file_cache_avoids_repeated_reads(self, tmp_path, monkeypatch):
		src = tmp_path / "shared.py"
		src.write_text("a\nb\nc\nd\ne\n")
		findings = [
			_finding(str(src), 2),
			_finding(str(src), 3),
			_finding(str(src), 4),
		]

		open_calls = []
		real_open = open

		def counting_open(path, *args, **kwargs):
			open_calls.append(path)
			return real_open(path, *args, **kwargs)

		monkeypatch.setattr("builtins.open", counting_open)

		analyze._enrich_findings_with_source_snippets(findings)

		# All three findings hit the same file → file should be opened once.
		hits_for_src = [p for p in open_calls if p == str(src)]
		assert len(hits_for_src) == 1
		# All three got snippets.
		assert all(_read_snippet(f) for f in findings)

	def test_non_utf8_file_silent_skip(self, tmp_path):
		src = tmp_path / "binary.py"
		src.write_bytes(b"\xff\xfe invalid utf8 \xc3\x28\n")
		findings = [_finding(str(src), 1)]

		analyze._enrich_findings_with_source_snippets(findings)

		# Decode error → silent skip, finding still has no source_snippet.
		assert _read_snippet(findings[0]) is None
