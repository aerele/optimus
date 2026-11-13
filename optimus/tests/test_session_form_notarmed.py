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


SESSION_SCRIPT = r"""
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const handlers={},banners={};
function banner(name){
 const data={};
 return {length:1,idClass:name,data,html(h){if(h===undefined)return this.h;this.h=h;return this;},
  attr(k,v){if(typeof k==='object'){Object.assign(data,k);return this;}
   if(v===undefined)return data[k];data[k]=v;return this;},
  remove(){delete banners[name];}};
}
const none={length:0,remove(){},attr(){}};
const layout={length:1,first(){return this;},prepend(b){banners[b.idClass]=b;}};
const wrapper={length:1,find(sel){
 const name=sel.slice(1);
 if(name==='form-layout')return layout;
 return banners[name]||none;}};
const ctx={__:(t,a)=>a?t.replace(/\{(\d+)\}/g,(m,i)=>a[i]):t,setTimeout:()=>{},clearTimeout:()=>{},
 $:(h)=>banner(/class="(\S+)/.exec(h)[1]),
 frappe:{ui:{form:{on:()=>{}}},router:{on:()=>{}},realtime:{on:(n,f)=>handlers[n]=f},
 utils:{escape_html:s=>String(s)},show_alert:()=>{}}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
ctx._can_act=()=>true;
const frm={$wrapper:wrapper,doc:{name:'docA',session_uuid:'sA',status:'Ready',phase_2_runs:[]},
 is_new:()=>false,is_dirty:()=>false,reload_doc(){},add_custom_button:()=>({removeClass(){return this},addClass(){return this}})};
ctx.subscribe_session_progress(frm);ctx.subscribe_phase2_events(frm);
handlers.optimus_phase2_not_armed({docname:'docA',session_uuid:'sA',event_id:'n1',
 skipped:[{function:'app_a.secret.path',reason:'why'}],skipped_count:1});
assert.match(banners['optimus-phase2-notarmed'].h,/app_a\.secret\.path/);
ctx.render_phase2_button(frm);
assert.match(banners['optimus-phase2-notarmed'].h,/app_a\.secret\.path/,'the same session keeps its notice');
frm.doc={name:'docB',session_uuid:'sB',status:'Ready',phase_2_runs:[]};
ctx.render_phase2_button(frm);
assert.equal(banners['optimus-phase2-notarmed'],undefined,'another Ready session does not inherit it');
"""


def test_the_not_armed_notice_does_not_leak_into_another_session():
	if not shutil.which("node"):
		pytest.skip("node not installed")
	result = subprocess.run(["node", "-e", SESSION_SCRIPT, str(FORM)], capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
