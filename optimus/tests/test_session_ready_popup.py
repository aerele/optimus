# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Run the widget's session-complete popup logic in node with a small Desk stub."""

import shutil
import subprocess
from pathlib import Path

import pytest

WIDGET_JS = Path(__file__).resolve().parents[1] / "public/js/floating_widget.js"

HARNESS = r"""
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
function desk(opts = {}) {
 const handlers={},dialogs=[],alerts=[],timers=[],store=opts.store||{};
 const doc={activeElement:null};
 class Dialog {
  constructor(cfg){ if(opts.dialogThrows && opts.throwsLeft-- !== 0) throw new Error('boom'); this.cfg=cfg; dialogs.push(this); }
  show(){ this.shown=true; }
  hide(){ this.shown=false; if(this.onhide) this.onhide(); }
 }
 const ctx={window:{},document:Object.assign(doc,{readyState:'loading',addEventListener:()=>{}}),
  setTimeout:(fn)=>{timers.push(fn);return timers.length;},clearTimeout:()=>{},clearInterval:()=>{},
  localStorage:{getItem:k=>(k in store?store[k]:null),setItem:(k,v)=>{store[k]=v;},removeItem:k=>{delete store[k];}},
  __:t=>t,
  frappe:{session:{user:'owner'},set_route:()=>{},ui:{Dialog},utils:{escape_html:s=>String(s)},
   realtime:{on:(n,fn)=>handlers[n]=fn},show_alert:a=>alerts.push(a)}};
 vm.createContext(ctx);
 let code=fs.readFileSync(process.argv[1],'utf8');
 code=code.replace(/\}\)\(\);\s*$/,`globalThis.w={subscribeRealtime,state:currentState};})();`);
 vm.runInContext(code,ctx);ctx.w.subscribeRealtime();
 const tick=()=>{ const fns=timers.splice(0); fns.forEach(f=>f()); };
 return {handlers,dialogs,alerts,store,doc,tick,state:ctx.w.state};
}
const ev=(o={})=>({session_uuid:'s1',docname:'OS-1',recording_user:'owner',event_id:'e1',...o});
"""


def _run(body: str) -> None:
	if not shutil.which("node"):
		pytest.skip("node not installed")
	result = subprocess.run(
		["node", "-e", HARNESS + body, str(WIDGET_JS)], capture_output=True, text=True
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


def test_empty_session_sets_no_data_pill_and_toast():
	_run(r"""
const d = desk();
d.handlers.optimus_session_ready(ev({empty:true}));
assert.equal(d.state.display, 'ready');
assert.equal(d.alerts.length, 1);
assert.ok(!/ready/i.test(d.alerts[0].message), d.alerts[0].message);
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


def test_empty_analyze_publishes_ready_with_empty_flag(monkeypatch):
	"""The session event payload carries the empty flag through to the realtime publish."""
	from optimus import analyze, realtime

	seen = []
	monkeypatch.setattr(realtime, "publish_session_event", lambda event, payload: seen.append((event, payload)))
	analyze._publish_session_event(
		"optimus_session_ready", session_uuid="s1", docname="OS-1", empty=True
	)
	assert seen == [
		("optimus_session_ready", {"session_uuid": "s1", "docname": "OS-1", "empty": True})
	]
