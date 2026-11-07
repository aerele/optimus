// Runs floating_widget.js in a vm with fake timers and frappe.call. Run by test_pill_state_js.py.
"use strict";
const vm = require("vm");
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const JS = path.join(__dirname, "..", "..", "public", "js");

function makeWidget() {
	const el = { classes: new Set(), label: { textContent: "" }, elapsed: { textContent: "" }, attrs: {}, title: "", focused: 0 };
	el.focus = () => { el.focused += 1; };
	el.classList = { remove: (...c) => c.forEach((x) => el.classes.delete(x)), add: (c) => el.classes.add(c) };
	el.querySelector = (sel) => (sel === ".fp-label" ? el.label : el.elapsed);
	el.setAttribute = (k, v) => { el.attrs[k] = v; };
	el.removeAttribute = (k) => { delete el.attrs[k]; };
	return el;
}

// Frappe's Dialog as the widget uses it; a click on Start is env.dialog.primary_action(values).
class FakeDialog {
	constructor(opts) {
		Object.assign(this, opts);
		this.disabled = false;
		this.hidden = false;
		this.value = opts.fields[0].default;
		FakeDialog.last = this;
	}
	show() {}
	hide() { this.hidden = true; if (this.onhide) this.onhide(); }
	get_value() { return this.value; }
	disable_primary_action() { this.disabled = true; }
	enable_primary_action() { this.disabled = false; }
	// jQuery's trigger("click") runs the handler even on a disabled button.
	click() { return this.primary_action({ label: this.value }); }
}

function load() {
	const env = { now: 1_000_000, hidden: false, timers: [], calls: [], intervals: 0, cleared: 0, reply: null, alerts: [], active: null };
	const doc = { readyState: "loading", addEventListener: () => {}, body: {} };
	Object.defineProperty(doc, "hidden", { get: () => env.hidden });
	Object.defineProperty(doc, "activeElement", { get: () => env.active });
	const ctx = {
		window: {}, document: doc, __: (t) => t,
		Date: class extends Date { static now() { return env.now; } },
		setTimeout: (fn, ms) => { const t = { fn, ms, at: env.now + ms, live: true }; env.timers.push(t); return t; },
		clearTimeout: (t) => { if (t) t.live = false; },
		setInterval: () => { env.intervals += 1; return env.intervals; },
		clearInterval: () => { env.cleared += 1; },
		frappe: {
			session: { user: "u" }, boot: {}, get_route: () => ["List", "ToDo"], ui: { Dialog: FakeDialog }, set_route: () => {}, show_alert: (a) => env.alerts.push(a),
			call: (opts) => {
				env.calls.push(opts);
				const message = env.reply ? env.reply(opts) : undefined;
				if (message !== undefined) opts.callback({ message });
			},
		},
	};
	vm.createContext(ctx);
	vm.runInContext(fs.readFileSync(path.join(JS, "optimus_pill_state.js"), "utf8"), ctx);
	const code = fs.readFileSync(path.join(JS, "floating_widget.js"), "utf8").replace(
		/\}\)\(\);\s*$/,
		"widget = makeWidget();\nglobalThis.t = { applyResult, openStartDialog, setDisplay, refreshStatus, onClick, confirmAndStop, stopPhase2, get widget() { return widget; }, startElapsedTimer, computeElapsed," +
		" get state() { return currentState; }, set state(v) { currentState = v; }, Pill };})();"
	);
	ctx.makeWidget = makeWidget;
	vm.runInContext(code, ctx);
	env.t = ctx.t;
	env.ctx = ctx;
	env.live = () => env.timers.filter((x) => x.live);
	// Fire the next live timer, advancing the clock to it.
	env.fire = () => {
		const live = env.live().sort((a, b) => a.at - b.at);
		if (!live.length) return false;
		const t = live[0];
		t.live = false;
		env.now = Math.max(env.now, t.at);
		t.fn();
		return true;
	};
	return env;
}

const analyzingReply = { phase: "analyzing", scope: "session", session_uuid: "s1", docname: "OS-1" };
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

function enterAnalyzing(env) {
	env.t.applyResult(env.t.Pill.reduceServer(env.t.state, analyzingReply, env.now));
}

