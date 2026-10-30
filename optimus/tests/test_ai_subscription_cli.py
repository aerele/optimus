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
import shutil
import stat
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from optimus import ai_fix, ai_transport

# The real path function, kept before the autouse fixture replaces it.
_REAL_CODEX_HOME_PATH = ai_fix._codex_home_path

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
	ai_fix._VERSION_CACHE.clear()
	yield
	ai_fix._LOGIN_CACHE.clear()
	ai_fix._VERSION_CACHE.clear()


_REAL_FEATURES = ai_fix._codex_features


@pytest.fixture(autouse=True)
def _no_feature_probe(monkeypatch):
	"""Never spawn ``codex features list`` implicitly: the CLI knows every floor name and has
	nothing else on, unless a test sets the probe."""
	ai_fix._FEATURES_CACHE.clear()
	known = frozenset(ai_fix._CLI_DISABLED_FEATURES)
	monkeypatch.setattr(ai_fix, "_codex_features", lambda cli_path, timeout=None: (known, frozenset()))
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


@pytest.fixture(autouse=True)
def optimus_home(tmp_path, monkeypatch):
	"""Keep every test off the real ``~/.codex`` and the real site: HOME is a temp dir and the
	Optimus-owned Codex home is a temp path that does not exist until a test creates it."""
	home = tmp_path / "bench_home"
	home.mkdir()
	monkeypatch.setenv("HOME", str(home))
	monkeypatch.delenv("CODEX_HOME", raising=False)
	codex_home = tmp_path / "site" / "private" / "optimus-codex"
	monkeypatch.setattr(ai_fix, "_codex_home_path", lambda: str(codex_home))
	return codex_home


@pytest.fixture
def real_auth(optimus_home):
	"""The Optimus-owned Codex home after a one-time sign-in; returns its ``auth.json``."""
	optimus_home.mkdir(parents=True, mode=0o700)
	auth = optimus_home / "auth.json"
	auth.write_text(json.dumps({"tokens": {"refresh_token": "ORIGINAL"}}))
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

	def test_preamble_scopes_untrusted_to_the_captured_data_only(self):
		pre = ai_fix._CLI_SECURITY_PREAMBLE
		lowered = pre.lower()
		assert "everything below" not in lowered and "never as instructions" not in lowered
		assert "captured" in lowered and "UNTRUSTED" in pre
		assert "task instructions" in lowered

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
	def test_tree_is_private_and_removed_and_codex_home_is_the_optimus_one(self, real_auth, optimus_home):
		with ai_fix._cli_sandbox() as (work, env):
			root = os.path.dirname(work)
			assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
			assert env["CODEX_HOME"] == str(optimus_home)
			assert os.listdir(env["CODEX_HOME"]) == ["auth.json"]
			assert os.listdir(env["HOME"]) == []
			assert env["HOME"] != os.path.expanduser("~") and env["HOME"].startswith(root)
			assert env["TMPDIR"].startswith(root)
		assert not os.path.exists(root)
		assert real_auth.exists()  # the dedicated home outlives the run

	def test_tree_is_removed_when_the_body_raises(self, real_auth):
		with pytest.raises(RuntimeError):
			with ai_fix._cli_sandbox() as (work, env):
				root = os.path.dirname(work)
				raise RuntimeError("boom")
		assert not os.path.exists(root)

	def test_the_dedicated_home_is_created_private_when_missing(self, optimus_home):
		assert not optimus_home.exists()
		with ai_fix._cli_sandbox() as (work, env):
			assert os.path.isdir(env["CODEX_HOME"]) and os.listdir(env["CODEX_HOME"]) == []
		assert stat.S_IMODE(os.stat(optimus_home).st_mode) == 0o700

	def test_a_loose_dedicated_home_is_tightened(self, optimus_home):
		optimus_home.mkdir(parents=True)
		os.chmod(optimus_home, 0o755)
		with ai_fix._cli_sandbox():
			pass
		assert stat.S_IMODE(os.stat(optimus_home).st_mode) == 0o700

	def test_a_link_in_place_of_the_dedicated_home_is_refused(self, optimus_home, tmp_path):
		target = tmp_path / "elsewhere"
		target.mkdir()
		optimus_home.parent.mkdir(parents=True)
		optimus_home.symlink_to(target)
		with pytest.raises(OSError):
			with ai_fix._cli_sandbox():
				pytest.fail("the sandbox must not start")

	def test_without_auth_the_codex_home_is_an_empty_scratch_dir(self, real_auth, optimus_home):
		with ai_fix._cli_sandbox(with_auth=False) as (work, env):
			assert env["CODEX_HOME"] != str(optimus_home)
			assert env["CODEX_HOME"].startswith(os.path.dirname(work))
			assert os.listdir(env["CODEX_HOME"]) == []

	def test_without_auth_the_dedicated_home_is_not_created(self, optimus_home):
		with ai_fix._cli_sandbox(with_auth=False):
			pass
		assert not optimus_home.exists()

	def test_the_operators_own_codex_home_is_never_touched(self, real_auth, tmp_path, monkeypatch):
		operator = tmp_path / "bench_home" / ".codex"
		operator.mkdir()
		(operator / "auth.json").write_text('{"tokens": {"refresh_token": "OPERATOR"}}')
		monkeypatch.setenv("CODEX_HOME", str(operator))
		before = {p.name: p.read_bytes() for p in operator.iterdir()}
		with ai_fix._cli_sandbox() as (work, env):
			assert env["CODEX_HOME"] == str(real_auth.parent)
		assert {p.name: p.read_bytes() for p in operator.iterdir()} == before


# ---------------------------------------------------------------------------
# Process-level behaviour against a fake codex script
# ---------------------------------------------------------------------------

