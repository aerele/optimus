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


def test_skipped_functions_are_named_in_full_with_reasons():
	resolved = [
		{"dotted_path": f"app{i}.mod.validate", "eligible": False, "ineligible_reason": f"r{i}"}
		for i in range(8)
	]
	resolved.append({"dotted_path": "a.b.ok", "eligible": True})
	everything = capture.skipped_functions(resolved)
	assert everything == [{"function": f"app{i}.mod.validate", "reason": f"r{i}"} for i in range(8)]
	assert capture.skipped_functions(resolved, limit=5) == everything[:5]


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
const skipped=[...Array(7).keys()].map(i=>({function:i?'app.mod.f'+i:'a.<b>x</b>',reason:i?'r'+i:'<i>why</i>'}));
skipped.push({function:'app.mod.noreason',reason:''});
handlers.optimus_phase2_armed({docname:'doc',session_uuid:'s',event_id:'e',count:2,functions:['a'],skipped:skipped.slice(0,5),skipped_count:8});
const text=alerts[0];
assert.match(text,/Skipped 8 function\(s\)/);
assert.match(text,/a\.&lt;b&gt;x&lt;\/b&gt; \(&lt;i&gt;why&lt;\/i&gt;\)/,'the full dotted path, escaped');
assert.match(text,/app\.mod\.f1 \(r1\)/);
assert.match(text,/and 5 more/);
assert.ok(!text.includes('<b>')&&!text.includes('<i>'));
assert.ok(!text.includes('<details'),'the alert stays a short summary');
handlers.optimus_phase2_armed({docname:'doc',session_uuid:'s',event_id:'quiet',count:1,functions:['a'],skipped:[{function:'m.quiet',reason:''}],skipped_count:1});
assert.match(alerts[1],/Skipped 1 function\(s\): m\.quiet\./,'no empty parentheses');
handlers.optimus_phase2_armed({docname:'doc',session_uuid:'s',event_id:'none',count:1,functions:['a']});
assert.ok(!alerts[2].includes('Skipped'),'no skips, no skipped text');
const rows=[{run_uuid:'aaaaaaaabbbb',picks_json:JSON.stringify([{dotted_path:'m.ok',source:'curated'}])},
 {run_uuid:'ccccccccdddd',picks_json:JSON.stringify([{dotted_path:'m.sub.bad',source:'curated',eligible:false,ineligible_reason:'too <long>'},
   {dotted_path:'m.sub.quiet',source:'curated',eligible:false}])},
 {run_uuid:'eeeeeeeeffff',picks_json:'not json'}];
