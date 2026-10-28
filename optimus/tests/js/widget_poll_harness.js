// Runs floating_widget.js in a vm with fake timers and frappe.call. Run by test_pill_state_js.py.
"use strict";
const vm = require("vm");
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const JS = path.join(__dirname, "..", "..", "public", "js");

function makeWidget() {
	const el = { classes: new Set(), label: { textContent: "" }, elapsed: { textContent: "" }, attrs: {} };
	el.classList = { remove: (...c) => c.forEach((x) => el.classes.delete(x)), add: (c) => el.classes.add(c) };
	el.querySelector = (sel) => (sel === ".fp-label" ? el.label : el.elapsed);
	el.setAttribute = (k, v) => { el.attrs[k] = v; };
	el.removeAttribute = (k) => { delete el.attrs[k]; };
	return el;
}

function load() {
	const env = { now: 1_000_000, hidden: false, timers: [], calls: [], intervals: 0, cleared: 0, reply: null, alerts: [] };
	const doc = { readyState: "loading", addEventListener: () => {} };
	Object.defineProperty(doc, "hidden", { get: () => env.hidden });
	const ctx = {
		window: {}, document: doc, __: (t) => t,
		Date: class extends Date { static now() { return env.now; } },
		setTimeout: (fn, ms) => { const t = { fn, ms, at: env.now + ms, live: true }; env.timers.push(t); return t; },
		clearTimeout: (t) => { if (t) t.live = false; },
		setInterval: () => { env.intervals += 1; return env.intervals; },
		clearInterval: () => { env.cleared += 1; },
		frappe: {
			session: { user: "u" }, set_route: () => {}, show_alert: (a) => env.alerts.push(a),
			call: (opts) => {
				env.calls.push(opts);
				if (env.reply) opts.callback({ message: env.reply(opts) });
			},
		},
	};
	vm.createContext(ctx);
	vm.runInContext(fs.readFileSync(path.join(JS, "optimus_pill_state.js"), "utf8"), ctx);
	const code = fs.readFileSync(path.join(JS, "floating_widget.js"), "utf8").replace(
		/\}\)\(\);\s*$/,
		"widget = makeWidget();\nglobalThis.t = { applyResult, refreshStatus, onClick, startElapsedTimer," +
		" get state() { return currentState; }, set state(v) { currentState = v; }, Pill };})();"
	);
	ctx.makeWidget = makeWidget;
	vm.runInContext(code, ctx);
	env.t = ctx.t;
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

let failed = 0;
for (const [name, fn] of tests) {
	try { fn(); console.log("ok   " + name); } catch (e) { failed++; console.log("FAIL " + name + "\n" + e.stack); }
}
process.exit(failed ? 1 : 0);
