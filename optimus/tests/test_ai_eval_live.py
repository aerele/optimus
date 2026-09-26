# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""scripts/ai_eval/live.py without a site or a network: the site guard, the provider
record and the per-case outcome logic against a fake ai_fix."""

import json
import types

import pytest

from optimus.tests.ai_eval_support import load


@pytest.fixture(scope="module")
def corpus():
	return load("_corpus").load_corpus()


# ---------------------------------------------------------------- live.py (no site, no network)
def test_site_guard():
	live = load("live")
	assert live.check_site("optimus.local", env={}) == "optimus.local"
	assert live.check_site("eval.test", env={"OPTIMUS_EVAL_ALLOW_SITE": "eval.test"}) == "eval.test"
	for site in ("prod.example.com", "", "optimus.local.evil"):
		with pytest.raises(live.EvalRefused):
			live.check_site(site, env={})


def test_main_refuses_before_touching_frappe(tmp_path, monkeypatch):
	live = load("live")
	# Even a deliberately broken site guard must never initialize a real site.
	monkeypatch.setattr(live, "_connect", lambda site: pytest.fail("site guard bypassed"))
	with pytest.raises(live.EvalRefused, match="refusing"):
		live.main(["--site", "erp.example.com", "--out", str(tmp_path / "run")])
	(tmp_path / "old").mkdir()
	(tmp_path / "old" / "run.json").write_text("{}")
	with pytest.raises(live.EvalRefused, match="exists"):
		live.main(["--out", str(tmp_path / "old")])


def test_provider_meta_never_carries_the_key():
	live = load("live")
	meta = live.provider_meta({"name": "OpenAI-compatible", "protocol": "openai", "model": "qwen3-coder:30b",
		"base_url": "http://10.0.0.5:11434/v1", "api_key": "sk-secret-value", "needs_key": False})
	assert "sk-secret-value" not in json.dumps(meta)
	assert meta == {"name": "OpenAI-compatible", "protocol": "openai", "model": "qwen3-coder:30b",
		"base_url_host": "10.0.0.5:11434", "has_key": True}


class _AiFixError(Exception):
	def __init__(self, message="", *, kind="unknown"):
		super().__init__(message)
		self.kind = kind


def _fake_ai_fix(**kw):
	calls = []

	def suggest_fix(finding):
		calls.append(finding)
		if "raise" in kw:
			raise kw["raise"]
		return {"suggestion": "**Diagnosis**: ok", "tokens": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}

	mod = types.SimpleNamespace(AiFixError=_AiFixError, suggest_fix=suggest_fix,
		AI_ELIGIBLE_FINDING_TYPES=kw.get("eligible", {"N+1 Query", "Redundant Call", "Hot Line"}))
	if "gate" in kw:
		mod.llm_gate_note = kw["gate"]
	return mod, calls


def test_run_case_outcomes(corpus):
	live = load("live")
	case = load("_corpus").case_by_name("3q1nfc4d2l", corpus)
	ok, calls = _fake_ai_fix()
	record = live.run_case(case, ok)
	assert record["result"]["tokens"]["total_tokens"] == 5 and record["error"] is None
	assert calls and calls[0]["source_window"]  # the production-shaped finding went in
	gated, calls = _fake_ai_fix(eligible={"Hot Line"})
	assert live.run_case(case, gated)["outcome"] == "gated" and not calls
	noted, calls = _fake_ai_fix(gate=lambda f: "re-record the flow")
	assert live.run_case(case, noted)["gate_note"] == "re-record the flow" and not calls
	failing, _ = _fake_ai_fix(**{"raise": _AiFixError("timed out", kind="timeout")})
	assert live.run_case(case, failing)["error"] == {"kind": "timeout", "message": "timed out"}
	refused, _ = _fake_ai_fix(**{"raise": _AiFixError("no", kind="not_eligible")})
	assert live.run_case(case, refused)["outcome"] == "gated"
	crashing, _ = _fake_ai_fix(**{"raise": KeyError("x")})
	assert live.run_case(case, crashing)["error"] == {"kind": "internal", "message": "KeyError"}


@pytest.mark.parametrize("url, expected", [
	("https://tester:fake-password@docs.example.com:8443/v1", "docs.example.com:8443"),
	("http://tester:fake-password@[::1]:11434/v1", "[::1]:11434"),
	("https://[invalid", ""),
])
def test_provider_meta_omits_url_credentials(url, expected):
	meta = load("live").provider_meta({"base_url": url})
	assert meta["base_url_host"] == expected
	assert "fake-password" not in json.dumps(meta)


def test_main_refuses_existing_output_directory(tmp_path, monkeypatch):
	live = load("live")
	monkeypatch.setattr(live, "_connect", lambda site: pytest.fail("unexpected connection"))
	with pytest.raises(live.EvalRefused, match="exists"):
		live.main(["--out", str(tmp_path)])


@pytest.mark.parametrize("symlink", [False, True])
def test_main_keeps_run_artifacts_outside_checkout(tmp_path, monkeypatch, symlink):
	live = load("live")
	repo = tmp_path / "checkout"
	repo.mkdir()
	monkeypatch.setattr(live._corpus, "REPO_ROOT", str(repo))
	monkeypatch.setattr(live, "_connect", lambda site: pytest.fail("unexpected connection"))
	out = repo / "run"
	if symlink:
		alias = tmp_path / "alias"
		alias.symlink_to(repo, target_is_directory=True)
		out = alias / "run"
	with pytest.raises(live.EvalRefused, match="outside"):
		live.main(["--out", str(out)])
	assert not out.exists()


def test_main_verifies_requested_checkout_before_ai_calls(tmp_path, monkeypatch):
	import sys

	live = load("live")
	checkout = tmp_path / "chosen"
	(checkout / "optimus").mkdir(parents=True)
	(checkout / "optimus" / "__init__.py").write_text("")
	ai = types.SimpleNamespace(is_available=lambda **kw: pytest.fail("unexpected AI call"))
	wrong = types.ModuleType("optimus")
	wrong.__file__ = str(tmp_path / "wrong" / "optimus" / "__init__.py")
	wrong.ai_fix = ai
	monkeypatch.setitem(sys.modules, "optimus", wrong)
	monkeypatch.setattr(sys, "path", list(sys.path))
	destroyed = []
	frappe = types.SimpleNamespace(local=types.SimpleNamespace(), destroy=lambda: destroyed.append(True))
	monkeypatch.setattr(live, "_connect", lambda site: (frappe, str(tmp_path)))
	with pytest.raises(live.EvalRefused, match="checkout"):
		live.main(["--out", str(tmp_path / "run"), "--optimus-src", str(checkout)])
	assert destroyed == [True]
