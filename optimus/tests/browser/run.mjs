// Manual real-browser check of the floating pill (not run in CI).
//
//   PLAYWRIGHT_CORE=/path/to/node_modules/playwright-core \
//   FRAPPE_APP=/path/to/apps/frappe \
//   node optimus/tests/browser/run.mjs <shots dir>
//
// Serves this checkout's real floating_widget.js/css, optimus_pill_state.js and Frappe v16's real Espresso
// token and toast stylesheets, with the Desk globals stubbed (frappe_stub.js) and the API answered by
// page.route from an in-memory fake server. Needs Google Chrome (channel "chrome"). It never talks to a site.
// Prints PASS/FAIL per check and exits 1 on any FAIL; read the screenshots too.
import { createRequire } from "module";
import fs from "fs";
import path from "path";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_CORE || "playwright-core");
const HERE = path.dirname(new URL(import.meta.url).pathname);
const APP = path.resolve(HERE, "..", "..");
const PUB = path.join(APP, "public");
const FRAPPE = process.env.FRAPPE_APP || "/home/sowmya/frappe-16/frappe-16/apps/frappe";
const ESPRESSO = path.join(FRAPPE, "frappe", "public", "css", "espresso");
const SHOTS = process.argv[2] || path.join(process.env.TMPDIR || "/tmp", "optimus-pill-shots");
const ORIGIN = "http://harness.test";
fs.mkdirSync(SHOTS, { recursive: true });

let failures = 0;
function check(name, ok, detail) {
	if (!ok) failures += 1;
	console.log((ok ? "PASS " : "FAIL ") + name + (ok && detail === undefined ? "" : "  " + JSON.stringify(detail)));
}

// Fake server: one user, a Redis pointer, Optimus Session rows, #137 stop semantics and pill_state "ended".
function makeServer() {
	const s = { pointer: null, rows: {}, n: 0, log: [] };
	const calls = (m) => s.log.filter((l) => l[0] === m).map((l) => l[1]);
	s.calls = calls;
	s.handle = (method, args) => {
		s.log.push([method.replace("optimus.api.", ""), args]);
		const m = method.replace("optimus.api.", "");
		if (m === "check_onboarding_seen") return { seen: true };
		if (m === "start") {
			if (args.label === "FAIL") return { __status: 417, _server_messages: "[\"boom\"]" };
			if (s.pointer) return { __status: 417, _server_messages: "[\"already active\"]" };
			const id = "s" + ++s.n;
			s.rows[id] = { status: "Recording", docname: "OS-" + s.n };
			s.pointer = id;
			return { session_uuid: id, docname: s.rows[id].docname, title: args.label, started_at: "2026-11-07 10:00:00" };
		}
		if (m === "pill_state") {
			const t = args.session_uuid;
			if (s.pointer && (!t || t === s.pointer)) return { phase: "recording", scope: "session", session_uuid: s.pointer, docname: s.rows[s.pointer].docname, started_at: "2026-11-07 10:00:00" };
			const id = t || Object.keys(s.rows).reverse().find((k) => s.rows[k].status !== "Ready");
			const r = id && s.rows[id];
			if (!r) return { phase: "idle" };
			const phase = { Recording: "ended", Stopping: "analyzing", Analyzing: "analyzing", Ready: "ready", Failed: "failed" }[r.status];
			return { phase, scope: "session", session_uuid: id, docname: r.docname, ...(phase === "ended" ? { cap_hit: !!r.cap_hit } : {}) };
		}
		if (m === "stop") {
			const t = args.session_uuid;
			const live = s.pointer && (t == null || t === s.pointer);
			const id = live ? s.pointer : (t || Object.keys(s.rows).reverse().find((k) => s.rows[k].status === "Recording"));
			if (live) s.pointer = null;
			const r = id && s.rows[id];
			if (!r || r.status !== "Recording") return { stopped: false, reason: "no active session" };
			r.status = "Stopping";
			return { stopped: true, session_uuid: id, docname: r.docname, ran_inline: false, ...(live ? {} : { expired: true }) };
		}
		if (m === "status") return { active: !!s.pointer };
		return {};
	};
	return s;
}