class TestStaleSweep:
	def _old(self, base, name, age=7200):
		path = base / name
		path.mkdir()
		(path / "auth.json").write_text("{}")
		os.utime(path, (time.time() - age, time.time() - age))
		return path

	def test_old_own_scratch_trees_are_deleted_and_fresh_or_foreign_ones_kept(self, tmp_path, monkeypatch):
		monkeypatch.setattr(ai_fix.tempfile, "tempdir", str(tmp_path))
		monkeypatch.setattr(ai_fix, "_LAST_SWEEP", 0.0)
		stale = self._old(tmp_path, "optimus-codex-old")
		fresh = self._old(tmp_path, "optimus-codex-new", age=10)
		other = self._old(tmp_path, "something-else")
		ai_fix._sweep_stale_sandboxes()
		assert not stale.exists() and fresh.exists() and other.exists()

	def test_the_sweep_is_throttled(self, tmp_path, monkeypatch):
		monkeypatch.setattr(ai_fix.tempfile, "tempdir", str(tmp_path))
		monkeypatch.setattr(ai_fix, "_LAST_SWEEP", 0.0)
		ai_fix._sweep_stale_sandboxes()
		stale = self._old(tmp_path, "optimus-codex-old")
		ai_fix._sweep_stale_sandboxes()
		assert stale.exists()

	def test_a_removal_logs_one_line(self, tmp_path, monkeypatch):
		monkeypatch.setattr(ai_fix.tempfile, "tempdir", str(tmp_path))
		monkeypatch.setattr(ai_fix, "_LAST_SWEEP", 0.0)
		lines = []
		monkeypatch.setattr(ai_fix, "_note_line", lines.append)
		self._old(tmp_path, "optimus-codex-old")
		ai_fix._sweep_stale_sandboxes()
		assert lines == ["codex cli: stale_sandbox_removed label=count_1"]

	def test_a_sweep_that_removes_nothing_logs_nothing(self, tmp_path, monkeypatch):
		monkeypatch.setattr(ai_fix.tempfile, "tempdir", str(tmp_path))
		monkeypatch.setattr(ai_fix, "_LAST_SWEEP", 0.0)
		lines = []
		monkeypatch.setattr(ai_fix, "_note_line", lines.append)
		ai_fix._sweep_stale_sandboxes()
		assert lines == []

	def test_creating_a_sandbox_sweeps(self, tmp_path, real_auth, monkeypatch):
		monkeypatch.setattr(ai_fix.tempfile, "tempdir", str(tmp_path))
		monkeypatch.setattr(ai_fix, "_LAST_SWEEP", 0.0)
		stale = self._old(tmp_path, "optimus-codex-old")
		with ai_fix._cli_sandbox(with_auth=False):
			pass
		assert not stale.exists()

	def test_a_symlink_named_like_a_scratch_tree_is_not_followed(self, tmp_path, monkeypatch):
		monkeypatch.setattr(ai_fix.tempfile, "tempdir", str(tmp_path))
		monkeypatch.setattr(ai_fix, "_LAST_SWEEP", 0.0)
		target = tmp_path / "precious"
		target.mkdir()
		(tmp_path / "optimus-codex-link").symlink_to(target)
		ai_fix._sweep_stale_sandboxes()
		assert target.exists()


