# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the Claude CLI subscription backend in ``ai_fix``.

The real ``claude`` is never run with a prompt. Process-level tests point ``ai_cli_path`` at a
small shell script named ``claude`` that records what the child saw (argv, environment, working
files, stdin) and answers like the CLI would: ``auth status`` prints the status JSON and
``-p --output-format json`` prints the result envelope on stdout.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import time
from types import SimpleNamespace

import pytest

from optimus import ai_fix, ai_transport

_FAKE_CLAUDE = """#!/bin/sh
R="{rec}"
echo "$1 $2" >> "$R/calls.txt"
env > "$R/env_$1.txt"
echo "$PWD" > "$R/pwd_$1.txt"
echo "$HOME" > "$R/home_$1.txt"
ls -ld "$HOME" | cut -c1-10 > "$R/homemode_$1.txt"
echo "$CLAUDE_CONFIG_DIR" > "$R/configdir_$1.txt"
echo "$1 $CLAUDE_CONFIG_DIR" >> "$R/configdir_log.txt"
ls -A "$CLAUDE_CONFIG_DIR" > "$R/configdir_ls_$1.txt" 2>&1
case "$1" in
	auth) {auth} ;;
	--version) echo "2.1.316 (Claude Code)" ;;
	-p)
		printf '%s\\n' "$@" > "$R/argv.txt"
		cat > "$R/stdin.txt"
		{exec_body}
		;;
esac
"""

_STATUS_OK = (
	'{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty", '
	'"email": "user@example.com", "orgId": "org-123", "orgName": "Acme Org", "subscriptionType": "max"}'
)


def _result(text="REPLY", **extra):
	return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text, **extra})


def _cfg(**kw):
	base = {
		"ai_enabled": True,
		"ai_connection_type": "Subscription (local CLI)",
		"ai_subscription_cli": "Claude CLI",
		"ai_cli_path": "",
		"ai_request_timeout_seconds": 45,
		"ai_suggest_findings": True,
		"ai_suggest_indexes": True,
		"ai_humanize_steps": True,
	}
	base.update(kw)
	return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _clear_caches():
	ai_fix._LOGIN_CACHE.clear()
	ai_fix._VERSION_CACHE.clear()
	yield
	ai_fix._LOGIN_CACHE.clear()
	ai_fix._VERSION_CACHE.clear()


@pytest.fixture
def sub_cfg(monkeypatch):
	"""Subscription mode on with the Claude CLI picked, and the dev-bench guard open."""
	cfg = _cfg()
	monkeypatch.setattr("optimus.settings.get_config", lambda: cfg)
	monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: True)
	monkeypatch.setattr(ai_fix, "_read_provider_snapshot", lambda: (cfg, None))
	return cfg