async function wire(ctx, server) {
	await ctx.route(ORIGIN + "/**", async (route) => {
		const p = new URL(route.request().url()).pathname;
		const file = (f, type) => route.fulfill({ status: 200, contentType: type, body: fs.readFileSync(f) });
		if (p === "/" || p === "/index.html") return file(path.join(HERE, "index.html"), "text/html");
		if (p === "/picker.html") return file(path.join(HERE, "picker.html"), "text/html");
		if (p === "/stub/frappe_stub.js") return file(path.join(HERE, "frappe_stub.js"), "text/javascript");
		if (p.startsWith("/frappe/")) {
			const f = path.resolve(ESPRESSO, p.slice("/frappe/".length));
			return f.startsWith(ESPRESSO) ? file(f, "text/css") : route.fulfill({ status: 404, body: "" });
		}
		if (p === "/optimus/floating_widget.css") return file(path.join(PUB, "css", "floating_widget.css"), "text/css");
		if (p === "/optimus/optimus_session.js") return file(path.join(APP, "optimus", "doctype", "optimus_session", "optimus_session.js"), "text/javascript");
		if (p.startsWith("/optimus/")) return file(path.join(PUB, "js", path.basename(p)), "text/javascript");
		if (p.startsWith("/api/method/")) {
			const out = server.handle(p.slice("/api/method/".length), JSON.parse(route.request().postData() || "{}"));
			const status = out.__status || 200;
			return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(status === 200 ? { message: out } : out) });
		}
		return route.fulfill({ status: 404, body: "" });
	});
}

const browser = await chromium.launch({ channel: "chrome", headless: true });
const messages = [];

async function open(server, opts = {}) {
	const ctx = opts.ctx || await browser.newContext({ viewport: { width: opts.width || 1100, height: opts.height || 700 }, forcedColors: opts.forced, reducedMotion: opts.reduced });
	if (!opts.ctx) await wire(ctx, server);
	const page = await ctx.newPage();
	page.on("pageerror", (e) => messages.push("pageerror: " + e));
	// The deliberate 417 of the failed-start scenario is the only expected browser-side network error.
	page.on("console", (m) => { if (["error", "warning"].includes(m.type()) && !/status of 417/.test(m.text())) messages.push(m.type() + ": " + m.text()); });
	if (opts.clock !== false) await page.clock.install();
	await page.goto(ORIGIN + "/?theme=" + (opts.theme || "light"));
	await page.waitForSelector("#frappe-profiler-widget");
	return { ctx, page };
}

const PILL = "#frappe-profiler-widget";
const label = (page) => page.textContent(PILL + " .fp-label");
const settle = (page, ms = 150) => page.waitForTimeout(ms);
const alerts = (page) => page.evaluate(() => window.__alerts.slice());
async function startRecording(page, text) {
	await page.click(PILL);
	if (text) await page.fill(".modal input", text);
	await page.click(".modal .btn-primary");
	await page.waitForFunction(() => document.querySelector("#frappe-profiler-widget").className.includes("recording"));
}
async function runClock(page, ms) {
	await page.clock.runFor(ms);
	await settle(page, 250);
}

