# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Run the widget's session-complete popup logic in node with a small Desk stub."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

WIDGET_JS = Path(__file__).resolve().parents[1] / "public/js/floating_widget.js"

PILL_JS = WIDGET_JS.with_name("optimus_pill_state.js")

HARNESS = r"""
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
function desk(opts = {}) {
 const handlers={},dialogs=[],alerts=[],pills=[],docListeners={},store=opts.store||{};
 const timers=new Map(); let nextTimer=1;
 const doc={activeElement:null,visibilityState:'visible'};
 class Dialog {
  constructor(cfg){ if(opts.beforeBuild) opts.beforeBuild(); if(opts.dialogThrows && opts.throwsLeft-- !== 0) throw new Error('boom'); this.cfg=cfg; dialogs.push(this); }
  show(){ this.shown=true; }
  hide(){ this.shown=false; if(this.onhide) this.onhide(); }
 }
 const storage=opts.storageThrows
  ? {getItem(){throw new Error('denied');},setItem(){throw new Error('denied');},removeItem(){throw new Error('denied');}}
  : {getItem:k=>(k in store?store[k]:null),
     setItem:(k,v)=>{store[k]=v; if(opts.race) store[k]='other-tab|x';},
     removeItem:k=>{delete store[k];}};
 const ctx={window:{},document:Object.assign(doc,{readyState:'loading',
   addEventListener:(n,fn)=>{(docListeners[n]=docListeners[n]||[]).push(fn);}}),
  setTimeout:(fn)=>{const id=nextTimer++;timers.set(id,fn);return id;},
  clearTimeout:(id)=>{timers.delete(id);},clearInterval:()=>{},
  console:{warn:(...a)=>{(opts.warns||[]).push(a);},log:()=>{}},
  localStorage:storage,Math,__:(t,a)=>a?t.replace('{0}',a[0]):t,
  __sd:(display,label)=>pills.push({display,label}),
  frappe:{session:{user:'owner'},set_route:()=>{},get_route:()=>opts.route||[],
   ui:{Dialog,open_dialogs:opts.openDialogs||[]},utils:{escape_html:s=>String(s)},
   realtime:{socket:opts.noSocket?undefined:{},on:(n,fn)=>{handlers[n]=fn;(opts.onCalls||[]).push(n);}},show_alert:a=>alerts.push(a)}};
 vm.createContext(ctx);
 vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),ctx);
 let code=fs.readFileSync(process.argv[1],'utf8');
 const tail=/\}\)\(\);\s*$/, hook='function setDisplay(display, label, elapsed, tone) {';
 assert.ok(tail.test(code) && code.includes(hook), 'widget source shape changed: update the harness');
 code=code.replace(tail,`globalThis.w={subscribeRealtime,getState:()=>currentState,realtime:frappe.realtime};})();`);
 code=code.replace(hook,hook+' globalThis.__sd(display,label);');
 vm.runInContext(code,ctx);ctx.w.subscribeRealtime();
 const realtime=ctx.w.realtime;
 const tick=()=>{ const fns=[...timers.values()]; timers.clear(); fns.forEach(f=>f()); };
 const setVisibility=v=>{ doc.visibilityState=v; (docListeners.visibilitychange||[]).forEach(f=>f()); };
 return {realtime,subscribeRealtime:ctx.w.subscribeRealtime,handlers,dialogs,alerts,pills,store,doc,tick,timers,setVisibility,get state(){return ctx.w.getState();}};
}
const ev=(o={})=>({session_uuid:'s1',docname:'OS-1',recording_user:'owner',event_id:'e1',...o});
"""


def _run(body: str) -> None:
	if not shutil.which("node"):
		if os.environ.get("CI"):
			pytest.fail("node is required in CI")
		pytest.skip("node not installed")
	result = subprocess.run(
		["node", "-e", HARNESS + body, str(WIDGET_JS), str(PILL_JS)], capture_output=True, text=True
	)
	assert result.returncode == 0, result.stderr


def test_ready_failed_and_empty_show_matching_popups():
	_run(r"""
for (const [name, data, title] of [
 ['optimus_session_ready', ev({event_id:'a'}), 'Optimus Session ready'],
 ['optimus_session_ready', ev({event_id:'b', empty:true}), 'Optimus Session has no data'],
 ['optimus_session_failed', ev({event_id:'c'}), 'Optimus analysis failed'],
]) {
 const d = desk();
 d.handlers[name](data);
 assert.equal(d.dialogs.length, 1);
 assert.equal(d.dialogs[0].cfg.title, title);
 assert.ok(d.dialogs[0].cfg.fields[0].options.includes('OS-1'));
}
""")


