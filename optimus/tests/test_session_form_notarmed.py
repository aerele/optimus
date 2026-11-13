"""A later successful auto-arm must not leave the earlier "No line profiling run was started" notice."""

import shutil
import subprocess
from pathlib import Path

import pytest

FORM = Path(__file__).resolve().parents[1] / "optimus/doctype/optimus_session/optimus_session.js"

SCRIPT = r"""
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const handlers={};
const ctx={__:(t,a)=>a?t.replace(/\{(\d+)\}/g,(m,i)=>a[i]):t,setTimeout:()=>{},clearTimeout:()=>{},
 frappe:{ui:{form:{on:()=>{}}},router:{on:()=>{}},realtime:{on:(n,f)=>handlers[n]=f},
 utils:{escape_html:s=>String(s)},show_alert:()=>{}}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
ctx._progress_banner=()=>{};ctx._phase2_armed_banner=()=>{};ctx._reload_clean_form=()=>{};
const calls={};ctx._single_banner=(f,id,cls,h)=>{calls[id]=h;return null;};
const frm={doc:{name:'doc',session_uuid:'s',status:'Ready',phase_2_runs:[]},is_new:()=>false,
 is_dirty:()=>false,reload_doc(){},add_custom_button:()=>({removeClass(){return this},addClass(){return this}})};
ctx._can_act=()=>true;
ctx.subscribe_session_progress(frm);ctx.subscribe_phase2_events(frm);
const show=()=>handlers.optimus_phase2_not_armed({docname:'doc',session_uuid:'s',event_id:'n'+Math.random(),
 skipped:[{function:'m.x.y',reason:'why'}],skipped_count:1});
show();
assert.match(calls['optimus-phase2-notarmed'],/m\.x\.y/);
ctx.render_phase2_button(frm);
assert.match(calls['optimus-phase2-notarmed'],/m\.x\.y/,'a plain re-render keeps the notice the user has not acted on');
handlers.optimus_phase2_armed({docname:'doc',session_uuid:'s',event_id:'a1',count:1,functions:['f']});
assert.equal(calls['optimus-phase2-notarmed'],null,'the armed event removes the stale notice');
show();
frm.doc.phase_2_runs=[{run_uuid:'aaaaaaaabbbb',status:'Recording',picks_json:'[]'}];
ctx.render_phase2_button(frm);
assert.equal(calls['optimus-phase2-notarmed'],null,'a Ready session with a live run drops the stale notice');
"""


def test_a_successful_auto_arm_removes_the_not_armed_notice():
	if not shutil.which("node"):
		pytest.skip("node not installed")
	result = subprocess.run(["node", "-e", SCRIPT, str(FORM)], capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
