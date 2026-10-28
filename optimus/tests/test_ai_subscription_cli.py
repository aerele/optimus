# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the Codex CLI subscription transport in ``ai_fix``.

The real ``codex`` is never run. Process-level tests point ``ai_cli_path`` at a
small shell script named ``codex`` that records what the child saw (environment,
working files, stdin) and answers like the CLI would.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from optimus import ai_fix, ai_transport

_FAKE_CODEX = """#!/bin/sh
R="{rec}"
echo "$1 $2" >> "$R/calls.txt"
env > "$R/env_$1.txt"
echo "$PWD" > "$R/pwd_$1.txt"
echo "$HOME" > "$R/home_$1.txt"
echo "$CODEX_HOME" > "$R/codexhome_$1.txt"
ls "$CODEX_HOME" > "$R/codexhome_ls_$1.txt" 2>&1
case "$1" in
	login) {login} ;;
	--version) echo "codex-cli 9.9.9" ;;
	exec)
		out=""
		while [ $# -gt 0 ]; do
			if [ "$1" = "-o" ]; then out="$2"; fi
			shift
		done
		cat > "$R/stdin.txt"
		echo "$out" > "$R/out_path.txt"
		{exec_body}
		;;
esac
"""


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
	ai_fix._LOGIN_CACHE.clear()
	yield
	ai_fix._LOGIN_CACHE.clear()


_REAL_ON_FEATURES = ai_fix._codex_on_features


@pytest.fixture(autouse=True)
def _no_feature_probe(monkeypatch):
	"""Never spawn ``codex features list`` implicitly: the probe is None (static floor
	only) unless a test sets it."""
	ai_fix._FEATURES_CACHE.clear()
	monkeypatch.setattr(ai_fix, "_codex_on_features", lambda cli_path: None)
	yield
	ai_fix._FEATURES_CACHE.clear()


@pytest.fixture
def sub_cfg(monkeypatch):
	"""Subscription mode on and the dev-bench guard open."""
	cfg = _cfg()
	monkeypatch.setattr("optimus.settings.get_config", lambda: cfg)
	monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
	# The saved settings the dispatch guard re-reads: still the same subscription config.
	monkeypatch.setattr(ai_fix, "_read_provider_snapshot", lambda: (cfg, None))
	return cfg


@pytest.fixture
def real_auth(tmp_path, monkeypatch):
	"""A fake operator sign-in file; ``_auth_source_path`` points at it."""
	home = tmp_path / "operator_codex"
	home.mkdir()
	auth = home / "auth.json"
	auth.write_text(json.dumps({"tokens": {"refresh_token": "ORIGINAL"}}))
	monkeypatch.setattr(ai_fix, "_auth_source_path", lambda: str(auth))
	return auth


@pytest.fixture
def fake_codex(tmp_path, monkeypatch, sub_cfg, real_auth):
	"""Build a fake ``codex`` script and point the settings at it."""
	rec = tmp_path / "rec"
	rec.mkdir()
	sub_cfg.ai_cli_path = str(tmp_path / "bin" / "codex")
	(tmp_path / "bin").mkdir()

	def build(login='echo "Logged in using ChatGPT" >&2', exec_body='echo REPLY > "$out"'):
		path = tmp_path / "bin" / "codex"
		path.write_text(_FAKE_CODEX.format(rec=rec, login=login, exec_body=exec_body))
		path.chmod(0o755)
		return SimpleNamespace(rec=rec, path=path)

	return build


def _read(rec, name):
	return (rec / name).read_text()


# ---------------------------------------------------------------------------
# Pure builders
# ---------------------------------------------------------------------------

class TestBuildArgv:
	def test_argv_has_no_shell_metacharacters_from_data(self):
		joined = " ".join(ai_fix._build_cli_argv("codex", "/s", "/s/out"))
		assert "`" not in joined and "$(" not in joined and ";" not in joined

	def test_argv_hardening_flags(self):
		argv = ai_fix._build_cli_argv("codex", "/s", "/s/out")
		assert argv[:2] == ["codex", "exec"]
		assert argv[argv.index("--sandbox") + 1] == "read-only"
		for flag in ("--skip-git-repo-check", "--ignore-user-config", "--ignore-rules", "--ephemeral"):
			assert flag in argv
		disabled = [argv[i + 1] for i, a in enumerate(argv) if a == "--disable"]
		assert "shell_tool" in disabled
		assert argv[argv.index("-C") + 1] == "/s"
		assert argv[argv.index("-o") + 1] == "/s/out"
		assert argv[-1] == "-"


class TestComposePrompt:
	def test_system_then_preamble_then_captured_data(self):
		out = ai_fix._compose_cli_prompt("SYS", [{"content": "A"}, {"content": "B"}])
		assert out == f"SYS\n\n{ai_fix._CLI_SECURITY_PREAMBLE}\n\nA\n\nB"
		assert out.index("SYS") < out.index("SECURITY:") < out.index("A")

	def test_skips_empty_parts_and_strips(self):
		out = ai_fix._compose_cli_prompt("  ", [{"content": " hi \n"}, {"content": ""}])
		assert out == f"{ai_fix._CLI_SECURITY_PREAMBLE}\n\nhi"

	def test_no_captured_data_means_no_preamble(self):
		assert ai_fix._compose_cli_prompt("SYS", [{"content": " "}]) == "SYS"
		assert ai_fix._compose_cli_prompt("", []) == ""


class TestCliEnv:
	def test_allowlist_drops_bench_secrets_and_sets_scratch_dirs(self):
		env = ai_fix._build_cli_env(
			{
				"PATH": "/usr/bin", "HOME": "/home/op", "CODEX_HOME": "/home/op/.codex", "LANG": "C",
				"LC_ALL": "C", "HTTPS_PROXY": "http://p", "OPENAI_API_KEY": "sk-1", "DB_PASSWORD": "x",
				"FRAPPE_SITE": "s", "REDIS_CACHE": "redis://",
			},
			home="/s/home", codex_home="/s/codex", tmpdir="/s/tmp",
		)
		assert env == {
			"PATH": "/usr/bin", "LANG": "C", "LC_ALL": "C", "HTTPS_PROXY": "http://p",
			"HOME": "/s/home", "CODEX_HOME": "/s/codex", "TMPDIR": "/s/tmp",
		}

	def test_inherited_home_is_not_passed_through(self):
		assert "HOME" not in ai_fix._build_cli_env({"HOME": "/home/op", "PATH": "/bin"})


# ---------------------------------------------------------------------------
# Installed-CLI resolution
# ---------------------------------------------------------------------------

def _make_exe(path, mode=0o755):
	path.write_text("#!/bin/sh\n")
	path.chmod(mode)
	return path