def test_popup_failure_does_not_block_state_transition():
	_run(r"""
const d = desk({dialogThrows:true});
d.handlers.optimus_session_ready(ev());
assert.equal(d.state.display, 'ready');
assert.equal(d.alerts.length, 1);
const f = desk({dialogThrows:true});
f.handlers.optimus_session_failed(ev());
assert.equal(f.state.display, 'ready');
""")


def test_popup_waits_while_typing_and_claims_only_when_shown():
	_run(r"""
const d = desk();
d.doc.activeElement = {tagName:'INPUT'};
d.handlers.optimus_session_ready(ev());
assert.equal(d.dialogs.length, 0);
assert.deepEqual(Object.keys(d.store), [], 'no claim while the dialog is deferred');
d.tick();
assert.equal(d.dialogs.length, 0, 'still typing');
// The focused field disappears without a blur: the periodic check still opens it.
d.doc.activeElement = null;
d.tick();
assert.equal(d.dialogs.length, 1);
assert.equal(Object.keys(d.store).length, 1);
""")


def test_other_tab_claim_suppresses_deferred_popup_but_not_a_new_run():
	_run(r"""
const store = {};
const a = desk({store}), b = desk({store});
b.doc.activeElement = {tagName:'TEXTAREA'};
a.handlers.optimus_session_ready(ev());
b.handlers.optimus_session_ready(ev());
b.doc.activeElement = null;
b.tick();
assert.equal(a.dialogs.length, 1);
assert.equal(b.dialogs.length, 0, 'tab that lost the claim shows nothing');
// Same session analysed again: a new event id pops again.
a.dialogs[0].hide();
a.handlers.optimus_session_ready(ev({event_id:'e2'}));
assert.equal(a.dialogs.length, 2);
// A duplicate room delivery of the same run does not.
a.dialogs[1].hide();
a.handlers.optimus_session_ready(ev({event_id:'e2'}));
assert.equal(a.dialogs.length, 2);
""")


def test_own_popup_hide_animation_does_not_count_as_another_dialog():
	_run(r"""
const d = desk();
d.doc.querySelector = sel => sel.includes(':not(.optimus-session-popup)') ? null : {};
d.handlers.optimus_session_ready(ev());
assert.equal(d.dialogs.length, 1);
""")


def test_empty_session_sets_no_data_pill_and_toast():
	_run(r"""
const d = desk();
d.handlers.optimus_session_ready(ev({empty:true}));
assert.equal(d.state.display, 'ready');
assert.deepEqual(d.pills, [{display:'ready', label:'No data'}]);
assert.equal(d.alerts.length, 1);
assert.equal(d.alerts[0].indicator, 'orange');
assert.ok(!/ready/i.test(d.alerts[0].message), d.alerts[0].message);
const r = desk();
r.handlers.optimus_session_ready(ev());
assert.deepEqual(r.pills, [{display:'ready', label:'Report ready'}]);
""")


def test_only_text_like_inputs_count_as_typing():
	_run(r"""
for (const el of [{tagName:'INPUT',type:'checkbox'},{tagName:'INPUT',type:'button'},
                  {tagName:'INPUT',type:'radio'},{tagName:'SELECT'},{tagName:'BUTTON'}]) {
 const d = desk(); d.doc.activeElement = el;
 d.handlers.optimus_session_ready(ev());
 assert.equal(d.dialogs.length, 1, JSON.stringify(el));
}
for (const el of [{tagName:'INPUT',type:'text'},{tagName:'INPUT'},{tagName:'TEXTAREA'},
                  {tagName:'DIV',isContentEditable:true}]) {
 const d = desk(); d.doc.activeElement = el;
 d.handlers.optimus_session_ready(ev());
 assert.equal(d.dialogs.length, 0, JSON.stringify(el));
}
""")