test("entering analyzing schedules a poll at the base delay", () => {
	const env = load();
	enterAnalyzing(env);
	assert.strictEqual(env.live().length, 1);
	assert.strictEqual(env.live()[0].ms, env.t.Pill.POLL.baseMs);
});

test("a stop click drops an in-flight resync reply", () => {
	const env = load();
	env.t.state = Object.assign(env.t.state, { display: "recording", session_uuid: "s1" });
	env.t.refreshStatus();
	const pending = env.calls[0];
	env.t.confirmAndStop();
	pending.callback({ message: { phase: "ready", session_uuid: "s1", scope: "session", docname: "OS-1" } });
	assert.strictEqual(env.t.state.display, "stopping");
});

test("a phase-2 stop click drops an in-flight resync reply", () => {
	const env = load();
	env.t.state = Object.assign(env.t.state, { display: "recording", phase2: true, run_uuid: "r1", session_uuid: "s1" });
	env.t.refreshStatus();
	const pending = env.calls[0];
	env.t.stopPhase2();
	pending.callback({ message: { phase: "ready", session_uuid: "s1", scope: "session", docname: "OS-1" } });
	assert.strictEqual(env.t.state.display, "stopping");
});

test("failed and check-report states get their own tone class", () => {
	const env = load();
	env.t.applyResult({ state: env.t.state, ui: { display: "ready", label: "Analyze failed", elapsed: "", tone: "failed" } });
	assert.ok(env.t.widget.classes.has("fp-tone-failed"));
	env.t.applyResult({ state: env.t.state, ui: { display: "ready", label: "Check report", elapsed: "", tone: "stale" } });
	assert.ok(env.t.widget.classes.has("fp-tone-stale") && !env.t.widget.classes.has("fp-tone-failed"));
	env.t.applyResult({ state: env.t.state, ui: { display: "ready", label: "Report ready", elapsed: "" } });
	assert.ok(!env.t.widget.classes.has("fp-tone-stale"));
});

test("a ready reply clears the pill and stops the poll", () => {
	const env = load();
	enterAnalyzing(env);
	env.reply = () => Object.assign({}, analyzingReply, { phase: "ready" });
	env.fire();
	assert.strictEqual(env.t.state.display, "ready");
	assert.strictEqual(env.live().length, 0);
});

test("still-analyzing replies grow the delay and end at Check report within the hard stop", () => {
	const env = load();
	enterAnalyzing(env);
	const start = env.now;
	env.reply = () => analyzingReply;
	const delays = [];
	let n = 0;
	while (env.live().length && n++ < 200) {
		delays.push(env.live()[0].ms);
		env.fire();
	}
	assert.ok(n < 100, "poll never stopped: " + n);
	assert.ok(delays[1] > delays[0], "delay did not grow");
	assert.strictEqual(env.t.state.display, "ready");
	assert.strictEqual(env.alerts.length, 1);
	assert.ok(env.now - start < env.t.Pill.POLL.hardStopMs + env.t.Pill.POLL.maxMs + 1);
});

test("a repaint while waiting does not add a timer or reset the backoff", () => {
	const env = load();
	enterAnalyzing(env);
	env.reply = () => analyzingReply;
	env.fire();
	env.fire();
	const before = env.live()[0].ms;
	env.t.applyResult({ state: env.t.state, ui: { display: "analyzing", label: "Analyzing 5%", elapsed: "" }, alert: null });
	assert.strictEqual(env.live().length, 1);
	assert.strictEqual(env.live()[0].ms, before);
});

test("a hidden tab makes no request and keeps waiting", () => {
	const env = load();
	enterAnalyzing(env);
	env.hidden = true;
	env.fire();
	assert.strictEqual(env.calls.length, 0);
	assert.strictEqual(env.live().length, 1);
});

test("poll and resync requests are silent", () => {
	const env = load();
	enterAnalyzing(env);
	env.fire();
	assert.strictEqual(env.calls.length, 1);
	assert.strictEqual(env.calls[0].silent, true);
});