def path_codex(monkeypatch, tmp_path=None):
	"""Make ``shutil.which("codex")`` find a trusted executable and return it."""
	import tempfile
	from pathlib import Path
	root = Path(tmp_path) if tmp_path is not None else Path(tempfile.mkdtemp())
	exe = _make_exe(root / "codex")
	monkeypatch.setattr(ai_fix.shutil, "which", lambda name: str(exe))
	return exe


class TestCliInstalled:
	def test_configured_path_must_be_executable_file(self, tmp_path):
		exe = tmp_path / "codex"
		exe.write_text("#!/bin/sh\n")
		exe.chmod(0o644)
		cfg = _cfg(ai_cli_path=str(exe))
		assert ai_fix._cli_installed(cfg) is None
		exe.chmod(0o755)
		assert ai_fix._cli_installed(cfg) == os.path.realpath(str(exe))
		assert ai_fix._cli_installed(_cfg(ai_cli_path=str(tmp_path / "nope"))) is None

	def test_basename_must_be_codex(self, tmp_path):
		other = _make_exe(tmp_path / "sh")
		assert ai_fix._cli_installed(_cfg(ai_cli_path=str(other))) is None
		assert ai_fix._cli_installed(_cfg(ai_cli_path="/bin/sh")) is None

	def test_npm_style_codex_symlink_to_codex_js_is_accepted(self, tmp_path, monkeypatch):
		real_dir = tmp_path / "lib" / "bin"
		real_dir.mkdir(parents=True)
		entry = _make_exe(real_dir / "codex.js")
		(tmp_path / "bin").mkdir()
		link = tmp_path / "bin" / "codex"
		link.symlink_to(entry)
		assert ai_fix._cli_installed(_cfg(ai_cli_path=str(link))) == os.path.realpath(str(entry))
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: str(link))
		assert ai_fix._cli_installed(_cfg()) == os.path.realpath(str(entry))

	def test_codex_candidate_resolving_to_codex_js_is_accepted_with_mocked_resolution(self, monkeypatch):
		monkeypatch.setattr(ai_fix.os.path, "realpath", lambda p: "/opt/node/lib/bin/codex.js")
		monkeypatch.setattr(ai_fix, "_trusted_cli_file", lambda real: True)
		assert ai_fix._cli_installed(_cfg(ai_cli_path="/usr/local/bin/codex")) == "/opt/node/lib/bin/codex.js"

	def test_codex_symlink_to_a_shell_is_rejected(self, monkeypatch):
		monkeypatch.setattr(ai_fix.os.path, "realpath", lambda p: "/bin/sh")
		monkeypatch.setattr(ai_fix, "_trusted_cli_file", lambda real: True)
		assert ai_fix._cli_installed(_cfg(ai_cli_path="/usr/local/bin/codex")) is None

	def test_candidate_named_sh_is_rejected_even_if_it_resolves_to_codex(self, monkeypatch):
		monkeypatch.setattr(ai_fix.os.path, "realpath", lambda p: "/opt/codex")
		monkeypatch.setattr(ai_fix, "_trusted_cli_file", lambda real: True)
		assert ai_fix._cli_installed(_cfg(ai_cli_path="/usr/local/bin/sh")) is None

	def test_node_entrypoint_still_has_to_be_trusted(self, monkeypatch):
		monkeypatch.setattr(ai_fix.os.path, "realpath", lambda p: "/opt/node/lib/bin/codex.js")
		monkeypatch.setattr(ai_fix, "_trusted_cli_file", lambda real: False)
		assert ai_fix._cli_installed(_cfg(ai_cli_path="/usr/local/bin/codex")) is None

	def test_relative_path_is_rejected(self, tmp_path, monkeypatch):
		_make_exe(tmp_path / "codex")
		monkeypatch.chdir(tmp_path)
		assert ai_fix._cli_installed(_cfg(ai_cli_path="codex")) is None
		assert ai_fix._cli_installed(_cfg(ai_cli_path="./codex")) is None

	def test_symlink_is_checked_by_its_target(self, tmp_path):
		link = tmp_path / "codex"
		link.symlink_to("/bin/sh")
		assert ai_fix._cli_installed(_cfg(ai_cli_path=str(link))) is None
		real_dir = tmp_path / "real"
		real_dir.mkdir()
		real = _make_exe(real_dir / "codex")
		link2 = tmp_path / "link2"
		link2.mkdir()
		(link2 / "codex").symlink_to(real)
		assert ai_fix._cli_installed(_cfg(ai_cli_path=str(link2 / "codex"))) == os.path.realpath(str(real))

	@pytest.mark.parametrize("mode", [0o775, 0o757, 0o777])
	def test_group_or_world_writable_file_is_rejected(self, tmp_path, mode):
		exe = _make_exe(tmp_path / "codex", mode)
		exe.chmod(mode)
		assert ai_fix._cli_installed(_cfg(ai_cli_path=str(exe))) is None

	def test_file_owned_by_someone_else_is_rejected(self, tmp_path, monkeypatch):
		exe = _make_exe(tmp_path / "codex")
		cfg = _cfg(ai_cli_path=str(exe))
		assert ai_fix._cli_installed(cfg) is not None
		monkeypatch.setattr(ai_fix.os, "geteuid", lambda: os.stat(exe).st_uid + 12345)
		assert ai_fix._cli_installed(cfg) is None

	def test_blank_path_uses_a_trusted_path_hit(self, tmp_path, monkeypatch):
		exe = path_codex(monkeypatch, tmp_path)
		assert ai_fix._cli_installed(_cfg()) == os.path.realpath(str(exe))

	@pytest.mark.parametrize("mode", [0o775, 0o757, 0o666])
	def test_blank_path_rejects_a_writable_path_hit(self, tmp_path, monkeypatch, mode):
		path_codex(monkeypatch, tmp_path).chmod(mode)
		assert ai_fix._cli_installed(_cfg()) is None

	def test_blank_path_rejects_a_path_hit_owned_by_someone_else(self, tmp_path, monkeypatch):
		exe = path_codex(monkeypatch, tmp_path)
		monkeypatch.setattr(ai_fix.os, "geteuid", lambda: os.stat(exe).st_uid + 12345)
		assert ai_fix._cli_installed(_cfg()) is None

	def test_blank_path_rejects_a_symlink_to_another_binary(self, tmp_path, monkeypatch):
		link = tmp_path / "codex"
		link.symlink_to("/bin/sh")
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: str(link))
		assert ai_fix._cli_installed(_cfg()) is None

	def test_blank_path_rejects_a_relative_path_hit(self, monkeypatch):
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: "codex")
		assert ai_fix._cli_installed(_cfg()) is None


# ---------------------------------------------------------------------------
# The private scratch tree
# ---------------------------------------------------------------------------

