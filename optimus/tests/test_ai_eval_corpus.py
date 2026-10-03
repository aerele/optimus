# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The committed AI eval corpus (optimus/tests/fixtures/ai_corpus_2026-09-09.json), the
production-shaped finding builder in scripts/ai_eval/_corpus.py, and the rule that keeps
scripts/ out of every test runner."""

import json
import os
import re

import pytest

from optimus.tests.ai_eval_support import REPO_ROOT, SCRIPTS_DIR, load

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "ai_corpus_2026-09-09.json")
PHASE2_CASES = {"3q1dsfrmti", "3q1gf7r9lq", "3q1nfc4d2l"}


@pytest.fixture(scope="module")
def corpus():
	with open(FIXTURE, encoding="utf-8") as fh:
		return json.load(fh)


def test_all_15_cases_parse(corpus):
	cases = corpus["cases"]
	assert len(cases) == 15
	assert len({c["name"] for c in cases}) == 15
	for c in cases:
		assert isinstance(c["technical_detail"], dict), c["name"]
		assert c["finding_type"] in ("N+1 Query", "Redundant Call", "Hot Line"), c["name"]
		assert c["suggestion"].startswith("**Diagnosis**"), c["name"]
	assert corpus["labels"] == ["correct", "safe-directional", "wrong", "harmful"]


def test_every_window_contains_its_target_line(corpus):
	ev = load("_corpus")
	for c in corpus["cases"]:
		i = c["target_lineno"] - c["window_first_lineno"]
		assert 0 <= i < len(c["source_lines"]), c["name"]
		target = c["source_lines"][i]
		assert target.strip() and not ev.is_masked_line(target), c["name"]
		detail = c["technical_detail"]
		if c["finding_type"] == "Hot Line":
			assert target == detail["line_content"], c["name"]
			assert "callsite" not in detail, c["name"]  # file/lineno live on the detail itself
		for row in (detail.get("callsite") or {}).get("source_snippet") or []:
			assert c["source_lines"][row["lineno"] - c["window_first_lineno"]] == row["content"], c["name"]


def test_fixture_is_publishable(corpus):
	assert corpus["session"] == "redacted", "the source session identifier must not be stored"
	text = open(FIXTURE, encoding="utf-8").read()
	assert not re.search(r"/(?:Users|home)/", text), "absolute home paths must be relativised"
	for c in corpus["cases"]:
		framework = c["source_file"].startswith(("erpnext/", "frappe/"))
		assert c["masked"] is framework, c["name"]
		if framework:
			masked = [ln for ln in c["source_lines"] if load("_corpus").is_masked_line(ln)]
			assert len(masked) * 2 > len(c["source_lines"]), c["name"]


_KEY_SHAPES = re.compile(r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{8,}|gsk_[A-Za-z0-9]{8,})|AIza[0-9A-Za-z_-]{16,}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
_PRIVATE_IPV4 = re.compile(r"\b(?:10(?:\.\d{1,3}){3}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|192\.168(?:\.\d{1,3}){2})\b")
_LAN_HOST = re.compile(r"\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:local|lan|internal|home|corp)\b")
# optimus.local: the documented test-site name, quoted in ugly_code's own source (a
# `bench --site optimus.local execute` docstring); frappe.local: Frappe's request proxy.
_ALLOWED_HOSTS = {"optimus.local", "frappe.local"}


def test_fixture_carries_no_keys_emails_or_lan_addresses():
	text = open(FIXTURE, encoding="utf-8").read()
	assert _KEY_SHAPES.findall(text) == []
	assert [d for d in _EMAIL.findall(text) if not re.search(r"(^|\.)example\.(com|org|net)$", d)] == []
	assert _PRIVATE_IPV4.findall(text) == []
	assert set(_LAN_HOST.findall(text)) <= _ALLOWED_HOSTS, set(_LAN_HOST.findall(text)) - _ALLOWED_HOSTS


def test_labels_use_the_four_values(corpus):
	for c in corpus["cases"]:
		for key in ("draft_label", "label"):
			value = c.get(key)
			if value is not None:
				assert value["label"] in corpus["labels"], (c["name"], key)
				assert value["reason"].strip(), (c["name"], key)
		assert c["correct_fix"].strip(), c["name"]


def test_build_finding_goes_through_the_current_message_builder(corpus):
	"""The eval sends what the product sends: the finding comes from
	analyze._ai_payload_for_finding and the message from ai_fix's current builder."""
	from optimus import ai_fix

	ev = load("_corpus")
	for c in corpus["cases"]:
		finding = ev.build_finding(c)
		target = c["target_lineno"]
		rows = {r["lineno"]: r for r in finding["source_window"]}
		assert rows[target]["is_target"] and rows[target]["content"] == c["source_lines"][target - c["window_first_lineno"]]
		assert len(finding["source_window"]) <= ai_fix._SOURCE_LINES_BEFORE + ai_fix._SOURCE_LINES_AFTER + 1
		assert finding["technical_detail"]["callsite"]["lineno"] == target, c["name"]
		assert bool(finding.get("phase2_hotline")) is (c["name"] in PHASE2_CASES), c["name"]
		if hasattr(ai_fix, "_build_fix_request"):  # prompt v2 (#54)
			_system, messages, _shown = ai_fix._build_fix_request(finding, threshold_ms=1000.0, context_tokens=4096)
		else:
			_system, messages = ai_fix._build_messages(finding, threshold_ms=1000.0)
		user = messages[0]["content"]
		assert f">> {target}:" in user, c["name"]
		assert c["title"].split(" ")[0] in user, c["name"]


