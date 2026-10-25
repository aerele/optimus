# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the Codex CLI subscription transport in ``ai_fix``.

Every subprocess call is MOCKED: nothing here ever runs a real ``codex``
command (the real CLI bills a subscription and sends data). ``subprocess``,
``shutil.which`` and ``os.killpg`` are patched on the ``ai_fix`` module, and
``settings.get_config`` returns a plain SimpleNamespace.
"""

from __future__ import annotations

import json
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


@pytest.fixture(autouse=True)
def _clear_login_cache():
	"""A memoized login success must not leak between tests."""
	ai_fix._LOGIN_CACHE.clear()
	yield
	ai_fix._LOGIN_CACHE.clear()


@pytest.fixture
def sub_cfg(monkeypatch):
	"""Subscription mode on and the dev-bench guard open; get_config returns the
	cfg. Tests that need the guard closed re-patch ``_subscription_allowed``."""
	cfg = _cfg()
	monkeypatch.setattr("optimus.settings.get_config", lambda: cfg)
	monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
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

	def test_refuses_to_run_when_guard_is_off(self, monkeypatch, sub_cfg):
		"""Defense in depth: a direct caller that bypasses is_available still
		cannot run codex on a non-dev site."""
		fake = _FakeSubprocess()
		monkeypatch.setattr(ai_fix, "subprocess", fake)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: False)
		with pytest.raises(ai_fix.AiFixError, match="developer-mode"):
			self._call()
		assert not fake.popen_calls
		assert not fake.run_calls

	def test_mkdtemp_oserror_becomes_aifixerror(self, monkeypatch, fake_cli):
		def boom(*a, **k):
			raise OSError("disk full")

		monkeypatch.setattr(ai_fix.tempfile, "mkdtemp", boom)
		with pytest.raises(ai_fix.AiFixError, match="working directory"):
			self._call()
		assert not fake_cli.popen_calls

	def test_prompt_goes_to_stdin_never_argv(self, fake_cli):
		"""AC-SEC-1 + AC-RES-2: metacharacters ride in communicate(input=...);
		argv stays the fixed list."""
		self._call(content=_INJECTION)
		call = fake_cli.popen_calls[0]
		assert not any("rm -rf" in a or "`" in a or "$(" in a for a in call["argv"])
		sent = fake_cli.proc.communicate_calls[0]["input"]
		assert _INJECTION in sent
		body = ai_fix._compose_cli_prompt("SYS", [{"role": "user", "content": _INJECTION}])
		assert sent == f"{ai_fix._CLI_SECURITY_PREAMBLE}\n\n{body}"
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
		assert argv[argv.index("--sandbox") + 1] == "read-only"

	def test_scratch_dir_removed_afterwards(self, fake_cli):
		self._call()
		argv = fake_cli.popen_calls[0]["argv"]
		assert not os.path.exists(argv[argv.index("-C") + 1])

	def test_env_is_an_allowlist(self, fake_cli, monkeypatch):
		"""AC-SEC-3: bench secrets and API-key vars never reach the child; TLS and
		proxy variables do (codex needs them to reach OpenAI)."""
		monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
		monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
		monkeypatch.setenv("OPENAI_BASE_URL", "http://evil")
		monkeypatch.setenv("CODEX_API_KEY", "sk-codex")
		monkeypatch.setenv("DATABASE_URL", "mysql://root:pw@db/x")
		monkeypatch.setenv("REDIS_CACHE", "redis://r:6379")
		monkeypatch.setenv("PATH", "/usr/bin:/bin")
		monkeypatch.setenv("HOME", "/home/dev")
		monkeypatch.setenv("LC_ALL", "C.UTF-8")
		monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
		monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/ca.pem")
		self._call()
		env = fake_cli.popen_calls[0]["kw"]["env"]
		for name in (
			"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY",
			"DATABASE_URL", "REDIS_CACHE",
		):
			assert name not in env
		assert env["PATH"] == "/usr/bin:/bin"
		assert env["HOME"] == "/home/dev"
		assert env["LC_ALL"] == "C.UTF-8"
		assert env["HTTPS_PROXY"] == "http://proxy:3128"
		assert env["SSL_CERT_FILE"] == "/etc/ssl/ca.pem"

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
		with patch("optimus.ai_fix.os.killpg") as killpg:
			with pytest.raises(ai_fix.AiFixError, match="didn't respond"):
				self._call()
		assert killpg.call_args_list
		assert all(c.args[0] == fake_cli.proc.pid for c in killpg.call_args_list)
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
		fake_cli.proc = _FakeProc(write_out="x" * 150_000)
		assert ai_fix._CLI_MAX_OUTPUT_CHARS == 20_000
		assert len(self._call()) == 20_000

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
		with pytest.raises(ai_fix.AiFixError, match="not signed in with ChatGPT") as ei:
			self._call()
		assert ei.value.retryable is False
		assert fake.popen_calls == []

	@pytest.mark.parametrize("login_stderr,login_rc,expected", [
		("Logged in using an API key", 0, "signed in with an API key, not ChatGPT"),
		("Not logged in", 1, "not signed in with ChatGPT"),
	])
	def test_login_failures_are_terminal_with_actionable_message(
		self, monkeypatch, sub_cfg, login_stderr, login_rc, expected,
	):
		fake = _FakeSubprocess(login_stderr=login_stderr, login_rc=login_rc)
		monkeypatch.setattr(ai_fix, "subprocess", fake)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		with pytest.raises(ai_fix.AiFixError) as ei:
			self._call()
		assert expected in str(ei.value)
		assert "Sign in with ChatGPT" in str(ei.value)
		assert ei.value.retryable is False

	def test_login_probe_timeout_is_terminal(self, monkeypatch, sub_cfg):
		fake = _FakeSubprocess()

		def slow(argv, **kw):
			raise subprocess.TimeoutExpired(cmd="codex", timeout=5)

		fake.run = slow
		monkeypatch.setattr(ai_fix, "subprocess", fake)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		with pytest.raises(ai_fix.AiFixError, match="session check timed out") as ei:
			self._call()
		assert ei.value.retryable is False
		assert fake.popen_calls == []

	def test_login_probe_error_is_terminal(self, monkeypatch, sub_cfg):
		fake = _FakeSubprocess()

		def broken(argv, **kw):
			raise OSError("nope")

		fake.run = broken
		monkeypatch.setattr(ai_fix, "subprocess", fake)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		with pytest.raises(ai_fix.AiFixError) as ei:
			self._call()
		assert ei.value.retryable is False
		assert fake.popen_calls == []

	def test_guard_off_and_missing_cli_are_terminal(self, monkeypatch, sub_cfg, fake_cli):
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: None)
		with pytest.raises(ai_fix.AiFixError) as ei:
			self._call()
		assert ei.value.retryable is False
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: False)
		with pytest.raises(ai_fix.AiFixError) as ei:
			self._call()
		assert ei.value.retryable is False

	def test_login_check_is_memoized_across_calls(self, fake_cli):
		self._call()
		self._call()
		assert len([c for c in fake_cli.run_calls if "login" in c["argv"]]) == 1

	def test_security_preamble_leads_the_stdin_prompt(self, fake_cli):
		self._call()
		sent = fake_cli.proc.communicate_calls[0]["input"]
		assert sent.startswith("SECURITY:")
		assert "UNTRUSTED DATA" in sent
		assert "Do NOT run any shell commands" in sent
		assert sent.index("UNTRUSTED DATA") < sent.index("SYS")

	@pytest.mark.parametrize("system,content", [("", ""), ("  ", " \n "), ("", "   ")])
	def test_empty_prompt_never_spawns(self, fake_cli, system, content):
		with pytest.raises(ai_fix.AiFixError, match="no content to send"):
			ai_fix._call_local_cli({"protocol": "subscription"}, system, [{"role": "user", "content": content}])
		assert fake_cli.popen_calls == []

	def test_output_is_secret_scrubbed(self, fake_cli):
		reply = (
			"**Fix**\n```python\nfrappe.get_all('User')\n```\n"
			'encryption_key = "abcd1234abcd1234abcd1234abcd1234"\n'
			"-----BEGIN RSA PRIVATE KEY-----\nMIIEvQIBADANBg\nkqhkiG9w0BAQ\n-----END RSA PRIVATE KEY-----\n"
		)
		fake_cli.proc = _FakeProc(write_out=reply)
		out = self._call()
		assert "abcd1234abcd1234abcd1234abcd1234" not in out
		assert "MIIEvQIBADANBg" not in out
		assert "BEGIN RSA PRIVATE KEY" not in out
		assert 'encryption_key = "[redacted]"' in out
		assert "frappe.get_all('User')" in out
		assert "**Fix**" in out

	def test_pipes_are_closed(self, fake_cli):
		fake_cli.proc.stdin = MagicMock()
		fake_cli.proc.stdout = MagicMock()
		fake_cli.proc.stderr = MagicMock()
		self._call()
		fake_cli.proc.stdin.close.assert_called_once()
		fake_cli.proc.stdout.close.assert_called_once()
		fake_cli.proc.stderr.close.assert_called_once()

	def test_pipes_closed_even_when_pipe_close_raises(self, fake_cli):
		fake_cli.proc.stdin = MagicMock()
		fake_cli.proc.stdin.close.side_effect = OSError("broken pipe")
		fake_cli.proc.stdout = MagicMock()
		self._call()
		fake_cli.proc.stdout.close.assert_called_once()

	def test_pipes_closed_after_kill_on_timeout(self, fake_cli):
		fake_cli.proc = _FakeProc(raise_timeout=True, alive=True)
		fake_cli.proc.stdout = MagicMock()
		with patch("optimus.ai_fix.os.killpg"):
			with pytest.raises(ai_fix.AiFixError):
				self._call()
		fake_cli.proc.stdout.close.assert_called_once()

	@pytest.mark.parametrize("stderr,message,retryable", [
		("HTTP 401 Unauthorized RAWCANARY", "Codex session expired. Run `codex login`.", False),
		("token expired RAWCANARY", "Codex session expired. Run `codex login`.", False),
		("You hit your usage limit RAWCANARY", "Codex usage limit reached. Try again later.", False),
		("error 429 RAWCANARY", "Codex usage limit reached. Try again later.", False),
		("error: unexpected argument '--foo' RAWCANARY", "Codex rejected a command-line flag. Update the codex CLI.", False),
		("dns lookup failed RAWCANARY", "Codex could not reach the network.", True),
		("some other crash RAWCANARY", "Codex CLI could not produce a suggestion (exit 3).", True),
	])
	def test_nonzero_exit_stderr_is_classified_without_leaking(
		self, fake_cli, monkeypatch, stderr, message, retryable,
	):
		fake_cli.proc = _FakeProc(returncode=3, stdout="STDOUTCANARY", stderr=stderr)
		logged = []
		monkeypatch.setattr(
			"frappe.log_error", lambda *a, **kw: logged.append((a, kw)), raising=False,
		)
		with pytest.raises(ai_fix.AiFixError) as ei:
			self._call()
		assert str(ei.value) == message
		assert ei.value.retryable is retryable
		assert len(logged) == 1
		assert "exit=3" in logged[0][1]["message"]
		assert "label=" in logged[0][1]["message"]
		assert "version=codex-cli 1.2.3" in logged[0][1]["message"]
		for needle in ("RAWCANARY", "STDOUTCANARY"):
			assert needle not in str(ei.value)
			assert needle not in repr(logged)


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

	@pytest.mark.parametrize("section,attr", [
		("findings", "ai_suggest_findings"),
		("indexes", "ai_suggest_indexes"),
		("humanize", "ai_humanize_steps"),
	])
	def test_sections_follow_their_toggle(self, sub_cfg, monkeypatch, section, attr):
		"""Subscription mode behaves like the API path: a section is available
		when its toggle is on and unavailable when it is off."""
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		assert ai_fix.is_available(section=section) is True
		setattr(sub_cfg, attr, False)
		assert ai_fix.is_available(section=section) is False
		assert ai_fix.is_available() is True

	@pytest.mark.parametrize("section", [None, "findings", "indexes", "humanize"])
	def test_sections_need_guard_and_cli(self, sub_cfg, monkeypatch, section):
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: False)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		assert ai_fix.is_available(section=section) is False
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: None)
		assert ai_fix.is_available(section=section) is False

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
		assert ai_fix.is_available(section="findings") is True
		trap.run.assert_not_called()
		trap.Popen.assert_not_called()

	def test_configured_path_must_be_executable_file(self, tmp_path):
		exe = tmp_path / "codex"
		exe.write_text("#!/bin/sh\n")
		cfg = _cfg(ai_cli_path=str(exe))
		assert ai_fix._cli_installed(cfg) is None  # not executable yet
		exe.chmod(0o755)
		assert ai_fix._cli_installed(cfg) == os.path.realpath(str(exe))
		assert ai_fix._cli_installed(_cfg(ai_cli_path=str(tmp_path / "nope"))) is None

	def test_configured_path_basename_must_be_codex(self, tmp_path):
		"""A SysMgr cannot point the path at an arbitrary executable."""
		other = tmp_path / "sh"
		other.write_text("#!/bin/sh\n")
		other.chmod(0o755)
		assert ai_fix._cli_installed(_cfg(ai_cli_path=str(other))) is None
		assert ai_fix._cli_installed(_cfg(ai_cli_path="/bin/sh")) is None

	def test_relative_path_is_rejected(self, tmp_path, monkeypatch):
		"""A relative path would pass the file checks from the bench cwd and then
		fail at spawn (the child runs in a scratch cwd), so it never counts."""
		exe = tmp_path / "codex"
		exe.write_text("#!/bin/sh\n")
		exe.chmod(0o755)
		monkeypatch.chdir(tmp_path)
		assert ai_fix._cli_installed(_cfg(ai_cli_path="codex")) is None
		assert ai_fix._cli_installed(_cfg(ai_cli_path="./codex")) is None

	def test_symlink_to_non_codex_is_rejected(self, tmp_path):
		"""The basename check applies to the resolved target, not the link name."""
		link = tmp_path / "codex"
		link.symlink_to("/bin/sh")
		assert ai_fix._cli_installed(_cfg(ai_cli_path=str(link))) is None

	def test_symlink_to_codex_returns_realpath(self, tmp_path):
		real_dir = tmp_path / "real"
		real_dir.mkdir()
		real = real_dir / "codex"
		real.write_text("#!/bin/sh\n")
		real.chmod(0o755)
		link = tmp_path / "codex"
		link.symlink_to(real)
		assert ai_fix._cli_installed(_cfg(ai_cli_path=str(link))) == os.path.realpath(str(real))


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

	def test_string_flags_from_set_config_are_cint_coerced(self, monkeypatch):
		"""`bench set-config` can store "0" / "false" strings: they must read as off."""
		self._with_conf(monkeypatch, {"developer_mode": "0", "optimus_allow_subscription_cli": 1})
		assert ai_fix._subscription_allowed() is False
		self._with_conf(monkeypatch, {"developer_mode": 1, "optimus_allow_subscription_cli": "0"})
		assert ai_fix._subscription_allowed() is False
		self._with_conf(monkeypatch, {"developer_mode": "false", "optimus_allow_subscription_cli": "false"})
		assert ai_fix._subscription_allowed() is False
		self._with_conf(monkeypatch, {"developer_mode": 1, "optimus_allow_subscription_cli": 1})
		assert ai_fix._subscription_allowed() is True
		self._with_conf(monkeypatch, {"developer_mode": "1", "optimus_allow_subscription_cli": "1"})
		assert ai_fix._subscription_allowed() is True

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
		assert st["message"] == (
			"Codex CLI is not signed in with ChatGPT (an API-key login does not count). "
			"Run `codex login` and choose Sign in with ChatGPT."
		)

	def test_connected_when_login_ok(self, sub_cfg, monkeypatch):
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		fake = _FakeSubprocess()
		monkeypatch.setattr(ai_fix, "subprocess", fake)
		st = ai_fix._cli_session_status()
		assert st["state"] == "connected" and st["ok"] is True
		assert st["cli"] == "codex"
		assert st["version"] == "codex-cli 1.2.3"
		assert "codex-cli 1.2.3" in st["message"]
		assert fake.popen_calls == []  # status never runs an exec

	def test_stderr_only_login_text_counts_as_connected(self, sub_cfg, monkeypatch):
		"""The real CLI prints the status to stderr with an empty stdout."""
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		fake = _FakeSubprocess(login_stdout="", login_stderr="Logged in using ChatGPT")
		monkeypatch.setattr(ai_fix, "subprocess", fake)
		assert ai_fix._cli_login_ok("/usr/bin/codex") == (True, "ok")
		assert ai_fix._cli_session_status()["state"] == "connected"
		login_calls = [c for c in fake.run_calls if "login" in c["argv"]]
		assert login_calls
		assert all(c["kw"]["timeout"] == 5 for c in login_calls)

	def test_api_key_login_is_rejected(self, sub_cfg, monkeypatch):
		"""Only the ChatGPT subscription is wanted, never a metered API key."""
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		fake = _FakeSubprocess(login_stderr="Logged in using an API key", login_rc=0)
		monkeypatch.setattr(ai_fix, "subprocess", fake)
		assert ai_fix._cli_login_ok("/usr/bin/codex") == (False, "not_chatgpt")
		st = ai_fix._cli_session_status()
		assert st["state"] == "signed_out" and st["ok"] is False
		assert st["message"] == (
			"Codex CLI is signed in with an API key, not ChatGPT. "
			"Run `codex login` and choose Sign in with ChatGPT."
		)

	def test_timeout_message(self, sub_cfg, monkeypatch):
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		fake = _FakeSubprocess()

		def slow_run(argv, **kw):
			if "login" in argv:
				raise subprocess.TimeoutExpired(cmd="codex", timeout=5)
			return SimpleNamespace(returncode=0, stdout="", stderr="codex-cli 1.2.3")

		fake.run = slow_run
		monkeypatch.setattr(ai_fix, "subprocess", fake)
		st = ai_fix._cli_session_status()
		assert st["ok"] is False
		assert st["message"] == "The Codex CLI session check timed out. Try again."

	@pytest.mark.parametrize("reason", ["not_chatgpt", "signed_out", "timeout", "error"])
	def test_non_connected_reason_is_logged_with_label_and_version(self, sub_cfg, monkeypatch, reason):
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		monkeypatch.setattr(ai_fix, "subprocess", _FakeSubprocess())
		monkeypatch.setattr(ai_fix, "_cli_login_ok", lambda path, **kw: (False, reason))
		logged = []
		monkeypatch.setattr(
			ai_fix, "_log_cli_error", lambda *a, **kw: logged.append((a, kw)),
		)
		ai_fix._cli_session_status()
		assert logged == [(("codex", "login_status", None), {"label": reason, "version": "codex-cli 1.2.3"})]

	def test_connect_button_does_not_use_cached_login(self, sub_cfg, monkeypatch):
		"""Connect must reflect the live session, so it bypasses the 30s memo."""
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "/usr/bin/codex")
		ai_fix._LOGIN_CACHE["/usr/bin/codex"] = (ai_fix.time.monotonic(), True, "ok")
		monkeypatch.setattr(ai_fix, "subprocess", _FakeSubprocess(login_stderr="Not logged in", login_rc=1))
		assert ai_fix._cli_session_status()["ok"] is False

	def test_not_logged_in_nonzero_exit(self, monkeypatch):
		monkeypatch.setattr(ai_fix, "subprocess", _FakeSubprocess(login_stderr="Not logged in", login_rc=1))
		assert ai_fix._cli_login_ok("/usr/bin/codex") == (False, "signed_out")

	def test_chatgpt_text_with_nonzero_exit_is_rejected(self, monkeypatch):
		monkeypatch.setattr(ai_fix, "subprocess", _FakeSubprocess(login_stderr="ChatGPT session expired", login_rc=1))
		assert ai_fix._cli_login_ok("/usr/bin/codex") == (False, "signed_out")

	def test_login_probe_swallows_errors(self, monkeypatch):
		boom = MagicMock()
		boom.TimeoutExpired = subprocess.TimeoutExpired
		boom.run.side_effect = OSError("nope")
		monkeypatch.setattr(ai_fix, "subprocess", boom)
		assert ai_fix._cli_login_ok("/usr/bin/codex") == (False, "error")

	def test_login_probe_timeout(self, monkeypatch):
		boom = MagicMock()
		boom.TimeoutExpired = subprocess.TimeoutExpired
		boom.run.side_effect = subprocess.TimeoutExpired(cmd="codex", timeout=5)
		monkeypatch.setattr(ai_fix, "subprocess", boom)
		assert ai_fix._cli_login_ok("/usr/bin/codex") == (False, "timeout")
		assert boom.run.call_args.kwargs["timeout"] == 5


class TestLoginMemo:
	def test_success_is_memoized_per_path(self, monkeypatch):
		fake = _FakeSubprocess()
		monkeypatch.setattr(ai_fix, "subprocess", fake)
		assert ai_fix._cli_login_ok("/a/codex") == (True, "ok")
		assert ai_fix._cli_login_ok("/a/codex") == (True, "ok")
		assert len(fake.run_calls) == 1
		assert ai_fix._cli_login_ok("/b/codex") == (True, "ok")
		assert len(fake.run_calls) == 2

	def test_memo_expires(self, monkeypatch):
		fake = _FakeSubprocess()
		monkeypatch.setattr(ai_fix, "subprocess", fake)
		ai_fix._cli_login_ok("/a/codex")
		ts, ok, reason = ai_fix._LOGIN_CACHE["/a/codex"]
		ai_fix._LOGIN_CACHE["/a/codex"] = (ts - 31, ok, reason)
		ai_fix._cli_login_ok("/a/codex")
		assert len(fake.run_calls) == 2

	def test_failure_is_never_memoized(self, monkeypatch):
		fake = _FakeSubprocess(login_stderr="Not logged in", login_rc=1)
		monkeypatch.setattr(ai_fix, "subprocess", fake)
		assert ai_fix._cli_login_ok("/a/codex") == (False, "signed_out")
		assert "/a/codex" not in ai_fix._LOGIN_CACHE
		# The operator logs in and retries immediately: no stale failure.
		fake._login_rc = 0
		fake._login_stderr = "Logged in using ChatGPT"
		assert ai_fix._cli_login_ok("/a/codex") == (True, "ok")


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
		pgids = []

		def killpg(pgid, sig):
			pgids.append(pgid)
			calls.append(sig)
			if sig == signal.SIGTERM:
				raise ProcessLookupError

		with patch("optimus.ai_fix.os.killpg", side_effect=killpg):
			ai_fix._kill_process_group(proc)
		assert calls == [signal.SIGTERM, signal.SIGKILL]
		assert pgids == [778, 778]


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


class TestRetryableFlag:
	def test_default_is_retryable(self):
		assert ai_fix.AiFixError("x").retryable is True
		assert ai_fix.AiFixError("x", status_code=429).retryable is True

	def test_can_be_marked_terminal(self):
		assert ai_fix.AiFixError("x", retryable=False).retryable is False

	def test_timeout_and_empty_response_stay_retryable(self, fake_cli):
		def call():
			return ai_fix._call_local_cli({"protocol": "subscription"}, "SYS", [{"role": "user", "content": "hi"}])

		fake_cli.proc = _FakeProc(raise_timeout=True, alive=True)
		with patch("optimus.ai_fix.os.killpg"):
			with pytest.raises(ai_fix.AiFixError) as ei:
				call()
		assert ei.value.retryable is True
		fake_cli.proc = _FakeProc(stdout="   ")
		with pytest.raises(ai_fix.AiFixError) as ei:
			call()
		assert ei.value.retryable is True


class TestSubscriptionConstant:
	def test_constant_is_a_doctype_option(self):
		"""The Python constant and the Select option must never drift apart."""
		from optimus.settings import SUBSCRIPTION_CONNECTION

		path = os.path.join(
			os.path.dirname(ai_fix.__file__), "optimus", "doctype", "optimus_settings",
			"optimus_settings.json",
		)
		with open(path, encoding="utf-8") as f:
			fields = json.load(f)["fields"]
		field = next(fd for fd in fields if fd["fieldname"] == "ai_connection_type")
		assert SUBSCRIPTION_CONNECTION in field["options"].split("\n")

	def test_ai_fix_uses_the_same_constant(self):
		from optimus import settings

		assert ai_fix.SUBSCRIPTION_CONNECTION is settings.SUBSCRIPTION_CONNECTION

	def test_subscription_mode_active_follows_constant(self, monkeypatch):
		from optimus.settings import SUBSCRIPTION_CONNECTION

		monkeypatch.setattr(
			"optimus.settings.get_config", lambda: _cfg(ai_connection_type=SUBSCRIPTION_CONNECTION),
		)
		assert ai_fix.subscription_mode_active() is True
		monkeypatch.setattr("optimus.settings.get_config", lambda: _cfg(ai_connection_type="API Key"))
		assert ai_fix.subscription_mode_active() is False


class TestCliEnv:
	def test_allowlist_keeps_only_safe_keys(self):
		env = ai_fix._build_cli_env({
			"PATH": "/bin", "HOME": "/h", "CODEX_HOME": "/c", "LANG": "C", "TMPDIR": "/t",
			"SHELL": "/bin/sh", "USER": "dev", "TERM": "xterm", "LC_CTYPE": "C", "LC_ALL": "C",
			"SSL_CERT_FILE": "/f", "SSL_CERT_DIR": "/d", "REQUESTS_CA_BUNDLE": "/b",
			"HTTPS_PROXY": "p", "HTTP_PROXY": "p", "NO_PROXY": "n",
			"ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k", "CODEX_API_KEY": "k",
			"DATABASE_URL": "u", "REDIS_QUEUE": "r", "FRAPPE_SITE": "s",
		})
		assert set(env) == {
			"PATH", "HOME", "CODEX_HOME", "LANG", "TMPDIR", "SHELL", "USER", "TERM", "LC_CTYPE",
			"LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "HTTPS_PROXY",
			"HTTP_PROXY", "NO_PROXY",
		}

	def test_absent_passthrough_keys_are_not_invented(self):
		assert ai_fix._build_cli_env({"PATH": "/bin"}) == {"PATH": "/bin"}

	def test_denylist_is_gone(self):
		assert not hasattr(ai_fix, "_CLI_ENV_DENYLIST")


class TestClassifyStderr:
	@pytest.mark.parametrize("stderr,label", [
		("401 Unauthorized", "auth_expired"),
		("please log in again", "auth_expired"),
		("session expired", "auth_expired"),
		("Usage limit reached", "usage_limit"),
		("rate limit exceeded", "usage_limit"),
		("quota exhausted", "usage_limit"),
		("HTTP 429", "usage_limit"),
		("error: unexpected argument '--x' found", "unknown_flag"),
		("unrecognized option", "unknown_flag"),
		("unknown option --y", "unknown_flag"),
		("network is unreachable", "network"),
		("connection reset", "network"),
		("TLS handshake failed", "network"),
		("dns error", "network"),
		("segfault", "other"),
		("", "other"),
		(None, "other"),
	])
	def test_labels(self, stderr, label):
		assert ai_fix._classify_cli_stderr(stderr) == label

	def test_catalog_interface_is_not_a_login_error(self):
		assert ai_fix._classify_cli_stderr("catalog interface failed") == "other"

	def test_log_cli_error_carries_only_fixed_fields(self, monkeypatch):
		logged = []
		monkeypatch.setattr("frappe.log_error", lambda *a, **kw: logged.append(kw), raising=False)
		ai_fix._log_cli_error("codex", "exec", 2, label="network", version="codex-cli 1.2.3")
		assert logged == [{
			"message": "AI fix CLI call failed: cli=codex at=exec exit=2 label=network version=codex-cli 1.2.3",
			"title": "optimus ai_fix cli",
		}]


class TestScrubSecrets:
	def test_quoted_assignments_are_redacted(self):
		src = (
			'encryption_key = "abcd1234abcd1234abcd1234abcd1234"\n'
			"db_password: 'hunter2hunter2'\n"
			'{"api_key": "sk-live-1234567890", "token": "tok_ABC123"}\n'
			"private_key='abc12345'\n"
		)
		out = ai_fix._scrub_secrets(src)
		for leaked in ("abcd1234abcd1234", "hunter2", "sk-live", "tok_ABC123", "abc12345"):
			assert leaked not in out
		assert out.count("[redacted]") == 5
		assert 'encryption_key = "[redacted]"' in out
		assert '"api_key": "[redacted]"' in out

	def test_bare_assignments_with_digits_are_redacted(self):
		out = ai_fix._scrub_secrets("password=hunter2hunter2\nsecret: s3cr3tvalue99")
		assert "hunter2" not in out and "s3cr3tvalue99" not in out

	def test_pem_block_is_redacted_including_truncated(self):
		full = "x\n-----BEGIN PRIVATE KEY-----\nAAAA\nBBBB\n-----END PRIVATE KEY-----\ny"
		out = ai_fix._scrub_secrets(full)
		assert "AAAA" not in out and out.startswith("x\n[redacted]") and out.endswith("y")
		cut = ai_fix._scrub_secrets("keep\n-----BEGIN PRIVATE KEY-----\nAAAA\nBBBB")
		assert "AAAA" not in cut and cut.startswith("keep")

	def test_long_hex_and_random_tokens_are_redacted(self):
		hexs = "a" * 8 + "0123456789" * 3 + "ef"
		tok = "Zm9vYmFyQmF6" * 4 + "1"
		out = ai_fix._scrub_secrets(f"hash {hexs} and tok {tok} done")
		assert hexs not in out and tok not in out
		assert out == "hash [redacted] and tok [redacted] done"

	def test_ordinary_code_and_prose_are_untouched(self):
		src = (
			"**Diagnosis**: line 14 `frappe.get_doc(...)` runs once per item.\n"
			"```diff\n-for d in self.items:\n+uoms = frappe.get_all('Item', limit=50)\n```\n"
			"max_tokens = 2000\npassword = frappe.conf.db_password\ntoken = get_token()\n"
			"password=password\nif password == 'x':\n    pass\n"
			"def get_all_sales_orders_with_customer_details_and_items_for_user():\n"
			"    return 'a_very_long_snake_case_value_that_is_not_a_secret_at_all_really'\n"
			"path /home/user/frappe-bench/apps/erpnext/erpnext/selling/doctype/Sales2Order/x\n"
			"**Verify**: query count dropped from 31 to 1.\n"
		)
		assert ai_fix._scrub_secrets(src) == src

	def test_empty_and_idempotent(self):
		assert ai_fix._scrub_secrets("") == ""
		once = ai_fix._scrub_secrets('secret = "abcdefgh"')
		assert ai_fix._scrub_secrets(once) == once

	def test_linear_time_on_pathological_input(self):
		# The key-value regexes once had an unbounded `[\w.\-]*` prefix that
		# backtracked quadratically: a 20k-char reply (a prompt-injected or
		# minified blob) stalled the web worker for ~20s. The prefix is bounded,
		# so even at the output cap the scrub stays well under a second.
		import time

		for src in ("a" * 20000, "token" * 4000, "x.y-" * 5000, "a_" * 10000):
			started = time.monotonic()
			ai_fix._scrub_secrets(src)
			assert time.monotonic() - started < 1.0