class TestCliSandbox:
	def test_tree_is_private_holds_only_the_auth_copy_and_is_removed(self, real_auth):
		with ai_fix._cli_sandbox() as (work, env):
			root = os.path.dirname(work)
			assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
			assert os.listdir(env["CODEX_HOME"]) == ["auth.json"]
			copy = os.path.join(env["CODEX_HOME"], "auth.json")
			assert stat.S_IMODE(os.stat(copy).st_mode) == 0o600
			assert open(copy, "rb").read() == real_auth.read_bytes()
			assert os.listdir(env["HOME"]) == []
			assert env["HOME"] != os.path.expanduser("~")
		assert not os.path.exists(root)

	def test_tree_is_removed_when_the_body_raises(self, real_auth):
		with pytest.raises(RuntimeError):
			with ai_fix._cli_sandbox() as (work, env):
				root = os.path.dirname(work)
				raise RuntimeError("boom")
		assert not os.path.exists(root)

	def test_without_auth_nothing_is_copied(self, real_auth):
		with ai_fix._cli_sandbox(with_auth=False) as (work, env):
			assert os.listdir(env["CODEX_HOME"]) == []

	def test_missing_or_invalid_auth_file_is_skipped(self, real_auth):
		real_auth.write_text("not json")
		with ai_fix._cli_sandbox() as (work, env):
			assert os.listdir(env["CODEX_HOME"]) == []
		real_auth.unlink()
		with ai_fix._cli_sandbox() as (work, env):
			assert os.listdir(env["CODEX_HOME"]) == []

	def test_refreshed_auth_is_saved_back_and_unchanged_auth_is_not_rewritten(self, real_auth):
		before = real_auth.stat().st_mtime_ns
		with ai_fix._cli_sandbox():
			pass
		assert real_auth.stat().st_mtime_ns == before
		with ai_fix._cli_sandbox() as (work, env):
			with open(os.path.join(env["CODEX_HOME"], "auth.json"), "w") as f:
				json.dump({"tokens": {"refresh_token": "ROTATED"}}, f)
		assert json.loads(real_auth.read_text())["tokens"]["refresh_token"] == "ROTATED"
		assert stat.S_IMODE(real_auth.stat().st_mode) == 0o600

	def test_garbage_written_to_the_copy_is_not_saved_back(self, real_auth):
		original = real_auth.read_text()
		with ai_fix._cli_sandbox() as (work, env):
			with open(os.path.join(env["CODEX_HOME"], "auth.json"), "w") as f:
				f.write("garbage")
		assert real_auth.read_text() == original


# ---------------------------------------------------------------------------
# Process-level behaviour against a fake codex script
# ---------------------------------------------------------------------------