def test_popup_opens_after_deferral_cap_even_if_still_typing():
	_run(r"""
const d = desk();
d.doc.activeElement = {tagName:'INPUT', type:'text'};
d.handlers.optimus_session_ready(ev());
for (let i = 0; i < 29; i++) { d.tick(); assert.equal(d.dialogs.length, 0, 'tick ' + i); }
d.tick();
assert.equal(d.dialogs.length, 1);
""")


def test_claim_released_when_dialog_throws_so_a_retry_can_show():
	_run(r"""
const store = {};
const d = desk({dialogThrows:true, throwsLeft:1, store});
d.handlers.optimus_session_ready(ev());
assert.deepEqual(Object.keys(store), [], 'claim released');
assert.equal(d.dialogs.length, 0);
d.handlers.optimus_session_ready(ev());
assert.equal(d.dialogs.length, 1, 'same run can still show');
""")


def test_popup_ignored_for_other_users_recording():
	_run(r"""
const d = desk();
d.handlers.optimus_session_ready(ev({recording_user:'someone-else'}));
assert.equal(d.dialogs.length, 0);
""")


def test_popups_open_one_at_a_time():
	_run(r"""
const d = desk();
d.handlers.optimus_session_failed(ev({event_id:'f'}));
d.handlers.optimus_session_ready(ev({event_id:'r', docname:'OS-2'}));
assert.equal(d.dialogs.length, 1, 'second popup waits for the first');
d.dialogs[0].hide();
assert.equal(d.dialogs.length, 2);
assert.ok(d.dialogs[1].cfg.fields[0].options.includes('OS-2'));
""")


def test_hidden_tab_does_not_claim_so_the_visible_tab_shows_the_popup():
	_run(r"""
const store = {};
const visible = desk({store}), hidden = desk({store});
hidden.setVisibility('hidden');
hidden.handlers.optimus_session_ready(ev());
visible.handlers.optimus_session_ready(ev());
assert.equal(hidden.dialogs.length, 0, 'hidden tab waits');
assert.equal(visible.dialogs.length, 1, 'visible tab shows it');
// The hidden tab comes to the front later: the claim is taken, so no duplicate.
hidden.setVisibility('visible');
assert.equal(hidden.dialogs.length, 0);
""")


def test_hidden_tab_shows_the_popup_once_it_becomes_visible():
	_run(r"""
const d = desk();
d.setVisibility('hidden');
d.handlers.optimus_session_ready(ev());
assert.equal(d.dialogs.length, 0);
assert.deepEqual(Object.keys(d.store), [], 'no claim while hidden');
d.setVisibility('visible');
assert.equal(d.dialogs.length, 1);
""")


def test_popup_opens_for_a_session_other_than_the_current_one():
	_run(r"""
const d = desk();
d.state.session_uuid = 'other';
d.state.display = 'recording';
d.handlers.optimus_session_ready(ev({session_uuid:'s1'}));
assert.equal(d.dialogs.length, 1, 'popup shows wherever you are');
assert.equal(d.state.display, 'recording', 'widget state is left alone');
assert.deepEqual(d.pills, []);
""")


def test_claim_is_confirmed_by_reading_it_back():
	_run(r"""
const d = desk({race:true});
d.handlers.optimus_session_ready(ev());
assert.equal(d.dialogs.length, 0, 'another tab wrote the claim last');
assert.equal(d.store['optimus_popup_shown:OS-1:ready'], 'other-tab|x');
""")


def test_release_only_removes_our_own_claim():
	_run(r"""
const store = {};
const key = 'optimus_popup_shown:OS-1:ready';
const d = desk({store, dialogThrows:true, throwsLeft:1});
d.handlers.optimus_session_ready(ev());
assert.equal(store[key], undefined, 'our own claim is released on failure');
const foreign = {};
const e = desk({store:foreign, dialogThrows:true, throwsLeft:1,
	beforeBuild:()=>{ foreign[key] = 'e1|someone-else'; }});
e.handlers.optimus_session_ready(ev());
assert.equal(foreign[key], 'e1|someone-else', 'a foreign claim is kept');
""")


def test_deferral_budget_resets_when_a_queued_popup_loses_the_claim():
	_run(r"""
const store = {'optimus_popup_shown:OS-1:ready': 'e1|other'};
const d = desk({store});
d.doc.activeElement = {tagName:'INPUT', type:'text'};
d.handlers.optimus_session_ready(ev());
for (let i = 0; i < 30; i++) d.tick();
assert.equal(d.dialogs.length, 0, 'claimed elsewhere');
d.handlers.optimus_session_ready(ev({event_id:'e2', docname:'OS-2'}));
assert.equal(d.dialogs.length, 0, 'the next popup still waits while typing');
""")