test("a reply sent before the ready pill was clicked cannot revive it", () => {
	const env = load();
	env.t.state = Object.assign(env.t.Pill.initialState(), { display: "ready", session_uuid: "s1", docname: "OS-1" });
	env.t.refreshStatus();
	env.t.onClick();
	assert.strictEqual(env.t.state.display, "inactive");
	env.calls[0].callback({ message: analyzingReply });
	assert.strictEqual(env.t.state.display, "inactive");
	assert.strictEqual(env.live().length, 0);
});

test("a resync that resets a recording pill stops the elapsed timer", () => {
	const env = load();
	env.t.state = Object.assign(env.t.Pill.initialState(), { display: "recording", session_uuid: "s1", started_at: new Date(env.now).toISOString() });
	env.t.startElapsedTimer();
	const cleared = env.cleared;
	env.t.refreshStatus();
	env.calls[0].callback({ message: { phase: "idle" } });
	assert.strictEqual(env.t.state.display, "inactive");
	assert.ok(env.cleared > cleared, "elapsed interval was left running");
});

test("a reload recovers a line profiling recording and its click stops the run", () => {
	const env = load();
	env.t.refreshStatus();
	env.calls[0].callback({ message: { phase: "recording", scope: "phase2", session_uuid: "s1", docname: "OS-1", run_uuid: "r1" } });
	assert.strictEqual(env.t.widget.label.textContent, "Line profiling recording…");
	assert.strictEqual(env.t.widget.elapsed.textContent, "click pill or form to stop");
	assert.strictEqual(env.intervals, 0, "no elapsed timer for line profiling");
	env.t.onClick();
	assert.strictEqual(env.calls[1].method, "optimus.api.stop_line_profile_pass");
	assert.strictEqual(env.calls[1].args.run_uuid, "r1");
});

test("elapsed reads the naive started_at in the system time zone", () => {
	const env = load();
	env.ctx.frappe.boot = { time_zone: { system: "Asia/Kolkata", user: "Asia/Kolkata" } };
	// moment.tz stub for the one zone this test uses (IST is a fixed +05:30).
	env.ctx.window.moment = { tz: (s, tz) => ({ valueOf: () => (tz === "Asia/Kolkata" ? Date.parse(s + "+05:30") : NaN) }) };
	env.now = Date.parse("2026-11-04T17:49:17+05:30");
	env.t.state = Object.assign(env.t.Pill.initialState(), { display: "recording", started_at: "2026-11-04T17:49:12.400777" });
	assert.strictEqual(env.t.computeElapsed(), "0:04");
});

const endedReply = { phase: "ended", scope: "session", session_uuid: "s1", docname: "OS-1", cap_hit: false };
const RECORDING_CHECK_MS = 60000;

function enterRecording(env) {
	env.t.state = Object.assign(env.t.Pill.initialState(), {
		display: "recording", active: true, session_uuid: "s1", docname: "OS-1", started_at: new Date(env.now).toISOString(),
	});
	env.t.setDisplay("recording", "Recording", "0:00");
}

const callsTo = (env, method) => env.calls.filter((c) => c.method === method);

test("a visible recording asks pill_state for its own session after a minute", () => {
	const env = load();
	enterRecording(env);
	assert.strictEqual(env.live().length, 1);
	assert.strictEqual(env.live()[0].ms, RECORDING_CHECK_MS);
	env.reply = () => ({ phase: "recording", scope: "session", session_uuid: "s1", docname: "OS-1" });
	env.fire();
	assert.strictEqual(callsTo(env, "optimus.api.pill_state").length, 1);
	assert.strictEqual(env.calls[0].args.session_uuid, "s1");
	assert.strictEqual(env.live().length, 1, "the next check is scheduled");
	assert.strictEqual(env.t.state.display, "recording");
});

test("repeated recording repaints keep one check timer and do not restart it", () => {
	const env = load();
	enterRecording(env);
	const first = env.live()[0];
	env.now += 30000;
	env.t.setDisplay("recording", "Recording", "0:30");
	assert.strictEqual(env.live().length, 1);
	assert.strictEqual(env.live()[0], first);
	assert.strictEqual(first.at, 1_000_000 + RECORDING_CHECK_MS);
});

test("a hidden recording tab makes no pill_state request", () => {
	const env = load();
	enterRecording(env);
	env.hidden = true;
	env.fire();
	env.fire();
	env.fire();
	assert.strictEqual(env.calls.length, 0);
	assert.strictEqual(env.live().length, 1);
});