@pytest.fixture(autouse=True)
def operator_claude(tmp_path, monkeypatch):
	"""Keep every test off the developer's real ``~/.claude``: HOME is a temp dir."""
	home = tmp_path / "bench_home"
	home.mkdir()
	monkeypatch.setenv("HOME", str(home))
	for name in ("CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
		monkeypatch.delenv(name, raising=False)
	return SimpleNamespace(path=home / ".claude")


@pytest.fixture
def real_login(operator_claude):
	"""The operator's own ``~/.claude`` after a terminal sign-in; returns its credential file."""
	operator_claude.path.mkdir(parents=True, mode=0o700)
	cred = operator_claude.path / ".credentials.json"
	cred.write_text(json.dumps({"claudeAiOauth": {"refreshToken": "ORIGINAL"}}))
	return cred


@pytest.fixture
def fake_claude(tmp_path, sub_cfg, real_login):
	"""Build a fake ``claude`` script and point the settings at it."""
	rec = tmp_path / "rec"
	rec.mkdir()
	(tmp_path / "bin").mkdir()
	sub_cfg.ai_cli_path = str(tmp_path / "bin" / "claude")

	def build(auth=None, exec_body=None):
		auth = f"echo '{_STATUS_OK}'" if auth is None else auth
		exec_body = f"echo '{_result()}'" if exec_body is None else exec_body
		path = tmp_path / "bin" / "claude"
		path.write_text(_FAKE_CLAUDE.format(rec=rec, auth=auth, exec_body=exec_body))
		path.chmod(0o755)
		return SimpleNamespace(rec=rec, path=path)

	return build


def _read(rec, name):
	return (rec / name).read_text()


def _call(system="SYS", content="hello", **kw):
	return ai_fix._call_local_cli(system, [{"role": "user", "content": content}], **kw)


def _settings_fields():
	path = os.path.join(
		os.path.dirname(__file__), "..", "optimus", "doctype", "optimus_settings", "optimus_settings.json"
	)
	with open(path, encoding="utf-8") as f:
		return {d["fieldname"]: d for d in json.load(f)["fields"]}


# ---------------------------------------------------------------------------
# B1: the discriminator and the staleness identity
# ---------------------------------------------------------------------------

class TestDiscriminator:
	def test_the_claude_choice_resolves_a_claude_subscription_provider(self, sub_cfg):
		p = ai_fix._provider_config()
		assert p["protocol"] == "subscription" and p["backend"] == "claude"
		assert p["name"] == "Claude CLI" and p["base_url"] == "cli://claude"
		assert p["needs_key"] is False and p["model"]
		assert "Claude CLI" not in ai_fix._PROVIDER_DEFAULTS

	def test_the_codex_choice_still_resolves_the_codex_provider(self, sub_cfg):
		sub_cfg.ai_subscription_cli = "Codex CLI"
		p = ai_fix._provider_config()
		assert (p["backend"], p["name"], p["base_url"], p["model"]) == ("codex", "Codex CLI", "cli://codex", "codex")

	def test_switching_the_field_flips_the_provider_identity(self, sub_cfg):
		claude = ai_fix._provider_config()
		sub_cfg.ai_subscription_cli = "Codex CLI"
		assert ai_fix._provider_config() != claude

	def test_a_queued_job_is_refused_once_the_saved_cli_changed(self, sub_cfg, monkeypatch):
		provider = ai_fix._provider_config()
		monkeypatch.setattr(ai_fix, "_call_local_cli", lambda *a, **kw: pytest.fail("a stale job ran"))
		monkeypatch.setattr(ai_fix, "_read_provider_snapshot", lambda: (_cfg(ai_subscription_cli="Codex CLI"), None))
		assert ai_fix._subscription_config_valid(provider) is False
		with pytest.raises(ai_fix.AiFixError, match="AI configuration changed") as err:
			ai_fix._dispatch_call(provider, "S", [{"content": "q"}], usage_out=None)
		assert err.value.kind == "config" and err.value.fatal is True

	def test_a_snapshot_that_still_says_claude_is_valid(self, sub_cfg):
		provider = ai_fix._provider_config()
		assert ai_fix._subscription_config_valid(provider) is True

	def test_a_never_saved_field_resolves_equal_on_the_live_and_snapshot_sides(self):
		# The snapshot is built from the Singles rows only, so a field nobody ever saved is absent
		# from it. Both sides must then fall back to the same default, or every queued job would
		# abort as "configuration changed".
		live = _cfg(ai_subscription_cli="Codex CLI")
		snapshot = _cfg()
		del snapshot.ai_subscription_cli
		assert ai_fix._provider_config(live) == ai_fix._provider_config(snapshot)
		assert ai_fix._provider_config(snapshot)["backend"] == "codex"

	def test_an_unknown_choice_is_a_config_error_not_a_silent_codex_run(self, sub_cfg):
		sub_cfg.ai_subscription_cli = "Gemini CLI"
		with pytest.raises(ai_fix.AiFixError) as err:
			ai_fix._provider_config()
		assert err.value.kind == "config" and "Gemini CLI" in str(err.value)

	def test_api_mode_is_unchanged_by_the_field(self, monkeypatch):
		monkeypatch.setattr(
			"optimus.settings.get_config",
			lambda: _cfg(ai_connection_type="API Key", ai_provider="OpenAI", ai_subscription_cli="Claude CLI"),
		)
		monkeypatch.setattr("frappe.conf", {}, raising=False)
		p = ai_fix._provider_config()
		assert p["protocol"] == "openai" and "backend" not in p


class TestSnapshotQuery:
	def _capture(self, monkeypatch):
		import frappe

		seen = {}

		def multisql(queries_by_backend, values):
			seen["query"], seen["values"] = queries_by_backend["mariadb"], values
			return []

		monkeypatch.setattr(frappe, "db", SimpleNamespace(multisql=multisql), raising=False)
		ai_fix._read_provider_snapshot()
		return seen

	def test_the_select_binds_one_value_per_placeholder_and_reads_the_cli_choice(self, monkeypatch):
		seen = self._capture(monkeypatch)
		assert seen["query"].count("%s") == len(seen["values"])
		assert "ai_subscription_cli" in seen["values"]
		# Three values bind the credential join, one the doctype; the rest are the field list.
		fields = seen["values"][4:]
		assert seen["query"].split("s.field IN (")[1].count("%s") == len(fields)
		assert {"ai_enabled", "ai_connection_type", "ai_provider", "ai_base_url", "ai_model",
			"ai_context_tokens", "ai_subscription_cli"} == set(fields)


class TestSettingsOptions:
	def test_the_select_options_match_the_backend_table_exactly(self):
		options = _settings_fields()["ai_subscription_cli"]["options"].split("\n")
		assert options == ["Codex CLI", "Claude CLI"]
		assert options == list(ai_fix._CLI_BACKENDS)
		assert _settings_fields()["ai_subscription_cli"]["default"] == "Codex CLI"

	def test_every_backend_display_name_is_its_option_label(self):
		for label, backend in ai_fix._CLI_BACKENDS.items():
			assert backend.display == label


# ---------------------------------------------------------------------------
# B3: installed_check (native and npm layouts)
# ---------------------------------------------------------------------------

def _exe(path, mode=0o755):
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text("#!/bin/sh\n")
	path.chmod(mode)
	return path


class TestInstalledCheck:
	def _native(self, root, version="2.1.316"):
		target = _exe(root / ".local" / "share" / "claude" / "versions" / version)
		link = root / ".local" / "bin" / "claude"
		link.parent.mkdir(parents=True)
		link.symlink_to(target)
		return link, target

	def test_the_native_install_a_claude_symlink_to_versions_semver_is_accepted(self, tmp_path):
		link, target = self._native(tmp_path)
		assert ai_fix._cli_installed(_cfg(ai_cli_path=str(link))) == os.path.realpath(target)

	def test_the_native_install_is_found_on_path_too(self, tmp_path, monkeypatch):
		link, target = self._native(tmp_path)
		seen = []
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: seen.append(name) or str(link))
		assert ai_fix._cli_installed(_cfg()) == os.path.realpath(target)
		assert seen == ["claude"]

	def test_the_npm_install_a_claude_symlink_to_cli_js_is_accepted(self, tmp_path):
		entry = _exe(tmp_path / "lib" / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js")
		link = tmp_path / "bin" / "claude"
		link.parent.mkdir()
		link.symlink_to(entry)
		assert ai_fix._cli_installed(_cfg(ai_cli_path=str(link))) == os.path.realpath(entry)

	def test_a_plain_claude_launcher_is_accepted(self, tmp_path):
		exe = _exe(tmp_path / "claude")
		assert ai_fix._cli_installed(_cfg(ai_cli_path=str(exe))) == os.path.realpath(exe)

	def test_a_claude_symlink_to_a_shell_is_rejected(self, tmp_path):
		link = tmp_path / "claude"
		link.symlink_to("/bin/sh")
		assert ai_fix._cli_resolve(_cfg(ai_cli_path=str(link))) == (None, "entrypoint")

	def test_a_versions_look_alike_outside_the_claude_install_dir_is_rejected(self, tmp_path):
		target = _exe(tmp_path / "other" / "versions" / "2.1.316")
		link = tmp_path / "claude"
		link.symlink_to(target)
		assert ai_fix._cli_resolve(_cfg(ai_cli_path=str(link))) == (None, "entrypoint")

	def test_a_non_version_file_under_versions_is_rejected(self, tmp_path):
		target = _exe(tmp_path / "claude" / "versions" / "evil")
		link = tmp_path / "bin" / "claude"
		link.parent.mkdir()
		link.symlink_to(target)
		assert ai_fix._cli_resolve(_cfg(ai_cli_path=str(link))) == (None, "entrypoint")

	@pytest.mark.parametrize("mode", [0o775, 0o757, 0o777])
	def test_a_writable_native_target_is_rejected_as_untrusted(self, tmp_path, mode):
		link, target = self._native(tmp_path)
		target.chmod(mode)
		assert ai_fix._cli_resolve(_cfg(ai_cli_path=str(link))) == (None, "untrusted")

	def test_a_target_owned_by_someone_else_is_rejected(self, tmp_path, monkeypatch):
		link, target = self._native(tmp_path)
		monkeypatch.setattr(ai_fix.os, "geteuid", lambda: os.stat(target).st_uid + 12345)
		assert ai_fix._cli_resolve(_cfg(ai_cli_path=str(link))) == (None, "untrusted")

	@pytest.mark.parametrize("path,reason", [("claude", "relative"), ("/opt/bin/codex", "name"), ("/bin/sh", "name")])
	def test_reasons_for_a_bad_configured_path(self, path, reason):
		assert ai_fix._cli_resolve(_cfg(ai_cli_path=path)) == (None, reason)

	def test_nothing_on_path_is_not_found(self, monkeypatch):
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: None)
		assert ai_fix._cli_resolve(_cfg()) == (None, "not_found")

	def test_every_reason_has_its_own_message_that_names_the_claude_cli(self):
		claude = ai_fix._CLI_BACKENDS["Claude CLI"]
		texts = {ai_fix._cli_missing_text(r, claude) for r in ("not_found", "relative", "name", "entrypoint", "untrusted")}
		assert len(texts) == 5
		assert all("Claude" in t or "`claude`" in t for t in texts)
		assert "codex" not in " ".join(texts).lower()

	def test_the_call_and_the_status_show_the_reason(self, sub_cfg):
		sub_cfg.ai_cli_path = "claude"
		with pytest.raises(ai_fix.AiFixError, match="absolute") as err:
			_call()
		assert err.value.kind == "config"
		status = ai_fix._cli_session_status()
		assert "absolute" in status["message"] and status["cli"] == "claude"

	def test_the_codex_choice_still_looks_for_codex(self, tmp_path, monkeypatch):
		seen = []
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: seen.append(name))
		ai_fix._cli_installed(_cfg(ai_subscription_cli="Codex CLI"))
		assert seen == ["codex"]