def test_popup_waits_for_another_open_dialog():
	_run(r"""
const open = [{}];
const d = desk({openDialogs:open});
d.handlers.optimus_session_ready(ev());
assert.equal(d.dialogs.length, 0);
open.length = 0;
d.tick();
assert.equal(d.dialogs.length, 1);
""")


def test_popup_skipped_on_the_sessions_own_form():
	_run(r"""
const d = desk({route:['Form','Optimus Session','OS-1']});
d.handlers.optimus_session_ready(ev());
assert.equal(d.dialogs.length, 0);
const o = desk({route:['Form','Optimus Session','OS-9']});
o.handlers.optimus_session_ready(ev());
assert.equal(o.dialogs.length, 1);
""")


def test_failed_popup_has_its_own_copy_and_label():
	_run(r"""
const d = desk();
d.handlers.optimus_session_failed(ev());
assert.equal(d.dialogs[0].cfg.primary_action_label, 'Review and Retry');
assert.ok(d.dialogs[0].cfg.fields[0].options.includes('review the error and retry'));
const r = desk();
r.handlers.optimus_session_ready(ev());
assert.equal(r.dialogs[0].cfg.primary_action_label, 'Open Session');
""")


def test_popup_failure_is_logged_with_console_warn():
	_run(r"""
const warns = [];
const d = desk({dialogThrows:true, warns});
d.handlers.optimus_session_ready(ev());
assert.equal(warns.length, 1);
""")


def test_popup_works_when_local_storage_is_unavailable():
	_run(r"""
const d = desk({storageThrows:true});
d.handlers.optimus_session_ready(ev());
assert.equal(d.dialogs.length, 1);
d.dialogs[0].hide();
d.handlers.optimus_session_ready(ev());
assert.equal(d.dialogs.length, 1, 'in-memory claim stops a duplicate');
d.handlers.optimus_session_ready(ev({event_id:'e2'}));
assert.equal(d.dialogs.length, 2, 'a new run still shows');
""")


def test_deferral_keeps_a_single_pending_timer():
	_run(r"""
const d = desk();
d.doc.activeElement = {tagName:'TEXTAREA'};
d.handlers.optimus_session_ready(ev());
d.handlers.optimus_session_ready(ev({event_id:'e2', docname:'OS-2'}));
assert.equal(d.timers.size, 1);
d.setVisibility('visible');
assert.equal(d.timers.size, 1, 'a re-flush replaces the timer');
""")


def test_handlers_register_once_the_socket_exists():
	_run(r"""
const d = desk({noSocket: true});
assert.equal(Object.keys(d.handlers).length, 0);
assert.equal(d.timers.size, 1);
d.tick();
assert.equal(Object.keys(d.handlers).length, 0);
d.realtime.socket = {};
d.tick();
assert.ok(d.handlers.optimus_session_ready);
assert.equal(d.timers.size, 0);
const first = d.handlers.optimus_session_ready;
d.subscribeRealtime();
d.tick();
assert.equal(d.handlers.optimus_session_ready, first);
d.handlers.optimus_session_ready(ev());
assert.equal(d.dialogs.length, 1);
""")


def test_realtime_retry_gives_up_without_a_socket():
	_run(r"""
const d = desk({noSocket: true});
for (let i = 0; i < 200; i++) d.tick();
assert.equal(d.timers.size, 0);
assert.equal(Object.keys(d.handlers).length, 0);
""")


def test_each_event_is_registered_exactly_once_and_popup_fires_once():
	_run(r"""
const calls = [];
const d = desk({noSocket: true, onCalls: calls});
d.realtime.socket = {};
d.tick();
d.subscribeRealtime();
d.tick();
for (const n of new Set(calls)) assert.equal(calls.filter(c => c === n).length, 1, n);
assert.ok(calls.includes('optimus_session_ready') && calls.includes('optimus_session_failed'));
d.handlers.optimus_session_ready(ev({empty: true}));
assert.equal(d.dialogs.length, 1);
assert.equal(d.alerts.length, 1);
assert.deepEqual(d.pills, [{display: 'ready', label: 'No data'}]);
""")
