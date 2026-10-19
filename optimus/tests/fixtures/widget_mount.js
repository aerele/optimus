// Runs floating_widget.js with stub globals and prints "mounted" or "hidden".
// Usage: node widget_mount.js <floating_widget.js> <boot json>
const fs = require("fs");
const vm = require("vm");

const [jsPath, bootJson] = process.argv.slice(2);

// Absorbs any property read, call or construct.
function sink() {
	const fn = function () {};
	return new Proxy(fn, {
		get: (t, k) => (k === "then" ? undefined : k === Symbol.toPrimitive ? () => "" : sink()),
		set: () => true,
		apply: () => sink(),
		construct: () => sink(),
	});
}

let mounted = 0;
const body = new Proxy({}, {
	get: (t, k) => (k === "appendChild" ? () => { mounted += 1; } : sink()),
	set: () => true,
});
const document = new Proxy({}, {
	get: (t, k) => (k === "readyState" ? "complete" : k === "body" ? body : k === "getElementById" ? () => null : sink()),
});
const frappe = new Proxy({ session: { user: "u@example.com" }, boot: JSON.parse(bootJson) }, {
	get: (t, k) => (k in t ? t[k] : sink()),
});

const ctx = { document, frappe, window: sink(), console, setTimeout: () => 0, setInterval: () => 0, clearInterval: () => {} };
vm.runInNewContext(fs.readFileSync(jsPath, "utf8"), ctx);
process.stdout.write(mounted ? "mounted" : "hidden");