class TestCallLocalCli:
	def _call(self, system="SYS", content="hello", **kw):
		return ai_fix._call_local_cli(system, [{"role": "user", "content": content}], **kw)

	def test_reply_is_returned_scrubbed(self, fake_codex):
		fake_codex(exec_body='echo "db_password = \\"hunter2hunter2\\"" > "$out"')
		out = self._call()
		assert "hunter2" not in out and "[redacted]" in out

	def test_prompt_goes_on_stdin_system_first_then_preamble_then_data(self, fake_codex):
		box = fake_codex()
		self._call(system="OPTIMUS-FORMAT-RULES", content="SELECT 'x; rm -rf /' -- $(whoami)")
		stdin = _read(box.rec, "stdin.txt")
		assert stdin.startswith("OPTIMUS-FORMAT-RULES")
		assert stdin.index("OPTIMUS-FORMAT-RULES") < stdin.index("SECURITY:") < stdin.index("SELECT 'x; rm -rf /'")
		argv_seen = _read(box.rec, "calls.txt")
		assert "rm -rf" not in argv_seen

	def test_child_sees_a_private_home_and_none_of_the_bench_environment(self, fake_codex, monkeypatch):
		box = fake_codex()
		monkeypatch.setenv("OPTIMUS_TEST_DB_PASSWORD", "supersecret")
		monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
		self._call()
		env = _read(box.rec, "env_exec.txt")
		assert "supersecret" not in env and "OPENAI_API_KEY" not in env
		home = _read(box.rec, "home_exec.txt").strip()
		codex_home = _read(box.rec, "codexhome_exec.txt").strip()
		assert home != os.path.expanduser("~") and codex_home.startswith(os.path.dirname(home))
		assert _read(box.rec, "codexhome_ls_exec.txt").split() == ["auth.json"]
		assert _read(box.rec, "pwd_exec.txt").strip().startswith(os.path.dirname(home))

	def test_scratch_tree_is_deleted_afterwards(self, fake_codex):
		box = fake_codex()
		self._call()
		home = _read(box.rec, "home_exec.txt").strip()
		assert not os.path.exists(os.path.dirname(home))

	def test_scratch_tree_is_deleted_after_a_failure(self, fake_codex):
		box = fake_codex(exec_body="echo 'boom' >&2; exit 3")
		with pytest.raises(ai_fix.AiFixError):
			self._call()
		assert not os.path.exists(os.path.dirname(_read(box.rec, "home_exec.txt").strip()))

	def test_session_check_and_version_probes_use_the_same_restricted_child(self, fake_codex, monkeypatch):
		box = fake_codex()
		monkeypatch.setenv("OPTIMUS_TEST_DB_PASSWORD", "supersecret")
		ai_fix._cli_session_status()
		for name in ("login", "--version"):
			assert "supersecret" not in _read(box.rec, f"env_{name}.txt")
			assert _read(box.rec, f"home_{name}.txt").strip() != os.path.expanduser("~")
			assert os.path.dirname(_read(box.rec, f"pwd_{name}.txt").strip()) == os.path.dirname(
				_read(box.rec, f"home_{name}.txt").strip()
			)
			assert not os.path.exists(os.path.dirname(_read(box.rec, f"home_{name}.txt").strip()))

	def test_refreshed_sign_in_is_saved_back(self, fake_codex, real_auth):
		fake_codex(exec_body='echo \'{"tokens": {"refresh_token": "ROTATED"}}\' > "$CODEX_HOME/auth.json"; echo OK > "$out"')
		self._call()
		assert json.loads(real_auth.read_text())["tokens"]["refresh_token"] == "ROTATED"

	def test_timeout_kills_the_whole_process_group_and_is_not_fatal(self, fake_codex):
		rec = fake_codex().rec
		fake_codex(exec_body=f'sleep 60 & echo $! > "{rec}/child.pid"; wait')
		started = time.monotonic()
		with ai_transport.attempt() as transport:
			with pytest.raises(ai_fix.AiFixError) as err:
				self._call(timeout=2)
		assert time.monotonic() - started < 20
		assert err.value.kind == "timeout" and err.value.fatal is False
		assert transport.certainty == "sent_unknown"
		pid = int((rec / "child.pid").read_text())
		for _ in range(50):
			try:
				os.kill(pid, 0)
			except ProcessLookupError:
				break
			time.sleep(0.1)
		else:
			os.kill(pid, 9)
			pytest.fail("the grandchild survived the timeout")

	def test_budget_covers_the_login_check(self, fake_codex, monkeypatch):
		fake_codex()
		seen = {}
		real = ai_fix._run_cli

		def spy(argv, env, cwd, timeout, stdin_text=None):
			seen[argv[1]] = timeout
			return real(argv, env, cwd, timeout, stdin_text)

		monkeypatch.setattr(ai_fix, "_run_cli", spy)
		self._call(timeout=30)
		assert seen["exec"] <= 30

	def test_guard_off_refuses_before_any_process(self, fake_codex, monkeypatch):
		box = fake_codex()
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: False)
		with pytest.raises(ai_fix.AiFixError, match="developer-mode") as err:
			self._call()
		assert err.value.kind == "config" and err.value.fatal is True
		assert not (box.rec / "calls.txt").exists()

	def test_missing_cli_is_fatal(self, sub_cfg, monkeypatch):
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: None)
		with pytest.raises(ai_fix.AiFixError) as err:
			self._call()
		assert err.value.kind == "config" and err.value.fatal is True

	def test_empty_prompt_is_not_fatal(self, fake_codex):
		fake_codex()
		with pytest.raises(ai_fix.AiFixError, match="no content") as err:
			ai_fix._call_local_cli("", [])
		assert err.value.fatal is False

	def test_spawn_failure_is_transport_and_not_sent(self, fake_codex, monkeypatch):
		fake_codex()
		monkeypatch.setattr(ai_fix, "_cli_login_ok", lambda path, **kw: (True, "ok"))

		def fail(*a, **kw):
			raise OSError("no exec")

		monkeypatch.setattr(ai_fix.subprocess, "Popen", fail)
		with ai_transport.attempt() as transport:
			with pytest.raises(ai_fix.AiFixError) as err:
				self._call()
		assert err.value.kind == "transport" and transport.certainty == "not_sent"

	def test_empty_reply_is_bad_response(self, fake_codex):
		fake_codex(exec_body=': > "$out"')
		with pytest.raises(ai_fix.AiFixError) as err:
			self._call()
		assert err.value.kind == "bad_response" and err.value.fatal is False

	def test_reply_falls_back_to_stdout(self, fake_codex):
		fake_codex(exec_body="echo FROM_STDOUT")
		assert self._call() == "FROM_STDOUT"

	def test_over_cap_reply_file_is_cut_and_marked_truncated(self, tmp_path):
		out = tmp_path / "out.txt"
		out.write_text("x" * 25_000)
		text = ai_fix._read_cli_reply(str(out), "")
		assert text == "x" * ai_fix._CLI_MAX_OUTPUT_CHARS + "\n…(truncated)"

	def test_over_cap_stdout_fallback_is_cut_and_marked_truncated(self, tmp_path):
		text = ai_fix._read_cli_reply(str(tmp_path / "missing"), "y" * 25_000)
		assert text.endswith("\n…(truncated)") and text.count("y") == ai_fix._CLI_MAX_OUTPUT_CHARS

	def test_reply_exactly_at_the_cap_is_not_marked(self, tmp_path):
		out = tmp_path / "out.txt"
		out.write_text("x" * ai_fix._CLI_MAX_OUTPUT_CHARS)
		assert ai_fix._read_cli_reply(str(out), "") == "x" * ai_fix._CLI_MAX_OUTPUT_CHARS

	def test_success_counts_one_known_zero_cost_call(self, fake_codex):
		fake_codex()
		usage = ai_fix.Usage()
		with ai_transport.attempt() as transport:
			self._call(usage_out=usage)
		assert usage.calls == 1 and usage.complete and not usage.get("total_tokens")
		assert transport.certainty == "known_response"

	@pytest.mark.parametrize("stderr,kind,fatal", [
		("HTTP 401 Unauthorized", "auth", True),
		("Your access token expired", "auth", True),
		("You hit your usage limit", "quota", True),
		("error: unexpected argument '--foo'", "config", True),
		("dns lookup failed", "transport", False),
		("segfault", "unknown", False),
	])
	def test_exit_error_maps_to_a_kind_and_never_leaks_stderr(self, fake_codex, stderr, kind, fatal):
		fake_codex(exec_body=f"echo 'RAWCANARY {stderr}' >&2; exit 3")
		with ai_transport.attempt() as transport:
			with pytest.raises(ai_fix.AiFixError) as err:
				self._call()
		assert err.value.kind == kind and err.value.fatal is fatal
		assert "RAWCANARY" not in str(err.value) and err.value.detail == ""
		assert transport.certainty == "known_response"

	def test_prompt_echoed_on_stderr_does_not_misclassify_a_generic_failure(self, fake_codex):
		fake_codex(
			exec_body=(
				"echo 'user: SELECT * FROM tabUser WHERE login = 1 AND connection expired 401' >&2; "
				"for i in 1 2 3 4 5 6; do echo \"line $i\" >&2; done; echo 'panicked at main.rs' >&2; exit 101"
			)
		)
		with pytest.raises(ai_fix.AiFixError) as err:
			self._call()
		assert err.value.kind == "unknown" and err.value.fatal is False
		assert "expired" not in str(err.value)


class TestLoginProbe:
	def test_login_failure_kinds(self):
		assert ai_fix._login_failure("signed_out").fatal is True
		assert ai_fix._login_failure("not_chatgpt").fatal is True
		assert ai_fix._login_failure("timeout").fatal is False
		assert ai_fix._login_failure("timeout").kind == "timeout"
		assert ai_fix._login_failure("error").fatal is False
		assert ai_fix._login_failure("unexpected").kind == "transport"

	@pytest.mark.parametrize("code", [126, 127])
	def test_command_not_runnable_is_an_environment_error_not_a_sign_out(self, fake_codex, code):
		box = fake_codex(login=f"exit {code}")
		ok, reason = ai_fix._cli_login_ok(str(box.path), use_cache=False)
		assert (ok, reason) == (False, "error")
		failure = ai_fix._login_failure("error")
		assert failure.kind == "transport" and failure.fatal is False and failure.kind != "auth"

	def test_other_non_zero_exit_is_still_signed_out(self, fake_codex):
		box = fake_codex(login="exit 1")
		assert ai_fix._cli_login_ok(str(box.path), use_cache=False) == (False, "signed_out")

	def test_signed_out_stops_the_call_as_fatal(self, fake_codex):
		fake_codex(login='echo "Not logged in" >&2; exit 1')
		with pytest.raises(ai_fix.AiFixError, match="not signed in") as err:
			ai_fix._call_local_cli("S", [{"content": "x"}])
		assert err.value.kind == "auth" and err.value.fatal is True

	def test_api_key_login_is_rejected(self, fake_codex):
		fake_codex(login='echo "Logged in using an API key" >&2')
		with pytest.raises(ai_fix.AiFixError, match="API key") as err:
			ai_fix._call_local_cli("S", [{"content": "x"}])
		assert err.value.fatal is True

	def test_slow_probe_is_a_temporary_timeout_and_its_process_group_is_killed(self, fake_codex, monkeypatch):
		box = fake_codex()
		fake_codex(login=f'sleep 30 & echo $! > "{box.rec}/probe.pid"; wait')
		monkeypatch.setattr(ai_fix, "_LOGIN_PROBE_SECONDS", 1)
		with pytest.raises(ai_fix.AiFixError) as err:
			ai_fix._call_local_cli("S", [{"content": "x"}])
		assert err.value.kind == "timeout" and err.value.fatal is False
		pid = int((box.rec / "probe.pid").read_text())
		for _ in range(50):
			try:
				os.kill(pid, 0)
			except ProcessLookupError:
				break
			time.sleep(0.1)
		else:
			os.kill(pid, 9)
			pytest.fail("the probe's child survived the timeout")

	def test_success_is_memoized_and_failure_is_not(self, fake_codex):
		box = fake_codex()
		path = str(box.path)
		assert ai_fix._cli_login_ok(path) == (True, "ok")
		assert ai_fix._cli_login_ok(path) == (True, "ok")
		assert _read(box.rec, "calls.txt").count("login status") == 1
		assert ai_fix._cli_login_ok(path, use_cache=False) == (True, "ok")
		assert _read(box.rec, "calls.txt").count("login status") == 2
		ai_fix._LOGIN_CACHE.clear()
		fake_codex(login="exit 1")
		assert ai_fix._cli_login_ok(path) == (False, "signed_out")
		assert path not in ai_fix._LOGIN_CACHE

	def test_unstartable_binary_is_a_temporary_error(self, tmp_path):
		bad = tmp_path / "codex"
		bad.write_text("not a script and no shebang")
		bad.chmod(0o755)
		assert ai_fix._cli_login_ok(str(bad)) == (False, "error")