class TestCliResolve:
	@pytest.mark.parametrize("path,reason", [
		("codex", "relative"),
		("/opt/bin/node", "name"),
	])
	def test_reasons_for_a_bad_configured_path(self, path, reason):
		assert ai_fix._cli_resolve(_cfg(ai_cli_path=path)) == (None, reason)

	def test_nothing_on_path_is_not_found(self, monkeypatch):
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: None)
		assert ai_fix._cli_resolve(_cfg()) == (None, "not_found")

	def test_untrusted_and_entrypoint_reasons(self, tmp_path):
		bad = tmp_path / "codex"
		bad.write_text("#!/bin/sh\n")
		bad.chmod(0o777)
		assert ai_fix._cli_resolve(_cfg(ai_cli_path=str(bad))) == (None, "untrusted")
		other = tmp_path / "real_node"
		other.write_text("x")
		link = tmp_path / "lnk" / "codex"
		link.parent.mkdir()
		link.symlink_to(other)
		assert ai_fix._cli_resolve(_cfg(ai_cli_path=str(link))) == (None, "entrypoint")

	def test_every_reason_has_its_own_message(self):
		texts = {ai_fix._cli_missing_text(r) for r in ("not_found", "relative", "name", "entrypoint", "untrusted")}
		assert len(texts) == 5

	def test_the_call_and_the_status_show_the_reason(self, sub_cfg):
		sub_cfg.ai_cli_path = "codex"
		with pytest.raises(ai_fix.AiFixError, match="absolute") as err:
			ai_fix._call_local_cli("S", [{"content": "x"}])
		assert err.value.kind == "config"
		assert "absolute" in ai_fix._cli_session_status()["message"]


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

	def test_child_sees_a_private_home_and_none_of_the_bench_environment(self, fake_codex, real_auth, monkeypatch):
		box = fake_codex()
		monkeypatch.setenv("OPTIMUS_TEST_DB_PASSWORD", "supersecret")
		monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
		self._call()
		env = _read(box.rec, "env_exec.txt")
		assert "supersecret" not in env and "OPENAI_API_KEY" not in env
		home = _read(box.rec, "home_exec.txt").strip()
		codex_home = _read(box.rec, "codexhome_exec.txt").strip()
		assert home != os.path.expanduser("~") and codex_home == str(real_auth.parent)
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

	def test_the_codex_cli_refreshes_its_own_sign_in_in_the_dedicated_home_only(self, fake_codex, real_auth, tmp_path):
		fake_codex(exec_body='echo \'{"tokens": {"refresh_token": "ROTATED"}}\' > "$CODEX_HOME/auth.json"; echo OK > "$out"')
		skip = {"rec", "bin"}

		def snapshot():
			found = set()
			for root, dirs, files in os.walk(tmp_path):
				dirs[:] = [d for d in dirs if not (root == str(tmp_path) and d in skip)]
				found.update(os.path.join(root, n) for n in dirs + files)
			return found

		before = snapshot()
		self._call()
		assert json.loads(real_auth.read_text())["tokens"]["refresh_token"] == "ROTATED"
		assert snapshot() == before  # nothing new anywhere, and no ~/.codex appeared
		assert not (tmp_path / "bench_home" / ".codex").exists()

	def test_timeout_kills_the_whole_process_group_and_is_not_fatal(self, fake_codex, monkeypatch):
		monkeypatch.setattr(ai_fix, "_CLI_MIN_EXEC_SECONDS", 1)  # let a 2s budget reach the exec
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

	def test_an_exhausted_deadline_raises_a_timeout_and_never_launches_the_exec(self, fake_codex, monkeypatch):
		fake_codex()
		monkeypatch.setattr(ai_fix, "_cli_login_ok", lambda path, **kw: (True, "ok"))
		ran = []
		monkeypatch.setattr(ai_fix, "_run_cli", lambda *a, **kw: ran.append(a) or (0, "REPLY", ""))
		with ai_transport.attempt() as transport:
			with pytest.raises(ai_fix.AiFixError, match="ran out of time") as err:
				self._call(timeout=ai_fix._CLI_MIN_EXEC_SECONDS - 1)
		assert err.value.kind == "timeout" and err.value.fatal is False
		assert ran == [] and transport.certainty == "not_sent"

	def test_a_probe_that_uses_up_the_deadline_stops_before_the_exec(self, fake_codex, monkeypatch):
		fake_codex()
		clock = {"now": 1000.0}

		class _Clock:
			monotonic = staticmethod(lambda: clock["now"])

			def __getattr__(self, name):
				return getattr(time, name)

		monkeypatch.setattr(ai_fix, "time", _Clock())
		monkeypatch.setattr(ai_fix, "_cli_login_ok", lambda path, **kw: (True, "ok"))
		real_build = ai_fix._build_cli_argv

		def slow_build(path, work, out_file, **kw):
			clock["now"] += 27  # leaves 3s of the 30s budget
			return real_build(path, work, out_file, **kw)

		monkeypatch.setattr(ai_fix, "_build_cli_argv", slow_build)
		ran = []
		monkeypatch.setattr(ai_fix, "_run_cli", lambda *a, **kw: ran.append(a) or (0, "REPLY", ""))
		with pytest.raises(ai_fix.AiFixError, match="ran out of time") as err:
			self._call(timeout=30)
		assert err.value.kind == "timeout" and ran == []

	def test_the_features_probe_is_capped_by_the_time_left(self, fake_codex, monkeypatch):
		fake_codex()
		seen = []
		known = frozenset(ai_fix._CLI_DISABLED_FEATURES)
		monkeypatch.setattr(ai_fix, "_codex_features", lambda p, timeout=None: (seen.append(timeout), (known, frozenset()))[1])
		self._call(timeout=30)
		assert seen and 0 < seen[0] <= 30

	def test_the_timeout_message_names_the_configured_limit(self, fake_codex, monkeypatch):
		fake_codex()

		def hang(*a, **kw):
			raise subprocess.TimeoutExpired("codex", 1)

		monkeypatch.setattr(ai_fix, "_cli_login_ok", lambda p, **kw: (True, "ok"))
		monkeypatch.setattr(ai_fix, "_run_cli", hang)
		with ai_transport.attempt() as transport:
			with pytest.raises(ai_fix.AiFixError) as err:
				self._call(timeout=30)
		assert "within the 30s limit" in str(err.value) and err.value.kind == "timeout"
		assert transport.certainty == "sent_unknown"

	def test_a_job_timeout_during_the_exec_records_sent_unknown(self, fake_codex, monkeypatch):
		fake_codex()

		class _Interrupt(Exception):
			pass

		def killed(*a, **kw):
			raise _Interrupt("job timeout")

		monkeypatch.setattr(ai_fix, "_cli_login_ok", lambda p, **kw: (True, "ok"))
		monkeypatch.setattr(ai_fix, "_run_cli", killed)
		with ai_transport.attempt() as transport:
			with pytest.raises(_Interrupt):
				self._call()
		assert transport.certainty == "sent_unknown"

	@pytest.mark.parametrize("code", [-9, -11, 137])
	def test_a_signal_killed_exec_is_sent_unknown_and_not_fatal(self, fake_codex, monkeypatch, code):
		fake_codex()
		monkeypatch.setattr(ai_fix, "_cli_login_ok", lambda p, **kw: (True, "ok"))
		monkeypatch.setattr(ai_fix, "_run_cli", lambda *a, **kw: (code, "", "RAWCANARY"))
		lines = []
		monkeypatch.setattr(ai_fix, "_note_line", lines.append)
		with ai_transport.attempt() as transport:
			with pytest.raises(ai_fix.AiFixError) as err:
				self._call()
		assert err.value.kind == "transport" and err.value.fatal is False
		assert transport.certainty == "sent_unknown" and "RAWCANARY" not in str(err.value)
		assert lines and lines[0].startswith("codex cli: exec_failed") and f"code={code}" in lines[0]

	def test_a_real_sigkill_of_the_exec_is_sent_unknown(self, fake_codex):
		fake_codex(exec_body="kill -9 $$")
		with ai_transport.attempt() as transport:
			with pytest.raises(ai_fix.AiFixError) as err:
				self._call()
		assert err.value.kind == "transport" and transport.certainty == "sent_unknown"

	def test_a_successful_exec_refreshes_the_login_memo(self, fake_codex, monkeypatch):
		box = fake_codex()
		path = str(box.path)
		real_login = ai_fix._cli_login_ok
		monkeypatch.setattr(ai_fix, "_cli_login_ok", lambda p, **kw: (True, "ok"))  # memoizes nothing
		assert path not in ai_fix._LOGIN_CACHE
		assert self._call() == "REPLY"
		assert ai_fix._LOGIN_CACHE[path][1:] == (True, "ok")
		monkeypatch.setattr(ai_fix, "_cli_login_ok", real_login)
		assert self._call() == "REPLY"
		assert "login status" not in _read(box.rec, "calls.txt")

	def test_a_failed_exec_does_not_refresh_the_login_memo(self, fake_codex, monkeypatch):
		box = fake_codex(exec_body="exit 3")
		monkeypatch.setattr(ai_fix, "_cli_login_ok", lambda p, **kw: (True, "ok"))
		with pytest.raises(ai_fix.AiFixError):
			self._call()
		assert str(box.path) not in ai_fix._LOGIN_CACHE

	def test_exec_failure_is_logged_with_event_version_label_and_code_only(self, fake_codex, monkeypatch):
		fake_codex(exec_body="echo 'RAWCANARY prompt text SELECT secret' >&2; exit 3")
		lines = []
		monkeypatch.setattr(ai_fix, "_note_line", lines.append)
		with pytest.raises(ai_fix.AiFixError):
			self._call()
		assert lines == ["codex cli: exec_failed version=codex-cli 9.9.9 label=other code=3"]
		assert "RAWCANARY" not in "".join(lines)

	def test_over_cap_reply_behind_a_long_run_of_leading_blanks_is_still_flagged(self, tmp_path):
		out = tmp_path / "out.txt"
		out.write_text(" \n" * 2000 + "x" * (ai_fix._CLI_MAX_OUTPUT_CHARS + 500))
		assert ai_fix._read_cli_reply(str(out), "") == ("x" * ai_fix._CLI_MAX_OUTPUT_CHARS, True)

	def test_leading_blanks_on_the_stdout_fallback_do_not_hide_an_overflow(self, tmp_path):
		stdout = " " * 3000 + "y" * (ai_fix._CLI_MAX_OUTPUT_CHARS + 500)
		assert ai_fix._read_cli_reply(str(tmp_path / "missing"), stdout) == ("y" * ai_fix._CLI_MAX_OUTPUT_CHARS, True)

	def test_a_short_reply_behind_blanks_is_returned_stripped_and_unflagged(self, tmp_path):
		out = tmp_path / "out.txt"
		out.write_text(" " * 5000 + "fix\n\n")
		assert ai_fix._read_cli_reply(str(out), "") == ("fix", False)

	def test_a_link_planted_as_the_reply_file_is_not_followed(self, tmp_path):
		secret = tmp_path / "site_config.json"
		secret.write_text("TOP-SECRET")
		out = tmp_path / "out.txt"
		out.symlink_to(secret)
		assert ai_fix._read_cli_reply(str(out), "from stdout") == ("from stdout", False)

	def test_a_fifo_planted_as_the_reply_file_does_not_block(self, tmp_path):
		out = tmp_path / "out.txt"
		os.mkfifo(out)
		assert ai_fix._read_cli_reply(str(out), "from stdout") == ("from stdout", False)

	def test_a_configured_path_that_is_not_named_codex_explains_what_to_set(self, sub_cfg, tmp_path):
		sub_cfg.ai_cli_path = str(tmp_path / "codex.js")
		with pytest.raises(ai_fix.AiFixError, match="symlink named `codex`") as err:
			self._call()
		assert err.value.kind == "config"

	def test_probe_time_comes_out_of_the_exec_budget(self, fake_codex, monkeypatch):
		fake_codex()
		clock = {"now": 1000.0}

		class _Clock:
			monotonic = staticmethod(lambda: clock["now"])

			def __getattr__(self, name):
				return getattr(time, name)

		monkeypatch.setattr(ai_fix, "time", _Clock())
		monkeypatch.setattr(ai_fix, "_cli_login_ok", lambda path, **kw: (True, "ok"))
		real_build = ai_fix._build_cli_argv

		def slow_build(path, work, out_file, **kw):
			clock["now"] += 12  # the features probe burns time
			return real_build(path, work, out_file, **kw)

		monkeypatch.setattr(ai_fix, "_build_cli_argv", slow_build)
		seen = {}

		def fake_run(argv, env, cwd, timeout, stdin_text=None):
			seen["timeout"] = timeout
			return 0, "REPLY", ""

		monkeypatch.setattr(ai_fix, "_run_cli", fake_run)
		assert self._call(timeout=30) == "REPLY"
		assert seen["timeout"] == 18

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

	def test_over_cap_reply_file_is_cut_and_flagged_truncated(self, tmp_path):
		out = tmp_path / "out.txt"
		out.write_text("x" * 25_000)
		assert ai_fix._read_cli_reply(str(out), "") == ("x" * ai_fix._CLI_MAX_OUTPUT_CHARS, True)

	def test_over_cap_stdout_fallback_is_cut_and_flagged_truncated(self, tmp_path):
		text, truncated = ai_fix._read_cli_reply(str(tmp_path / "missing"), "y" * 25_000)
		assert truncated is True and text == "y" * ai_fix._CLI_MAX_OUTPUT_CHARS

	def test_reply_exactly_at_the_cap_is_not_flagged(self, tmp_path):
		out = tmp_path / "out.txt"
		out.write_text("x" * ai_fix._CLI_MAX_OUTPUT_CHARS)
		assert ai_fix._read_cli_reply(str(out), "") == ("x" * ai_fix._CLI_MAX_OUTPUT_CHARS, False)

	def test_over_cap_reply_comes_back_scrubbed_and_marked_truncated(self, fake_codex):
		fake_codex(
			exec_body='echo "db_password = \\"hunter2hunter2\\"" > "$out"; '
			'i=0; while [ $i -lt 5000 ]; do printf "word " >> "$out"; i=$((i+1)); done'
		)
		reply = self._call()
		assert "hunter2" not in reply and "[redacted]" in reply
		assert reply.endswith("word\n…(truncated)") and len(reply) < ai_fix._CLI_MAX_OUTPUT_CHARS + 50

	def test_truncation_marker_survives_an_unterminated_pem_block(self, fake_codex):
		body = "-----BEGIN PRIVATE KEY-----\n" + "A" * 25_000
		fake_codex(exec_body=f"printf '%s' '{body}' > \"$out\"")
		reply = self._call()
		assert "BEGIN PRIVATE KEY" not in reply and "A" * 100 not in reply
		assert reply.endswith("\n…(truncated)")

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
		("error 429 too many requests", "rate_limited", False),
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
		# A rejection before any answer is a known outcome; a network drop or crash may have been acted on.
		known = kind in ("auth", "quota", "config", "rate_limited")
		assert transport.certainty == ("known_response" if known else "sent_unknown")

	@pytest.mark.parametrize("code", [126, 127])
	def test_a_cli_that_cannot_run_names_the_cli_and_node(self, fake_codex, code):
		fake_codex(exec_body=f"exit {code}")
		with pytest.raises(ai_fix.AiFixError, match="Node.js") as err:
			self._call()
		assert err.value.kind == "transport" and err.value.fatal is False

	def test_the_codex_home_path_is_absolute_even_when_the_site_path_is_relative(self, monkeypatch, tmp_path):
		import frappe

		# Frappe's get_site_path is relative to sites_path ("."): the child runs elsewhere.
		monkeypatch.setattr(frappe, "get_site_path", lambda *p: os.path.join(".", "site1", *p), raising=False)
		monkeypatch.setattr(ai_fix, "_codex_home_path", _REAL_CODEX_HOME_PATH)
		monkeypatch.chdir(tmp_path)
		path = ai_fix._ensure_codex_home()
		assert os.path.isabs(path) and path == str(tmp_path / "site1" / "private" / "optimus-codex")
		assert os.path.isabs(ai_fix._codex_home_path())
		with ai_fix._cli_sandbox() as (work, env):
			assert env["CODEX_HOME"] == path
			monkeypatch.chdir(work)  # the child's cwd: the home still resolves
			assert os.path.isdir(env["CODEX_HOME"])
		monkeypatch.chdir(tmp_path)
		assert ai_fix._codex_login_command().startswith(f"CODEX_HOME={path} ")

	def test_a_relative_codex_home_is_refused(self, monkeypatch):
		monkeypatch.setattr(ai_fix, "_codex_home_path", lambda: "site1/private/optimus-codex")
		with pytest.raises(ai_fix.AiFixError) as err:
			ai_fix._ensure_codex_home()
		assert err.value.kind == "config"

	@pytest.mark.parametrize("stderr", [
		"Your access token could not be refreshed. Please log out and sign in again.",
		"refresh token was already used. Please log out and sign in again.",
		"token was revoked. Please log out and sign in again.",
	])
	def test_real_refresh_failures_are_an_auth_error_with_the_login_hint(self, fake_codex, optimus_home, stderr):
		fake_codex(exec_body=f"echo '{stderr}' >&2; exit 1")
		with pytest.raises(ai_fix.AiFixError) as err:
			self._call()
		assert err.value.kind == "auth" and f"CODEX_HOME={optimus_home} codex login" in str(err.value)

	def test_a_lost_refresh_race_is_retried_once_and_succeeds(self, fake_codex, monkeypatch, tmp_path):
		monkeypatch.setattr(ai_fix, "_CLI_AUTH_RETRY_WAIT", 0)
		count = tmp_path / "rec" / "n"
		fake_codex(exec_body=(
			f'echo x >> "{count}"; '
			f'if [ "$(wc -l < "{count}")" = 1 ]; then echo "refresh token was already used" >&2; exit 1; fi; '
			'echo RETRIED > "$out"'
		))
		assert self._call().startswith("RETRIED")
		assert len(count.read_text().split()) == 2

	def _count_runs(self, fake_codex, monkeypatch, tmp_path, line, code):
		monkeypatch.setattr(ai_fix, "_CLI_AUTH_RETRY_WAIT", 0)
		count = tmp_path / "rec" / "n"
		fake_codex(exec_body=f'echo x >> "{count}"; echo "{line}" >&2; exit {code}')
		with pytest.raises(ai_fix.AiFixError):
			self._call()
		return len(count.read_text().split())

	def test_a_signal_exit_with_auth_text_is_not_retried(self, fake_codex, monkeypatch, tmp_path):
		assert self._count_runs(fake_codex, monkeypatch, tmp_path, "refresh token was already used", 137) == 1

	def test_an_unrelated_unauthorized_line_is_not_retried(self, fake_codex, monkeypatch, tmp_path):
		assert self._count_runs(fake_codex, monkeypatch, tmp_path, "tool call unauthorized by policy", 1) == 1

	def test_the_retry_wait_is_jittered(self, fake_codex, monkeypatch):
		monkeypatch.setattr(ai_fix, "_CLI_AUTH_RETRY_WAIT", 2)
		monkeypatch.setattr(ai_fix.random, "uniform", lambda lo, hi: hi)
		slept = []
		monkeypatch.setattr(ai_fix.time, "sleep", slept.append)
		fake_codex(exec_body='echo "refresh token was already used" >&2; exit 1')
		with pytest.raises(ai_fix.AiFixError):
			self._call()
		assert 3.0 in slept

	def test_a_second_auth_failure_is_not_retried_again(self, fake_codex, monkeypatch, tmp_path):
		monkeypatch.setattr(ai_fix, "_CLI_AUTH_RETRY_WAIT", 0)
		count = tmp_path / "rec" / "n"
		fake_codex(exec_body=f'echo x >> "{count}"; echo "token was revoked" >&2; exit 1')
		with pytest.raises(ai_fix.AiFixError) as err:
			self._call()
		assert err.value.kind == "auth"
		assert len(count.read_text().split()) == 2

	def test_no_retry_when_too_little_time_is_left(self):
		assert not ai_fix._auth_retry_fits(time.monotonic() + 3)
		assert ai_fix._auth_retry_fits(time.monotonic() + 60)

	def test_an_auth_failure_clears_the_login_memo(self, fake_codex):
		box = fake_codex(exec_body="echo 'HTTP 401 Unauthorized' >&2; exit 1")
		with pytest.raises(ai_fix.AiFixError):
			self._call()
		assert str(box.path) not in ai_fix._LOGIN_CACHE

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

	def test_a_failure_that_says_not_logged_in_is_a_sign_out(self, fake_codex):
		box = fake_codex(login='echo "Not logged in" >&2; exit 1')
		assert ai_fix._cli_login_ok(str(box.path), use_cache=False) == (False, "signed_out")

	def test_an_unrecognised_failure_is_an_error_not_a_sign_out(self, fake_codex):
		box = fake_codex(login='echo "thread panicked" >&2; exit 1')
		assert ai_fix._cli_login_ok(str(box.path), use_cache=False) == (False, "error")

	@pytest.mark.parametrize("code", [137, 139])
	def test_a_shell_style_signal_exit_is_a_crash_not_a_sign_out(self, fake_codex, code):
		box = fake_codex(login=f"exit {code}")
		assert ai_fix._cli_login_ok(str(box.path), use_cache=False) == (False, "error")

	@pytest.mark.parametrize("sig", ["KILL", "SEGV"])
	def test_a_real_signal_kill_has_a_negative_returncode_and_is_not_a_sign_out(self, fake_codex, sig):
		box = fake_codex(login=f"kill -{sig} $$")
		env = {"PATH": os.environ["PATH"]}
		code, _, _ = ai_fix._run_cli([str(box.path), "login", "status"], env, str(box.rec), 10)
		assert code < 0  # Popen without a shell reports a signal as -N, not 128+N
		assert ai_fix._cli_login_ok(str(box.path), use_cache=False) == (False, "error")

	@pytest.mark.parametrize("code,expected", [
		(-9, (False, "error")), (-11, (False, "error")), (2, (False, "error")), (1, (False, "signed_out")),
	])
	def test_login_returncode_classification(self, monkeypatch, code, expected):
		monkeypatch.setattr(ai_fix, "_run_cli", lambda *a, **kw: (code, "", "Not logged in"))
		assert ai_fix._cli_login_ok("/x/codex", use_cache=False) == expected
		assert "/x/codex" not in ai_fix._LOGIN_CACHE

	def test_not_chatgpt_logs_the_version_and_the_message_names_it(self, fake_codex, monkeypatch):
		box = fake_codex(login='echo "Logged in using an API key" >&2')
		lines = []
		monkeypatch.setattr(ai_fix, "_note_line", lines.append)
		assert ai_fix._cli_login_ok(str(box.path), use_cache=False) == (False, "not_chatgpt")
		assert lines == ["codex cli: login_not_chatgpt version=codex-cli 9.9.9"]
		message = ai_fix._login_failure("not_chatgpt", str(box.path)).args[0]
		assert "your codex CLI (codex-cli 9.9.9) may need updating" in message
		assert "may need updating" not in ai_fix._login_failure("not_chatgpt").args[0]

	def test_an_operator_who_has_not_signed_in_yet_gets_signed_out_not_an_error(self, fake_codex, optimus_home):
		box = fake_codex(login='echo "Not logged in" >&2; exit 1')
		shutil.rmtree(optimus_home)  # fake_codex starts signed in; this operator has not
		assert ai_fix._cli_login_ok(str(box.path), use_cache=False) == (False, "signed_out")
		assert stat.S_IMODE(os.stat(optimus_home).st_mode) == 0o700

	@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
	def test_an_unwritable_site_folder_is_an_error_not_a_sign_out(self, fake_codex, optimus_home, tmp_path):
		box = fake_codex(login='echo "Not logged in" >&2; exit 1')
		shutil.rmtree(optimus_home.parent)  # no private/ folder yet, and the site folder is read-only
		os.chmod(optimus_home.parent.parent, 0o500)
		try:
			assert ai_fix._cli_login_ok(str(box.path), use_cache=False) == (False, "error")
		finally:
			os.chmod(optimus_home.parent.parent, 0o700)

	def test_the_not_signed_in_message_gives_the_exact_login_command_with_the_path(self, optimus_home):
		for reason in ("signed_out", "not_chatgpt"):
			message = ai_fix._login_failure(reason).args[0]
			assert f"CODEX_HOME={optimus_home} codex login" in message
			assert "~/.codex" in message

	def test_the_login_command_quotes_a_path_with_spaces(self, monkeypatch):
		monkeypatch.setattr(ai_fix, "_codex_home_path", lambda: "/srv/my site/private/optimus-codex")
		assert ai_fix._codex_login_command() == "CODEX_HOME='/srv/my site/private/optimus-codex' codex login"

	def test_connect_shows_the_login_command_when_signed_out(self, fake_codex, optimus_home):
		fake_codex(login='echo "Not logged in" >&2; exit 1')
		st = ai_fix._cli_session_status()
		assert st["state"] == "signed_out" and f"CODEX_HOME={optimus_home} codex login" in st["message"]

	def test_an_expired_session_message_gives_the_login_command(self, fake_codex, optimus_home):
		fake_codex(exec_body="echo 'HTTP 401 unauthorized' >&2; exit 1")
		with pytest.raises(ai_fix.AiFixError) as err:
			ai_fix._call_local_cli("S", [{"content": "x"}])
		assert err.value.kind == "auth" and f"CODEX_HOME={optimus_home} codex login" in err.value.args[0]

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
		fake_codex(login='echo "Not logged in" >&2; exit 1')
		assert ai_fix._cli_login_ok(path) == (False, "signed_out")
		assert path not in ai_fix._LOGIN_CACHE

	def test_unstartable_binary_is_a_temporary_error(self, tmp_path):
		bad = tmp_path / "codex"
		bad.write_text("not a script and no shebang")
		bad.chmod(0o755)
		assert ai_fix._cli_login_ok(str(bad)) == (False, "error")


