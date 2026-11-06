"""Automatic line profiling skips what it cannot capture instead of giving up on the run."""

import inspect
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from optimus import api
from optimus.line_profile import capture, picker
from optimus.renderer import source
from optimus.tests.test_line_profile_source_capture import BLANKS, load

pytestmark = pytest.mark.rq


def wire(monkeypatch, by_dotted):
	monkeypatch.setattr(capture, "_LP_AVAILABLE", True)
	monkeypatch.setattr(source, "_installed_apps", lambda: {"optimus"})
	monkeypatch.setattr(
		picker,
		"resolve_freeform",
		lambda dotted: {
			"dotted_path": dotted,
			"eligible": True,
			"qualname": "f",
			"file": "x.py",
			"lineno": 1,
		},
	)
	monkeypatch.setattr(capture, "_resolve_attr", by_dotted.get)


def pair(tmp_path, monkeypatch):
	path_a, good = load(tmp_path, monkeypatch, BLANKS)
	path_b, bad = load(tmp_path, monkeypatch, BLANKS)
	path_b.write_text("x = 1\n")
	paths = {good.__code__.co_filename: path_a, bad.__code__.co_filename: path_b}
	wire(monkeypatch, {"optimus.mod.good": good, "optimus.mod.bad": bad})
	monkeypatch.setattr(
		source, "_resolve_source_path", lambda filename, must_exist=True: str(paths[filename])
	)


def curated(*names):
	return [{"dotted_path": f"optimus.mod.{name}", "source": "curated"} for name in names]


def test_server_chosen_skips_an_unreadable_function_and_arms_the_rest(tmp_path, monkeypatch):
	pair(tmp_path, monkeypatch)
	prepared = capture.prepare_line_profile_picks(curated("bad", "good"), server_chosen=True)
	assert list(json.loads(prepared.source_json)) == ["optimus.mod.good"]
	resolved = {r["dotted_path"]: r for r in prepared.resolved}
	assert resolved["optimus.mod.bad"]["eligible"] is False
	assert resolved["optimus.mod.bad"]["ineligible_reason"]


def test_a_user_picked_unreadable_function_still_refuses_naming_it(tmp_path, monkeypatch):
	pair(tmp_path, monkeypatch)
	with pytest.raises(capture.CaptureError, match="optimus.mod.bad"):
		capture.prepare_line_profile_picks(curated("bad", "good"))


def test_server_chosen_with_nothing_readable_raises_the_no_function_error(tmp_path, monkeypatch):
	pair(tmp_path, monkeypatch)
	with pytest.raises(capture.CaptureError, match="No selected function has eligible source"):
		capture.prepare_line_profile_picks(curated("bad"), server_chosen=True)


def long_function(lines):
	return "def f():\n" + "\tx = 1\n" * (lines - 1)


def test_a_function_over_the_line_cap_is_refused_or_skipped_without_parsing(tmp_path, monkeypatch):
	_, big = load(tmp_path, monkeypatch, long_function(capture.MAX_PICK_SOURCE_LINES + 1000))
	wire(monkeypatch, {"optimus.mod.f": big})
	monkeypatch.setattr(inspect, "getblock", lambda *a: pytest.fail("an over-cap function was parsed"))
	with pytest.raises(capture.CaptureError, match="longer than 5000 lines"):
		capture.prepare_line_profile_picks(curated("f"))
	with pytest.raises(capture.CaptureError, match="No selected function has eligible source"):
		capture.prepare_line_profile_picks(curated("f"), server_chosen=True)


def test_a_function_at_the_line_cap_is_captured(tmp_path, monkeypatch):
	_, fn = load(tmp_path, monkeypatch, long_function(capture.MAX_PICK_SOURCE_LINES))
	wire(monkeypatch, {"optimus.mod.f": fn})
	prepared = capture.prepare_line_profile_picks(curated("f"))
	assert len(json.loads(prepared.source_json)["optimus.mod.f"]) == capture.MAX_PICK_SOURCE_LINES


def many(tmp_path, monkeypatch, count):
	fns = {}
	for i in range(count):
		_, fns[f"optimus.mod.f{i}"] = load(tmp_path, monkeypatch, BLANKS)
	wire(monkeypatch, fns)
	return [{"dotted_path": name, "source": "curated"} for name in fns]