# ---------------------------------------------------------------------------
# B3: sandbox and environment (real ~/.claude only for the status check and the exec)
# ---------------------------------------------------------------------------

_LEAKING = {
	"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-ant-1", "ANTHROPIC_AUTH_TOKEN": "tok", "ANTHROPIC_BASE_URL": "http://y",
	"CLAUDE_CODE_OAUTH_TOKEN": "oauth", "CLAUDE_CONFIG_DIR": "/elsewhere", "OPENAI_API_KEY": "sk-1",
	"CODEX_API_KEY": "sk-2", "AWS_SECRET_ACCESS_KEY": "a", "GITHUB_TOKEN": "g", "CLAUDE_CODE_USE_BEDROCK": "1",
}


class TestSandbox:
	def test_a_real_login_sandbox_points_the_cli_at_the_real_claude_dir_with_a_scratch_home(
		self, real_login, operator_claude,
	):
		claude = ai_fix._CLI_BACKENDS["Claude CLI"]
		with ai_fix._cli_sandbox(real_login=True, backend=claude) as (work, env):
			root = os.path.dirname(work)
			assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
			assert env["CLAUDE_CONFIG_DIR"] == os.path.expanduser("~/.claude") == str(operator_claude.path)
			assert os.listdir(env["HOME"]) == []
			assert env["HOME"] != os.path.expanduser("~") and env["HOME"].startswith(root)
			assert stat.S_IMODE(os.stat(env["HOME"]).st_mode) == 0o700
			assert env["TMPDIR"].startswith(root)
			assert "CODEX_HOME" not in env
		assert not os.path.exists(root)
		assert real_login.exists()  # the operator's login outlives the run

	def test_a_terminal_claude_config_dir_is_ignored_for_the_real_login(self, monkeypatch, real_login):
		monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/elsewhere")
		with ai_fix._cli_sandbox(real_login=True, backend=ai_fix._CLI_BACKENDS["Claude CLI"]) as (_w, env):
			assert env["CLAUDE_CONFIG_DIR"] == str(real_login.parent)

	def test_a_probe_sandbox_gets_an_empty_scratch_config_dir_not_the_real_one(self, real_login):
		with ai_fix._cli_sandbox() as (work, env):
			root = os.path.dirname(work)
			assert env["CLAUDE_CONFIG_DIR"].startswith(root + os.sep) and env["CLAUDE_CONFIG_DIR"] != str(real_login.parent)
			assert os.listdir(env["CLAUDE_CONFIG_DIR"]) == []
			assert stat.S_IMODE(os.stat(env["CLAUDE_CONFIG_DIR"]).st_mode) == 0o700

	def test_the_sandbox_neither_creates_nor_tightens_the_operators_claude_dir(self, operator_claude):
		claude = ai_fix._CLI_BACKENDS["Claude CLI"]
		with ai_fix._cli_sandbox(real_login=True, backend=claude):
			assert not operator_claude.path.exists()
		operator_claude.path.mkdir(mode=0o755)
		os.chmod(operator_claude.path, 0o755)
		with ai_fix._cli_sandbox(real_login=True, backend=claude):
			pass
		assert stat.S_IMODE(os.stat(operator_claude.path).st_mode) == 0o755

	def test_a_relative_home_is_refused_with_a_config_error(self, monkeypatch):
		monkeypatch.setattr(ai_fix.os.path, "expanduser", lambda p: p)
		with pytest.raises(ai_fix.AiFixError, match="no home directory") as err:
			with ai_fix._cli_sandbox(real_login=True, backend=ai_fix._CLI_BACKENDS["Claude CLI"]):
				pytest.fail("the sandbox must not open")
		assert err.value.kind == "config" and "Claude login" in str(err.value)
		with ai_fix._cli_sandbox():  # a probe never needs the real home
			pass

	def test_the_login_check_and_connect_report_a_relative_home_as_config_not_signed_out(self, fake_claude, monkeypatch):
		box = fake_claude()
		monkeypatch.setattr(ai_fix.os.path, "expanduser", lambda p: p)
		with pytest.raises(ai_fix.AiFixError, match="no home directory") as err:
			ai_fix._claude_login_ok(str(box.path), use_cache=False)
		assert err.value.kind == "config"
		status = ai_fix._cli_session_status()
		assert status["state"] == "error" and "no home directory" in status["message"]
		assert "auth" not in (box.rec / "calls.txt").read_text()  # the login check never started

	def test_anthropic_and_provider_secrets_never_reach_the_child_env(self):
		env = ai_fix._build_cli_env(_LEAKING, home="/h", tmpdir="/t", extra={"CLAUDE_CONFIG_DIR": "/real/.claude"})
		assert set(env) == {"PATH", "HOME", "TMPDIR", "CLAUDE_CONFIG_DIR"}
		assert env["CLAUDE_CONFIG_DIR"] == "/real/.claude"

	def test_the_claude_telemetry_and_autoupdate_switches_are_set_on_every_child(self):
		extra = ai_fix._CLI_BACKENDS["Claude CLI"].extra_env
		assert extra == {
			"DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
		}
		with ai_fix._cli_sandbox(real_login=True, backend=ai_fix._CLI_BACKENDS["Claude CLI"]) as (_w, env):
			assert all(env[name] == "1" for name in extra)
		with ai_fix._cli_sandbox() as (_w, env):  # the version probe too
			assert all(env[name] == "1" for name in extra)

	def test_the_codex_exec_env_gets_none_of_the_claude_switches(self, real_login):
		(real_login.parent.parent / ".codex").mkdir()
		with ai_fix._cli_sandbox(real_login=True) as (_w, env):
			assert "DISABLE_TELEMETRY" not in env and "CLAUDE_CONFIG_DIR" not in env
			assert env["CODEX_HOME"] == os.path.expanduser("~/.codex")