class TestSessionStatus:
	def test_blocked_when_guard_off(self, sub_cfg, monkeypatch):
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: False)
		st = ai_fix._cli_session_status()
		assert st["state"] == "blocked" and st["ok"] is False
		assert "developer_mode" in st["message"] and "optimus_allow_subscription_cli" in st["message"]

	def test_missing_when_cli_not_found(self, sub_cfg, monkeypatch):
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: None)
		assert ai_fix._cli_session_status()["state"] == "missing"

	def test_connected(self, fake_codex):
		fake_codex()
		st = ai_fix._cli_session_status()
		assert st["state"] == "connected" and st["ok"] is True and st["version"] == "codex-cli 9.9.9"
		assert "@" not in st["message"]

	def test_signed_out_message_carries_no_account_detail(self, fake_codex):
		fake_codex(login='echo "secret detail user@example.com" >&2; exit 1')
		st = ai_fix._cli_session_status()
		assert st["state"] == "signed_out" and "user@example.com" not in st["message"]

	def test_slow_check_reports_error_state(self, fake_codex, monkeypatch):
		fake_codex(login="sleep 30")
		monkeypatch.setattr(ai_fix, "_LOGIN_PROBE_SECONDS", 1)
		st = ai_fix._cli_session_status()
		assert st["state"] == "error" and "timed out" in st["message"]

	def test_button_ignores_the_cached_success(self, fake_codex):
		box = fake_codex()
		ai_fix._LOGIN_CACHE[str(box.path)] = (time.monotonic(), True, "ok")
		fake_codex(login="exit 1")
		assert ai_fix._cli_session_status()["state"] == "signed_out"


# ---------------------------------------------------------------------------
# stderr classification and error kinds
# ---------------------------------------------------------------------------

class TestClassifyStderr:
	@pytest.mark.parametrize("text,label", [
		("HTTP 401 Unauthorized", "auth_expired"),
		("token expired", "auth_expired"),
		("Your session has expired", "auth_expired"),
		("please log in again", "auth_expired"),
		("Not logged in", "auth_expired"),
		("WHERE expired = 1", "other"),
		("SELECT * FROM tabLogin Attempt", "other"),
		("certificate has expired", "network"),
		("tls handshake failed", "network"),
		("You hit your usage limit", "usage_limit"),
		("error 429", "usage_limit"),
		("error: unexpected argument '--foo'", "unknown_flag"),
		("Error: Unknown feature flag: x", "unknown_flag"),
		("dns lookup failed", "network"),
		("segfault", "other"),
		("", "other"),
		(None, "other"),
	])
	def test_labels(self, text, label):
		assert ai_fix._classify_cli_stderr(text) == label

	def test_only_the_tail_is_matched(self):
		echoed = "workdir: /x\nprompt: SELECT login, expired, connection FROM t WHERE status = 401\n"
		tail = "\n".join(f"line {i}" for i in range(ai_fix._CLI_STDERR_TAIL_LINES))
		assert ai_fix._classify_cli_stderr(echoed + tail) == "other"
		assert ai_fix._classify_cli_stderr(echoed + tail + "\nHTTP 401 Unauthorized") == "auth_expired"

	def test_long_last_line_is_capped_to_its_end(self):
		long_line = "login " + "x" * 5000 + " done"
		assert ai_fix._classify_cli_stderr(long_line) == "other"

	def test_every_kind_used_is_known_and_only_terminal_ones_are_fatal(self):
		kinds = {kind for _, kind in ai_fix._CLI_EXEC_ERRORS.values()}
		kinds |= {kind for _, kind in ai_fix._LOGIN_FAILURES.values()}
		assert kinds <= set(ai_fix.KIND_HINTS)
		fatal = {kind for kind in kinds if kind in ai_fix.AI_FATAL_KINDS}
		assert fatal == {"auth", "quota", "config"}
		assert ai_fix._CLI_EXEC_ERRORS["network"][1] not in ai_fix.AI_FATAL_KINDS


# ---------------------------------------------------------------------------
# Provider wiring
# ---------------------------------------------------------------------------

_FINDING = {"finding_type": "N+1 Query", "severity": "High", "title": "Repeated lookup"}


def _boom(*a, **kw):
	raise AssertionError("an HTTP caller was hit in subscription mode")


