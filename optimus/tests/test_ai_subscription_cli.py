# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the Codex CLI subscription transport in ``ai_fix``.

Every subprocess call is MOCKED: nothing here ever runs a real ``codex``
command (the real CLI bills a subscription and sends data). ``subprocess``,
``shutil.which`` and ``os.killpg`` are patched on the ``ai_fix`` module, and
``settings.get_config`` returns a plain SimpleNamespace.
"""

from __future__ import annotations

import os
import signal
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from optimus import ai_fix

_INJECTION = 'x; rm -rf / `id` $(whoami) "quoted" \'single\''


def _cfg(**kw):
	base = {
		"ai_enabled": True,
		"ai_connection_type": "Subscription (local CLI)",
		"ai_subscription_cli": "Codex CLI",
		"ai_cli_path": "",
		"ai_request_timeout_seconds": 45,
		"ai_suggest_findings": True,
		"ai_suggest_indexes": True,
		"ai_humanize_steps": True,
	}
	base.update(kw)
	return SimpleNamespace(**base)


@pytest.fixture
def sub_cfg(monkeypatch):
	"""Subscription mode on; get_config returns the cfg."""
	cfg = _cfg()
	monkeypatch.setattr("optimus.settings.get_config", lambda: cfg)
	return cfg


class _FakeProc:
	def __init__(
		self, returncode=0, stdout="", stderr="", raise_timeout=False, write_out=None,
		raise_exc=None, alive=False,
	):
		self.returncode = returncode
		self._raise_exc = raise_exc
		self._alive = alive
		self._stdout = stdout
		self._stderr = stderr
		self._raise_timeout = raise_timeout
		self._write_out = write_out
		self.pid = 4242
		self.communicate_calls = []
		self.out_file = None

	def communicate(self, input=None, timeout=None):
		self.communicate_calls.append({"input": input, "timeout": timeout})
		if self._raise_timeout:
			raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout)
		if self._raise_exc is not None:
			raise self._raise_exc
		if self._write_out is not None and self.out_file:
			with open(self.out_file, "w", encoding="utf-8") as f:
				f.write(self._write_out)
		return self._stdout, self._stderr

	def wait(self, timeout=None):
		return self.returncode

	def poll(self):
		return None if self._alive else self.returncode


class _FakeSubprocess:
	"""Stands in for the ``subprocess`` module inside ai_fix. Keeps the real
	exception class and PIPE so ``except subprocess.TimeoutExpired`` works."""

	PIPE = subprocess.PIPE
	TimeoutExpired = subprocess.TimeoutExpired

	def __init__(
		self, proc=None, login_stdout="", login_stderr="Logged in using ChatGPT",
		login_rc=0, popen_exc=None,
	):
		self.proc = proc or _FakeProc(stdout="FIXTEXT")
		self._popen_exc = popen_exc
		self._login_stderr = login_stderr
		self.popen_calls = []
		self.run_calls = []
		self._login_stdout = login_stdout
		self._login_rc = login_rc

	def Popen(self, argv, **kw):
		self.popen_calls.append({"argv": argv, "kw": kw})
		if self._popen_exc is not None:
			raise self._popen_exc
		if "-o" in argv:
			self.proc.out_file = argv[argv.index("-o") + 1]
		return self.proc

	def run(self, argv, **kw):
		self.run_calls.append({"argv": argv, "kw": kw})
		if "login" in argv:
			return SimpleNamespace(
				returncode=self._login_rc, stdout=self._login_stdout, stderr=self._login_stderr,
			)
		return SimpleNamespace(returncode=0, stdout="", stderr="codex-cli 1.2.3\n")


@pytest.fixture
def fake_cli(monkeypatch, sub_cfg):
	"""A resolvable codex path plus a signed-in fake subprocess (the real CLI
	prints the login status to stderr). Returns the
	fake so a test can inspect the calls (or swap fake.proc)."""
	fake = _FakeSubprocess()
	monkeypatch.setattr(ai_fix, "subprocess", fake)
	monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
	return fake


# ---------------------------------------------------------------------------
# Pure argv / prompt builders
# ---------------------------------------------------------------------------

class TestBuildArgv:
	def test_argv_has_no_shell_metacharacters_from_data(self):
		"""AC-SEC-1: the argv is a fixed list built from paths only."""
		argv = ai_fix._build_cli_argv("codex", "/s", "/s/out")
		joined = " ".join(argv)
		assert "; rm -rf /" not in joined
		assert "`" not in joined
		assert "$(" not in joined

	def test_argv_hardening_flags(self):
		"""AC-SEC-2: read-only sandbox, ephemeral, no user config, scratch dir,
		output file, and the trailing '-' (prompt from stdin)."""
		argv = ai_fix._build_cli_argv("codex", "/s", "/s/out")
		assert argv[:2] == ["codex", "exec"]
		assert argv[argv.index("--sandbox") + 1] == "read-only"
		for flag in ("--skip-git-repo-check", "--ignore-user-config", "--ignore-rules", "--ephemeral"):
			assert flag in argv
		assert argv[argv.index("-C") + 1] == "/s"
		assert argv[argv.index("-o") + 1] == "/s/out"
		assert argv[-1] == "-"


class TestComposePrompt:
	def test_joins_system_and_messages_with_blank_lines(self):
		out = ai_fix._compose_cli_prompt("SYS", [{"role": "user", "content": "A"}, {"role": "user", "content": "B"}])
		assert out == "SYS\n\nA\n\nB"

	def test_skips_empty_parts_and_strips(self):
		out = ai_fix._compose_cli_prompt("  ", [{"role": "user", "content": " hi \n"}, {"role": "user", "content": ""}])
		assert out == "hi"


# ---------------------------------------------------------------------------
# _call_local_cli hardening
# ---------------------------------------------------------------------------

class TestCallLocalCli:
	def _call(self, system="SYS", content="hello"):
		return ai_fix._call_local_cli(
			{"protocol": "subscription"}, system, [{"role": "user", "content": content}],
		)

	def test_prompt_goes_to_stdin_never_argv(self, fake_cli):
		"""AC-SEC-1 + AC-RES-2: metacharacters ride in communicate(input=...);
		argv stays the fixed list."""
		self._call(content=_INJECTION)
		call = fake_cli.popen_calls[0]
		assert not any("rm -rf" in a or "`" in a or "$(" in a for a in call["argv"])
		sent = fake_cli.proc.communicate_calls[0]["input"]
		assert _INJECTION in sent
		assert sent == ai_fix._compose_cli_prompt("SYS", [{"role": "user", "content": _INJECTION}])
		assert call["argv"][0] == "/usr/bin/codex"
		assert call["argv"][-1] == "-"

	def test_scratch_cwd_and_flags(self, fake_cli):
		"""AC-SEC-2: Popen runs in the scratch dir that argv names with -C."""
		self._call()
		call = fake_cli.popen_calls[0]
		argv = call["argv"]
		scratch = argv[argv.index("-C") + 1]
		assert call["kw"]["cwd"] == scratch
		assert os.path.basename(scratch).startswith("optimus-codex-")
		assert argv[argv.index("-o") + 1] == os.path.join(scratch, "out.txt")
		assert call["kw"]["start_new_session"] is True
		assert call["kw"].get("shell") in (None, False)
		assert call["kw"]["errors"] == "replace"
		assert call["kw"]["stdin"] == subprocess.PIPE
		assert "read-only" in argv

	def test_scratch_dir_removed_afterwards(self, fake_cli):
		self._call()
		argv = fake_cli.popen_calls[0]["argv"]
		assert not os.path.exists(argv[argv.index("-C") + 1])

	def test_env_drops_openai_keys_and_keeps_path(self, fake_cli, monkeypatch):
		"""AC-SEC-3: API-key vars removed so only the subscription session works."""
		monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
		monkeypatch.setenv("OPENAI_BASE_URL", "http://evil")
		monkeypatch.setenv("CODEX_API_KEY", "sk-codex")
		monkeypatch.setenv("OPENAI_ORGANIZATION", "org-x")
		monkeypatch.setenv("OPENAI_ORG_ID", "org-y")
		monkeypatch.setenv("PATH", "/usr/bin:/bin")
		self._call()
		env = fake_cli.popen_calls[0]["kw"]["env"]
		assert "OPENAI_API_KEY" not in env
		for name in ("OPENAI_BASE_URL", "CODEX_API_KEY", "OPENAI_ORGANIZATION", "OPENAI_ORG_ID"):
			assert name not in env
		assert env["PATH"] == "/usr/bin:/bin"

	def test_nonzero_exit_leaks_nothing(self, fake_cli, monkeypatch):
		"""AC-SEC-4: neither the error message nor any log call carries stderr."""
		fake_cli.proc = _FakeProc(
			returncode=1, stdout="CANARY_STDOUT_SECRET", stderr="OPENAI_API_KEY=sk-secret boom",
		)
		logged = []
		monkeypatch.setattr(
			"frappe.log_error", lambda *a, **kw: logged.append((a, kw)), raising=False,
		)
		with pytest.raises(ai_fix.AiFixError) as ei:
			self._call()
		msg = str(ei.value)
		assert "exit 1" in msg
		for needle in ("sk-secret", "OPENAI_API_KEY=", "boom", "CANARY_STDOUT_SECRET"):
			assert needle not in msg
			assert needle not in repr(logged)
		assert len(logged) == 1
		assert logged[0][1]["title"] == "optimus ai_fix cli"
		assert "exit=1" in logged[0][1]["message"]

	def test_timeout_kills_process_group(self, fake_cli):
		"""AC-RES-1: TimeoutExpired -> SIGTERM then SIGKILL on the group."""
		fake_cli.proc = _FakeProc(raise_timeout=True, alive=True)
		with patch("optimus.ai_fix.os.killpg") as killpg, \
		     patch("optimus.ai_fix.os.getpgid", return_value=999):
			with pytest.raises(ai_fix.AiFixError, match="didn't respond"):
				self._call()
		sigs = [c.args[1] for c in killpg.call_args_list]
		assert signal.SIGTERM in sigs
		assert signal.SIGKILL in sigs
		assert sigs.index(signal.SIGTERM) < sigs.index(signal.SIGKILL)

	def test_timeout_value_comes_from_cli_clamp(self, fake_cli, sub_cfg):
		sub_cfg.ai_request_timeout_seconds = 500
		self._call()
		assert fake_cli.proc.communicate_calls[0]["timeout"] == 90

	def test_reads_output_file_before_stdout(self, fake_cli):
		fake_cli.proc = _FakeProc(stdout="from stdout", write_out="  from file \n")
		assert self._call() == "from file"

	def test_falls_back_to_stdout_when_file_missing(self, fake_cli):
		fake_cli.proc = _FakeProc(stdout="  from stdout \n")
		assert self._call() == "from stdout"

	def test_empty_response_raises(self, fake_cli):
		fake_cli.proc = _FakeProc(stdout="   ")
		with pytest.raises(ai_fix.AiFixError, match="empty response"):
			self._call()

	def test_output_capped(self, fake_cli):
		fake_cli.proc = _FakeProc(write_out="a" * 150_000)
		assert len(self._call()) == 100_000

	def test_child_killed_on_non_timeout_exception(self, fake_cli):
		"""A non-timeout failure after spawn must not leak the child or scratch."""
		fake_cli.proc = _FakeProc(raise_exc=ValueError("boom"), alive=True)
		with patch("optimus.ai_fix.os.killpg") as killpg:
			with pytest.raises(ValueError):
				self._call()
		sigs = [c.args[1] for c in killpg.call_args_list]
		assert signal.SIGTERM in sigs and signal.SIGKILL in sigs
		argv = fake_cli.popen_calls[0]["argv"]
		assert not os.path.exists(argv[argv.index("-C") + 1])

	def test_no_kill_when_child_already_exited(self, fake_cli):
		with patch("optimus.ai_fix.os.killpg") as killpg:
			self._call()
		killpg.assert_not_called()

	def test_lone_surrogate_is_sanitised_before_communicate(self, fake_cli):
		self._call(content="bad \ud800 text")
		sent = fake_cli.proc.communicate_calls[0]["input"]
		sent.encode("utf-8")  # must not raise
		assert "bad" in sent and "text" in sent

	def test_popen_oserror_becomes_aifixerror(self, monkeypatch, sub_cfg):
		fake = _FakeSubprocess(popen_exc=OSError("/secret/path/codex: bad interpreter"))
		monkeypatch.setattr(ai_fix, "subprocess", fake)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		logged = []
		monkeypatch.setattr(ai_fix, "_log_cli_error", lambda *a: logged.append(a))
		with pytest.raises(ai_fix.AiFixError) as ei:
			self._call()
		assert str(ei.value) == "Codex CLI could not be started."
		assert logged == [("codex", "spawn", None)]

	def test_missing_cli_raises(self, fake_cli, monkeypatch):
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: None)
		with pytest.raises(ai_fix.AiFixError, match="not found"):
			self._call()
		assert fake_cli.popen_calls == []

	def test_signed_out_raises_and_never_execs(self, monkeypatch, sub_cfg):
		fake = _FakeSubprocess(login_stderr="Not logged in", login_rc=1)
		monkeypatch.setattr(ai_fix, "subprocess", fake)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		with pytest.raises(ai_fix.AiFixError, match="isn't signed in"):
			self._call()
		assert fake.popen_calls == []


# ---------------------------------------------------------------------------
# Dispatch through the three entrypoints
# ---------------------------------------------------------------------------

def _boom(*a, **kw):
	raise AssertionError("an HTTP caller was hit in subscription mode")


@pytest.fixture
def sub_dispatch(sub_cfg, monkeypatch):
	"""Subscription mode with _call_local_cli stubbed and HTTP callers armed."""
	monkeypatch.setattr(ai_fix, "_call_openai_chat", _boom)
	monkeypatch.setattr(ai_fix, "_call_anthropic", _boom)
	holder = {"text": "FIXTEXT", "calls": []}

	def fake_cli(provider, system, messages, **kw):
		holder["calls"].append((provider, system, messages, kw))
		return holder["text"]

	monkeypatch.setattr(ai_fix, "_call_local_cli", fake_cli)
	return holder


_FINDING = {"finding_type": "N+1 Query", "severity": "High", "title": "Repeated lookup"}


class TestDispatch:
	def test_resolve_provider_subscription(self, sub_cfg):
		p = ai_fix._resolve_provider()
		assert p["protocol"] == "subscription"
		assert p["name"] == "Codex CLI"
		assert p["model"] == "codex"
		assert p["base_url"] and p["needs_key"] is False
		assert "Subscription (local CLI)" not in ai_fix._PROVIDER_DEFAULTS

	def test_api_key_mode_unchanged(self, monkeypatch):
		monkeypatch.setattr(
			"optimus.settings.get_config", lambda: _cfg(ai_connection_type="API Key", ai_provider="Anthropic"),
		)
		assert ai_fix._resolve_provider()["protocol"] == "anthropic"

	def test_all_entrypoints_use_cli_not_http(self, sub_dispatch):
		"""AC-DISP-1."""
		fix = ai_fix.suggest_fix(_FINDING)
		idx = ai_fix.suggest_index({"table": "tabX", "doctype": "X"})
		steps = ai_fix.humanize_steps([{"label": "Save Sales Order", "cmd": "frappe.client.save"}])
		assert "FIXTEXT" in fix["suggestion"]
		assert "FIXTEXT" in idx["suggestion"]
		assert steps == "FIXTEXT"
		assert len(sub_dispatch["calls"]) == 3

	def test_result_shape_has_no_tokens(self, sub_dispatch):
		"""AC-RESULT-1."""
		fix = ai_fix.suggest_fix(_FINDING)
		assert fix["provider"] == "Codex CLI"
		assert fix["model"] == "codex"
		assert "tokens" not in fix
		idx = ai_fix.suggest_index({"table": "tabX"})
		assert "tokens" not in idx

	def test_raw_sql_guardrail_applies(self, sub_dispatch):
		"""AC-GUARDRAIL-1: same post-processing as the HTTP path."""
		sub_dispatch["text"] = '**Fix**\n```python\nrows = frappe.db.sql("SELECT name FROM `tabUser`")\n```'
		fix = ai_fix.suggest_fix(_FINDING)
		assert "Profiler note" in fix["suggestion"]
		assert "frappe.db.sql" in fix["suggestion"]

	def test_dispatch_routes_http_protocols(self, monkeypatch):
		"""Both HTTP protocols still reach their handlers through _dispatch_chat."""
		seen = {}
		monkeypatch.setattr(ai_fix, "_call_anthropic", lambda *a, **kw: seen.setdefault("a", (a, kw)) and "A")
		monkeypatch.setattr(ai_fix, "_call_openai_chat", lambda *a, **kw: seen.setdefault("o", (a, kw)) and "O")
		base = {"base_url": "http://x", "model": "m", "api_key": "k"}
		assert ai_fix._dispatch_chat({**base, "protocol": "anthropic"}, "s", []) == "A"
		assert ai_fix._dispatch_chat({**base, "protocol": "openai"}, "s", [], metadata={"m": 1}) == "O"
		assert seen["o"][1]["metadata"] == {"m": 1}
		assert "metadata" not in seen["a"][1]

	def test_connection_uses_status_not_exec(self, sub_cfg, monkeypatch):
		"""test_connection must not run codex exec (it would bill the subscription)."""
		monkeypatch.setattr(ai_fix, "_dispatch_chat", _boom)
		monkeypatch.setattr(ai_fix, "_cli_session_status", lambda: {
			"ok": True, "state": "connected", "message": "Connected.", "cli": "codex", "version": None,
		})
		assert ai_fix.test_connection() == {"ok": True, "message": "Connected.", "model": "codex"}

	def test_connection_api_mode_probes_via_dispatch(self, monkeypatch):
		monkeypatch.setattr(
			"optimus.settings.get_config", lambda: _cfg(ai_connection_type="API Key", ai_provider="Anthropic"),
		)
		monkeypatch.setattr(ai_fix, "_resolve_provider", lambda: {
			"name": "Anthropic", "protocol": "anthropic", "base_url": "http://x",
			"model": "m", "needs_key": False, "api_key": "",
		})
		seen = {}

		def fake_dispatch(provider, system, messages, **kw):
			seen.update(kw)
			return "OK"

		monkeypatch.setattr(ai_fix, "_dispatch_chat", fake_dispatch)
		out = ai_fix.test_connection()
		assert out["ok"] is True
		assert seen["max_tokens"] == 16


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

class TestAvailability:
	def test_available_when_guard_on_and_cli_present(self, sub_cfg, monkeypatch):
		"""AC-AVAIL-1."""
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		assert ai_fix.is_available() is True

	@pytest.mark.parametrize("section", ["findings", "indexes", "humanize"])
	def test_sections_disabled_in_subscription_mode(self, sub_cfg, monkeypatch, section):
		"""AC-AVAIL-1: auto / analyze-time passes always carry a section."""
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		assert ai_fix.is_available(section=section) is False

	def test_on_demand_index_allowed_in_subscription_mode(self, sub_cfg, monkeypatch):
		"""The per-table Suggest index button is click-driven, so it stays usable."""
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		assert ai_fix.is_available(section="indexes", on_demand=True) is True
		assert ai_fix.is_available(section="indexes") is False

	def test_on_demand_honours_section_toggle_guard_and_cli(self, sub_cfg, monkeypatch):
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		sub_cfg.ai_suggest_indexes = False
		assert ai_fix.is_available(section="indexes", on_demand=True) is False
		sub_cfg.ai_suggest_indexes = True
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: False)
		assert ai_fix.is_available(section="indexes", on_demand=True) is False
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: None)
		assert ai_fix.is_available(section="indexes", on_demand=True) is False

	def test_unavailable_when_cli_missing(self, sub_cfg, monkeypatch):
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: None)
		assert ai_fix.is_available() is False

	def test_unavailable_when_guard_off(self, sub_cfg, monkeypatch):
		"""AC-GUARD-1."""
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: False)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		assert ai_fix.is_available() is False

	def test_no_subprocess_on_availability_path(self, sub_cfg, monkeypatch):
		"""AC-AVAIL-2: any subprocess use would raise."""
		trap = MagicMock()
		trap.run.side_effect = AssertionError("subprocess.run used")
		trap.Popen.side_effect = AssertionError("subprocess.Popen used")
		monkeypatch.setattr(ai_fix, "subprocess", trap)
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		assert ai_fix.is_available() is True
		assert ai_fix.is_available(section="findings") is False
		trap.run.assert_not_called()
		trap.Popen.assert_not_called()

	def test_configured_path_must_be_executable_file(self, tmp_path):
		exe = tmp_path / "codex"
		exe.write_text("#!/bin/sh\n")
		cfg = _cfg(ai_cli_path=str(exe))
		assert ai_fix._cli_installed(cfg) is None  # not executable yet
		exe.chmod(0o755)
		assert ai_fix._cli_installed(cfg) == str(exe)
		assert ai_fix._cli_installed(_cfg(ai_cli_path=str(tmp_path / "nope"))) is None


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------

class TestSubscriptionGuard:
	def _with_conf(self, monkeypatch, conf):
		import frappe
		monkeypatch.setattr(frappe, "conf", conf, raising=False)

	def test_requires_both_flags(self, monkeypatch):
		"""AC-GUARD-1."""
		self._with_conf(monkeypatch, {"developer_mode": 1, "optimus_allow_subscription_cli": 1})
		assert ai_fix._subscription_allowed() is True
		self._with_conf(monkeypatch, {"developer_mode": 1})
		assert ai_fix._subscription_allowed() is False
		self._with_conf(monkeypatch, {"optimus_allow_subscription_cli": 1})
		assert ai_fix._subscription_allowed() is False
		self._with_conf(monkeypatch, {"developer_mode": 0, "optimus_allow_subscription_cli": 1})
		assert ai_fix._subscription_allowed() is False

	def test_fails_closed_when_conf_unreadable(self, monkeypatch):
		import frappe
		monkeypatch.delattr(frappe, "conf", raising=False)
		assert ai_fix._subscription_allowed() is False


# ---------------------------------------------------------------------------
# Session status (Connect button, library half)
# ---------------------------------------------------------------------------

class TestSessionStatus:
	def test_blocked_when_guard_off(self, sub_cfg, monkeypatch):
		"""AC-CONNECT-1."""
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: False)
		st = ai_fix._cli_session_status()
		assert st["state"] == "blocked" and st["ok"] is False
		assert "developer_mode" in st["message"]
		assert "optimus_allow_subscription_cli" in st["message"]

	def test_missing_when_cli_not_found(self, sub_cfg, monkeypatch):
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: None)
		st = ai_fix._cli_session_status()
		assert st["state"] == "missing" and st["ok"] is False

	def test_signed_out_when_login_fails(self, sub_cfg, monkeypatch):
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		monkeypatch.setattr(ai_fix, "subprocess", _FakeSubprocess(login_stderr="Not logged in", login_rc=1))
		st = ai_fix._cli_session_status()
		assert st["state"] == "signed_out" and st["ok"] is False
		assert "codex login" in st["message"]

	def test_connected_when_login_ok(self, sub_cfg, monkeypatch):
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		fake = _FakeSubprocess()
		monkeypatch.setattr(ai_fix, "subprocess", fake)
		st = ai_fix._cli_session_status()
		assert st["state"] == "connected" and st["ok"] is True
		assert st["cli"] == "codex"
		assert st["version"] == "codex-cli 1.2.3"
		assert fake.popen_calls == []  # status never runs an exec

	def test_stderr_only_login_text_counts_as_connected(self, sub_cfg, monkeypatch):
		"""The real CLI prints the status to stderr with an empty stdout."""
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		fake = _FakeSubprocess(login_stdout="", login_stderr="Logged in using ChatGPT")
		monkeypatch.setattr(ai_fix, "subprocess", fake)
		assert ai_fix._cli_login_ok("/usr/bin/codex") is True
		assert ai_fix._cli_session_status()["state"] == "connected"
		login_call = [c for c in fake.run_calls if "login" in c["argv"]][0]
		assert login_call["kw"]["timeout"] == 5

	def test_api_key_login_is_rejected(self, sub_cfg, monkeypatch):
		"""Only the ChatGPT subscription is wanted, never a metered API key."""
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		fake = _FakeSubprocess(login_stderr="Logged in using an API key", login_rc=0)
		monkeypatch.setattr(ai_fix, "subprocess", fake)
		assert ai_fix._cli_login_ok("/usr/bin/codex") is False
		assert ai_fix._cli_session_status()["state"] == "signed_out"

	def test_not_logged_in_nonzero_exit(self, monkeypatch):
		monkeypatch.setattr(ai_fix, "subprocess", _FakeSubprocess(login_stderr="Not logged in", login_rc=1))
		assert ai_fix._cli_login_ok("/usr/bin/codex") is False

	def test_chatgpt_text_with_nonzero_exit_is_rejected(self, monkeypatch):
		monkeypatch.setattr(ai_fix, "subprocess", _FakeSubprocess(login_stderr="ChatGPT session expired", login_rc=1))
		assert ai_fix._cli_login_ok("/usr/bin/codex") is False

	def test_login_probe_swallows_errors(self, monkeypatch):
		boom = MagicMock()
		boom.run.side_effect = OSError("nope")
		monkeypatch.setattr(ai_fix, "subprocess", boom)
		assert ai_fix._cli_login_ok("/usr/bin/codex") is False


class TestKillProcessGroup:
	def test_sigkill_sent_even_if_leader_already_reaped(self):
		"""The group id is the child's pid, captured once; no getpgid lookup that
		could fail after the leader is reaped."""
		proc = MagicMock()
		proc.pid = 777
		proc.wait.side_effect = [None, None]
		with patch("optimus.ai_fix.os.killpg") as killpg, \
		     patch("optimus.ai_fix.os.getpgid", side_effect=ProcessLookupError):
			ai_fix._kill_process_group(proc)
		assert [c.args for c in killpg.call_args_list] == [(777, signal.SIGTERM), (777, signal.SIGKILL)]

	def test_sigkill_attempted_when_sigterm_raises(self):
		proc = MagicMock()
		proc.pid = 778
		calls = []

		def killpg(pgid, sig):
			calls.append(sig)
			if sig == signal.SIGTERM:
				raise ProcessLookupError

		with patch("optimus.ai_fix.os.killpg", side_effect=killpg):
			ai_fix._kill_process_group(proc)
		assert calls == [signal.SIGTERM, signal.SIGKILL]


class TestCliTimeout:
	@pytest.mark.parametrize("raw,expected", [(5, 10), (60, 60), (999, 90), (None, 45)])
	def test_clamped(self, monkeypatch, raw, expected):
		monkeypatch.setattr("optimus.settings.get_config", lambda: _cfg(ai_request_timeout_seconds=raw))
		assert ai_fix._resolve_cli_timeout_seconds() == expected

	def test_unreadable_settings_default(self, monkeypatch):
		def boom():
			raise RuntimeError("no bench")
		monkeypatch.setattr("optimus.settings.get_config", boom)
		assert ai_fix._resolve_cli_timeout_seconds() == 45