# ---------------------------------------------------------------------------
# B3: argv
# ---------------------------------------------------------------------------

class TestArgv:
	CLAUDE = None

	@pytest.fixture(autouse=True)
	def _claude(self):
		self.claude = ai_fix._CLI_BACKENDS["Claude CLI"]

	def _argv(self):
		return self.claude.build_argv("/x/claude", "/s", "/s/out")

	def test_the_isolation_flags_are_exactly_the_planned_ones(self):
		assert self._argv() == [
			"/x/claude", "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config", "--safe-mode",
			"--restricted", "--no-session-persistence", "--permission-prompts", "none",
		]

	def test_never_bare_bypass_budget_or_a_model_by_default(self):
		joined = " ".join(self._argv())
		for banned in ("--bare", "bypassPermissions", "--max-budget-usd", "--model", "--dangerously-skip-permissions",
			"--permission-mode"):
			assert banned not in joined
		assert "claude" not in self._argv()[1:]  # the sentinel model is never an argument

	def test_no_shell_metacharacters_and_no_prompt_on_argv(self):
		joined = " ".join(self._argv())
		assert "`" not in joined and "$(" not in joined and ";" not in joined

	@pytest.mark.parametrize("alias", ["opus", "sonnet", "haiku", "fable", "claude-sonnet-4-5"])
	def test_a_real_operator_alias_is_passed_as_model(self, monkeypatch, alias):
		monkeypatch.setattr("frappe.conf", {"optimus_claude_cli_model": alias}, raising=False)
		argv = self._argv()
		assert argv[argv.index("--model") + 1] == alias

	@pytest.mark.parametrize("alias", ["claude", "Claude", "", "  ", "--evil", "-p", "a b", "sonnet;ls", "x" * 100, 5, None])
	def test_a_sentinel_or_malformed_alias_is_dropped(self, monkeypatch, alias):
		monkeypatch.setattr("frappe.conf", {"optimus_claude_cli_model": alias}, raising=False)
		assert "--model" not in self._argv()

	def test_an_unreadable_site_config_means_no_model(self, monkeypatch):
		monkeypatch.delattr("frappe.conf", raising=False)
		assert "--model" not in self._argv()

	def test_the_probe_budget_is_ignored_because_claude_has_no_features_probe(self):
		assert self.claude.build_argv("/x/claude", "/s", "/s/out", probe_seconds=0) == self._argv()


