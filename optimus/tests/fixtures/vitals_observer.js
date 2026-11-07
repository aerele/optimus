// Runs optimus_frontend.js in a vm with a fake PerformanceObserver. Run by test_frontend_vitals_observer.py.
"use strict";
const vm = require("vm");
const fs = require("fs");
const assert = require("assert");

const SHIM = process.argv[2];
const scenario = process.argv[3];

function load(opts) {
	opts = opts || {};
	const env = { observed: [], observers: [], calls: [], widget: { "data-session-uuid": "s1" } };
	class FakeObserver {
		constructor(cb) {
			if (opts.ctorThrows) throw new Error("no observer");
			this.cb = cb;
			env.observers.push(this);
		}
		observe(o) {
			if (opts.observeThrows && opts.observeThrows === o.type) throw new Error("bad type");
			env.observed.push(o);
		}
	}
	if (opts.supported) FakeObserver.supportedEntryTypes = opts.supported;
	const win = {
		location: { pathname: "/app", search: "" },
		addEventListener: () => {},
	};
	if (opts.fetch) {
		win.fetch = () => Promise.resolve({
			status: 200,
			headers: { get: (k) => (k === "X-Optimus-Recording-Id" ? "rec1" : "10") },
		});
	}
	const ctx = {
		window: win,
		document: { getElementById: () => ({ getAttribute: (k) => env.widget[k] || null }) },
		navigator: {},
		performance: { now: () => 0 },
		setInterval: () => 1,
		Date,
		JSON,
		Math,
		frappe: {
			session: { user: "u" },
			csrf_token: "t",
			call: (c) => env.calls.push(c),
		},
	};
	if (!opts.noObserver) ctx.PerformanceObserver = FakeObserver;
	vm.createContext(ctx);
	vm.runInContext(fs.readFileSync(SHIM, "utf8"), ctx);
	env.win = win;
	env.emit = (entries) => env.observers.forEach((o) => o.cb({ getEntries: () => entries }));
	env.flushed = () => {
		env.calls.length = 0;
		win.optimus_frontend.flush({ sync: false });
		return env.calls.length ? JSON.parse(env.calls[0].args.payload) : { xhr: [], vitals: [] };
	};
	return env;
}

const fcp = { entryType: "paint", name: "first-contentful-paint", startTime: 300 };
const lcp = (t) => ({ entryType: "largest-contentful-paint", startTime: t });
const shift = (v) => ({ entryType: "layout-shift", value: v, hadRecentInput: false });
const nav = (loadEnd) => ({
	entryType: "navigation", startTime: 0,
	domainLookupStart: 1, domainLookupEnd: 2, connectStart: 2, connectEnd: 4,
	requestStart: 5, responseStart: 105,
	domContentLoadedEventEnd: loadEnd ? 400 : 0, loadEventEnd: loadEnd,
});
const names = (p) => p.vitals.map((v) => v.name).sort();

const scenarios = {
	observes_each_type_buffered() {
		const env = load();
		assert.ok(env.observed.length === 4, "observe calls: " + env.observed.length);
		env.observed.forEach((o) => {
			assert.strictEqual(o.entryTypes, undefined, "entryTypes ignores buffered");
			assert.strictEqual(o.buffered, true);
		});
		assert.deepStrictEqual(env.observed.map((o) => o.type).sort(),
			["largest-contentful-paint", "layout-shift", "navigation", "paint"]);
	},
	skips_unsupported_and_survives_a_throw() {
		let env = load({ supported: ["paint", "navigation"] });
		assert.deepStrictEqual(env.observed.map((o) => o.type), ["paint", "navigation"]);
		env = load({ observeThrows: "paint" });
		assert.strictEqual(env.observed.length, 3);
	},
	navigation_delivered_twice_is_one_vital() {
		const env = load();
		env.emit([nav(0)]);
		env.emit([nav(623)]);
		env.emit([nav(623)]);
		const p = env.flushed();
		assert.deepStrictEqual(names(p), ["navigation"]);
		assert.strictEqual(p.vitals[0].load_ms, 623);
		assert.strictEqual(p.vitals[0].dom_content_loaded_ms, 400);
	},
	incomplete_navigation_alone_is_dropped() {
		const env = load();
		env.emit([nav(0)]);
		assert.strictEqual(env.flushed().vitals.length, 0);
	},
	fcp_twice_is_one() {
		const env = load();
		env.emit([fcp]);
		env.emit([fcp]);
		assert.deepStrictEqual(names(env.flushed()), ["fcp"]);
	},
	lcp_candidates_all_kept() {
		const env = load();
		env.emit([lcp(500), lcp(900)]);
		assert.deepStrictEqual(names(env.flushed()), ["lcp", "lcp"]);
	},
	load_url_is_frozen_for_every_vital() {
		const env = load();
		env.emit([fcp, lcp(500)]);
		env.win.location.pathname = "/app/home";
		env.emit([lcp(900), shift(0.2), nav(600)]);
		const p = env.flushed();
		assert.strictEqual(p.vitals.length, 5);
		p.vitals.forEach((v) => assert.strictEqual(v.page_url, "/app", v.name));
	},
	survives_without_an_observer() {
		for (const opts of [{ noObserver: true, fetch: true }, { ctorThrows: true, fetch: true }]) {
			const env = load(opts);
			assert.strictEqual(typeof env.win.optimus_frontend.flush, "function");
			assert.strictEqual(typeof env.win.optimus_frontend.beginSession, "function");
			assert.notStrictEqual(env.win.fetch, undefined);
		}
	},
	begin_session_clears_buffered_vitals_and_xhr() {
		const env = load({ fetch: true });
		env.emit([fcp, lcp(500), nav(600)]);
		return env.win.fetch("/x").then(() => {
			assert.strictEqual(env.win.optimus_frontend.getState().xhr_buffered, 1);
			env.win.optimus_frontend.beginSession();
			const s = env.win.optimus_frontend.getState();
			assert.strictEqual(s.xhr_buffered, 0);
			assert.strictEqual(s.vitals_buffered, 0);
			return env.win.fetch("/y");
		}).then(() => {
			const p = env.flushed();
			assert.strictEqual(p.xhr.length, 1);
			assert.strictEqual(p.xhr[0].url, "/y");
			assert.strictEqual(p.vitals.length, 0);
		});
	},
	begin_session_drops_a_load_that_finishes_later() {
		const env = load();
		env.emit([fcp, nav(0)]);
		env.win.optimus_frontend.beginSession();
		env.emit([lcp(900), shift(0.3), nav(700)]);
		assert.strictEqual(env.flushed().vitals.length, 0);
	},
	begin_session_twice_is_harmless() {
		const env = load();
		env.win.optimus_frontend.beginSession();
		env.win.optimus_frontend.beginSession();
		assert.strictEqual(env.flushed().vitals.length, 0);
	},
};

Promise.resolve().then(() => scenarios[scenario]()).then(
	() => { console.log("ok " + scenario); },
	(e) => { console.log("FAIL " + scenario + "\n" + e.stack); process.exit(1); },
);
