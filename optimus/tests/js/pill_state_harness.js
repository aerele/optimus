// Node harness for optimus_pill_state.js. Run by tests/test_pill_state_js.py.
"use strict";
const assert = require("assert");
const path = require("path");
const Pill = require(path.join(__dirname, "..", "..", "public", "js", "optimus_pill_state.js"));

const T0 = 1_000_000;
const analyzing = (extra) => Object.assign({}, Pill.initialState(), { display: "analyzing", session_uuid: "s1", docname: "OS-1" }, extra);
const ev = (state, name, data, now) => Pill.reduceEvent(state, name, Object.assign({ session_uuid: "s1", docname: "OS-1" }, data), now || T0);
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("ready event clears analyzing and toasts", () => {
	const r = ev(analyzing(), "optimus_session_ready");
	assert.strictEqual(r.state.display, "ready");
	assert.strictEqual(r.ui.label, "Report ready");
	assert.ok(r.alert);
});

test("failed event clears analyzing", () => {
	const r = ev(analyzing(), "optimus_session_failed");
	assert.strictEqual(r.ui.label, "Analyze failed");
	assert.strictEqual(r.alert.indicator, "red");
	assert.strictEqual(r.ui.tone, "failed");
});

test("phase 2 ready/failed clear analyzing and drop the run", () => {
	const s = analyzing({ phase2: true, run_uuid: "r1" });
	const ok = ev(s, "phase_2_run_ready", { run_uuid: "r1", parent: "OS-1" });
	assert.strictEqual(ok.state.display, "ready");
	assert.strictEqual(ok.state.run_uuid, null);
	const bad = ev(s, "phase_2_run_failed", { run_uuid: "r1", error: "boom" });
	assert.strictEqual(bad.state.display, "ready");
	assert.strictEqual(bad.alert.message, "Line profiling analyze failed: {0}");
	assert.deepStrictEqual(bad.alert.args, ["boom"]);
});

test("phase 2 ready with the report update owed says results saved", () => {
	const s = analyzing({ phase2: true, run_uuid: "r1" });
	const owed = ev(s, "phase_2_run_ready", { run_uuid: "r1", parent: "OS-1", render_pending: true });
	assert.strictEqual(owed.ui.label, "Line profiling results saved");
	assert.strictEqual(owed.alert.message, "Line profiling results saved. Use Regenerate Reports to update the report.");
	assert.strictEqual(owed.alert.indicator, "orange");
	const done = ev(s, "phase_2_run_ready", { run_uuid: "r1", parent: "OS-1", render_pending: false });
	assert.strictEqual(done.ui.label, "Line profiling report ready");
	assert.strictEqual(done.alert.indicator, "blue");
	const first = ev(analyzing(), "optimus_session_ready", { render_pending: true });
	assert.strictEqual(first.ui.label, "Report ready");
});

test("normal ready with no requests recorded says no data", () => {
	const empty = ev(analyzing(), "optimus_session_ready", { empty: true });
	assert.strictEqual(empty.ui.label, "No data");
	assert.strictEqual(empty.alert.message, "Profiling finished, but no requests were recorded");
	assert.strictEqual(empty.alert.indicator, "orange");
	const p2 = ev(analyzing({ phase2: true, run_uuid: "r1" }), "phase_2_run_ready", { run_uuid: "r1", parent: "OS-1", render_pending: true, empty: true });
	assert.strictEqual(p2.ui.label, "Line profiling results saved");
	assert.strictEqual(p2.alert.message, "Line profiling results saved. Use Regenerate Reports to update the report.");
	assert.strictEqual(p2.alert.indicator, "orange");
	const plain = ev(analyzing(), "optimus_session_ready", { empty: false });
	assert.strictEqual(plain.ui.label, "Report ready");
	assert.strictEqual(plain.alert.indicator, "blue");
});

test("late analyzing event after ready is ignored (reordered delivery)", () => {
	const done = ev(analyzing(), "optimus_session_ready", {}, T0).state;
	assert.strictEqual(ev(done, "optimus_session_analyzing", {}, T0 + 100), null);
	assert.strictEqual(ev(done, "optimus_progress", { percent: 40 }, T0 + 100), null);
});

test("a retry analyze after the stale window shows analyzing again", () => {
	const failed = ev(analyzing(), "optimus_session_failed", {}, T0).state;
	const r = ev(failed, "optimus_session_analyzing", {}, T0 + Pill.STALE_EVENT_WINDOW_MS + 1);
	assert.strictEqual(r.state.display, "analyzing");
});

test("events for another session are ignored", () => {
	assert.strictEqual(ev(analyzing(), "optimus_session_ready", { session_uuid: "other" }), null);
});

test("progress only repaints while analyzing", () => {
	assert.strictEqual(ev(Pill.initialState(), "optimus_progress", { percent: 5 }), null);
	assert.strictEqual(ev(analyzing(), "optimus_progress", { percent: 41.6 }).ui.label, "Analyzing 42%");
});

test("server ready clears a stuck analyzing pill (missed event)", () => {
	const r = Pill.reduceServer(analyzing(), { phase: "ready", scope: "session", session_uuid: "s1", docname: "OS-1" }, T0);
	assert.strictEqual(r.state.display, "ready");
	assert.strictEqual(r.ui.label, "Report ready");
});