# ---------------------------------------------------------------------------
# B3: login check (`claude auth status` against the real ~/.claude)
# ---------------------------------------------------------------------------

def _status(**kw):
	base = json.loads(_STATUS_OK)
	base.update(kw)
	return "echo '" + json.dumps(base) + "'"


class TestLoginCheck:
	def test_a_subscription_login_is_connected(self, fake_claude):
		box = fake_claude()
		assert ai_fix._claude_login_ok(str(box.path), use_cache=False) == (True, "ok")

	def test_the_status_check_runs_against_the_real_claude_dir_and_only_reads(self, fake_claude, real_login):
		box = fake_claude()
		before = real_login.read_bytes(), real_login.stat().st_mtime_ns
		ai_fix._claude_login_ok(str(box.path), use_cache=False)
		assert _read(box.rec, "configdir_auth.txt").strip() == os.path.expanduser("~/.claude")
		assert _read(box.rec, "configdir_ls_auth.txt").split() == [".credentials.json"]
		assert _read(box.rec, "calls.txt").splitlines() == ["auth status"]
		assert (real_login.read_bytes(), real_login.stat().st_mtime_ns) == before

	def test_logged_out_is_signed_out_even_though_the_cli_exits_one(self, fake_claude):
		box = fake_claude(auth='echo \'{"loggedIn": false, "authMethod": "none"}\'; exit 1')
		assert ai_fix._claude_login_ok(str(box.path), use_cache=False) == (False, "signed_out")

	@pytest.mark.parametrize("method", ["apiKey", "api_key", "oauth_token", "none", ""])
	def test_any_login_that_is_not_the_claude_ai_subscription_is_rejected(self, fake_claude, method):
		box = fake_claude(auth=_status(authMethod=method))
		assert ai_fix._claude_login_ok(str(box.path), use_cache=False) == (False, "not_subscription")

	def test_an_api_key_login_is_told_to_use_the_api_key_connection(self, fake_claude):
		fake_claude(auth=_status(authMethod="apiKey"))
		status = ai_fix._cli_session_status()
		assert status["state"] == "signed_out" and status["ok"] is False
		assert "Connection = API Key" in status["message"]
		with pytest.raises(ai_fix.AiFixError, match="Connection = API Key") as err:
			_call()
		assert err.value.kind == "auth" and err.value.fatal is True

	def test_a_missing_claude_dir_is_signed_out_without_running_the_cli(self, fake_claude, operator_claude):
		box = fake_claude()
		shutil.rmtree(operator_claude.path)
		assert ai_fix._claude_login_ok(str(box.path), use_cache=False) == (False, "signed_out")
		assert not (box.rec / "calls.txt").exists()  # the precheck answered; no process was started
		assert not operator_claude.path.exists()  # and nothing was created

	def test_a_claude_path_that_is_a_file_is_signed_out(self, fake_claude, operator_claude):
		box = fake_claude()
		shutil.rmtree(operator_claude.path)
		operator_claude.path.write_text("not a folder")
		assert ai_fix._claude_login_ok(str(box.path), use_cache=False) == (False, "signed_out")
		assert not (box.rec / "calls.txt").exists()

	@pytest.mark.parametrize("auth", [
		"exit 127", "exit 126", "exit 2", "exit 137", "kill -9 $$", "kill -11 $$",
		"echo 'not json at all'", "echo '[]'", "echo '{}'", "echo '{\"loggedIn\": \"yes\"}'", ": # prints nothing",
	])
	def test_an_unreadable_or_crashed_check_is_an_error_not_a_sign_out(self, fake_claude, auth):
		box = fake_claude(auth=auth)
		assert ai_fix._claude_login_ok(str(box.path), use_cache=False) == (False, "error")
		assert str(box.path) not in ai_fix._LOGIN_CACHE

	def test_a_slow_check_is_a_timeout_and_its_process_group_is_killed(self, fake_claude, monkeypatch):
		box = fake_claude()
		fake_claude(auth=f'sleep 30 & echo $! > "{box.rec}/probe.pid"; wait')
		monkeypatch.setattr(ai_fix, "_LOGIN_PROBE_SECONDS", 1)
		assert ai_fix._claude_login_ok(str(box.path), use_cache=False) == (False, "timeout")
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

	def test_a_success_is_memoized_for_thirty_seconds_and_a_failure_never_is(self, fake_claude, monkeypatch):
		box = fake_claude()
		path = str(box.path)
		clock = {"now": 1000.0}
		monkeypatch.setattr(ai_fix.time, "monotonic", lambda: clock["now"])
		assert ai_fix._claude_login_ok(path) == (True, "ok")
		clock["now"] += 29
		assert ai_fix._claude_login_ok(path) == (True, "ok")
		assert _read(box.rec, "calls.txt").count("auth status") == 1
		assert ai_fix._claude_login_ok(path, use_cache=False) == (True, "ok")
		assert _read(box.rec, "calls.txt").count("auth status") == 2
		clock["now"] += 31
		ai_fix._claude_login_ok(path)
		assert _read(box.rec, "calls.txt").count("auth status") == 3
		ai_fix._LOGIN_CACHE.clear()
		fake_claude(auth='echo \'{"loggedIn": false, "authMethod": "none"}\'; exit 1')
		assert ai_fix._claude_login_ok(path) == (False, "signed_out")
		assert path not in ai_fix._LOGIN_CACHE

	def test_the_signed_out_diagnostics_are_fixed_labels_with_the_claude_prefix(self, fake_claude, operator_claude, monkeypatch):
		lines = []
		monkeypatch.setattr(ai_fix, "_note_line", lines.append)
		box = fake_claude(auth='echo \'{"loggedIn": false, "authMethod": "none"}\'; exit 1')
		ai_fix._claude_login_ok(str(box.path), use_cache=False)
		shutil.rmtree(operator_claude.path)
		ai_fix._claude_login_ok(str(box.path), use_cache=False)
		assert lines == ["claude cli: signed_out label=claude_dir_present", "claude cli: signed_out label=claude_dir_missing"]

	def test_the_dispatch_uses_the_claude_check_for_the_claude_choice(self, fake_claude):
		box = fake_claude()
		_call()
		assert "auth status" in _read(box.rec, "calls.txt")
		assert "login" not in _read(box.rec, "calls.txt")


