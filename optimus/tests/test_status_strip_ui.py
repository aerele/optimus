"""Run the slim analyze status strip logic against a fake banner element."""

import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "optimus/doctype/optimus_session/optimus_session.js"

SCRIPT = r"""
const vm = require("node:vm"), fs = require("node:fs"), assert = require("node:assert/strict");
const ctx = {
  __: (text, args=[]) => text.replace(/\{(\d+)\}/g, (_, n) => args[n]),
  frappe: {ui: {form: {on: () => {}}}, router: {on: () => {}},
    utils: {escape_html: v => String(v).replace(/[&<>"'`=]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "`": "&#x60;", "=": "&#x3D;"}[c]))}},
};
vm.createContext(ctx); vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
let banner = null, rewrites = 0;
function make(html) {
  const b = {attrs: {}, bar: null, text: null};
  const set = h => { rewrites++; b.html_src = h;
    const m = h.match(/aria-valuenow="(\d+)"[^]*?style="width:(\d+)%"/);
    b.bar = m ? {now: +m[1], width: m[2] + "%", label: h.match(/aria-label="([^"]*)"/)[1]} : null;
    b.text = h.match(/optimus-status-text"[^>]*>([^]*?)<\/span>/)[1]; };
  set(html);
  b.html = set;
  b.attr = (k, v) => { b.attrs[k] = v; };
  const kids = () => ({css: (k, v) => { b.bar.width = v; }});
  b.find = sel => sel === ".optimus-status-bar"
    ? {length: b.bar ? 1 : 0, children: kids,
       attr: o => { b.bar.now = o["aria-valuenow"]; b.bar.label = o["aria-label"]; return {children: kids}; }}
    : {html: h => { b.text = h; }};
  return b;
}
ctx._single_banner = (frm, id, cls, html, update) => {
  if (html == null) { banner = null; return null; }
  if (!banner) banner = make(html); else if (update) update(banner); else banner.html(html);
  return banner;
};
ctx.frm = {doc: {session_uuid: "s1"}};
const strip = (pct, desc) => vm.runInContext(
  "_analyze_progress_strip(frm, " + pct + ", " + JSON.stringify(desc) + ")", ctx);
strip(40, "Analyzing queries");
assert.equal(banner.bar.now, 40);
assert.equal(banner.bar.width, "40%");
assert.match(banner.bar.label, /Preparing report 40% · Analyzing queries/);
assert.equal(banner.attrs["data-optimus-session"], "s1");
const first = rewrites;
strip(55.4, "Building");
assert.equal(rewrites, first, "bar must be patched in place, not rebuilt");
assert.equal(banner.bar.width, "55%");
assert.equal(banner.bar.now, 55);
strip(120, "x");
assert.equal(banner.bar.now, 100);
assert.match(banner.text, /Preparing report 100%/);
strip(-5, "x");
assert.equal(banner.bar.now, 0);
assert.match(banner.text, /Preparing report 0%/);
strip("null", "<b>x</b>");
assert.equal(banner.bar, null, "no percent means no bar");
assert.match(banner.html_src, /&lt;b&gt;x&lt;\/b&gt;/);
strip(10, "y");
assert.equal(banner.bar.now, 10, "bar comes back when a percent arrives");
assert.match(banner.html_src, /<span class="optimus-status-text" role="status" aria-live="polite">/);
assert.doesNotMatch(banner.html_src, /optimus-status-bar"[^>]*role="status"/, "the bar is not a live region");
let drained = null;
ctx._single_banner = (frm, id, cls, html) => { drained = html; };
vm.runInContext("_drain_banner(frm, 'Waiting for jobs')", ctx);
assert.match(drained, /^<span class="optimus-status-text" role="status" aria-live="polite">Waiting for jobs<\/span>$/);
vm.runInContext("_drain_banner(frm, '<i>x</i>')", ctx);
assert.match(drained, /&lt;i&gt;x&lt;\/i&gt;/, "drain text is escaped");
console.log("ok");
"""


def test_analyze_strip_updates_bar_in_place_and_clamps_percent():
	node = shutil.which("node")
	if not node:
		pytest.skip("node not installed")
	result = subprocess.run([node, "-e", SCRIPT, str(SOURCE)], capture_output=True, text=True, timeout=60)
	assert result.returncode == 0, result.stderr
	assert "ok" in result.stdout