def test_line_total_stops_reading_before_the_next_function(tmp_path, monkeypatch):
	picks = many(tmp_path, monkeypatch, 4)
	monkeypatch.setattr(capture, "MAX_SOURCE_LINES", 12)
	reads = []
	real = capture._capture_source_lines
	monkeypatch.setattr(capture, "_capture_source_lines", lambda fn: reads.append(fn) or real(fn))
	prepared = capture.prepare_line_profile_picks(picks, server_chosen=True)
	assert len(json.loads(prepared.source_json)) == 2
	assert len(reads) == 2
	skipped = [r for r in prepared.resolved if not r["eligible"]]
	assert len(skipped) == 2 and all(r["ineligible_reason"] for r in skipped)
	reads.clear()
	with pytest.raises(capture.CaptureError, match="Select fewer functions"):
		capture.prepare_line_profile_picks(picks)
	assert len(reads) == 2


def test_a_function_that_crosses_the_line_total_is_skipped_for_server_chosen(tmp_path, monkeypatch):
	picks = many(tmp_path, monkeypatch, 3)
	monkeypatch.setattr(capture, "MAX_SOURCE_LINES", 9)
	prepared = capture.prepare_line_profile_picks(picks, server_chosen=True)
	assert list(json.loads(prepared.source_json)) == [picks[0]["dotted_path"]]
	with pytest.raises(capture.CaptureError, match="Select fewer functions"):
		capture.prepare_line_profile_picks(picks)


def test_byte_cap_skips_for_server_chosen_and_refuses_for_users(tmp_path, monkeypatch):
	picks = many(tmp_path, monkeypatch, 3)
	one = capture.prepare_line_profile_picks(picks[:1])
	limit = len(one.picks_json) + len(one.source_json) + 50
	monkeypatch.setattr(capture, "MAX_CAPTURE_BYTES", limit)
	prepared = capture.prepare_line_profile_picks(picks, server_chosen=True)
	assert len(json.loads(prepared.source_json)) == 1
	assert len(prepared.picks_json) + len(prepared.source_json) <= limit
	with pytest.raises(capture.CaptureError, match="Select fewer functions"):
		capture.prepare_line_profile_picks(picks)


def test_prepared_size_is_counted_exactly(tmp_path, monkeypatch):
	picks = many(tmp_path, monkeypatch, 3)
	prepared = capture.prepare_line_profile_picks(picks)
	exact = len(prepared.picks_json) + len(prepared.source_json)
	monkeypatch.setattr(capture, "MAX_CAPTURE_BYTES", exact)
	assert capture.prepare_line_profile_picks(picks).source_json == prepared.source_json
	monkeypatch.setattr(capture, "MAX_CAPTURE_BYTES", exact - 1)
	with pytest.raises(capture.CaptureError):
		capture.prepare_line_profile_picks(picks)


def test_an_uninstalled_app_pick_is_skipped_for_server_chosen_and_refused_for_users(tmp_path, monkeypatch):
	_, good = load(tmp_path, monkeypatch, BLANKS)
	wire(monkeypatch, {"optimus.mod.good": good})
	picks = curated("good") + [{"dotted_path": "stranger.mod.f", "source": "curated"}]
	prepared = capture.prepare_line_profile_picks(picks, server_chosen=True)
	resolved = {r["dotted_path"]: r for r in prepared.resolved}
	assert resolved["stranger.mod.f"]["eligible"] is False and resolved["stranger.mod.f"]["ineligible_reason"]
	with pytest.raises(capture.CaptureError, match="installed app"):
		capture.prepare_line_profile_picks(picks)


def test_skipped_functions_are_named_with_reasons_and_bounded():
	resolved = [
		{"dotted_path": f"a.b.f{i}", "eligible": False, "ineligible_reason": f"r{i}"} for i in range(8)
	]
	resolved.append({"dotted_path": "a.b.ok", "eligible": True})
	assert capture.skipped_functions(resolved) == [{"function": f"f{i}", "reason": f"r{i}"} for i in range(5)]


def test_a_client_can_never_set_server_chosen():
	assert "server_chosen" not in inspect.signature(api.start_line_profile_pass).parameters
	assert "server_chosen" not in inspect.getsource(api)
	assert capture.prepare_line_profile_picks.__wrapped__.__kwdefaults__["server_chosen"] is False