class TestStatusIsPiiFree:
	PII = ("user@example.com", "org-123", "Acme Org", "max", "subscriptionType", "orgName", "orgId", "email")

	def test_connected_status_carries_no_account_detail_even_when_the_cli_prints_it(self, fake_claude):
		fake_claude()
		status = ai_fix._cli_session_status()
		assert status["state"] == "connected" and status["ok"] is True
		assert set(status) == {"ok", "state", "message", "cli", "version"}
		assert status["cli"] == "claude" and status["version"] == "2.1.316 Claude Code"
		for pii in self.PII:
			assert pii not in str(status), pii

	def test_not_subscription_and_signed_out_statuses_carry_no_account_detail(self, fake_claude):
		fake_claude(auth=_status(authMethod="apiKey"))
		text = str(ai_fix._cli_session_status())
		fake_claude(auth=_status(loggedIn=False, authMethod="none") + "; exit 1")
		ai_fix._LOGIN_CACHE.clear()
		text += str(ai_fix._cli_session_status())
		for pii in self.PII[:4]:
			assert pii not in text, pii

	def test_no_diagnostic_line_carries_account_detail(self, fake_claude, monkeypatch):
		lines = []
		monkeypatch.setattr(ai_fix, "_note_line", lines.append)
		fake_claude(auth=_status(authMethod="apiKey"))
		ai_fix._cli_session_status()
		fake_claude(auth=_status(loggedIn=False) + "; exit 1")
		ai_fix._cli_session_status()
		assert lines and all(line.startswith("claude cli: ") for line in lines)
		for pii in self.PII[:4]:
			assert pii not in " ".join(lines), pii

	def test_the_stored_connect_message_is_the_status_message_only(self, fake_claude):
		fake_claude()
		message = ai_fix._cli_session_status()["message"]
		assert message.startswith("Connected. Claude CLI") and "@" not in message

	def test_only_the_two_fields_are_read(self):
		import inspect

		src = inspect.getsource(ai_fix._claude_login_state)
		for field in ("email", "orgId", "orgName", "subscriptionType"):
			assert field not in src
		assert ai_fix._claude_login_state(_STATUS_OK) == (True, "claude.ai")