class TestChildDiesWithTheWorker:
	linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux only")

	def test_the_preexec_hook_is_used_on_linux_and_absent_elsewhere(self, monkeypatch):
		monkeypatch.setattr(ai_fix.sys, "platform", "darwin")
		assert ai_fix._cli_preexec() is None
		monkeypatch.setattr(ai_fix.sys, "platform", "linux")
		assert callable(ai_fix._cli_preexec())

	def test_a_libc_without_prctl_means_no_hook(self, monkeypatch):
		monkeypatch.setattr(ai_fix.sys, "platform", "linux")
		monkeypatch.setattr(ai_fix.ctypes, "CDLL", lambda *a, **kw: object())
		assert ai_fix._cli_preexec() is None

	def test_run_cli_passes_the_hook_to_popen(self, monkeypatch):
		seen = {}

		class _Stop(Exception):
			pass

		def popen(argv, **kw):
			seen.update(kw)
			raise _Stop

		monkeypatch.setattr(ai_fix.subprocess, "Popen", popen)
		monkeypatch.setattr(ai_fix, "_cli_preexec", lambda: "HOOK")
		with pytest.raises(_Stop):
			ai_fix._run_cli(["x"], {}, "/tmp", 1)
		assert seen["preexec_fn"] == "HOOK" and seen["start_new_session"] is True

	@linux
	def test_the_child_asks_the_kernel_to_kill_it_with_its_parent(self):
		code, out, err = ai_fix._run_cli(
			[sys.executable, "-I", "-c", "import ctypes,sys;v=ctypes.c_int();ctypes.CDLL(None).prctl(2,ctypes.byref(v));print(v.value)"],
			{"PATH": os.environ["PATH"]}, "/tmp", 20,
		)
		assert code == 0 and out.strip() == str(int(ai_fix.signal.SIGKILL))

	def test_an_abandoned_run_logs_one_line(self, fake_codex, monkeypatch):
		fake_codex()
		lines = []
		monkeypatch.setattr(ai_fix, "_note_line", lines.append)

		def stuck(*a, **kw):
			raise ai_fix.subprocess.TimeoutExpired("codex", 1)

		monkeypatch.setattr(ai_fix, "_run_cli", lambda argv, *a, **kw: stuck() if argv[1] == "exec" else (0, "Logged in using ChatGPT", ""))
		with pytest.raises(ai_fix.AiFixError):
			ai_fix._call_local_cli("S", [{"content": "x"}])
		assert "codex cli: exec_abandoned label=TimeoutExpired" in lines