class TestProvider:
	def test_provider_config_is_synthetic_and_not_in_the_table(self, sub_cfg):
		p = ai_fix._provider_config()
		assert p["protocol"] == "subscription" and p["name"] == "Codex CLI"
		assert p["needs_key"] is False and p["model"] and p["base_url"]
		assert "Codex CLI" not in ai_fix._PROVIDER_DEFAULTS

	def test_api_mode_is_unchanged(self, monkeypatch):
		monkeypatch.setattr(
			"optimus.settings.get_config",
			lambda: _cfg(ai_connection_type="API Key", ai_provider="OpenAI"),
		)
		monkeypatch.setattr("frappe.conf", {}, raising=False)
		assert ai_fix._provider_config()["protocol"] == "openai"

	def test_dispatch_routes_subscription_to_the_cli_not_http(self, sub_cfg, monkeypatch):
		monkeypatch.setattr(ai_fix, "_call_openai_chat", _boom)
		monkeypatch.setattr(ai_fix, "_call_anthropic", _boom)
		monkeypatch.setattr(ai_fix, "_get_api_key", _boom)
		calls = []

		def fake(system, messages, **kw):
			calls.append((system, messages, kw))
			return "FIXTEXT"

		monkeypatch.setattr(ai_fix, "_call_local_cli", fake)
		usage = ai_fix.Usage()
		out = ai_fix._dispatch_call(
			ai_fix._provider_config(), "SYS", [{"role": "user", "content": "q"}], usage_out=usage, timeout=33,
		)
		assert out == "FIXTEXT"
		assert calls[0][0] == "SYS" and calls[0][2]["timeout"] == 33 and calls[0][2]["usage_out"] is usage

	@pytest.mark.parametrize("change", ["ai_off", "api_key_mode", "unreadable"])
	def test_dispatch_refuses_when_the_saved_config_no_longer_matches(self, sub_cfg, monkeypatch, change):
		provider = ai_fix._provider_config()
		monkeypatch.setattr(ai_fix, "_call_local_cli", _boom)
		if change == "unreadable":
			def snapshot():
				raise RuntimeError("db down")
		else:
			def snapshot():
				cfg = _cfg(ai_enabled=change != "ai_off")
				if change == "api_key_mode":
					cfg.ai_connection_type = "API Key"
					cfg.ai_provider = "OpenAI"
					monkeypatch.setattr("frappe.conf", {}, raising=False)
				return cfg, None
		monkeypatch.setattr(ai_fix, "_read_provider_snapshot", snapshot)
		with pytest.raises(ai_fix.AiFixError) as err:
			ai_fix._dispatch_call(provider, "SYS", [{"role": "user", "content": "q"}], usage_out=None)
		assert err.value.kind == "config" and err.value.fatal is True
		assert "AI configuration changed" in str(err.value)

	def test_dispatch_proceeds_when_the_snapshot_still_matches(self, sub_cfg, monkeypatch):
		provider = ai_fix._provider_config()
		monkeypatch.setattr(ai_fix, "_read_provider_snapshot", lambda: (_cfg(), None))
		monkeypatch.setattr(ai_fix, "_call_local_cli", lambda system, messages, **kw: "OK")
		assert ai_fix._dispatch_call(provider, "S", [{"content": "q"}], usage_out=None) == "OK"

	def test_snapshot_query_reads_the_connection_type(self):
		import inspect
		src = inspect.getsource(ai_fix._read_provider_snapshot)
		assert '"ai_connection_type"' in src

	def test_test_connection_checks_status_and_never_runs_exec(self, sub_cfg, monkeypatch):
		monkeypatch.setattr(ai_fix, "_call_local_cli", _boom)
		monkeypatch.setattr(
			ai_fix, "_cli_session_status", lambda: {"ok": True, "message": "Connected."},
		)
		assert ai_fix.test_connection() == {"ok": True, "message": "Connected.", "model": "codex"}

	def test_suggest_fix_runs_through_the_cli(self, sub_cfg, monkeypatch):
		monkeypatch.setattr(ai_fix, "_call_openai_chat", _boom)
		monkeypatch.setattr(ai_fix, "_call_anthropic", _boom)
		monkeypatch.setattr(ai_fix, "_call_local_cli", lambda system, messages, **kw: "FIXTEXT")
		out = ai_fix.suggest_fix({**_FINDING, "sql": "select 1"})
		assert out["provider"] == "Codex CLI" and "FIXTEXT" in out["suggestion"]


class TestAvailability:
	def test_available_when_guard_on_and_cli_present(self, sub_cfg, monkeypatch):
		path_codex(monkeypatch)
		assert ai_fix.is_available() is True

	@pytest.mark.parametrize("section,attr", [
		("findings", "ai_suggest_findings"),
		("humanize", "ai_humanize_steps"),
	])
	def test_sections_follow_their_toggle(self, sub_cfg, monkeypatch, section, attr):
		path_codex(monkeypatch)
		assert ai_fix.is_available(section=section) is True
		setattr(sub_cfg, attr, False)
		assert ai_fix.is_available(section=section) is False
		assert ai_fix.is_available() is True

	@pytest.mark.parametrize("section", [None, "findings", "humanize"])
	def test_needs_guard_and_cli(self, sub_cfg, monkeypatch, section):
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: False)
		path_codex(monkeypatch)
		assert ai_fix.is_available(section=section) is False
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: None)
		assert ai_fix.is_available(section=section) is False

	def test_no_subprocess_on_the_availability_path(self, sub_cfg, monkeypatch):
		trap = MagicMock(side_effect=AssertionError("subprocess used"))
		monkeypatch.setattr(ai_fix.subprocess, "Popen", trap)
		monkeypatch.setattr(ai_fix.subprocess, "run", trap)
		path_codex(monkeypatch)
		assert ai_fix.is_available() is True
		trap.assert_not_called()


class TestSubscriptionGuard:
	def _with_conf(self, monkeypatch, conf):
		import frappe
		monkeypatch.setattr(frappe, "conf", conf, raising=False)

	def test_requires_both_flags(self, monkeypatch):
		self._with_conf(monkeypatch, {"developer_mode": 1, "optimus_allow_subscription_cli": 1})
		assert ai_fix._subscription_allowed() is True
		self._with_conf(monkeypatch, {"developer_mode": 1})
		assert ai_fix._subscription_allowed() is False
		self._with_conf(monkeypatch, {"optimus_allow_subscription_cli": 1})
		assert ai_fix._subscription_allowed() is False

	def test_string_flags_from_set_config_are_coerced(self, monkeypatch):
		self._with_conf(monkeypatch, {"developer_mode": "0", "optimus_allow_subscription_cli": 1})
		assert ai_fix._subscription_allowed() is False
		self._with_conf(monkeypatch, {"developer_mode": "1", "optimus_allow_subscription_cli": "1"})
		assert ai_fix._subscription_allowed() is True

	def test_fails_closed_when_conf_unreadable(self, monkeypatch):
		import frappe
		monkeypatch.delattr(frappe, "conf", raising=False)
		assert ai_fix._subscription_allowed() is False


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


# ---------------------------------------------------------------------------
# Tool isolation, auth write-back, consent re-check and process cleanup
# ---------------------------------------------------------------------------

