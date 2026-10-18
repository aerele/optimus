"""Run the actual realtime handlers with duplicate and foreign-session events."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_widget_deduplicates_room_delivery_and_follows_only_its_actor():
	if not shutil.which("node"):
		pytest.skip("node not installed")
	source = Path(__file__).resolve().parents[1] / "public/js/floating_widget.js"
	script = r'''
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
function widget(user) {
 const handlers={},alerts=[],displays=[];
 const ctx={window:{},document:{readyState:'loading',addEventListener:()=>{}},
  setTimeout:()=>{},clearInterval:()=>{},displays,
  __:text=>text,frappe:{session:{user},set_route:()=>{},realtime:{on:(name,fn)=>handlers[name]=fn},
   show_alert:a=>alerts.push(a)}};
 vm.createContext(ctx);
 let code=fs.readFileSync(process.argv[1],'utf8');
 code=code.replace(/\}\)\(\);\s*$/,`setDisplay=(...args)=>displays.push(args);
 globalThis.widgetTest={subscribeRealtime,onClick,state:currentState};})();`);
 vm.runInContext(code,ctx);ctx.widgetTest.subscribeRealtime();
 return {state:ctx.widgetTest.state,click:ctx.widgetTest.onClick,handlers,alerts,displays};
}
const owner=widget('owner'),reader=widget('reader'),actor=widget('actor');
const ready={session_uuid:'session',docname:'doc',recording_user:'owner',event_id:'ready'};
reader.handlers.optimus_session_analyzing(ready);
reader.handlers.optimus_session_ready(ready);
assert.equal(reader.state.display,'inactive','shared events must not take over an idle widget');
reader.handlers.optimus_session_ready({session_uuid:'legacy-unknown'});
assert.equal(reader.state.display,'inactive','unidentified legacy events cannot adopt an idle widget');
owner.handlers.optimus_session_ready(ready);owner.handlers.optimus_session_ready(ready);
assert.equal(owner.alerts.length,1,'two room copies must produce one widget toast');
assert.equal(owner.state.session_uuid,'session');
owner.handlers.optimus_session_analyzing({...ready,event_id:'other',session_uuid:'another'});
assert.equal(owner.state.display,'ready','another session must not replace current work');
const phase2={...ready,event_id:'recording',run_uuid:'run',user:'actor'};
owner.handlers.phase_2_run_recording(phase2);reader.handlers.phase_2_run_recording(phase2);
assert.equal(owner.state.display,'ready','owner must not be offered Stop for a different capture actor');
assert.equal(reader.state.display,'inactive');
actor.handlers.phase_2_run_recording(phase2);actor.handlers.phase_2_run_recording(phase2);
assert.equal(actor.state.display,'recording');assert.equal(actor.state.run_uuid,'run');
assert.equal(actor.displays.length,1);
actor.handlers.phase_2_run_ready({...phase2,event_id:'phase2-ready',parent:'doc'});
actor.handlers.phase_2_run_ready({...phase2,event_id:'phase2-ready',parent:'doc'});
assert.equal(actor.alerts.length,1);assert.equal(actor.state.phase2,false);
// Distinct events remain live while the duplicate cache stays bounded.
for(let i=0;i<200;i++)owner.handlers.optimus_session_ready({...ready,event_id:'next-'+i});
assert.equal(owner.alerts.length,201);
owner.handlers.optimus_session_ready(ready);
assert.equal(owner.alerts.length,202,'old IDs must be evicted from the bounded cache');
owner.click();assert.equal(owner.state.display,'inactive');
owner.handlers.optimus_session_analyzing({...ready,session_uuid:'next-session',event_id:'next-session'});
assert.equal(owner.state.display,'analyzing','after opening a report, a new own session must still update');
assert.equal(owner.state.session_uuid,'next-session');
'''
	result = subprocess.run(["node", "-e", script, str(source)], capture_output=True, text=True)
	assert result.returncode == 0, result.stderr


def test_form_deduplicates_all_realtime_paths_and_preserves_navigation_and_edits():
	if not shutil.which("node"):
		pytest.skip("node not installed")
	source = Path(__file__).resolve().parents[1] / "optimus/doctype/optimus_session/optimus_session.js"
	script = r'''
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const handlers={},alerts=[],timers=[];
const ctx={__:text=>text,setTimeout:fn=>timers.push(fn),clearTimeout:()=>{},
 frappe:{ui:{form:{on:()=>{}}},router:{on:()=>{}},
 realtime:{on:(name,fn)=>handlers[name]=fn},utils:{escape_html:s=>s},show_alert:a=>alerts.push(a)}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
ctx._progress_banner=()=>{};
const frm={doc:{name:'doc',session_uuid:'session'},is_new:()=>false,is_dirty:()=>false,
 reloads:0,reload_doc(){this.reloads++;}};
ctx.subscribe_session_progress(frm);ctx.subscribe_phase2_events(frm);
const p={session_uuid:'session',docname:'doc',event_id:'ready'};
handlers.optimus_session_ready(p);handlers.optimus_session_ready(p);
assert.equal(alerts.length,1);assert.equal(timers.length,1);
timers.shift()();assert.equal(frm.reloads,1);
const armed={...p,event_id:'armed',count:1,functions:['example']};
handlers.optimus_phase2_armed(armed);handlers.optimus_phase2_armed(armed);
assert.equal(alerts.length,2);assert.equal(timers.length,1);
frm.doc={name:'other',session_uuid:'other'};timers.shift()();
assert.equal(frm.reloads,1,'a delayed event must not reload a different form');
frm.doc={name:'doc',session_uuid:'session'};
const phase2={...p,event_id:'phase2'};
handlers.phase_2_run_ready(phase2);handlers.phase_2_run_ready(phase2);
assert.equal(alerts.length,3);assert.equal(frm.reloads,2);
frm.is_dirty=()=>true;
handlers.optimus_session_ready({...p,event_id:'dirty'});timers.shift()();
assert.equal(frm.reloads,2,'completion must preserve unsaved edits');
'''
	result = subprocess.run(["node", "-e", script, str(source)], capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