class TestSessionStatus:
	def test_blocked_when_guard_off(self, sub_cfg, monkeypatch):
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: False)
		st = ai_fix._cli_session_status()
		assert st["state"] == "blocked" and st["ok"] is False
		assert "developer_mode" in st["message"] and "optimus_allow_subscription_cli" in st["message"]

	def test_the_blocked_message_says_how_to_enable_it_and_that_a_restart_is_needed(self, sub_cfg, monkeypatch):
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: False)
		message = ai_fix._cli_session_status()["message"]
		assert "set-config optimus_allow_subscription_cli 1" in message
		assert "set-config developer_mode 1" in message
		assert "restart the bench" in message and "bench clear-cache" in message

	def test_the_settings_field_description_carries_the_same_instructions(self):
		path = os.path.join(os.path.dirname(__file__), "..", "optimus", "doctype", "optimus_settings", "optimus_settings.json")
		with open(path) as f:
			fields = {d["fieldname"]: d for d in json.load(f)["fields"]}
		text = fields["ai_connection_type"]["description"]
		assert "set-config optimus_allow_subscription_cli 1" in text and "restart the bench" in text

	@pytest.mark.parametrize("version,tested", [
		("codex-cli 0.164.0", True), ("codex-cli 0.165.9", True),
		("codex-cli 0.163.0", False), ("codex-cli 0.170.1", False), ("dev", None), (None, None),
	])
	def test_the_tested_version_range(self, version, tested):
		assert ai_fix._version_tested(version) is tested

	def test_connect_warns_about_an_untested_version(self, fake_codex, monkeypatch):
		fake_codex()
		monkeypatch.setattr(ai_fix, "_cli_version_cached", lambda p, probe=True: "codex-cli 0.200.0")
		message = ai_fix._cli_session_status()["message"]
		assert "outside the range" in message and "0.164" in message and "0.165" in message

	def test_missing_when_cli_not_found(self, sub_cfg, monkeypatch):
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: None)
		assert ai_fix._cli_session_status()["state"] == "missing"

	def test_connected(self, fake_codex):
		fake_codex()
		st = ai_fix._cli_session_status()
		assert st["state"] == "connected" and st["ok"] is True and st["version"] == "codex-cli 9.9.9"
		assert "@" not in st["message"]

	def test_signed_out_message_carries_no_account_detail(self, fake_codex):
		fake_codex(login='echo "Not logged in: secret detail user@example.com" >&2; exit 1')
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
		fake_codex(login='echo "Not logged in" >&2; exit 1')
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
		("error 429", "rate_limited"),
		("rate limit exceeded", "rate_limited"),
		("Too Many Requests", "rate_limited"),
		("status: 401", "auth_expired"),
		("processed 401 rows", "other"),
		("quota exceeded", "usage_limit"),
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
# Tool isolation, consent re-check and process cleanup
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
		assert argv.count("--disable") == len(ai_fix._CLI_DISABLED_FEATURES)  # the fixture CLI knows every name

	@staticmethod
	def _disabled(argv):
		return [argv[i + 1] for i, a in enumerate(argv) if a == "--disable"]

	@staticmethod
	def _probe(monkeypatch, on, known=None):
		"""Fake ``(known, on)``; ``known`` defaults to the floor plus ``on``."""
		listing = None if on is None else (
			frozenset(ai_fix._CLI_DISABLED_FEATURES if known is None else known) | frozenset(on), frozenset(on)
		)
		monkeypatch.setattr(ai_fix, "_codex_features", lambda p, timeout=None: listing)

	_NEW_FLOOR = (
		"browser_use_external", "browser_use_full_cdp_access", "browser_annotation_api",
		"code_mode_host", "code_mode_interrupt", "in_app_browser", "in_app_local_automation",
	)

	def test_the_floor_covers_the_default_on_browser_and_automation_tools(self):
		assert set(self._NEW_FLOOR) <= set(ai_fix._CLI_DISABLED_FEATURES)
		assert {"shell_tool", "browser_use", "plugins", "apps"} <= set(ai_fix._CLI_DISABLED_FEATURES)

	def test_on_capability_features_from_the_probe_are_disabled_too(self, monkeypatch):
		on = {"future_browser_tool", "remote_repl", "fast_mode", "goals", "steer", "worktrees", "collab"}
		self._probe(monkeypatch, on)
		disabled = self._disabled(ai_fix._build_cli_argv("codex", "/s", "/s/out"))
		assert {"future_browser_tool", "remote_repl", "goals", "worktrees", "collab"} <= set(disabled)
		for name in ("fast_mode", "steer"):
			assert name not in disabled
		assert set(ai_fix._CLI_DISABLED_FEATURES) <= set(disabled)
		assert len(disabled) == len(set(disabled)) and disabled == sorted(disabled)

	def test_a_probed_browser_use_external_is_in_the_list_alongside_the_floor(self, monkeypatch):
		self._probe(monkeypatch, {"browser_use_external"})
		disabled = ai_fix._cli_disabled_features("codex")
		assert "browser_use_external" in disabled
		assert set(ai_fix._CLI_DISABLED_FEATURES) <= set(disabled)

	def test_only_floor_names_the_cli_lists_are_passed(self, monkeypatch):
		# A name the installed codex does not know makes every `codex exec` exit 1.
		known = set(ai_fix._CLI_DISABLED_FEATURES) - {"multi_agent_v2", "computer_use"}
		self._probe(monkeypatch, set(), known=known)
		disabled = self._disabled(ai_fix._build_cli_argv("codex", "/s", "/s/out"))
		assert sorted(disabled) == sorted(known)

	def test_a_floor_name_listed_in_any_stage_is_still_passed(self, monkeypatch):
		# Listed but off (a removed or under-development feature): known, so disabled.
		self._probe(monkeypatch, set())
		assert sorted(ai_fix._cli_disabled_features("codex")) == sorted(ai_fix._CLI_DISABLED_FEATURES)

	@pytest.mark.parametrize("probe", [None, "empty", "garbage"])
	def test_a_failed_probe_refuses_the_run_and_passes_no_argv(self, monkeypatch, probe):
		if probe is None:
			self._probe(monkeypatch, None)
		else:
			monkeypatch.setattr(ai_fix, "_run_cli", lambda *a, **kw: (0, "" if probe == "empty" else "garbage\nUPPER  stable  true\n", ""))
			monkeypatch.setattr(ai_fix, "_codex_features", _REAL_FEATURES)
			ai_fix._FEATURES_CACHE.clear()
		with pytest.raises(ai_fix.AiFixError, match="refused for safety") as err:
			ai_fix._build_cli_argv("/x/codex", "/s", "/s/out")
		assert err.value.kind == "transport" and err.value.fatal is False

	def test_a_failed_probe_never_starts_the_exec(self, fake_codex, monkeypatch):
		box = fake_codex()
		monkeypatch.setattr(ai_fix, "_codex_features", lambda p, timeout=None: None)
		with pytest.raises(ai_fix.AiFixError, match="refused for safety"):
			ai_fix._call_local_cli("S", [{"content": "x"}])
		assert "exec" not in _read(box.rec, "calls.txt").replace("login status", "")

	def test_other_hardening_flags_are_kept_with_a_probe(self, monkeypatch):
		self._probe(monkeypatch, {"browser_use_external"})
		argv = ai_fix._build_cli_argv("codex", "/s", "/s/out")
		assert argv[argv.index("-c") + 1] == 'web_search="disabled"'
		assert argv[argv.index("--sandbox") + 1] == "read-only" and argv[-1] == "-"

	def test_features_listing_yields_known_and_on_names_including_dotted_ones(self):
		listing = (
			"browser_use_external                     stable             true\n"
			"code_mode_host_grpc                      under development  false\n"
			"fast_mode                                stable             true\n"
			"ns.dotted_tool                           stable             true\n"
			"\n"
		)
		known, on = ai_fix._parse_features(listing)
		assert on == {"browser_use_external", "fast_mode", "ns.dotted_tool"}
		assert known == on | {"code_mode_host_grpc"}

	def test_a_listing_with_no_feature_lines_parses_to_none_and_all_off_to_empty(self):
		assert ai_fix._parse_features("Feature Stage\nonlyone\n") is None
		known, on = ai_fix._parse_features("fast_mode stable false\n")
		assert known == {"fast_mode"} and on == frozenset()

	def test_probe_runs_features_list_without_auth_and_is_cached(self, monkeypatch):
		calls = []

		def fake_run(argv, env, cwd, timeout, stdin_text=None):
			calls.append((argv, timeout))
			return 0, "browser_use_external   stable   true\nfast_mode   stable   false\n", ""

		monkeypatch.setattr(ai_fix, "_run_cli", fake_run)
		expected = (frozenset({"browser_use_external", "fast_mode"}), frozenset({"browser_use_external"}))
		assert _REAL_FEATURES("/x/codex") == expected
		assert _REAL_FEATURES("/x/codex") == expected
		assert calls == [(["/x/codex", "features", "list"], ai_fix._FEATURES_PROBE_SECONDS)]

	def test_probe_timeout_is_capped_by_the_time_left(self, monkeypatch):
		seen = []
		monkeypatch.setattr(ai_fix, "_run_cli", lambda argv, env, cwd, timeout, stdin_text=None: (seen.append(timeout), (0, "a_b stable true\n", ""))[1])
		_REAL_FEATURES("/x/codex", 3)
		assert seen == [3]

	@pytest.mark.parametrize("outcome", ["raises", "exit1", "empty"])
	def test_probe_failure_is_none_and_never_raises(self, monkeypatch, outcome):
		monkeypatch.setattr(ai_fix, "_note_line", lambda line: None)

		def fake_run(*a, **kw):
			if outcome == "raises":
				raise subprocess.TimeoutExpired("codex", 10)
			return (1, "", "boom") if outcome == "exit1" else (0, "garbage\n", "")

		monkeypatch.setattr(ai_fix, "_run_cli", fake_run)
		assert _REAL_FEATURES("/x/codex") is None

	@pytest.mark.parametrize("name", ["daemon_auto_start", "skill_search", "realtime_conversation"])
	def test_capability_regex_covers_daemon_skill_and_realtime_features(self, name):
		assert ai_fix._CLI_CAPABILITY_RE.search(name)

	def test_capability_regex_leaves_plain_completion_features_alone(self):
		for name in ("fast_mode", "steer", "sqlite", "item_ids"):
			assert not ai_fix._CLI_CAPABILITY_RE.search(name)
		for name in (
			"browser_use_external", "code_mode_host", "unified_exec", "sandbox_bypass_x",
			"in_app_local_automation", "remote_control", "js_repl", "apply_patch_freeform", "memories",
		):
			assert ai_fix._CLI_CAPABILITY_RE.search(name), name

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


