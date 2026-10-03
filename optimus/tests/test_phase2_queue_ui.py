"""Phase 2 UI reports queue admission, bounds batches, and preserves dirty forms."""

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
const calls = [], alerts = [], buttons = new Map();
const ctx = {__:(text,args=[])=>text.replace(/\{(\d+)\}/g,(_,n)=>args[n]),
  frappe:{ui:{form:{on:()=>{}}},call:x=>calls.push(x),show_alert:x=>alerts.push(x),msgprint:x=>alerts.push(x)}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],"utf8"),ctx);
ctx._phase2_armed_banner=()=>{};
let dirty=false, reloads=0;
const frm={is_new:()=>false,is_dirty:()=>dirty,reload_doc:()=>{reloads++;},
 doc:{status:"Ready",phase_2_runs:Array.from({length:8},(_,i)=>({run_uuid:"fake-run-"+i,status:"Failed"}))},
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
const single=[...buttons].find(([name])=>name.startsWith("Retry Phase 2 Analyze"))[1];
single();calls.shift().callback({message:{status:"Analyzing"}});
assert.match(alerts.at(-1).message,/queued/i);
assert.equal(alerts.at(-1).indicator,"blue");
dirty=true;single();batch();
assert.equal(calls.length,0,"do not reload over unsaved form edits");
'''
	out = subprocess.run([node, "-e", script, str(source)], capture_output=True, text=True)
	assert out.returncode == 0, out.stderr
