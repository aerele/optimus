"""Capture limits stay exact, and a server-chosen run that fails its final checks is reported, not lost."""

import json

import pytest

from optimus.line_profile import capture
from optimus.tests.test_line_profile_auto_arm_skips import curated, long_function, wire
from optimus.tests.test_line_profile_source_capture import BLANKS, load

pytestmark = pytest.mark.rq


def one_function(tmp_path, monkeypatch, text=BLANKS):
	_, fn = load(tmp_path, monkeypatch, text)
	wire(monkeypatch, {"optimus.mod.f": fn})
	return fn


def payload_size(prepared):
	return len(prepared.picks_json) + len(prepared.source_json)


def limit_applies_only_to_the_final_check(monkeypatch, limit):
	"""The per-item running estimate sees a huge limit; the final payload check sees ``limit``."""
	real = capture._snapshot_picks

	def snapshot(eligible, *, server_chosen):
		monkeypatch.setattr(capture, "MAX_CAPTURE_BYTES", 10**9)
		try:
			return real(eligible, server_chosen=server_chosen)
		finally:
			monkeypatch.setattr(capture, "MAX_CAPTURE_BYTES", limit)

	monkeypatch.setattr(capture, "_snapshot_picks", snapshot)


def test_capture_source_lines_has_no_per_function_cap_of_its_own(tmp_path, monkeypatch):
	_, fn = load(tmp_path, monkeypatch, long_function(capture.MAX_PICK_SOURCE_LINES + 10))
	assert len(capture._capture_source_lines(fn)) == capture.MAX_PICK_SOURCE_LINES + 10


def test_the_running_estimate_equals_the_stored_payload(tmp_path, monkeypatch):
	one_function(tmp_path, monkeypatch)
	size = payload_size(capture.prepare_line_profile_picks(curated("f")))
	monkeypatch.setattr(capture, "MAX_CAPTURE_BYTES", size - 1)
	with pytest.raises(capture.CaptureError, match="exceeds the capture limits"):
		capture.prepare_line_profile_picks(curated("f"))
	monkeypatch.setattr(capture, "MAX_CAPTURE_BYTES", size)
	assert payload_size(capture.prepare_line_profile_picks(curated("f"))) == size


def test_the_final_payload_is_checked_exactly_against_the_size_limit(tmp_path, monkeypatch):
	one_function(tmp_path, monkeypatch)
	size = payload_size(capture.prepare_line_profile_picks(curated("f")))
	limit_applies_only_to_the_final_check(monkeypatch, size - 1)
	with pytest.raises(capture.CaptureError, match="exceeds the capture size limit") as caught:
		capture.prepare_line_profile_picks(curated("f"))
	assert not isinstance(caught.value, capture.NoEligibleSource)
	limit_applies_only_to_the_final_check(monkeypatch, size)
	assert payload_size(capture.prepare_line_profile_picks(curated("f"))) == size


def test_server_chosen_final_size_overflow_is_reported_as_nothing_capturable(tmp_path, monkeypatch):
	one_function(tmp_path, monkeypatch)
	size = payload_size(capture.prepare_line_profile_picks(curated("f")))
	limit_applies_only_to_the_final_check(monkeypatch, size - 1)
	with pytest.raises(capture.NoEligibleSource) as caught:
		capture.prepare_line_profile_picks(curated("f"), server_chosen=True)
	assert [s["function"] for s in caught.value.skipped] == ["optimus.mod.f"]
	assert caught.value.skipped[0]["reason"]


def test_server_chosen_final_validation_failure_is_reported_as_nothing_capturable(tmp_path, monkeypatch):
	one_function(tmp_path, monkeypatch)
	real = capture._snapshot_picks

	def emptied(eligible, *, server_chosen):
		metas, snapshot = real(eligible, server_chosen=server_chosen)
		return metas, {key: [] for key in snapshot}

	monkeypatch.setattr(capture, "_snapshot_picks", emptied)
	monkeypatch.setattr(capture, "_drop_invalid_picks", lambda eligible, metas, snapshot: metas)
	with pytest.raises(capture.NoEligibleSource) as caught:
		capture.prepare_line_profile_picks(curated("f"), server_chosen=True)
	assert [s["function"] for s in caught.value.skipped] == ["optimus.mod.f"]
	assert caught.value.skipped[0]["reason"]
	with pytest.raises(capture.CaptureError, match="cannot be captured") as plain:
		capture.prepare_line_profile_picks(curated("f"))
	assert not isinstance(plain.value, capture.NoEligibleSource)


def test_a_clean_server_chosen_run_still_prepares(tmp_path, monkeypatch):
	one_function(tmp_path, monkeypatch)
	prepared = capture.prepare_line_profile_picks(curated("f"), server_chosen=True)
	assert list(json.loads(prepared.source_json)) == ["optimus.mod.f"]