class _FakeJobTimeout(Exception):
	"""Stands in for rq's ``JobTimeoutException`` (an ``Exception``)."""


@pytest.fixture(params=["job_timeout", "system_exit"])
def interrupt(request, monkeypatch):
	"""An interrupt the guard must re-raise: an RQ-style job timeout (the type registered
	with the guard) or a non-Exception one (a gunicorn ``SystemExit``)."""
	monkeypatch.setattr(ai_fix, "_job_timeout_types", lambda: (_FakeJobTimeout,))
	return _FakeJobTimeout if request.param == "job_timeout" else SystemExit


class TestInterruptsAreNotSwallowed:
	def test_config_check_re_raises_an_interrupt(self, interrupt, monkeypatch):
		def boom():
			raise interrupt("worker timeout")

		monkeypatch.setattr(ai_fix, "_read_provider_snapshot", boom)
		with pytest.raises(interrupt):
			ai_fix._subscription_config_valid({"protocol": "subscription"})

	def test_config_check_still_fails_closed_on_a_plain_error(self, monkeypatch):
		def boom():
			raise RuntimeError("db down")

		monkeypatch.setattr(ai_fix, "_read_provider_snapshot", boom)
		assert ai_fix._subscription_config_valid({"protocol": "subscription"}) is False

	def test_features_probe_re_raises_an_interrupt_and_caches_nothing(self, interrupt, monkeypatch):
		def boom(*a, **kw):
			raise interrupt("worker timeout")

		monkeypatch.setattr(ai_fix, "_run_cli", boom)
		with pytest.raises(interrupt):
			_REAL_FEATURES("/x/codex")
		assert "/x/codex" not in ai_fix._FEATURES_CACHE

	def test_features_probe_failure_is_logged_without_stderr(self, monkeypatch):
		lines = []
		monkeypatch.setattr(ai_fix, "_note_line", lines.append)
		monkeypatch.setattr(ai_fix, "_run_cli", lambda *a, **kw: (1, "RAWCANARY out", "RAWCANARY err"))
		assert _REAL_FEATURES("/x/codex") is None
		assert lines == ["codex cli: features_probe_failed"]

	def test_diagnostic_carries_only_the_whitelisted_fields(self, monkeypatch):
		lines = []
		monkeypatch.setattr(ai_fix, "_note_line", lines.append)
		ai_fix._log_cli_diagnostic("exec_failed", version="codex-cli 1.2.3", label="network", code=3)
		ai_fix._log_cli_diagnostic("features_probe_failed")
		assert lines == ["codex cli: exec_failed version=codex-cli 1.2.3 label=network code=3", "codex cli: features_probe_failed"]

	def test_features_probe_failure_is_remembered_as_none(self, monkeypatch):
		def boom(*a, **kw):
			raise RuntimeError("spawn failed")

		monkeypatch.setattr(ai_fix, "_run_cli", boom)
		assert _REAL_FEATURES("/x/codex") is None
		assert ai_fix._FEATURES_CACHE["/x/codex"][1] is None


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