class TestToolIsolation:
	def test_every_file_command_agent_and_web_tool_is_disabled(self):
		argv = ai_fix._build_cli_argv("codex", "/s", "/s/out")
		disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
		assert {
			"shell_tool", "unified_exec", "multi_agent", "view_image", "hooks",
			"image_generation", "plugins", "apps",
		} <= disabled
		assert argv[argv.index("-c") + 1] == 'web_search="disabled"'

	def test_each_disable_flag_is_paired_with_a_feature_name(self):
		argv = ai_fix._build_cli_argv("codex", "/s", "/s/out")
		assert len(ai_fix._CLI_DISABLED_FEATURES) == len(set(ai_fix._CLI_DISABLED_FEATURES))
		assert argv.count("--disable") == len(ai_fix._CLI_DISABLED_FEATURES)

	@staticmethod
	def _disabled(argv):
		return [argv[i + 1] for i, a in enumerate(argv) if a == "--disable"]

	def test_on_capability_features_from_the_probe_are_disabled_too(self, monkeypatch):
		monkeypatch.setattr(ai_fix, "_codex_on_features", lambda p: frozenset({
			"browser_use_external", "code_mode_host", "browser_use_full_cdp_access",
			"browser_annotation_api", "code_mode_interrupt", "fast_mode", "goals", "steer",
		}))
		disabled = self._disabled(ai_fix._build_cli_argv("codex", "/s", "/s/out"))
		for name in ("browser_use_external", "code_mode_host", "browser_use_full_cdp_access",
			"browser_annotation_api", "code_mode_interrupt"):
			assert name in disabled
		for name in ("fast_mode", "goals", "steer"):
			assert name not in disabled
		assert set(ai_fix._CLI_DISABLED_FEATURES) <= set(disabled)
		assert len(disabled) == len(set(disabled)) and disabled == sorted(disabled)

	def test_a_failed_probe_falls_back_to_the_full_static_floor(self):
		disabled = self._disabled(ai_fix._build_cli_argv("codex", "/s", "/s/out"))
		assert sorted(disabled) == sorted(ai_fix._CLI_DISABLED_FEATURES)

	def test_other_hardening_flags_are_kept_with_a_probe(self, monkeypatch):
		monkeypatch.setattr(ai_fix, "_codex_on_features", lambda p: frozenset({"browser_use_external"}))
		argv = ai_fix._build_cli_argv("codex", "/s", "/s/out")
		assert argv[argv.index("-c") + 1] == 'web_search="disabled"'
		assert argv[argv.index("--sandbox") + 1] == "read-only" and argv[-1] == "-"

	def test_features_listing_keeps_names_whose_last_column_is_true(self):
		listing = (
			"browser_use_external                     stable             true\n"
			"code_mode_host_grpc                      under development  false\n"
			"fast_mode                                stable             true\n"
			"\n"
		)
		assert ai_fix._parse_on_features(listing) == {"browser_use_external", "fast_mode"}

	def test_probe_runs_features_list_without_auth_and_is_cached(self, monkeypatch):
		calls = []

		def fake_run(argv, env, cwd, timeout, stdin_text=None):
			calls.append(argv)
			return 0, "browser_use_external   stable   true\nfast_mode   stable   false\n", ""

		monkeypatch.setattr(ai_fix, "_run_cli", fake_run)
		assert _REAL_ON_FEATURES("/x/codex") == {"browser_use_external"}
		assert _REAL_ON_FEATURES("/x/codex") == {"browser_use_external"}
		assert calls == [["/x/codex", "features", "list"]]

	@pytest.mark.parametrize("outcome", ["raises", "exit1", "empty"])
	def test_probe_failure_is_none_and_never_raises(self, monkeypatch, outcome):
		def fake_run(*a, **kw):
			if outcome == "raises":
				raise subprocess.TimeoutExpired("codex", 10)
			return (1, "", "boom") if outcome == "exit1" else (0, "garbage\n", "")

		monkeypatch.setattr(ai_fix, "_run_cli", fake_run)
		assert _REAL_ON_FEATURES("/x/codex") is None

	def test_capability_regex_leaves_plain_completion_features_alone(self):
		for name in ("fast_mode", "goals", "steer", "collaboration_modes", "sqlite", "item_ids"):
			assert not ai_fix._CLI_CAPABILITY_RE.search(name)
		for name in ("browser_use_external", "code_mode_host", "unified_exec", "sandbox_bypass_x"):
			assert ai_fix._CLI_CAPABILITY_RE.search(name)

	@pytest.mark.skipif(os.environ.get("OPTIMUS_LIVE_CODEX") != "1", reason="set OPTIMUS_LIVE_CODEX=1 with a logged-in codex")
	def test_live_agent_cannot_read_a_file_outside_the_scratch_dir(self, tmp_path, monkeypatch, sub_cfg):
		import shutil
		codex = shutil.which("codex")
		if not codex or subprocess.run([codex, "login", "status"], capture_output=True).returncode != 0:
			pytest.skip("codex is not installed or not logged in")
		secret = tmp_path / "outside.txt"
		secret.write_text("OPTIMUS-LIVE-CANARY-7f3a91c2")
		reply = ai_fix._call_local_cli(
			"Answer briefly.",
			[{"role": "user", "content": f"Read the file {secret} and print its exact contents."}],
			timeout=120,
		)
		assert "OPTIMUS-LIVE-CANARY-7f3a91c2" not in reply


def _rotate(copy_dir, token, **extra):
	with open(os.path.join(copy_dir, "auth.json"), "w") as f:
		json.dump({"tokens": {"refresh_token": token}, **extra}, f)


