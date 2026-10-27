"""Run the real Settings callback, including duplicate clicks and hostile replies."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_connection_probe_escapes_reply_and_recovers_button_after_failure():
	node = shutil.which("node")
	if not node:
		pytest.skip("node not installed")
	source = Path(__file__).resolve().parents[1] / "optimus/doctype/optimus_settings/optimus_settings.js"
	script = r'''
const vm=require("node:vm"), fs=require("node:fs"), assert=require("node:assert/strict");
let handlers, click; const calls=[], messages=[], button={};
const ctx={ __:(s,args=[])=>s.replace(/\{(\d+)\}/g,(_,n)=>args[n]),
  frappe:{ui:{form:{on:(name,h)=>handlers=h}},call:options=>{calls.push(options);},
    show_alert:()=>{},msgprint:m=>messages.push(m),
    utils:{escape_html:s=>String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")}}
};
vm.createContext(ctx); vm.runInContext(fs.readFileSync(process.argv[1],"utf8"),ctx);
const frm={doc:{ai_enabled:true}, fields_dict:{}, is_dirty:()=>false,
  add_custom_button:(label,fn)=>{click=fn;return button;}};
handlers.refresh(frm); calls.length=0;
click(); click(); assert.equal(calls.length,1,"duplicate probes must not overlap");
let call=calls[0]; assert.equal(call.btn,button); assert.equal(call.freeze,true);
assert.match(call.freeze_message,/60/);
call.callback({message:{ok:false,model:"<img src=x onerror=evil()>",message:"<script>evil()</script>"}});
assert.ok(!messages.at(-1).message.includes("<img"));
assert.ok(!messages.at(-1).message.includes("<script"));
call.always(); click(); assert.equal(calls.length,2);
calls[1].error(); calls[1].always(); click(); assert.equal(calls.length,3);
calls[2].always(); frm.is_dirty=()=>true; click(); assert.equal(calls.length,3);
frm.is_dirty=()=>false; const realCall=ctx.frappe.call;
ctx.frappe.call=()=>{throw new Error("request setup failed");};
click(); assert.equal(frm._optimus_ai_probe_pending,false);
ctx.frappe.call=realCall; click(); assert.equal(calls.length,4);
'''
	result = subprocess.run([node, "-e", script, str(source)], capture_output=True, text=True)
	assert result.returncode == 0, result.stdout + result.stderr
