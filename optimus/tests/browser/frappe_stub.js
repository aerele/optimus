// Minimal Desk / frappe globals for the pill harness. API calls go to fetch(), answered by page.route.
// Limits: no real Bootstrap focus handling, no socket, no frappe.call msgprint modal, no v15.
(function () {
	"use strict";
	const params = new URLSearchParams(location.search);
	document.documentElement.setAttribute("data-theme", params.get("theme") || "light");
	window.__calls = [];
	window.__alerts = [];
	window.__handlers = {};
	window.__ = (s, a) => (a ? String(s).replace(/\{(\d+)\}/g, (m, i) => a[i]) : s);

	function StubDialog(opts) {
		this.opts = opts;
		this.values = {};
	}
	StubDialog.prototype.show = function () {
		this.backdrop = document.createElement("div");
		this.backdrop.className = "modal-backdrop";
		this.el = document.createElement("div");
		this.el.className = "modal show";
		this.el.setAttribute("role", "dialog");
		this.el.setAttribute("aria-label", this.opts.title || "");
		let html = "<h4>" + (this.opts.title || "") + "</h4>";
		(this.opts.fields || []).forEach((f) => {
			if (f.fieldtype === "Data") {
				html += '<label>' + f.label + ' <input data-fieldname="' + f.fieldname + '" value="' + frappe.utils.escape_html(f.default || "") + '"></label>';
			} else if (f.fieldtype === "HTML") {
				html += f.options || "";
			}
		});
		html += '<div class="modal-footer"><button type="button" class="btn-primary">' + (this.opts.primary_action_label || "OK") + "</button></div>";
		this.el.innerHTML = html;
		document.body.appendChild(this.backdrop);
		document.body.appendChild(this.el);
		document.body.classList.add("modal-open");
		this.primary = this.el.querySelector(".btn-primary");
		this.primary.addEventListener("click", () => this.trigger_primary_action());
		// Frappe: Enter in a field and Ctrl+S trigger the primary button's click handler even when it is disabled.
		this.el.addEventListener("keydown", (e) => {
			if (e.key === "Escape") this.hide();
			else if (e.key === "Enter" && e.target.tagName === "INPUT") this.trigger_primary_action();
			else if (e.key === "s" && e.ctrlKey) { e.preventDefault(); this.trigger_primary_action(); }
		});
		const first = this.el.querySelector("input");
		if (first) first.focus();
		frappe.ui.open_dialogs.push(this);
	};
	StubDialog.prototype.trigger_primary_action = function () {
		if (this.opts.primary_action) return this.opts.primary_action(this.get_values());
	};
	StubDialog.prototype.get_values = function () {
		if (!this.el) return this.values;
		this.el.querySelectorAll("input[data-fieldname]").forEach((i) => { this.values[i.dataset.fieldname] = i.value; });
		return this.values;
	};
	StubDialog.prototype.get_value = function (name) { return this.get_values()[name]; };
	StubDialog.prototype.hide = function () {
		if (!this.el) return;
		this.get_values();
		this.el.remove();
		this.backdrop.remove();
		this.el = null;
		frappe.ui.open_dialogs = frappe.ui.open_dialogs.filter((d) => d !== this);
		if (!frappe.ui.open_dialogs.length) document.body.classList.remove("modal-open");
		if (this.onhide) this.onhide();
	};
	StubDialog.prototype.disable_primary_action = function () { this.primary.disabled = true; };
	StubDialog.prototype.enable_primary_action = function () { this.primary.disabled = false; };

	window.frappe = {
		session: { user: "alice@example.com" },
		boot: { optimus_enabled: true, optimus_can_profile: true, time_zone: { system: "UTC" } },
		get_route: () => ["List", "ToDo"],
		set_route: function () { window.__route = [].slice.call(arguments); },
		// Frappe v16 toast markup, styled by its real components/toast.css.
		show_alert: (o) => {
			const msg = typeof o === "string" ? o : o.message;
			window.__alerts.push({ message: msg, indicator: o.indicator });
			const d = document.createElement("div");
			d.className = "es-toast";
			d.setAttribute("role", "status");
			d.innerHTML = '<div class="es-toast__content"><div class="es-toast__message"></div></div>';
			d.querySelector(".es-toast__message").textContent = msg;
			document.getElementById("toasts").appendChild(d);
		},
		utils: {
			escape_html: (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
			get_form_link: (dt, n) => "/app/" + dt + "/" + n,
		},
		realtime: {
			socket: { on: (n, fn) => { (window.__handlers["socket:" + n] = window.__handlers["socket:" + n] || []).push(fn); } },
			on: (n, fn) => { (window.__handlers[n] = window.__handlers[n] || []).push(fn); },
		},
		ui: { Dialog: StubDialog, open_dialogs: [], form: { on: () => {} } },
		call: (opts) => {
			window.__calls.push({ method: opts.method, args: opts.args || {} });
			return fetch("/api/method/" + opts.method, {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify(opts.args || {}),
			}).then(async (r) => {
				const j = await r.json().catch(() => ({}));
				if (r.ok) { if (opts.callback) opts.callback(j); }
				else if (opts.error) opts.error(j);
			}).catch(() => opts.error && opts.error({}));
		},
	};
	// Test hook: deliver a realtime event as the socket would.
	window.__emit = (name, data) => (window.__handlers[name] || []).forEach((fn) => fn(data));
	document.addEventListener("DOMContentLoaded", () => {
		const name = document.getElementById("theme-name");
		if (name) name.textContent = params.get("theme") || "light";
	});
})();