assert.deepEqual(rows.map(r=>ctx._phase2_run_skips(r).length),[0,2,0]);
assert.equal(ctx._phase2_run_skips(rows[1])[0].function,'m.sub.bad');
const calls={};ctx._single_banner=(f,id,cls,h,u,attrs)=>{calls[id]={h,attrs};return null;};
frm.doc.phase_2_runs=[rows[0],rows[2],rows[1]];ctx._phase2_skipped_banner(frm);
const call=calls['optimus-phase2-skipped'];
const html=call.h;
assert.match(html,/run cccccccc could not include 2 function/);
assert.match(html,/<details/);
assert.match(html,/<code[^>]*>m\.sub\.bad<\/code> \(too &lt;long&gt;\)/);
assert.match(html,/<code[^>]*>m\.sub\.quiet<\/code><\/li>/,'an empty reason shows no parentheses');
assert.ok(!html.includes('()')&&!html.includes('<long>'));
assert.equal(JSON.stringify(call.attrs),'{"role":"status"}','role is applied with the insert');
frm.doc.phase_2_runs=[rows[1],rows[0]];ctx._phase2_skipped_banner(frm);
assert.equal(calls['optimus-phase2-skipped'].h,null,'a later clean run hides the notice');
frm.doc.phase_2_runs=[rows[0],rows[1]];ctx._phase2_skipped_banner(frm);
assert.match(calls['optimus-phase2-skipped'].h,/run cccccccc/);
assert.equal(typeof handlers.optimus_phase2_not_armed,'function');
handlers.optimus_phase2_not_armed({docname:'doc',session_uuid:'s',event_id:'n1',skipped:[{function:'m.x.y',reason:'it is longer than 5000 lines'}],skipped_count:1});
assert.match(calls['optimus-phase2-notarmed'].h,/m\.x\.y/);
assert.match(calls['optimus-phase2-notarmed'].h,/longer than 5000 lines/);
assert.match(calls['optimus-phase2-notarmed'].h,/No line profiling run was started/);
assert.equal(JSON.stringify(calls['optimus-phase2-notarmed'].attrs),'{"role":"status"}');
calls['optimus-phase2-skipped']=undefined;
ctx._phase2_armed_banner=()=>{};
frm.doc.status='Failed';frm.doc.phase_2_runs=[];
ctx.render_phase2_button(frm);
assert.equal(calls['optimus-phase2-skipped'].h,null,'cleared when the session is not Ready');
assert.equal(calls['optimus-phase2-notarmed'].h,null,'the not-run notice is cleared too');
"""
	result = subprocess.run(["node", "-e", script, str(form)], capture_output=True, text=True)
	assert result.returncode == 0, result.stderr


def test_a_function_one_line_over_the_cap_is_refused(tmp_path, monkeypatch):
	_, fn = load(tmp_path, monkeypatch, long_function(capture.MAX_PICK_SOURCE_LINES + 1))
	wire(monkeypatch, {"optimus.mod.f": fn})
	with pytest.raises(capture.CaptureError, match="longer than 5000 lines"):
		capture.prepare_line_profile_picks(curated("f"))


def test_a_skipped_entry_keeps_the_pick_source_label(tmp_path, monkeypatch):
	pair(tmp_path, monkeypatch)
	picks = [
		{"dotted_path": "optimus.mod.bad", "source": "auto_expand"},
		{"dotted_path": "optimus.mod.good", "source": "curated"},
		{"dotted_path": "stranger.mod.f", "source": "curated"},
	]
	prepared = capture.prepare_line_profile_picks(picks, server_chosen=True)
	sources = {r["dotted_path"]: r["source"] for r in prepared.resolved}
	assert sources == {
		"optimus.mod.bad": "auto_expand",
		"optimus.mod.good": "curated",
		"stranger.mod.f": "curated",
	}


def test_nothing_capturable_carries_every_skip_for_the_caller(tmp_path, monkeypatch):
	pair(tmp_path, monkeypatch)
	with pytest.raises(capture.NoEligibleSource) as caught:
		capture.prepare_line_profile_picks(curated("bad"), server_chosen=True)
	assert [s["function"] for s in caught.value.skipped] == ["optimus.mod.bad"]
	assert caught.value.skipped[0]["reason"]


def test_a_failed_import_reason_is_fixed_text_without_the_raw_error(tmp_path, monkeypatch):
	_, good = load(tmp_path, monkeypatch, BLANKS)
	wire(monkeypatch, {"optimus.mod.good": good})
	secret = "cannot import name 'x' from '/home/secret/site-packages/pkg/__init__.py'"

	def resolve(dotted):
		if dotted.endswith("broken"):
			raise picker.PickerError(f"could not import any prefix of '{dotted}' (last error: {secret})")
		return {"dotted_path": dotted, "eligible": True, "qualname": "f", "file": "x.py", "lineno": 1}

	monkeypatch.setattr(picker, "resolve_freeform", resolve)
	prepared = capture.prepare_line_profile_picks(curated("broken", "good"), server_chosen=True)
	reason = {r["dotted_path"]: r for r in prepared.resolved}["optimus.mod.broken"]["ineligible_reason"]
	assert reason and "/home" not in reason and "last error" not in reason


@pytest.mark.parametrize("error", [SyntaxError("bad"), NameError("nope")])
def test_an_import_that_fails_badly_skips_only_that_server_chosen_pick(tmp_path, monkeypatch, error):
	_, good = load(tmp_path, monkeypatch, BLANKS)
	wire(monkeypatch, {"optimus.mod.good": good})
	real = picker.resolve_freeform

	def resolve(dotted):
		if dotted.endswith("broken"):
			raise error
		return real(dotted)

	monkeypatch.setattr(picker, "resolve_freeform", resolve)
	prepared = capture.prepare_line_profile_picks(curated("broken", "good"), server_chosen=True)
	assert list(json.loads(prepared.source_json)) == ["optimus.mod.good"]
	skipped = [r for r in prepared.resolved if not r["eligible"]]
	assert [r["dotted_path"] for r in skipped] == ["optimus.mod.broken"]
	assert skipped[0]["ineligible_reason"] and str(error) not in skipped[0]["ineligible_reason"]
	with pytest.raises(type(error)):
		capture.prepare_line_profile_picks(curated("broken", "good"))


def test_a_timeout_during_import_is_not_swallowed_when_server_chosen(tmp_path, monkeypatch):
	_, good = load(tmp_path, monkeypatch, BLANKS)
	wire(monkeypatch, {"optimus.mod.good": good})
	from optimus.ai_fix import _job_timeout_types

	def resolve(dotted):
		raise _job_timeout_types()[0]()

	monkeypatch.setattr(picker, "resolve_freeform", resolve)
	with pytest.raises(_job_timeout_types()):
		capture.prepare_line_profile_picks(curated("good"), server_chosen=True)


def test_a_server_chosen_pick_that_fails_validation_is_skipped_not_fatal(tmp_path, monkeypatch):
	_, good = load(tmp_path, monkeypatch, BLANKS)
	_, other = load(tmp_path, monkeypatch, BLANKS)
	wire(monkeypatch, {"optimus.mod.good": good, "optimus.mod.other": other})
	real = capture._validate_captured_picks

	def validate(picks):
		if any(p["dotted_path"].endswith("other") for p in picks):
			raise capture.CaptureInputError("Line profiling captured function metadata is malformed")
		return real(picks)

	monkeypatch.setattr(capture, "_validate_captured_picks", validate)
	prepared = capture.prepare_line_profile_picks(curated("good", "other"), server_chosen=True)
	assert list(json.loads(prepared.source_json)) == ["optimus.mod.good"]
	assert [m["dotted_path"] for m in json.loads(prepared.picks_json)] == ["optimus.mod.good"]
	skipped = [r for r in prepared.resolved if not r["eligible"]]
	assert [r["dotted_path"] for r in skipped] == ["optimus.mod.other"] and skipped[0]["ineligible_reason"]
	with pytest.raises(capture.CaptureError, match="cannot be captured"):
		capture.prepare_line_profile_picks(curated("good", "other"))


def test_a_banner_gets_its_role_before_its_content_is_inserted():
	if not shutil.which("node"):
		pytest.skip("node not installed")
	form = Path(__file__).resolve().parents[1] / "optimus/doctype/optimus_session/optimus_session.js"
	script = r"""
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const order=[];
const fake=()=>({attr(a){order.push('attr:'+(typeof a==='string'?'class':JSON.stringify(a)));return this;},html(h){order.push('html');return this;}});
const ctx={__:t=>t,setTimeout:()=>{},frappe:{ui:{form:{on:()=>{}}},router:{on:()=>{}},realtime:{on:()=>{}},utils:{escape_html:s=>s}},
 $:()=>fake()};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
const none={length:0,first:()=>({length:0})};
const frm={$wrapper:{length:1,find:()=>none,prepend:()=>order.push('insert')}};
ctx._single_banner(frm,'b','c','<p>x</p>',null,{role:'status'});
const role=order.indexOf('attr:{"role":"status"}');
assert.ok(role>=0,'role is set');
assert.ok(role<order.indexOf('html'),'before the content');
assert.ok(role<order.indexOf('insert'),'before the banner is inserted');
"""
	result = subprocess.run(["node", "-e", script, str(form)], capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
