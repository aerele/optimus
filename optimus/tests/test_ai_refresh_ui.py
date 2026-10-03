"""Execute the polling state machine, including reordered replies and navigation."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_refresh_form_handles_stale_progress_and_recovers_without_unlocking_unknown_work():
	node = shutil.which("node")
	if not node:
		pytest.skip("node not installed")
	source = Path(__file__).resolve().parents[1] / "optimus/doctype/optimus_session/optimus_session.js"
	script = r'''
const vm = require("node:vm"), fs = require("node:fs"), assert = require("node:assert/strict");
const pending = [], timers = [], alerts = [];
let route = ["Form", "Optimus Session", "fake-doc"];
const ctx = {
  __: (text, args=[]) => text.replace(/\{(\d+)\}/g, (_, n) => args[n]),
  setTimeout: (fn, ms) => { timers.push({fn, ms}); return timers.length; },
  clearTimeout: () => {},
  frappe: {
    ui: {form: {on: () => {}}}, router: {on: () => {}},
    get_route: () => route, call: args => pending.push(args),
    utils: {escape_html: value => String(value).replace(/[<>&]/g, "_")},
    show_alert: args => alerts.push(args),
  },
};
vm.createContext(ctx); vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
ctx._single_banner = () => ({attr: () => {}, find: () => ({on: () => {}})});
const buttons = new Map();
const frm = {
  doc: {name: "fake-doc", session_uuid: "fake-session", status: "Ready"},
  is_new: () => false, save_disabled: false, reloads: 0,
  disable_save(dirty) { this.save_disabled = true; this.set_dirty = dirty; }, enable_save() { this.save_disabled = false; },
  add_custom_button: (name, cb) => buttons.set(name, cb),
  remove_custom_button: name => buttons.delete(name), reload_doc() {this.reloads++;},
};
ctx.cur_frm = frm;
function reply(req, run, extra={}) { req.callback({message: {status:"ok", can_act:true, plan:null, refresh:run, ...extra}}); }
function run(id, seq, state, when) {return {run_id:id, seq, state, requested_at:when, done:1, failed:0,
  skipped:0, uncertain:0, blocked_uncertain:0, not_reached:0, usage:{tokens_reported:7,incomplete_attempts:0}};}
ctx.render_ai_buttons(frm);
assert.equal(pending[0].method, "optimus.api.ai_refresh_status");
reply(pending.shift(), run("a", 2, "running", 1));
assert.equal(frm.save_disabled, true);
assert.equal(frm.set_dirty, true, "holding Save must preserve the unsaved-edit indicator");
assert.equal(timers.at(-1).ms, 5000);
// A form refresh resets Frappe's Save flag. Reapply our own hold immediately.
frm.save_disabled = false;
ctx.render_ai_buttons(frm);
assert.equal(frm.save_disabled, true);
reply(pending.shift(), null, {status:"unknown"});
assert.equal(frm.save_disabled, true);
assert.equal(timers.at(-1).ms, 10000);
ctx.render_ai_buttons(frm);
const oldReply = pending.shift();
ctx.render_ai_buttons(frm);
// Resumed runs deliberately keep the original selection cutoff.
reply(pending.shift(), run("b", 1, "running", 1));
reply(oldReply, run("a", 9, "complete", 1));
assert.equal(frm.save_disabled, true);
assert.equal(frm._optimus_ai.run.run_id, "b");
// Stale snapshots of the same generation cannot move its state backwards.
ctx.render_ai_buttons(frm);
reply(pending.shift(), run("b", 0, "complete", 1));
assert.equal(frm.save_disabled, true);
ctx.render_ai_buttons(frm);
reply(pending.shift(), run("b", 3, "complete", 1));
assert.equal(frm.save_disabled, false);
assert.equal(frm.reloads, 1);
// Permission-based Save suppression belongs to Frappe, not this feature.
frm.save_disabled = true;
ctx.render_ai_buttons(frm);
reply(pending.shift(), run("c", 1, "running", 3));
ctx.render_ai_buttons(frm);
reply(pending.shift(), run("c", 2, "cancelled", 3));
assert.equal(frm.save_disabled, true);
// Navigation stops polling and late replies cannot repaint the old form.
ctx.render_ai_buttons(frm);
const departed = pending.shift();
route = ["List", "Optimus Session"];
reply(departed, run("d", 1, "running", 4));
assert.notEqual(frm._optimus_ai.run.run_id, "d");
const before = pending.length;
timers.at(-1).fn();
assert.equal(pending.length, before);
// A lost admission response with no committed run must eventually release our hold.
route = ["Form", "Optimus Session", "fake-doc"];
frm.save_disabled = false;
frm._optimus_ai.run = null;
frm._optimus_ai.busy = true;
ctx._ai_hold_save(frm, frm._optimus_ai);
ctx.render_ai_buttons(frm);
reply(pending.shift(), null);
assert.equal(frm.save_disabled, false);
// A form refresh during POST must also restore a hold reset by Frappe.
frm._optimus_ai.submitting = true;
frm._optimus_ai.held_save = true;
frm.save_disabled = false;
ctx.render_ai_buttons(frm);
assert.equal(frm.save_disabled, true);

// The first poll can fail while another process owns an unknown run.
const unknownFrm = {...frm, save_disabled:false, _optimus_ai:null, reloads:0};
ctx.cur_frm = unknownFrm;pending.length=0;
ctx.render_ai_buttons(unknownFrm);pending.shift().error();
assert.equal(unknownFrm.save_disabled,true,"initial unknown SQL state must hold Save");
unknownFrm.save_disabled=false;
ctx.render_ai_buttons(unknownFrm);
assert.equal(unknownFrm.save_disabled,true,"a form refresh must reapply the unknown-state hold");
reply(pending.shift(),null);
assert.equal(unknownFrm.save_disabled,false);
// Users can type into a form while background work runs. Keep those edits.
ctx.render_ai_buttons(unknownFrm);reply(pending.shift(),run("dirty-run",1,"running",3));
unknownFrm.is_dirty=()=>true;
ctx.render_ai_buttons(unknownFrm);reply(pending.shift(),run("dirty-run",2,"complete",3));
assert.equal(unknownFrm.reloads,0,"completion must not discard unsaved form content");
// Permission or workflow restrictions may change while our own hold is active.
for (const restriction of [{read_only:true}, {perm:[{write:0}]}]) {
  const restricted = {...frm, save_disabled:false, _optimus_ai:null, read_only:false, perm:[{write:1}]};
  pending.length=0;
  ctx.render_ai_buttons(restricted);reply(pending.shift(),run("permissions",1,"running",5));
  Object.assign(restricted,restriction);
  ctx.render_ai_buttons(restricted);reply(pending.shift(),run("permissions",2,"complete",5));
  assert.equal(restricted.save_disabled,true,"completion must respect current write restrictions");
}
'''
	out = subprocess.run([node, "-e", script, str(source)], text=True, capture_output=True)
	assert out.returncode == 0, out.stderr


def test_real_dialogs_preserve_opt_in_and_cancel_replies_cannot_regress_newer_state():
	node = shutil.which("node")
	if not node:
		pytest.skip("node not installed")
	source = Path(__file__).resolve().parents[1] / "optimus/doctype/optimus_session/optimus_session.js"
	script = r'''
const vm=require("node:vm"),fs=require("node:fs"),assert=require("node:assert/strict");
const requests=[],dialogs=[],buttons=new Map(),messages=[];
let dirty=false,cancel;
const ctx={__:(text,args=[])=>text.replace(/\{(\d+)\}/g,(_,n)=>args[n]),setTimeout:()=>1,clearTimeout:()=>{},
 frappe:{ui:{form:{on:()=>{}},Dialog:class {constructor(options){this.options=options;dialogs.push(this);}show(){}hide(){}}},
 router:{on:()=>{}},get_route:()=>["Form","Optimus Session","fake-doc"],call:r=>requests.push(r),
 utils:{escape_html:String},show_alert:r=>messages.push(r),msgprint:r=>messages.push(r)}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],"utf8"),ctx);
ctx._single_banner=()=>({find:()=>({on:(_event,handler)=>{cancel=handler;}})});
const frm={doc:{name:"fake-doc",session_uuid:"fake-session",status:"Ready"},is_new:()=>false,is_dirty:()=>dirty,
 add_custom_button:(name,fn)=>buttons.set(name,fn),remove_custom_button:name=>buttons.delete(name),
 disable_save(){this.save_disabled=true;},enable_save(){this.save_disabled=false;},reload_doc:()=>{}};
ctx.cur_frm=frm;ctx.render_ai_buttons(frm);
const plan={total:4,pending:0,selected:0,selected_all:4,steps:false,cap:20};
requests.shift().callback({message:{status:"ok",refresh:null,can_act:true,plan}});
buttons.get("Refresh AI suggestions")();
const confirm=dialogs.pop();
confirm.options.primary_action({regenerate_all:false});assert.equal(requests.length,0);
confirm.options.primary_action({regenerate_all:true});
const admission=requests.shift();assert.equal(admission.args.regenerate_all,true);
assert.equal(admission.method,"optimus.api.refill_ai_suggestions");
const active={run_id:"run-a",scope:"all",state:"running",seq:1,requested_at:1,usage:{},retry_no:0};
admission.callback({message:{ok:true,refresh:active}});
cancel();const cancellation=requests.shift();
assert.equal(cancellation.args.run_id,"run-a");
// A later poll has already observed completion before the old cancel error.
ctx.render_ai_buttons(frm);
requests.shift().callback({message:{status:"ok",refresh:{...active,state:"interrupted",seq:3,
 uncertain:1,end_reason:"worker_interrupted"},can_act:true,plan}});
assert.equal(frm.save_disabled,false);
cancellation.error();
assert.equal(frm.save_disabled,false,"old cancel error cannot replace known newer SQL state");
assert.match(ctx._ai_end_message(frm._optimus_ai.run),/may have been billed/);
buttons.get("Resume AI refresh")();const resume=dialogs.pop();
const consent=resume.options.fields.find(f=>f.fieldname==="retry_uncertain");
assert.equal(consent.default,0);assert.equal(consent.hidden,false);
dirty=true;resume.options.primary_action({retry_uncertain:true});assert.equal(requests.length,0);
dirty=false;resume.options.primary_action({retry_uncertain:false});
const resumed=requests.shift();assert.equal(resumed.args.resume_from,"run-a");
assert.equal(resumed.args.retry_uncertain,false);
'''
	out = subprocess.run([node, "-e", script, str(source)], text=True, capture_output=True)
	assert out.returncode == 0, out.stderr