def test_run_row_keeps_skipped_picks_with_their_reason():
	eligible = {"dotted_path": "a.b.ok", "source": "curated", "eligible": True}
	skipped = {
		"dotted_path": "a.b.bad",
		"source": "curated",
		"eligible": False,
		"ineligible_reason": "too long",
	}
	assert api._run_pick_entry(eligible) == {"dotted_path": "a.b.ok", "source": "curated"}
	assert api._run_pick_entry(skipped) == {
		"dotted_path": "a.b.bad",
		"source": "curated",
		"eligible": False,
		"ineligible_reason": "too long",
	}


def test_path_predicate_matches_the_batch_validation():
	for good in ("a.b", "a.B.c_1"):
		assert capture.is_valid_dotted_path(good)
	for bad in ("f", "a..b", "a.1b", "", None, 5, "a." + "b" * 500):
		assert not capture.is_valid_dotted_path(bad)


def test_form_shows_skipped_functions_escaped_in_the_alert_and_the_run_banner():
	if not shutil.which("node"):
		pytest.skip("node not installed")
	form = Path(__file__).resolve().parents[1] / "optimus/doctype/optimus_session/optimus_session.js"
	script = r"""
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const handlers={},alerts=[];
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ctx={__:(t,a)=>a?t.replace(/\{(\d+)\}/g,(m,i)=>a[i]):t,setTimeout:()=>{},clearTimeout:()=>{},
 frappe:{ui:{form:{on:()=>{}}},router:{on:()=>{}},realtime:{on:(n,f)=>handlers[n]=f},
 utils:{escape_html:esc},show_alert:a=>alerts.push(a.message)}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
ctx._progress_banner=()=>{};
const frm={doc:{name:'doc',session_uuid:'s'},is_new:()=>false,is_dirty:()=>false,reload_doc(){}};
ctx.subscribe_session_progress(frm);ctx.subscribe_phase2_events(frm);
const skipped=[...Array(7).keys()].map(i=>({function:i?'f'+i:'<b>x</b>',reason:i?'r'+i:'<i>why</i>'}));
handlers.optimus_phase2_armed({docname:'doc',session_uuid:'s',event_id:'e',count:2,functions:['a'],skipped:skipped.slice(0,5),skipped_count:7});
const text=alerts[0];
assert.match(text,/Skipped: &lt;b&gt;x&lt;\/b&gt; \(&lt;i&gt;why&lt;\/i&gt;\), f1 \(r1\), f2 \(r2\), f3 \(r3\), f4 \(r4\) and 2 more\./);
assert.ok(!text.includes('<b>')&&!text.includes('<i>'));
handlers.optimus_phase2_armed({docname:'doc',session_uuid:'s',event_id:'none',count:1,functions:['a']});
assert.ok(!alerts[1].includes('Skipped'),'no skips, no skipped text');
const rows=[{run_uuid:'aaaaaaaabbbb',picks_json:JSON.stringify([{dotted_path:'m.ok',source:'curated'}])},
 {run_uuid:'ccccccccdddd',picks_json:JSON.stringify([{dotted_path:'m.bad',source:'curated',eligible:false,ineligible_reason:'too <long>'}])},
 {run_uuid:'eeeeeeeeffff',picks_json:'not json'}];
assert.deepEqual(rows.map(r=>ctx._phase2_run_skips(r).length),[0,1,0]);
let html='unset';ctx._single_banner=(f,id,cls,h)=>{if(id==='optimus-phase2-skipped')html=h;};
frm.doc.phase_2_runs=[rows[0],rows[2],rows[1]];ctx._phase2_skipped_banner(frm);
assert.match(html,/run cccccccc could not include 1 function/);
assert.match(html,/bad \(too &lt;long&gt;\)/);assert.ok(!html.includes('<long>'));
frm.doc.phase_2_runs=[rows[1],rows[0]];ctx._phase2_skipped_banner(frm);
assert.equal(html,null,'a later clean run hides the notice');
frame=[rows[0],rows[1]];frm.doc.phase_2_runs=frame;ctx._phase2_skipped_banner(frm);
assert.match(html,/run cccccccc/);
"""
	result = subprocess.run(["node", "-e", script, str(form)], capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