test("leaving recording clears the check timer", () => {
	const env = load();
	enterRecording(env);
	env.t.setDisplay("analyzing", "Analyzing…", "");
	assert.ok(env.live().every((t) => t.ms !== RECORDING_CHECK_MS));
});

test("an ended recording stops that session without any toast until the stop reply", () => {
	const env = load();
	enterRecording(env);
	env.reply = (opts) => (opts.method === "optimus.api.pill_state" ? endedReply : undefined);
	env.fire();
	const stop = callsTo(env, "optimus.api.stop");
	assert.strictEqual(stop.length, 1);
	assert.strictEqual(JSON.stringify(stop[0].args), JSON.stringify({ session_uuid: "s1" }));
	assert.strictEqual(env.alerts.length, 0);
	assert.strictEqual(env.t.state.display, "stopping");
	assert.strictEqual(env.t.widget.label.textContent, "Stopping…");
	stop[0].callback({ message: { stopped: true, session_uuid: "s1", docname: "OS-1", ran_inline: false, expired: true } });
	assert.strictEqual(env.alerts.length, 1);
	assert.ok(/without activity/.test(env.alerts[0].message), env.alerts[0].message);
	assert.strictEqual(env.t.state.display, "analyzing");
});

test("an ended recording that hit the request limit says so", () => {
	const env = load();
	enterRecording(env);
	env.reply = (opts) => (opts.method === "optimus.api.pill_state" ? Object.assign({}, endedReply, { cap_hit: true }) : undefined);
	env.fire();
	callsTo(env, "optimus.api.stop")[0].callback({ message: { stopped: true, expired: true, docname: "OS-1" } });
	assert.strictEqual(env.alerts.length, 1);
	assert.ok(/request limit/.test(env.alerts[0].message), env.alerts[0].message);
});

test("the stop of an ended session names the server's session, not whatever the tab shows later", () => {
	const env = load();
	env.t.state = Object.assign(env.t.Pill.initialState(), { display: "recording", session_uuid: "s1" });
	env.t.refreshStatus();
	const pending = env.calls[0];
	env.t.state.session_uuid = "s1";
	pending.callback({ message: Object.assign({}, endedReply, { session_uuid: "s1" }) });
	assert.strictEqual(callsTo(env, "optimus.api.stop")[0].args.session_uuid, "s1");
});

test("an ended session found on page load is stopped by uuid", () => {
	const env = load();
	env.t.refreshStatus();
	env.calls[0].callback({ message: Object.assign({}, endedReply, { session_uuid: "s9" }) });
	assert.strictEqual(callsTo(env, "optimus.api.stop")[0].args.session_uuid, "s9");
});

test("a tab that loses the stop claim resyncs and stays silent when the session is analyzing", () => {
	const env = load();
	enterRecording(env);
	env.reply = (opts) => (opts.method === "optimus.api.pill_state" ? endedReply : undefined);
	env.fire();
	env.reply = () => analyzingReply;
	callsTo(env, "optimus.api.stop")[0].callback({ message: { stopped: false, reason: "no active session" } });
	assert.strictEqual(env.t.state.display, "analyzing");
	assert.strictEqual(env.alerts.length, 0);
});

test("a stop that finds nothing resets the pill with one gray notice, never stuck in stopping", () => {
	const env = load();
	enterRecording(env);
	env.t.confirmAndStop();
	env.reply = () => ({ phase: "idle" });
	callsTo(env, "optimus.api.stop")[0].callback({ message: { stopped: false, reason: "no active session" } });
	assert.strictEqual(env.t.state.display, "inactive");
	assert.strictEqual(env.alerts.length, 1);
	assert.strictEqual(env.alerts[0].indicator, "gray");
	assert.ok(/had already ended/.test(env.alerts[0].message));
});

test("a failed resync after stopped:false leaves the pill stopping and says nothing", () => {
	const env = load();
	enterRecording(env);
	env.t.confirmAndStop();
	callsTo(env, "optimus.api.stop")[0].callback({ message: { stopped: false } });
	env.calls[env.calls.length - 1].error({});
	assert.strictEqual(env.t.state.display, "stopping");
	assert.strictEqual(env.alerts.length, 0);
});