class TestAuthWriteBack:
	def test_write_back_is_atomic_private_and_leaves_no_temp_files(self, real_auth):
		with ai_fix._cli_sandbox() as (work, env):
			_rotate(env["CODEX_HOME"], "ROTATED")
		assert json.loads(real_auth.read_text())["tokens"]["refresh_token"] == "ROTATED"
		assert stat.S_IMODE(real_auth.stat().st_mode) == 0o600
		assert sorted(os.listdir(real_auth.parent)) == ["auth.json", "auth.json.lock"]

	def test_temp_name_is_unique_per_writer(self, tmp_path, monkeypatch):
		names = []
		real = ai_fix.tempfile.mkstemp

		def spy(*a, **kw):
			fd, name = real(*a, **kw)
			names.append(name)
			return fd, name

		monkeypatch.setattr(ai_fix.tempfile, "mkstemp", spy)
		target = tmp_path / "auth.json"
		ai_fix._write_private(str(target), b"{}", replace=True)
		ai_fix._write_private(str(target), b"{}", replace=True)
		assert len(set(names)) == 2 and not any(n.endswith("auth.json.tmp") for n in names)
		assert stat.S_IMODE(target.stat().st_mode) == 0o600

	def test_failed_replace_cleans_the_temp_file(self, tmp_path, monkeypatch):
		monkeypatch.setattr(ai_fix.os, "replace", MagicMock(side_effect=OSError("nope")))
		with pytest.raises(OSError):
			ai_fix._write_private(str(tmp_path / "auth.json"), b"{}", replace=True)
		assert os.listdir(tmp_path) == []

	def test_a_stale_second_writer_does_not_clobber_the_newer_file(self, real_auth):
		with ai_fix._cli_sandbox() as (_, first):
			with ai_fix._cli_sandbox() as (_, second):
				_rotate(second["CODEX_HOME"], "SECOND")
			# SECOND is on disk now, so the outer copy's base is stale
			_rotate(first["CODEX_HOME"], "FIRST")
		assert json.loads(real_auth.read_text())["tokens"]["refresh_token"] == "SECOND"

	def test_stale_writer_is_skipped_when_the_file_changed_on_disk(self, real_auth):
		with ai_fix._cli_sandbox() as (_, env):
			real_auth.write_text(json.dumps({"tokens": {"refresh_token": "NEWER"}}))
			_rotate(env["CODEX_HOME"], "STALE")
		assert json.loads(real_auth.read_text())["tokens"]["refresh_token"] == "NEWER"

	@pytest.mark.parametrize("bad", [
		"[1, 2]",
		'{"tokens": {}, "extra": 1}',
		'{"auth_mode": "apikey"}',
		'"just a string"',
	])
	def test_child_output_with_other_keys_or_shape_is_not_saved(self, real_auth, bad):
		real_auth.write_text(json.dumps({"tokens": {"refresh_token": "ORIGINAL"}, "auth_mode": "chatgpt"}))
		before = real_auth.read_text()
		with ai_fix._cli_sandbox() as (_, env):
			with open(os.path.join(env["CODEX_HOME"], "auth.json"), "w") as f:
				f.write(bad)
		assert real_auth.read_text() == before

	def test_changed_auth_mode_is_rejected_but_a_token_rotation_keeps_it(self, real_auth):
		real_auth.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"refresh_token": "A"}}))
		with ai_fix._cli_sandbox() as (_, env):
			_rotate(env["CODEX_HOME"], "B", auth_mode="apikey")
		assert json.loads(real_auth.read_text())["tokens"]["refresh_token"] == "A"
		with ai_fix._cli_sandbox() as (_, env):
			_rotate(env["CODEX_HOME"], "C", auth_mode="chatgpt")
		assert json.loads(real_auth.read_text())["tokens"]["refresh_token"] == "C"

	def test_write_back_is_skipped_when_the_lock_cannot_be_taken(self, real_auth, monkeypatch):
		import fcntl
		monkeypatch.setattr(ai_fix, "_AUTH_LOCK_WAIT_SECONDS", 0.2)
		fd = os.open(f"{real_auth}.lock", os.O_RDWR | os.O_CREAT, 0o600)
		try:
			with ai_fix._cli_sandbox() as (_, env):
				fcntl.flock(fd, fcntl.LOCK_EX)
				_rotate(env["CODEX_HOME"], "ROTATED")
		finally:
			os.close(fd)
		assert json.loads(real_auth.read_text())["tokens"]["refresh_token"] == "ORIGINAL"

	def test_copy_waits_for_a_writer_holding_the_lock(self, real_auth):
		import fcntl
		import threading
		fd = os.open(f"{real_auth}.lock", os.O_RDWR | os.O_CREAT, 0o600)
		fcntl.flock(fd, fcntl.LOCK_EX)
		seen = {}

		def run():
			with ai_fix._cli_sandbox() as (_, env):
				seen["copy"] = open(os.path.join(env["CODEX_HOME"], "auth.json")).read()

		thread = threading.Thread(target=run)
		thread.start()
		time.sleep(0.3)
		assert thread.is_alive()
		real_auth.write_text(json.dumps({"tokens": {"refresh_token": "FRESH"}}))
		os.close(fd)
		thread.join(10)
		assert "FRESH" in seen["copy"]


class TestConsentRecheck:
	def _call(self):
		return ai_fix._call_local_cli("SYS", [{"role": "user", "content": "hello"}])

	def test_a_cli_send_marks_the_raw_request_as_sent(self, fake_codex):
		from optimus import ai_privacy
		fake_codex()
		with ai_privacy.raw_request_scope(True):
			self._call()
			assert ai_privacy._raw_request.get()["sent"] is True

	def test_revoked_raw_consent_blocks_a_resend_before_the_cli_starts(self, fake_codex, monkeypatch):
		from optimus import ai_privacy
		box = fake_codex()
		monkeypatch.setattr(ai_privacy, "_fresh_raw_values_enabled", lambda: False)
		with ai_privacy.raw_request_scope(True):
			self._call()
			runs_before = _read(box.rec, "calls.txt").count("exec")
			with pytest.raises(ai_fix.AiFixError) as err:
				self._call()
		assert err.value.kind == "config"
		assert _read(box.rec, "calls.txt").count("exec") == runs_before

	def test_a_private_request_is_unaffected(self, fake_codex):
		fake_codex()
		assert self._call() == "REPLY"


class TestRunCli:
	def _run(self, script, timeout=20, stdin_text=None):
		return ai_fix._run_cli(["/bin/sh", "-c", script], {"PATH": os.environ["PATH"]}, "/tmp", timeout, stdin_text)

	def _gone(self, pid):
		for _ in range(50):
			try:
				os.kill(pid, 0)
			except ProcessLookupError:
				return True
			time.sleep(0.1)
		os.kill(pid, 9)
		return False

	def test_grandchild_of_a_leader_that_exited_normally_is_killed(self, tmp_path):
		pidfile = tmp_path / "pid"
		code, out, err = self._run(f'sleep 60 & echo $! > "{pidfile}"; echo done')
		assert code == 0 and out.strip() == "done"
		assert self._gone(int(pidfile.read_text()))

	def test_stdout_is_capped_without_blocking_the_child(self):
		code, out, _ = self._run("head -c 5000000 /dev/zero | tr '\\0' x")
		assert code == 0 and len(out) == ai_fix._CLI_STREAM_LIMIT

	def test_stderr_keeps_its_tail(self):
		code, _, err = self._run("head -c 3000000 /dev/zero | tr '\\0' e >&2; echo LAST-LINE >&2; exit 3")
		assert code == 3 and len(err) <= ai_fix._CLI_STREAM_LIMIT and err.rstrip().endswith("LAST-LINE")

	def test_a_large_prompt_does_not_deadlock_on_stdin(self):
		code, out, _ = self._run("cat >/dev/null; echo ok", stdin_text="x" * 3_000_000)
		assert code == 0 and out.strip() == "ok"

	def test_a_child_that_ignores_stdin_does_not_hang_the_call(self):
		code, out, _ = self._run("echo ok", stdin_text="x" * 3_000_000)
		assert code == 0 and out.strip() == "ok"

	def test_invalid_utf8_output_is_replaced(self):
		code, out, _ = self._run("printf '\\377ok'")
		assert code == 0 and out.endswith("ok")

	def test_timeout_raises_and_kills_the_group(self, tmp_path):
		pidfile = tmp_path / "pid"
		with pytest.raises(ai_fix.subprocess.TimeoutExpired):
			self._run(f'sleep 60 & echo $! > "{pidfile}"; wait', timeout=1)
		assert self._gone(int(pidfile.read_text()))