def test_hydrate_fills_masked_lines_only_when_the_shape_matches(corpus, tmp_path):
	ev = load("_corpus")
	case = ev.case_by_name("5qi7eha1pf", corpus)
	disk = [""] * (case["window_first_lineno"] - 1) + [
		re.sub(r"x+$", lambda m: "y" * len(m.group(0)), ln) if ev.is_masked_line(ln) else ln
		for ln in case["source_lines"]
	]
	path = tmp_path / case["source_file"]
	path.parent.mkdir(parents=True)
	path.write_text("\n".join(disk) + "\n", encoding="utf-8")
	lines, drift = ev.hydrate(case, str(tmp_path))
	assert drift == [] and not any(ev.is_masked_line(ln) for ln in lines)
	disk[case["target_lineno"] - 1] = "\t\t\tself.something_else()"
	path.write_text("\n".join(disk) + "\n", encoding="utf-8")
	lines, drift = ev.hydrate(case, str(tmp_path))
	assert drift and lines == case["source_lines"]
	disk[case["target_lineno"] - 1] = case["source_lines"][case["target_lineno"] - case["window_first_lineno"]]
	masked_at = next(i for i, ln in enumerate(case["source_lines"]) if ev.is_masked_line(ln))
	disk[case["window_first_lineno"] - 1 + masked_at] += "  # longer now"
	path.write_text("\n".join(disk) + "\n", encoding="utf-8")
	lines, drift = ev.hydrate(case, str(tmp_path))
	assert drift and lines == case["source_lines"]  # a masked line changed shape
	assert ev.hydrate(case, str(tmp_path / "nowhere"))[1]


def test_scripts_stay_out_of_every_test_runner():
	for _root, _dirs, names in os.walk(SCRIPTS_DIR):
		for name in names:
			assert name != "__init__.py", "scripts/ai_eval must not become an importable package"
			assert not re.match(r"(test_.*|.*_test)\.py$|conftest\.py$", name), name
	for root, _dirs, names in os.walk(os.path.join(REPO_ROOT, "optimus")):
		if os.sep + "tests" in root:
			continue
		for name in names:
			if name.endswith(".py"):
				with open(os.path.join(root, name), encoding="utf-8") as fh:
					assert "ai_eval" not in fh.read(), os.path.join(root, name)



# PR-L1: what happens to each of the 15 original cases once the gates land (verified
# against the committed fixture; the same set with and without Tracked Apps).
POST_L1_GATED = {
	# Redundant Call without the D-STAMP stamp (analyzed before the callsite fix)
	"3q1efl686s": "pre-L5", "3q1fsdhl5p": "pre-L5", "3q1gf7r9lq": "pre-L5", "3q1qg8btng": "pre-L5",
	# Hot Line inside ERPNext code
	"5qi7eha1pf": "framework", "5qiej4vhmk": "framework", "5qih2paean": "framework", "5qij9o50so": "framework",
	# Hot Line whose time is spent inside super().validate()
	"5qid8t8lu5": "callee",
}
POST_L1_AI = {"3q1dsfrmti", "3q1j3utnoa", "3q1ln8bv2o", "3q1nfc4d2l", "5qimkr12p6", "5qirpu97ci"}


@pytest.mark.parametrize("tracked_apps", [(), ("ugly_code",)])
def test_post_l1_disposition_of_every_original_case(tracked_apps):
	"""9 of the 15 original cases are gated after PR-L1 (4 pre-L5 Redundant Call, 4
	framework Hot Lines, 1 callee Hot Line); the 4 N+1 cases and the 2 pure-Python Hot
	Lines still reach the AI. A change here changes the eval's coverage: update it only
	with the owner's acceptance (master "Coverage")."""
	from types import SimpleNamespace
	from unittest.mock import patch

	from optimus import ai_fix
	from optimus.renderer import fix_recipes

	kinds = {
		"pre-L5": lambda note: note == fix_recipes.PRE_L5_REDUNDANT_CALL_NOTE,
		"framework": lambda note: note.startswith("This line is in framework or library code"),
		"callee": lambda note: note.startswith("Most of this line's time is spent inside super().validate"),
	}
	ev = load("_corpus")
	cfg = SimpleNamespace(tracked_apps=tracked_apps, ai_excluded_finding_types=())
	with patch("optimus.settings.get_config", return_value=cfg):
		got = {c["name"]: ai_fix.llm_gate_note(ev.build_finding(c)) for c in ev.cases()[:15]}
	assert {name for name, note in got.items() if note is None} == POST_L1_AI
	for name, kind in POST_L1_GATED.items():
		assert got[name] and kinds[kind](got[name]), (name, got[name])