class TestSessionStatus:
	def test_blocked_when_the_guard_is_off(self, sub_cfg, monkeypatch):
		monkeypatch.setattr(ai_fix, "_subscription_allowed", lambda: False)
		status = ai_fix._cli_session_status()
		assert status["state"] == "blocked" and status["cli"] == "claude"

	def test_missing_when_the_cli_is_not_found_and_the_message_names_claude(self, sub_cfg, monkeypatch):
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: None)
		status = ai_fix._cli_session_status()
		assert status["state"] == "missing" and "Claude CLI not found" in status["message"]

	def test_the_not_signed_in_message_says_claude_in_your_terminal_and_never_codex(self, fake_claude, operator_claude):
		fake_claude()
		shutil.rmtree(operator_claude.path)
		status = ai_fix._cli_session_status()
		assert status["state"] == "signed_out" and status["ok"] is False
		assert "`claude`" in status["message"] and "terminal" in status["message"]
		assert "codex" not in status["message"].lower() and "CLAUDE_CONFIG_DIR" not in status["message"]

	def test_no_untested_version_warning_for_claude(self, fake_claude, monkeypatch):
		fake_claude()
		monkeypatch.setattr(ai_fix, "_cli_version_cached", lambda p, probe=True: "9.9.9 (Claude Code)")
		assert "outside the range" not in ai_fix._cli_session_status()["message"]

	def test_an_unknown_choice_is_an_error_status(self, sub_cfg):
		sub_cfg.ai_subscription_cli = "Gemini CLI"
		status = ai_fix._cli_session_status()
		assert status["state"] == "error" and "Gemini CLI" in status["message"]

	def test_test_connection_checks_status_and_never_runs_exec(self, sub_cfg, monkeypatch):
		monkeypatch.setattr(ai_fix, "_call_local_cli", lambda *a, **kw: pytest.fail("exec in a status check"))
		monkeypatch.setattr(ai_fix, "_cli_session_status", lambda: {"ok": True, "message": "Connected."})
		assert ai_fix.test_connection() == {"ok": True, "message": "Connected.", "model": "claude"}

	def test_suggest_fix_runs_through_the_cli_and_names_it(self, sub_cfg, monkeypatch):
		monkeypatch.setattr(ai_fix, "_call_local_cli", lambda system, messages, **kw: "FIXTEXT")
		out = ai_fix.suggest_fix({"finding_type": "N+1 Query", "severity": "High", "title": "x", "sql": "select 1"})
		assert out["provider"] == "Claude CLI" and "FIXTEXT" in out["suggestion"]

	def test_availability_needs_the_guard_and_an_installed_claude(self, sub_cfg, tmp_path, monkeypatch):
		exe = _exe(tmp_path / "claude")
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: str(exe))
		assert ai_fix.is_available() is True
		monkeypatch.setattr(ai_fix.shutil, "which", lambda name: None)
		assert ai_fix.is_available() is False