// ---------- 1. Look, geometry and stacking: widths x themes ----------
const luminance = (rgb) => {
	const c = rgb.map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
	return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
};
const contrast = (a, b) => { const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
const parseRgb = (s) => s.match(/[\d.]+/g).slice(0, 3).map(Number);

for (const theme of ["light", "dark"]) {
	for (const width of [1440, 1100, 375, 320]) {
		const tag = `${theme}@${width}`;
		const server = makeServer();
		const { ctx, page } = await open(server, { width, theme });
		const inactive = await page.evaluate(() => {
			const w = document.querySelector("#frappe-profiler-widget");
			const cs = getComputedStyle(w);
			const r = w.getBoundingClientRect();
			return { tag: w.tagName, bg: cs.backgroundColor, color: cs.color, border: cs.borderTopColor, z: cs.zIndex, page: getComputedStyle(document.body).backgroundColor,
				inside: r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight, hscroll: document.documentElement.scrollWidth > innerWidth };
		});
		check(`${tag} pill is a button, inside the viewport, no horizontal scroll`, inactive.tag === "BUTTON" && inactive.inside && !inactive.hscroll, inactive);
		check(`${tag} inactive label contrast >= 4.5`, contrast(parseRgb(inactive.color), parseRgb(inactive.bg)) >= 4.5, contrast(parseRgb(inactive.color), parseRgb(inactive.bg)));
		check(`${tag} inactive border contrast against the page >= 3`, contrast(parseRgb(inactive.border), parseRgb(inactive.page)) >= 3, contrast(parseRgb(inactive.border), parseRgb(inactive.page)));
		await page.screenshot({ path: path.join(SHOTS, `${tag}-inactive.png`) });
		await page.click(PILL);
		await page.screenshot({ path: path.join(SHOTS, `${tag}-dialog.png`) });
		const topWithDialog = await page.evaluate(() => {
			const r = document.querySelector("#frappe-profiler-widget").getBoundingClientRect();
			const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
			return { cls: el.className, z: getComputedStyle(document.querySelector("#frappe-profiler-widget")).zIndex };
		});
		check(`${tag} with a dialog open the backdrop covers the pill`, topWithDialog.cls === "modal-backdrop" && topWithDialog.z === "1039", topWithDialog);
		await page.click(".modal .btn-primary");
		await page.waitForFunction(() => document.querySelector("#frappe-profiler-widget").className.includes("recording"));
		await page.screenshot({ path: path.join(SHOTS, `${tag}-recording.png`) });
		const rec = await page.evaluate(() => {
			const w = document.querySelector("#frappe-profiler-widget");
			const cs = getComputedStyle(w);
			const dot = getComputedStyle(w.querySelector(".fp-dot"));
			const toast = document.querySelector(".es-toast");
			const r = w.getBoundingClientRect();
			const t = toast && toast.getBoundingClientRect();
			const x1 = t ? Math.max(r.left, t.left) : 0, x2 = t ? Math.min(r.right, t.right) : 0;
			const y1 = t ? Math.max(r.top, t.top) : 0, y2 = t ? Math.min(r.bottom, t.bottom) : 0;
			const overlap = x2 > x1 && y2 > y1;
			const top = overlap ? document.elementFromPoint((x1 + x2) / 2, (y1 + y2) / 2) : null;
			const pillOnTop = !overlap || toast.contains(top);
			return { color: cs.color, bg: cs.backgroundColor, dot: dot.backgroundColor, toast: !!toast, overlap, pillOnTop };
		});
		check(`${tag} recording label contrast >= 4.5 and dot follows the text colour`, contrast(parseRgb(rec.color), parseRgb(rec.bg)) >= 4.5 && rec.dot === rec.color, rec);
		check(`${tag} a toast showing over the pill is on top of it (fully readable)`, rec.toast && rec.pillOnTop, rec);
		await ctx.close();
	}
}

// ---------- 2. Keyboard and accessible name (real key presses) ----------
{
	const server = makeServer();
	const { ctx, page } = await open(server, { width: 1440 });
	const seen = [];
	for (let i = 0; i < 4 && seen[seen.length - 1] !== "frappe-profiler-widget"; i++) {
		await page.keyboard.press("Tab");
		seen.push(await page.evaluate(() => document.activeElement.id || document.activeElement.tagName));
	}
	check("Tab reaches the pill", seen.includes("frappe-profiler-widget"), seen);
	const outline = await page.evaluate(() => { const cs = getComputedStyle(document.activeElement); return { w: cs.outlineWidth, style: cs.outlineStyle }; });
	check("the focused pill has a visible outline of at least 2px", parseFloat(outline.w) >= 2 && outline.style !== "none", outline);
	await page.screenshot({ path: path.join(SHOTS, "keyboard-focus.png"), clip: { x: 1100, y: 600, width: 340, height: 100 } });
	check("the button has the accessible name Profiler", (await page.getByRole("button", { name: /Profiler/ }).count()) === 1);
	await page.keyboard.press("Enter");
	check("Enter on the pill opens the start dialog", (await page.locator(".modal").count()) === 1);
	await page.keyboard.press("Escape");
	check("Escape closes the dialog and focus is back on the pill", (await page.locator(".modal").count()) === 0 && (await page.evaluate(() => document.activeElement.id)) === "frappe-profiler-widget");
	await page.keyboard.press("Enter");
	await page.keyboard.press("Enter");
	await page.waitForFunction(() => document.querySelector("#frappe-profiler-widget").className.includes("recording"));
	check("Enter in the label field starts the session", server.calls("start").length === 1);
	check("getByRole finds the recording button by name", (await page.getByRole("button", { name: /Recording/ }).count()) === 1);
	const names = [];
	for (let i = 0; i < 3; i++) { names.push(await page.evaluate(() => { const w = document.querySelector("#frappe-profiler-widget"); return w.getAttribute("title") + "|" + [...w.childNodes].filter((n) => !(n.getAttribute && n.getAttribute("aria-hidden"))).map((n) => n.textContent.trim()).join(" "); })); await page.clock.runFor(1000); }
	check("the accessible name of the focused pill does not change over 3 s of recording", new Set(names).size === 1, names);
	await page.keyboard.press("Enter");
	await page.waitForFunction(() => document.querySelector("#frappe-profiler-widget").className.includes("analyzing"));
	check("Enter on the recording pill stops it", server.calls("stop").length === 1 && (await label(page)) === "Analyzing…");
	check("an analyzing pill is aria-disabled and focusable", (await page.getAttribute(PILL, "aria-disabled")) === "true");
	await page.keyboard.press("Enter");
	await settle(page);
	check("Enter while analyzing is a no-op", server.calls("stop").length === 1 && (await page.locator(".modal").count()) === 0);
	await ctx.close();
}

// ---------- 3. Ended by itself: auto-stop, one toast, never "No active session" ----------
for (const cap of [false, true]) {
	const server = makeServer();
	const { ctx, page } = await open(server, { width: 1100 });
	await startRecording(page);
	server.pointer = null;
	if (cap) server.rows.s1.cap_hit = true;
	const before = server.log.length;
	await runClock(page, 61000);
	const stop = server.log.slice(before).filter((l) => l[0] === "stop");
	const toasts = await alerts(page);
	const ended = toasts.filter((a) => /without activity|request limit/.test(a.message));
	check(`ended${cap ? " (cap hit)" : ""}: stop carries the session uuid`, stop.length === 1 && stop[0][1].session_uuid === "s1", stop);
	check(`ended${cap ? " (cap hit)" : ""}: exactly one notice with the right wording`, ended.length === 1 && (cap ? /request limit/ : /without activity/).test(ended[0].message), toasts);
	check(`ended${cap ? " (cap hit)" : ""}: never "No active session", pill Analyzing`, !toasts.some((a) => /No active session|Nothing to stop/.test(a.message)) && (await label(page)) === "Analyzing…", await label(page));
	await page.screenshot({ path: path.join(SHOTS, `ended${cap ? "-cap" : ""}-analyzing.png`) });
	if (!cap) {
		server.rows.s1.status = "Ready";
		await page.evaluate(() => window.__emit("optimus_session_ready", { session_uuid: "s1", docname: "OS-1", recording_user: "alice@example.com" }));
		check("analyzing turns Report ready on the ready event", (await label(page)) === "Report ready");
		await page.screenshot({ path: path.join(SHOTS, "ready.png") });
	}
	await ctx.close();
}
{
	const server = makeServer();
	const { ctx, page } = await open(server);
	await startRecording(page);
	await page.evaluate(() => window.__emit("optimus_session_stopping", { session_uuid: "s1", recording_user: "alice@example.com" }));
	await page.evaluate(() => window.__emit("optimus_session_failed", { session_uuid: "s1", docname: "OS-1", recording_user: "alice@example.com" }));
	check("a failed analyze shows Analyze failed", (await label(page)) === "Analyze failed");
	await page.screenshot({ path: path.join(SHOTS, "failed.png") });
	await ctx.close();
}

// ---------- 4. stopped:false that finds nothing: never stuck in Stopping ----------
{
	const server = makeServer();
	const { ctx, page } = await open(server);
	await startRecording(page);
	server.pointer = null;
	delete server.rows.s1;
	// Toasts cover the pill for their 7 s lifetime; wait it out as a mouse user would.
	await page.evaluate(() => document.querySelectorAll(".es-toast").forEach((t) => t.remove()));
	await page.click(PILL);
	await settle(page, 300);
	const toasts = await alerts(page);
	check("stopped:false then idle resets the pill with one gray notice", (await label(page)) === "Profiler" && toasts.filter((a) => a.indicator === "gray").length === 1, toasts);
	await ctx.close();
}

// ---------- 5. Two tabs ----------
{
	const server = makeServer();
	const ctx = await browser.newContext({ viewport: { width: 1100, height: 600 } });
	await wire(ctx, server);
	const t1 = (await open(server, { ctx })).page;
	const t2 = (await open(server, { ctx })).page;
	await startRecording(t1);
	server.pointer = null;
	await startRecording(t2);
	await t1.bringToFront();
	await runClock(t1, 61000);
	await runClock(t2, 61000);
	check("tab 1 analyzes its expired session A", server.rows.s1.status === "Stopping" && (await label(t1)) === "Analyzing…", server.rows);
	check("tab 2's live session B stays Recording with its pointer", server.rows.s2.status === "Recording" && server.pointer === "s2" && (await label(t2)) === "Recording", { rows: server.rows, pointer: server.pointer });
	await ctx.close();
}
{
	const server = makeServer();
	const ctx = await browser.newContext({ viewport: { width: 1100, height: 600 } });
	await wire(ctx, server);
	const tabs = [(await open(server, { ctx })).page];
	await startRecording(tabs[0]);
	tabs.push((await open(server, { ctx })).page);
	await settle(tabs[1], 300);
	server.pointer = null;
	for (const t of tabs) await t.clock.runFor(61000);
	await settle(tabs[0], 500);
	const stops = server.calls("stop");
	const all = (await Promise.all(tabs.map(alerts))).map((a) => a.filter((x) => /without activity/.test(x.message)).length);
	check("two tabs on the same expired session: one stop wins and in total one notice", stops.length >= 1 && all.reduce((a, b) => a + b, 0) === 1, { stops, all });
	check("both tabs converge on Analyzing", (await Promise.all(tabs.map(label))).every((l) => l === "Analyzing…"), await Promise.all(tabs.map(label)));
	await ctx.close();
}

// ---------- 6. Hidden tab ----------
{
	const server = makeServer();
	const ctx = await browser.newContext({ viewport: { width: 1100, height: 600 } });
	await wire(ctx, server);
	await ctx.addInitScript(() => {
		window.__hidden = false;
		Object.defineProperty(document, "hidden", { get: () => window.__hidden });
		Object.defineProperty(document, "visibilityState", { get: () => (window.__hidden ? "hidden" : "visible") });
	});
	const { page } = await open(server, { ctx });
	await startRecording(page);
	server.pointer = null;
	await page.evaluate(() => { window.__hidden = true; });
	const before = server.calls("pill_state").length;
	await runClock(page, 3 * 60 * 1000);
	check("a hidden recording tab makes no pill_state request in 3 minutes", server.calls("pill_state").length === before, server.calls("pill_state").length - before);
	await page.evaluate(() => { window.__hidden = false; document.dispatchEvent(new Event("visibilitychange")); });
	await settle(page, 400);
	check("coming back resyncs once and analyzes the ended session", server.calls("pill_state").length === before + 1 && server.rows.s1.status === "Stopping" && (await label(page)) === "Analyzing…", { calls: server.calls("pill_state").length - before, status: server.rows.s1.status });
	await ctx.close();
}

// ---------- 7. Failed start, double Enter ----------
{
	const server = makeServer();
	const { ctx, page } = await open(server);
	await page.click(PILL);
	await page.fill(".modal input", "FAIL");
	await page.click(".modal .btn-primary");
	await settle(page, 300);
	const st = await page.evaluate(() => ({ open: !!document.querySelector(".modal"), value: document.querySelector(".modal input")?.value, disabled: document.querySelector(".modal .btn-primary")?.disabled }));
	check("a 417 keeps the dialog open with the label and Start enabled", st.open && st.value === "FAIL" && st.disabled === false, st);
	await page.screenshot({ path: path.join(SHOTS, "failed-start.png") });
	await page.fill(".modal input", "second try");
	await page.keyboard.press("Enter");
	await page.keyboard.press("Enter");
	await page.waitForFunction(() => document.querySelector("#frappe-profiler-widget").className.includes("recording"));
	await settle(page);
	check("double Enter starts exactly one session after the retry", server.calls("start").length === 2, server.calls("start").length);
	await ctx.close();
	const second = await open(makeServer());
	await second.page.click(PILL);
	await second.page.fill(".modal input", "typed then escape");
	await second.page.keyboard.press("Escape");
	await second.page.click(PILL);
	check("Escape then reopen keeps the typed label", (await second.page.inputValue(".modal input")) === "typed then escape");
	await second.ctx.close();
}

// ---------- 8. Forced colors and reduced motion ----------
{
	const { ctx, page } = await open(makeServer(), { width: 1100, forced: "active" });
	await startRecording(page);
	const forced = await page.evaluate(() => {
		const dot = getComputedStyle(document.querySelector("#frappe-profiler-widget .fp-dot"));
		return { bg: dot.backgroundColor, adjust: dot.forcedColorAdjust };
	});
	check("forced colors: the dot is painted (not transparent) with ButtonText", forced.bg !== "rgba(0, 0, 0, 0)" && forced.adjust === "none", forced);
	await page.keyboard.press("Tab");
	await page.screenshot({ path: path.join(SHOTS, "forced-colors-recording.png"), clip: { x: 800, y: 500, width: 300, height: 200 } });
	await ctx.close();
}
{
	const { ctx, page } = await open(makeServer(), { reduced: "reduce" });
	await startRecording(page);
	const anim = await page.evaluate(() => getComputedStyle(document.querySelector("#frappe-profiler-widget .fp-dot")).animationName);
	check("reduced motion: the recording dot does not pulse", anim === "none", anim);
	await ctx.close();
}

// ---------- 9. Line profiling picker ----------
for (const theme of ["light", "dark"]) {
	for (const forced of [undefined, "active"]) {
		const ctx = await browser.newContext({ viewport: { width: 700, height: 700 }, forcedColors: forced });
		await wire(ctx, makeServer());
		const page = await ctx.newPage();
		page.on("pageerror", (e) => messages.push("pageerror: " + e));
		await page.goto(ORIGIN + "/picker.html?theme=" + theme);
		await page.waitForSelector(".fp-tree");
		const tag = `picker-${theme}${forced ? "-forced" : ""}`;
		await page.screenshot({ path: path.join(SHOTS, tag + ".png") });
		if (!forced) {
			const c = await page.evaluate(() => {
				const bg = getComputedStyle(document.querySelector(".modal")).backgroundColor;
				return { bg, chevron: getComputedStyle(document.querySelector(".fp-toggle")).color, meta: getComputedStyle(document.querySelector(".optimus-picker-meta")).color,
					empty: getComputedStyle(document.querySelector(".optimus-picker-empty-hint")).color, emptyBg: getComputedStyle(document.querySelector(".optimus-picker-empty")).backgroundColor };
			});
			check(`${tag} chevron >= 3:1 and meta text >= 4.5:1 on the dialog`, contrast(parseRgb(c.chevron), parseRgb(c.bg)) >= 3 && contrast(parseRgb(c.meta), parseRgb(c.bg)) >= 4.5, c);
			check(`${tag} empty-state text >= 4.5:1 on its own background`, contrast(parseRgb(c.empty), parseRgb(c.emptyBg)) >= 4.5, c);
		}
		await ctx.close();
	}
}

check("no console errors or warnings, no page errors", messages.length === 0, messages.slice(0, 5));
await browser.close();
console.log(failures ? `${failures} check(s) FAILED` : "all checks passed");
process.exit(failures ? 1 : 0);