class TestConcurrencyAndFollowUps:
	def _call(self, **kw):
		return ai_fix._call_local_cli("SYS", [{"role": "user", "content": "hello"}], **kw)

	def test_concurrent_calls_run_together_not_one_after_the_other(self, fake_codex):
		import threading
		box = fake_codex()
		order = f"{box.rec}/order.txt"
		# Each run waits (up to 10s) for the other to have started before it ends.
		fake_codex(exec_body=(
			f'echo start >> "{order}"; i=0; while [ "$(wc -l < "{order}")" -lt 2 ] && [ $i -lt 100 ]; do sleep 0.1; i=$((i+1)); done; '
			f'echo end >> "{order}"; echo REPLY > "$out"'
		))
		errors = []

		def run():
			try:
				self._call(timeout=30)
			except Exception as e:
				errors.append(e)

		threads = [threading.Thread(target=run) for _ in range(2)]
		for t in threads:
			t.start()
		for t in threads:
			t.join(30)
		assert errors == []
		assert _read(box.rec, "order.txt").split() == ["start", "start", "end", "end"]

	def test_a_truncated_reply_sets_the_length_finish_reason(self, fake_codex):
		fake_codex(exec_body='head -c 25000 /dev/zero | tr "\\000" "a" > "$out"')
		meta = {}
		text = self._call(meta_out=meta)
		assert text.endswith("(truncated)") and meta["finish_reason"] == "length"

	def test_a_complete_reply_leaves_the_finish_reason_alone(self, fake_codex):
		fake_codex()
		meta = {}
		self._call(meta_out=meta)
		assert "finish_reason" not in meta

	def _reask_run(self, monkeypatch, provider):
		from optimus.tests.test_ai_fix import TestGuardedCompletion as _Replies
		sent = []

		def dispatch(*args, **kwargs):
			sent.append(args)
			kwargs["meta_out"]["finish_reason"] = "stop"
			return _Replies._RAW

		monkeypatch.setattr(ai_fix, "_dispatch_call", dispatch)
		monkeypatch.setattr(ai_fix, "_reask_enabled", lambda: True)
		shown = [line["content"] for line in _Replies._FINDING["source_window"]]
		result = ai_fix._complete_with_guardrails(
			{**_Replies._PROVIDER, **provider}, "system", [], shown_lines=shown,
			usage={}, started_at=time.monotonic(), timeout=60,
		)
		return sent, result[1]

	def test_the_guardrail_re_ask_is_skipped_for_the_cli(self, monkeypatch):
		sent, guardrail = self._reask_run(monkeypatch, {"protocol": "subscription"})
		assert len(sent) == 1 and guardrail["reasked"] is False

	def test_the_re_ask_still_runs_for_an_http_provider(self, monkeypatch):
		sent, _ = self._reask_run(monkeypatch, {})
		assert len(sent) == 2

	def test_a_descendant_that_keeps_the_pipe_open_does_not_hang_the_call(self, tmp_path):
		script = tmp_path / "leak.sh"
		script.write_text("#!/bin/sh\n(setsid sleep 8 &)\necho hi\n")
		script.chmod(0o755)
		started = time.monotonic()
		code, out, _ = ai_fix._run_cli([str(script)], {"PATH": os.environ["PATH"]}, str(tmp_path), 20)
		assert code == 0 and out.strip() == "hi" and time.monotonic() - started < 15

	def test_a_spawn_error_is_logged_with_its_errno(self, fake_codex, monkeypatch):
		fake_codex()
		lines = []
		monkeypatch.setattr(ai_fix, "_note_line", lines.append)

		def run(argv, *a, **kw):
			if "exec" in argv:
				raise OSError(8, "Exec format error")
			return 0, "Logged in using ChatGPT", ""

		monkeypatch.setattr(ai_fix, "_run_cli", run)
		with pytest.raises(ai_fix.AiFixError, match="could not be started"):
			self._call()
		assert any("exec_spawn_failed label=errno_8" in line for line in lines)

	def test_a_non_zero_exit_with_no_recognised_cause_is_sent_unknown_and_logged(self, fake_codex, monkeypatch):
		fake_codex(exec_body="echo 'panicked at main.rs' >&2; exit 101")
		lines = []
		monkeypatch.setattr(ai_fix, "_note_line", lines.append)
		with ai_transport.attempt() as transport:
			with pytest.raises(ai_fix.AiFixError):
				self._call()
		assert transport.certainty == "sent_unknown"
		assert any("exec_failed" in line and "label=other code=101" in line for line in lines)
