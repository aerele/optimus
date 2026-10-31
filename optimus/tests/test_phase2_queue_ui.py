"""Line profiling UI reports queue admission, bounds batches, and preserves dirty forms."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_phase2_retries_are_bounded_and_never_report_queued_work_as_complete():
	node = shutil.which("node")
	if not node:
		pytest.skip("node not installed")
	source = Path(__file__).parents[1] / "optimus/doctype/optimus_session/optimus_session.js"
	script = r'''
const vm = require("node:vm"), fs = require("node:fs"), assert = require("node:assert/strict");
const calls = [], alerts = [], buttons = new Map(), events = new Map();
const ctx = {__:(text,args=[])=>text.replace(/\{(\d+)\}/g,(_,n)=>args[n]),
  frappe:{utils:{escape_html:s=>String(s).replace(/</g,"&lt;").replace(/>/g,"&gt;")},realtime:{on:(name,fn)=>events.set(name,fn)},ui:{form:{on:()=>{}}},call:x=>calls.push(x),show_alert:x=>alerts.push(x),msgprint:x=>alerts.push(x)}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],"utf8"),ctx);
ctx._phase2_armed_banner=()=>{};
let dirty=false, reloads=0;
const frm={is_new:()=>false,is_dirty:()=>dirty,reload_doc:()=>{reloads++;},
 doc:{session_uuid:"fake-session",status:"Ready",phase_2_runs:Array.from({length:8},(_,i)=>({run_uuid:"fake-run-"+i,status:"Failed"}))},
 add_custom_button:(name,fn)=>buttons.set(name,fn)};
ctx.render_phase2_button(frm);
const batch=[...buttons].find(([name])=>/Retry (all|next)/.test(name))[1];
batch();
assert.equal(calls.length,1);
assert.equal(calls[0].args.run_uuids.length,5,"server accepts at most five retries");
dirty=true;
calls.shift().callback({message:{tallies:{Analyzing:5,Failed:0}}});
assert.equal(reloads,0,"queue acknowledgement must keep edits made during the request");
assert.match(alerts[0].message,/queued/i);
assert.doesNotMatch(alerts[0].message,/finished|Ready/);
dirty=false;
const single=[...buttons].find(([name])=>name.startsWith("Retry Line Profiling Analyze"))[1];
single();calls.shift().callback({message:{status:"Analyzing"}});
assert.match(alerts.at(-1).message,/queued/i);
assert.equal(alerts.at(-1).indicator,"blue");
dirty=true;single();batch();
assert.equal(calls.length,0,"do not reload over unsaved form edits");
ctx.subscribe_phase2_events(frm);
events.get("phase_2_run_ready")({session_uuid:"another-session",render_pending:true});
const beforeAlerts=alerts.length,beforeReloads=reloads;
events.get("phase_2_run_ready")({session_uuid:"fake-session",render_pending:true});
assert.match(alerts[beforeAlerts].message,/results saved.*Regenerate Reports/);
assert.doesNotMatch(alerts[beforeAlerts].message,/report ready/i);
assert.equal(alerts[beforeAlerts].indicator,"orange");
assert.equal(reloads,beforeReloads,"pending report notice must preserve dirty content");
dirty=false;
events.get("phase_2_run_ready")({session_uuid:"fake-session",render_pending:false});
assert.equal(alerts.at(-1).message,"Line profiling report ready");
assert.equal(alerts.at(-1).indicator,"green");
assert.equal(reloads,beforeReloads+1);
batch();calls.shift().callback({message:{tallies:{Analyzing:0,Refused:5}}});
assert.match(alerts.at(-1).message,/0 queued or running; 5 refused or failed/);
assert.equal(alerts.at(-1).indicator,"orange");
single();calls.shift().callback({message:{status:"Refused",message:"Retry limit <record again>"}});
assert.equal(alerts.at(-1).message,"Retry limit &lt;record again&gt;");
'''
	out = subprocess.run([node, "-e", script, str(source)], capture_output=True, text=True)
	assert out.returncode == 0, out.stderr