test("a manual stop of an expired session tells the user once why it had ended", () => {
	const env = load();
	enterRecording(env);
	env.t.onClick();
	callsTo(env, "optimus.api.stop")[0].callback({ message: { stopped: true, expired: true, docname: "OS-1" } });
	assert.strictEqual(env.alerts.length, 1);
	assert.ok(/without activity/.test(env.alerts[0].message));
});

test("a manual stop of a live session keeps the usual notice", () => {
	const env = load();
	enterRecording(env);
	env.t.onClick();
	callsTo(env, "optimus.api.stop")[0].callback({ message: { stopped: true, docname: "OS-1" } });
	assert.strictEqual(env.alerts.length, 1);
	assert.ok(/stopped analyzing/.test(env.alerts[0].message));
});

test("a failed stop resyncs by session, so a newer session in another tab is never mistaken for this one", () => {
	const env = load();
	enterRecording(env);
	env.t.confirmAndStop();
	callsTo(env, "optimus.api.stop")[0].error({});
	assert.strictEqual(callsTo(env, "optimus.api.status").length, 0);
	const resync = callsTo(env, "optimus.api.pill_state");
	assert.strictEqual(resync.length, 1);
	assert.strictEqual(resync[0].args.session_uuid, "s1");
});

test("an ended session whose automatic stop failed is auto-stopped once, not every minute", () => {
	const env = load();
	enterRecording(env);
	env.reply = (opts) => (opts.method === "optimus.api.pill_state" ? endedReply : undefined);
	env.fire();
	callsTo(env, "optimus.api.stop")[0].error({});
	assert.strictEqual(callsTo(env, "optimus.api.stop").length, 1);
	assert.strictEqual(env.t.state.display, "inactive");
	for (let i = 0; i < 3; i++) env.fire();
	assert.strictEqual(callsTo(env, "optimus.api.stop").length, 1);
	assert.ok(env.alerts.every((a) => a.indicator !== "orange"));
});

test("a failed stop on a live session says so and keeps recording", () => {
	const env = load();
	enterRecording(env);
	env.t.confirmAndStop();
	env.reply = () => ({ phase: "recording", scope: "session", session_uuid: "s1", docname: "OS-1" });
	callsTo(env, "optimus.api.stop")[0].error({});
	assert.strictEqual(env.t.state.display, "recording");
	assert.strictEqual(env.alerts.length, 1);
	assert.strictEqual(env.alerts[0].indicator, "red");
});

test("the pill title says what a click does and in-progress states are aria-disabled", () => {
	const env = load();
	env.t.setDisplay("inactive", "Profiler", "");
	assert.strictEqual(env.t.widget.title, "Start a profiling session");
	enterRecording(env);
	assert.strictEqual(env.t.widget.title, "Stop recording and analyze");
	assert.strictEqual(env.t.widget.attrs["aria-disabled"], undefined);
	env.t.setDisplay("stopping", "Stopping…", "");
	assert.strictEqual(env.t.widget.attrs["aria-disabled"], "true");
	assert.strictEqual(env.t.widget.title, "Analysis in progress");
	env.t.state.docname = "OS-1";
	env.t.setDisplay("ready", "Report ready", "");
	assert.strictEqual(env.t.widget.title, "Open the session");
	assert.strictEqual(env.t.widget.attrs["aria-disabled"], undefined);
});

function startFailure(env, status) {
	env.t.openStartDialog();
	const d = FakeDialog.last;
	d.click();
	assert.strictEqual(d.disabled, true);
	env.calls[0].error(status);
	return d;
}

test("a failed start keeps the dialog open with its label and Start usable again", () => {
	const env = load();
	const d = startFailure(env, {});
	assert.strictEqual(d.hidden, false);
	assert.strictEqual(d.disabled, false);
	assert.strictEqual(env.alerts.length, 1);
	assert.strictEqual(env.alerts[0].indicator, "red");
	d.click();
	assert.strictEqual(env.calls.length, 2, "a retry is possible");
});

test("a failed start with a server message adds no extra toast", () => {
	const env = load();
	startFailure(env, { _server_messages: "[]" });
	assert.strictEqual(env.alerts.length, 0);
});