test("server failed and phase 2 ready are mapped", () => {
	assert.strictEqual(Pill.reduceServer(analyzing(), { phase: "failed", scope: "session", session_uuid: "s1" }, T0).ui.label, "Analyze failed");
	const p2 = Pill.reduceServer(analyzing({ phase2: true }), { phase: "ready", scope: "phase2", session_uuid: "s1", run_uuid: "r1" }, T0);
	assert.strictEqual(p2.ui.label, "Line profiling report ready");
});

test("server idle clears analyzing, leaves stopping and phase 2 recording alone", () => {
	assert.strictEqual(Pill.reduceServer(analyzing(), { phase: "idle" }, T0).state.display, "inactive");
	assert.strictEqual(Pill.reduceServer(analyzing({ display: "stopping" }), { phase: "idle" }, T0), null);
	assert.strictEqual(Pill.reduceServer(analyzing({ display: "recording", phase2: true }), { phase: "idle" }, T0), null);
});

test("server analyzing never overrides a ready pill", () => {
	const ready = analyzing({ display: "ready" });
	assert.strictEqual(Pill.reduceServer(ready, { phase: "analyzing", scope: "session", session_uuid: "s1" }, T0), null);
});

test("reload recovery: inactive pill adopts an in-flight analyze, phase 2 included", () => {
	const r = Pill.reduceServer(Pill.initialState(), { phase: "analyzing", scope: "phase2", session_uuid: "s1", docname: "OS-1", run_uuid: "r1" }, T0);
	assert.strictEqual(r.state.display, "analyzing");
	assert.strictEqual(r.state.run_uuid, "r1");
	assert.strictEqual(r.ui.label, "Line profiling analyzing…");
});

test("stale recording snapshot is ignored once stopping/analyzing/ready", () => {
	for (const display of ["stopping", "analyzing", "ready"]) {
		assert.strictEqual(Pill.reduceServer(analyzing({ display }), { phase: "recording", session_uuid: "s1" }, T0), null);
	}
	assert.strictEqual(Pill.reduceServer(Pill.initialState(), { phase: "recording", session_uuid: "s1" }, T0).state.display, "recording");
});

test("duplicate ready (event then poll) toasts once", () => {
	const first = ev(analyzing(), "optimus_session_ready");
	const again = Pill.reduceServer(first.state, { phase: "ready", scope: "session", session_uuid: "s1", docname: "OS-1" }, T0 + 1);
	assert.strictEqual(again.alert, null);
});

test("hard stop clears a transient pill", () => {
	assert.strictEqual(Pill.reduceHardStop(analyzing()).ui.label, "Check report");
	assert.strictEqual(Pill.reduceHardStop(analyzing()).ui.tone, "stale");
	const noDoc = Pill.reduceHardStop(analyzing({ docname: null }));
	assert.strictEqual(noDoc.state.display, "inactive");
	assert.strictEqual(Pill.reduceHardStop(analyzing({ display: "ready" })), null);
});

test("poll backoff grows, caps, and hard stop is bounded", () => {
	const delays = [0, 1, 2, 3, 20].map(Pill.nextDelay);
	assert.strictEqual(delays[0], Pill.POLL.baseMs);
	assert.ok(delays[1] > delays[0] && delays[2] > delays[1]);
	assert.strictEqual(delays[4], Pill.POLL.maxMs);
	assert.ok(!Pill.pollExpired(Pill.POLL.hardStopMs - 1));
	assert.ok(Pill.pollExpired(Pill.POLL.hardStopMs));
	// Total requests before the hard stop stay modest.
	let t = 0, n = 0;
	while (!Pill.pollExpired(t)) { t += Pill.nextDelay(n++); }
	assert.ok(n < 60, "too many polls: " + n);
});

test("pollAction: stop, wait, expire and fetch", () => {
	const hs = Pill.POLL.hardStopMs;
	assert.strictEqual(Pill.pollAction("ready", 0, false), "stop");
	assert.strictEqual(Pill.pollAction("inactive", hs, true), "stop");
	assert.strictEqual(Pill.pollAction("analyzing", 0, true), "wait");
	assert.strictEqual(Pill.pollAction("analyzing", hs + 1, true), "wait");
	assert.strictEqual(Pill.pollAction("analyzing", hs, false), "expire");
	assert.strictEqual(Pill.pollAction("analyzing", hs - 1, false), "fetch");
	assert.strictEqual(Pill.pollAction("stopping", 0, false), "fetch");
	assert.strictEqual(Pill.pollAction("stopping", hs, false), "expire");
});

test("a resynced phase 2 failure has no placeholder text", () => {
	const r = Pill.reduceServer(analyzing({ phase2: true }), { phase: "failed", scope: "phase2", session_uuid: "s1", run_uuid: "r1" }, T0);
	assert.ok(!r.alert.message.includes("see session"));
	assert.ok(!r.alert.message.includes("undefined"));
});

test("resetState drops the session but keeps the finished-uuid memory", () => {
	const done = ev(analyzing(), "optimus_session_ready").state;
	const r = Pill.resetState(done);
	assert.strictEqual(r.display, "inactive");
	assert.strictEqual(r.session_uuid, null);
	assert.deepStrictEqual(r.terminal, done.terminal);
});

test("reducers never mutate their input", () => {
	const s = analyzing();
	const frozen = JSON.stringify(s);
	ev(s, "optimus_session_ready");
	Pill.reduceServer(s, { phase: "idle" }, T0);
	assert.strictEqual(JSON.stringify(s), frozen);
});

let failed = 0;
for (const [name, fn] of tests) {
	try { fn(); console.log("ok   " + name); } catch (e) { failed++; console.log("FAIL " + name + "\n" + e.stack); }
}
process.exit(failed ? 1 : 0);