test("Enter and Ctrl+S on Start start only one session", () => {
	const env = load();
	env.t.openStartDialog();
	const d = FakeDialog.last;
	d.click();
	d.click();
	d.click();
	assert.strictEqual(callsTo(env, "optimus.api.start").length, 1);
});

test("a successful start hides the dialog and clears the remembered label", () => {
	const env = load();
	env.t.openStartDialog();
	const d = FakeDialog.last;
	d.value = "typed";
	d.click();
	env.calls[0].callback({ message: { session_uuid: "s1", docname: "OS-1", title: "typed", started_at: "2026-11-07 10:00:00" } });
	assert.strictEqual(d.hidden, true);
	assert.strictEqual(env.t.state.display, "recording");
	env.t.openStartDialog();
	assert.notStrictEqual(FakeDialog.last.value, "typed");
});

test("Escape keeps the typed label for the next dialog on the same page", () => {
	const env = load();
	env.t.openStartDialog();
	FakeDialog.last.value = "my flow";
	FakeDialog.last.hide();
	env.t.openStartDialog();
	assert.strictEqual(FakeDialog.last.value, "my flow");
});

test("a label typed on another page is not carried over", () => {
	const env = load();
	env.t.openStartDialog();
	FakeDialog.last.value = "my flow";
	FakeDialog.last.hide();
	env.ctx.frappe.get_route = () => ["List", "Item"];
	env.t.openStartDialog();
	assert.notStrictEqual(FakeDialog.last.value, "my flow");
});

test("focus returns to the pill when the dialog hides, even if a click never focused the button", () => {
	const env = load();
	env.active = null;
	env.t.openStartDialog();
	FakeDialog.last.hide();
	assert.strictEqual(env.t.widget.focused, 1);
	env.active = {};
	env.t.openStartDialog();
	FakeDialog.last.hide();
	assert.strictEqual(env.t.widget.focused, 1, "no focus grab when focus went to another control");
});

test("an inline stop of an expired session gives exactly one notice with the expired wording", () => {
	const env = load();
	enterRecording(env);
	env.t.confirmAndStop({ sessionUuid: "s1", auto: true });
	callsTo(env, "optimus.api.stop")[0].callback({ message: { stopped: true, ran_inline: true, expired: true, status: "Ready", docname: "OS-1" } });
	assert.strictEqual(env.alerts.length, 1);
	assert.ok(/without activity/.test(env.alerts[0].message), env.alerts[0].message);
	assert.strictEqual(env.t.state.display, "ready");
});

test("an inline stop of an expired session that hit the request limit says so", () => {
	const env = load();
	enterRecording(env);
	env.t.confirmAndStop({ sessionUuid: "s1", auto: true, capHit: true });
	callsTo(env, "optimus.api.stop")[0].callback({ message: { stopped: true, ran_inline: true, expired: true, status: "Ready", docname: "OS-1" } });
	assert.strictEqual(env.alerts.length, 1);
	assert.ok(/request limit/.test(env.alerts[0].message), env.alerts[0].message);
});

test("an inline stop of a live session keeps the report-ready notice, and a failed one stays red", () => {
	const env = load();
	enterRecording(env);
	env.t.confirmAndStop();
	callsTo(env, "optimus.api.stop")[0].callback({ message: { stopped: true, ran_inline: true, status: "Ready", docname: "OS-1" } });
	assert.strictEqual(env.alerts.length, 1);
	assert.ok(/report ready/.test(env.alerts[0].message));
	const bad = load();
	enterRecording(bad);
	bad.t.confirmAndStop({ sessionUuid: "s1", auto: true });
	callsTo(bad, "optimus.api.stop")[0].callback({ message: { stopped: true, ran_inline: true, expired: true, status: "Failed", docname: "OS-1" } });
	assert.strictEqual(bad.alerts.length, 1);
	assert.strictEqual(bad.alerts[0].indicator, "red");
});

let failed = 0;
for (const [name, fn] of tests) {
	try { fn(); console.log("ok   " + name); } catch (e) { failed++; console.log("FAIL " + name + "\n" + e.stack); }
}
process.exit(failed ? 1 : 0);
