// Copyright (c) 2026, Optimus contributors
// For license information, please see license.txt
//
// Optimus Session form script (Phase 5).
//
// Customizes the detail view to feel like a "report" rather than a raw
// data form. The customer-facing summary HTML is rendered prominently at
// the top, the analyzer findings are listed in a friendly format and the
// two report files get prominent download buttons (raw is gated to admins).

frappe.ui.form.on("Optimus Session", {
	refresh(frm) {
		render_status_indicator(frm);
		render_phase2_progress(frm);
		render_drain_progress(frm);
		render_analyze_progress(frm);
		render_download_buttons(frm);
		render_retry_button(frm);
		render_regenerate_report_button(frm);
		render_findings_summary(frm);
		render_phase2_button(frm);
		render_ai_buttons(frm);
		subscribe_phase2_events(frm);
		subscribe_session_progress(frm);
	},
});

// Duration formatter matching the server-side rule (optimus.analyzers.base
// humanize_duration_ms): a value at or above the "render durations in seconds
// above (ms)" threshold reads as "1.50s", below it stays in ms. The threshold
// comes from frappe.boot (optimus.boot.boot_session) so the picker rolls over
// at the same point the report does. Defaults to 1000ms.
function optimus_fmt_ms(ms, decimals) {
	var v = Number(ms) || 0;
	var dec = decimals == null ? 0 : decimals;
	var t = frappe.boot && frappe.boot.optimus_large_duration_threshold_ms;
	// Only a missing value falls back to 1000; an explicit 0 disables the rollover.
	var threshold = t === undefined || t === null ? 1000 : t;
	// Decide the unit from the value rounded to whole milliseconds (matches the
	// server, independent of decimals), so a value that rounds up to a full
	// second reads as "1.00s", never "1000ms".
	var rounded = Math.round(Math.abs(v));
	// Convert from the whole-millisecond value (Math.round(v)/1000), matching the
	// server's UNIT decision. The final 2-decimal seconds can still differ from the
	// report by 0.01s on a whole-ms value ending in 5 (e.g. 1125ms -> 1.125s: this
	// picker's toFixed rounds half-away to "1.13s" while the report's Python %.2f
	// rounds half-to-even to "1.12s"). The picker is a live convenience; the report
	// HTML is the source of truth. Exact rounding parity isn't worth the FP fiddle.
	if (threshold && rounded >= threshold) {
		var secs = (Math.round(v) / 1000).toFixed(2);
		// Match the server + the ms branch below: a value that rounds to zero must
		// not keep a sign ("-0.00s" -> "0.00s"). Reachable only if the helper is
		// reused for a signed value with a threshold <= 1.
		if (secs.charAt(0) === "-" && Number(secs) === 0) secs = secs.slice(1);
		return secs + "s";
	}
	var text = v.toFixed(dec);
	// Match the server: a value that rounds to zero must not keep a sign
	// ("-0ms" -> "0ms"). Reachable only if the helper is reused for a signed value.
	if (text.charAt(0) === "-" && Number(text) === 0) text = text.slice(1);
	return text + "ms";
}

// A background completion must not discard edits typed while it was running.
function _reload_clean_form(frm) {
	if (frm.is_dirty && frm.is_dirty()) {
		frappe.show_alert({message: __("Background results are saved. Your unsaved edits were kept; reload after saving or discarding them."), indicator: "orange"});
		return;
	}
	frm.reload_doc();
}

// SQL progress is authoritative. Queue outages never imply completion.
function _ai_current(frm, state) {
	const route = frappe.get_route();
	return frm._optimus_ai === state && frm.doc.session_uuid === state.session_uuid &&
		route[0] === "Form" && route[1] === "Optimus Session" && route[2] === frm.doc.name;
}

function _ai_hold_save(frm, state) {
	if (!frm.save_disabled || state.held_save) {
		state.held_save = true;
		frm.disable_save(true);
	}
}

function _ai_release_save(frm, state) {
	const writable = !frm.read_only && (!frm.perm || (frm.perm[0] && frm.perm[0].write)) &&
		!(frappe.boot && frappe.boot.read_only);
	if (state.held_save && writable && frm.doc.session_uuid === state.session_uuid) frm.enable_save();
	state.held_save = false;
}

function _ai_stop(frm, state) {
	if (!state) return;
	clearTimeout(state.timer);
	state.epoch++;
	_ai_release_save(frm, state);
}

function render_ai_buttons(frm) {
	let state = frm._optimus_ai;
	if (!state || state.session_uuid !== frm.doc.session_uuid) {
		_ai_stop(frm, state);
		state = frm._optimus_ai = {session_uuid: frm.doc.session_uuid, epoch: 0, delay: 5000,
			run: null, busy: false, held_save: false, submitting: false, can_act: false};
		_single_banner(frm, "optimus-ai-progress", "", null);
	}
	if (frm.is_new()) return;
	if (!frm._optimus_ai_route_watch) {
		frm._optimus_ai_route_watch = true;
		frappe.router.on("change", () => {
			if (frm._optimus_ai && !_ai_current(frm, frm._optimus_ai)) _ai_stop(frm, frm._optimus_ai);
		});
	}
	// Frappe may reset Save while refreshing the same form.
	if (state.busy || state.submitting || state.unknown) _ai_hold_save(frm, state);
	clearTimeout(state.timer);
	_ai_poll(frm, state);
}

function _ai_schedule(frm, state, delay) {
	clearTimeout(state.timer);
	state.timer = setTimeout(() => {
		if (_ai_current(frm, state)) _ai_poll(frm, state);
		else _ai_stop(frm, state);
	}, delay);
}

function _ai_unknown(frm, state) {
	// Even the first failed poll can hide another process's active reservation.
	state.unknown = true;
	_ai_hold_save(frm, state);
	state.can_act = false;
	frm.remove_custom_button(__("Refresh AI suggestions"), __("AI"));
	frm.remove_custom_button(__("Resume AI refresh"), __("AI"));
	_single_banner(frm, "optimus-ai-progress", "form-message orange",
		'<span role="status">' + frappe.utils.escape_html(__("AI refresh status is unavailable. Checking again; saved profiling results are retained.")) + '</span>');
	state.delay = Math.min(30000, state.delay * 2);
	_ai_schedule(frm, state, state.delay);
}

function _ai_poll(frm, state) {
	if (!_ai_current(frm, state)) return _ai_stop(frm, state);
	if (state.submitting) return _ai_schedule(frm, state, 5000);
	const epoch = ++state.epoch;
	frappe.call({
		method: "optimus.api.ai_refresh_status", args: {session_uuid: state.session_uuid},
		callback: (r) => {
			if (!_ai_current(frm, state) || state.epoch !== epoch) return;
			const data = r && r.message;
			if (!data || data.status !== "ok") return _ai_unknown(frm, state);
			_ai_apply_status(frm, state, data);
		},
		error: () => {
			if (_ai_current(frm, state) && state.epoch === epoch) _ai_unknown(frm, state);
		},
	});
}

function _ai_apply_status(frm, state, data) {
	const run = data.refresh;
	const old = state.run;
	if (run && old && ((run.run_id === old.run_id && run.seq < old.seq) ||
		(run.run_id !== old.run_id && run.requested_at < old.requested_at))) {
		return _ai_schedule(frm, state, 5000);
	}
	if ((!run && state.busy && old) || (run && !["queued", "running", "complete", "stopped", "cancelled", "interrupted"].includes(run.state))) {
		return _ai_unknown(frm, state);
	}
	const was_busy = state.busy;
	state.unknown = false;
	state.run = run;
	state.busy = !!run && ["queued", "running"].includes(run.state);
	state.can_act = !!data.can_act;
	state.plan = data.plan;
	state.delay = 5000;
	if (state.busy) _ai_hold_save(frm, state);
	else _ai_release_save(frm, state);
	frm.remove_custom_button(__("Refresh AI suggestions"), __("AI"));
	frm.remove_custom_button(__("Resume AI refresh"), __("AI"));
	if (state.can_act && !state.busy && frm.doc.status === "Ready") {
		const plan = state.plan;
		if (plan && (plan.total || plan.steps)) render_ai_refill_button(frm, state);
		if (run && run.scope === "all" && ["stopped", "interrupted", "cancelled"].includes(run.state) && run.retry_no < 3) {
			frm.add_custom_button(__("Resume AI refresh"), () => _ai_resume_dialog(frm, state), __("AI"));
		}
	}
	_ai_progress_banner(frm, state);
	if (was_busy && !state.busy && run) {
		frappe.show_alert({message: __("AI refresh finished. Saved profiling results are retained."),
			indicator: run.state === "complete" ? "green" : "orange"});
		_reload_clean_form(frm);
	}
	_ai_schedule(frm, state, state.busy ? 5000 : 30000);
}

function _ai_progress_banner(frm, state) {
	const run = state.run;
	if (!run) return _single_banner(frm, "optimus-ai-progress", "", null);
	const labels = {queued: __("Queued"), running: __("Running"), complete: __("Complete"),
		stopped: __("Stopped"), interrupted: __("Interrupted"), cancelled: __("Cancelled")};
	const usage = run.usage || {};
	let text = __("AI refresh: {0}. Saved {1}, failed {2}, skipped {3}, uncertain {4}, not reached {5}. Reported tokens: {6}.",
		[labels[run.state], run.done || 0, run.failed || 0, run.skipped || 0,
			(run.uncertain || 0) + (run.blocked_uncertain || 0), run.not_reached || 0, usage.tokens_reported || 0]);
	if (usage.incomplete_attempts) text += " " + __("Provider usage is incomplete; these counts are not an exact bill.");
	if (run.render_pending) text += " " + __("Saved answers still need a report update. Use Regenerate Reports after the refresh stops.");
	if (run.end_reason) text += " " + _ai_end_message(run);
	const cancel = state.busy && state.can_act;
	const html = '<span role="status">' + frappe.utils.escape_html(text) + '</span>' +
		(cancel ? ' <button type="button" class="btn btn-xs btn-default optimus-ai-cancel">' + frappe.utils.escape_html(__("Cancel AI refresh")) + '</button>' : '');
	const banner = _single_banner(frm, "optimus-ai-progress", "form-message blue", html);
	if (banner && cancel) banner.find(".optimus-ai-cancel").on("click", () => {
		if (!_ai_current(frm, state)) return;
		const run_id = run.run_id;
		const epoch = ++state.epoch;
		const current = () => _ai_current(frm, state) && state.epoch === epoch && state.run && state.run.run_id === run_id;
		frappe.call({
			method: "optimus.api.cancel_ai_refresh", args: {session_uuid: state.session_uuid, run_id},
			callback: () => { if (current()) _ai_poll(frm, state); },
			error: () => { if (current()) _ai_unknown(frm, state); },
		});
	});
}

function _ai_end_message(run) {
	if (run.end_reason === "breaker") return __("Repeated provider errors or a provider limit stopped this run. Check the provider configuration and Error Log before retrying.");
	if (run.end_reason === "deadline") return __("The refresh time limit was reached.");
	if (run.end_reason === "render_failed") return __("Report replacement failed; the previous report was kept.");
	if (["uncertain", "lease_expired", "worker_interrupted"].includes(run.end_reason)) return __("An interrupted call may have been billed. It will not be repeated automatically.");
	if (run.end_reason === "history_limit") return __("Refresh history reached its safety limit. Ask an administrator to review the retained attempts.");
	if (run.end_reason === "not_ready") return __("The session changed and is no longer Ready for AI work.");
	if (run.end_reason === "permission") return __("The requesting user no longer has permission.");
	if (run.end_reason === "phase2") return __("A Phase 2 run prevented further AI work.");
	return "";
}

// Shared single-banner mechanism for the in-form status banners (analyze
// "preparing report" and background-jobs drain). Frappe's frm.set_intro and
// frm.dashboard.set_headline both route to layout.js show_message, which APPENDS
// a fresh .form-message on every non-null call rather than replacing the previous
// one (layout.js: $html ... appendTo(this.message)), so driving either from a
// per-tick event stacked one bar per tick instead of updating one. This keeps a
// single element found and removed by `idClass` (which must be unique to this
// banner) and rewrites its contents in place, returning it so the caller can tag
// it. Pass html=null to remove it. `extraClasses` are added only when the element
// is created (e.g. Frappe theme classes); `styler` runs once on a freshly created
// element whose classes do not carry its colours. The element is prepended to
// .form-layout, outside .form-message-container, so Frappe's own show_message()
// clearing on each refresh never removes it.
function _single_banner(frm, idClass, extraClasses, html, styler) {
	const root = frm.$wrapper;
	if (!root || !root.length) return null;
	if (html == null) {
		root.find("." + idClass).remove();
		return null;
	}
	let $b = root.find("." + idClass);
	if (!$b.length) {
		const cls = extraClasses ? idClass + " " + extraClasses : idClass;
		$b = $('<div class="' + cls + '"></div>');
		if (styler) styler($b);
		const $host = root.find(".form-layout").first();
		($host.length ? $host : root).prepend($b);
	}
	$b.html(html);
	return $b;
}

// The analyze "preparing report" banner. Uses Frappe's native .form-message.blue
// theme (same padding, font size, blue scheme plus dark-theme variants) so it
// matches the bar the old set_headline produced, no inline styling needed. It is
// tagged with the owning session so render_analyze_progress can drop it once the
// form shows a different or a new session. Pass html=null to remove it.
function _progress_banner(frm, html) {
	const $b = _single_banner(frm, "optimus-analyze-banner", "form-message blue", html);
	if ($b) $b.attr("data-optimus-session", frm.doc.session_uuid || "");
}

// Clear a stale analyze banner on refresh / navigation. The banner is prepended
// to .form-layout, which Frappe reuses across sessions of this doctype (in-app
// navigation keeps one frm and one $wrapper), so a banner painted for session A
// outlives the move to another form. It must be dropped on refresh unless the
// shown form is that same session still analyzing, so three cases clear it: a new
// unsaved form (is_new, which never emits progress to self-correct), a
// Ready / Failed session and a different Analyzing session (which would otherwise
// show session A's stale percentage until its own first tick). Live progress
// repaints the banner via the optimus_progress handler, so clearing here never
// hides current progress. The ready / failed handlers also remove it but are
// gated by mine(p) and never fire for the form you navigate to.
function render_analyze_progress(frm) {
	const root = frm.$wrapper;
	if (!root || !root.length) return;
	const $b = root.find(".optimus-analyze-banner");
	if (!$b.length) return;
	const owns_live_analyze =
		!frm.is_new() &&
		frm.doc.status === "Analyzing" &&
		$b.attr("data-optimus-session") === frm.doc.session_uuid;
	if (!owns_live_analyze) {
		_progress_banner(frm, null);
	}
}

// Show a live headline on the form while analyze is running (the floating
// widget shows the same progress, but if you're sitting on the Profiler
// Session form you shouldn't have to stare at a static "Analyzing" status
// especially when AI fix suggestions are being generated, which can take
// a while). Cleared + reloaded when the session reaches Ready / Failed.
function subscribe_session_progress(frm) {
	if (frm.is_new()) return;
	if (frm._progress_subscribed) return;
	frm._progress_subscribed = true;

	const mine = (p) => p && p.session_uuid === frm.doc.session_uuid;

	frappe.realtime.on("optimus_progress", (p) => {
		if (!mine(p)) return;
		const pct = typeof p.percent === "number" ? Math.round(p.percent) : null;
		const desc = frappe.utils.escape_html(p.description || "Analyzing…");
		// Update one in-place banner rather than frm.dashboard.set_headline,
		// which appends a new bar on every progress tick (see _progress_banner).
		_progress_banner(
			frm,
			'<span class="text-muted">' +
				'<i class="fa fa-spinner fa-spin" style="margin-right:6px;"></i>' +
				(pct !== null ? __("Preparing report {0}% · {1}", [pct, desc]) : desc) +
				"</span>"
		);
	});
	frappe.realtime.on("optimus_session_ready", (p) => {
		if (!mine(p)) return;
		_progress_banner(frm, null);
		frappe.show_alert({ message: __("Report ready"), indicator: "green" });
		setTimeout(() => frm.reload_doc(), 800);
	});
	frappe.realtime.on("optimus_session_failed", (p) => {
		if (!mine(p)) return;
		_progress_banner(frm, null);
		setTimeout(() => frm.reload_doc(), 800);
	});
	// v0.7.x: auto-arm fires server-side during analyze (off-form). Tell the
	// user a pass is armed and what to do next; the reload re-runs
	// render_phase2_button → the in-form banner + Stop button appear.
	frappe.realtime.on("optimus_phase2_armed", (p) => {
		if (!p || p.docname !== frm.doc.name) return;
		const fns = (p.functions || []).join(", ");
		frappe.show_alert(
			{
				message: __(
					"Line profiling armed for {0} hot path(s){1}. Re-run your " +
					"flow, then click Stop Phase 2 Run to capture the exact lines.",
					[p.count || 0, fns ? " (" + frappe.utils.escape_html(fns) + ")" : ""]
				),
				indicator: "orange",
			},
			12
		);
		setTimeout(() => frm.reload_doc(), 800);
	});
}

function render_ai_refill_button(frm, state) {
	frm.add_custom_button(__("Refresh AI suggestions"), () => {
		if (!_ai_current(frm, state) || state.busy || state.submitting) return;
		if (frm.is_dirty && frm.is_dirty()) return frappe.msgprint(__("Save or discard your edits before starting an AI refresh."));
		const plan = state.plan;
		const description = (all) => frappe.utils.escape_html(__("Current selection: {0} finding(s), plus {1} Steps to Reproduce rewrite. The worker rechecks eligibility; the finding limit for this refresh is {2}. Saved profiling results remain available.",
			[all ? plan.selected_all : plan.selected, plan.steps ? 1 : 0, plan.cap || __("unlimited")]));
		const dialog = new frappe.ui.Dialog({title: __("Refresh AI suggestions"), fields: [
			{fieldtype: "HTML", fieldname: "summary", options: description(false)},
			{fieldtype: "Check", fieldname: "regenerate_all", default: 0,
				label: __("Also replace current suggestions"), onchange: () => {
					dialog.fields_dict.summary.df.options = description(!!dialog.get_value("regenerate_all"));
					dialog.refresh_field("summary");
				}},
		], primary_action_label: __("Queue refresh"), primary_action: (values) => {
			if (!values.regenerate_all && !plan.selected && !plan.steps) return frappe.msgprint(__("No missing or outdated suggestions need a refresh."));
			dialog.hide();
			_refill_ai_call(frm, state, {regenerate_all: !!values.regenerate_all});
		}});
		dialog.show();
	}, __("AI"));
}

function _ai_resume_dialog(frm, state) {
	if (!_ai_current(frm, state) || state.busy || state.submitting) return;
	const run = state.run;
	const uncertain = (run.uncertain || 0) + (run.blocked_uncertain || 0);
	const dialog = new frappe.ui.Dialog({title: __("Resume AI refresh"), fields: [
		{fieldtype: "HTML", fieldname: "summary", options: frappe.utils.escape_html(__("Resume the original selection and cap. Already saved answers will be kept."))},
		{fieldtype: "Check", fieldname: "retry_uncertain", default: 0, hidden: !uncertain,
			label: __("Repeat uncertain calls, accepting possible duplicate provider charges")},
	], primary_action_label: __("Queue resume"), primary_action: (values) => {
		dialog.hide();
		_refill_ai_call(frm, state, {resume_from: run.run_id, retry_uncertain: !!values.retry_uncertain});
	}});
	dialog.show();
}

function _refill_ai_call(frm, state, options) {
	if (!_ai_current(frm, state) || state.busy || state.submitting) return;
	if (frm.is_dirty && frm.is_dirty()) return frappe.msgprint(__("Save or discard your edits before starting an AI refresh."));
	state.submitting = true;
	state.epoch++; // invalidate a status response sent before this request
	clearTimeout(state.timer);
	_ai_hold_save(frm, state);
	frappe.call({
		method: "optimus.api.refill_ai_suggestions",
		args: {session_uuid: state.session_uuid, ...options},
		callback: (r) => {
			if (!_ai_current(frm, state)) return;
			state.submitting = false;
			const data = r && r.message;
			if (data && data.ok && data.refresh) {
				_ai_apply_status(frm, state, {refresh: data.refresh, can_act: true, plan: null});
			} else if (data && data.status === "refused") {
				_ai_release_save(frm, state);
				frappe.msgprint(frappe.utils.escape_html(data.message || __("AI refresh could not start.")));
				_ai_poll(frm, state);
			} else {
				state.busy = true;
				_ai_unknown(frm, state);
			}
		},
		error: () => {
			if (!_ai_current(frm, state)) return;
			state.submitting = false;
			// A lost HTTP response can follow a committed admission. Poll SQL.
			state.busy = true;
			_ai_unknown(frm, state);
		},
	});
}

// v0.6.0: Phase-2 line-profile picker.
//
// Adds a "Run Line-Profile Pass" custom button when the session is Ready.
// Clicking opens a dialog that fetches curated candidates (top hot frames
// from phase-1) plus a free-form textbox for dotted paths the user types.
// Submission posts to api.start_line_profile_pass; realtime events drive
// the form's Phase-2 history child table updates.
// The Phase 2 "line profiling is armed" banner. Same in-place mechanism as the
// analyze banner (see _single_banner); frm.set_intro appended a duplicate on every
// refresh while a pass was Recording. Uses Frappe's native orange form-message
// theme. Pass html=null to remove it.
function _phase2_armed_banner(frm, html) {
	_single_banner(frm, "optimus-phase2-armed", "form-message orange", html);
}

function render_phase2_button(frm) {
	if (frm.is_new() || frm.doc.status !== "Ready") {
		// Not a Ready session (or unsaved): drop any armed banner left in the
		// reused form wrapper by a session that was mid-Recording.
		_phase2_armed_banner(frm, null);
		return;
	}

	// If there's an in-flight Recording row, surface Stop as the primary
	// affordance that's what the user is looking for after they've
	// reproduced their flow.
	var recording = (frm.doc.phase_2_runs || []).find(function (r) {
		return r.status === "Recording";
	});

	if (recording) {
		var stop_btn = frm.add_custom_button(
			__("Stop Phase 2 Run"),
			function () {
				frappe.call({
					method: "optimus.api.stop_line_profile_pass",
					args: { run_uuid: recording.run_uuid },
					freeze: true,
					freeze_message: __("Stopping phase 2..."),
					callback: function () {
						frappe.show_alert({
							message: __(
								"Phase 2 stopped. Analyzing now the report " +
								"section will refresh when ready."
							),
							indicator: "blue",
						});
						frm.reload_doc();
					},
				});
			},
			__("Phase 2")
		);
		// Visually emphasize the stop action while a run is live.
		try {
			stop_btn.removeClass("btn-default").addClass("btn-warning");
		} catch (e) { /* noop */ }

		// v0.7.x: a Recording pass does nothing until the flow re-executes and
		// the pass is stopped. Auto-arm (and the picker) leave users staring at
		// a Stop button with no context spell out the two steps.
		// One in-place banner; set_intro appended a duplicate on every refresh in
		// this Frappe version (see _single_banner).
		_phase2_armed_banner(
			frm,
			__(
				"🔬 Line profiling is armed. Re-run your flow now so the hot " +
				"path(s) execute again, then click \"Stop Phase 2 Run\" above " +
				"the report will then pinpoint the exact hot line(s). " +
				"(Profiling has to re-execute your code; it can't replay the " +
				"original run.)"
			)
		);
	} else {
		// A saved result can still need a report after a file/render failure.
		const pending = (frm.doc.phase_2_runs || []).some(row => row.analyze_render_pending);
		_phase2_armed_banner(frm, pending
			? __("Phase 2 results are saved. Use Regenerate Reports to update the report.") : null);
	}

	// Retrying attaches to live work or queues a bounded new generation.
	const stuck_runs = (frm.doc.phase_2_runs || []).filter(row =>
		row.status === "Analyzing" || row.status === "Failed");
	const clean_form = () => {
		if (!frm.is_dirty()) return true;
		frappe.msgprint(__("Save or discard your changes before queueing Phase 2 analysis."));
		return false;
	};
	if (stuck_runs.length >= 2) {
		const batch = stuck_runs.slice(0, 5);
		frm.add_custom_button(__("Retry next {0} Phase 2 runs", [batch.length]), () => {
			if (!clean_form()) return;
			frappe.call({
				method: "optimus.api.retry_phase2_analyzes_batch",
				args: {run_uuids: batch.map(row => row.run_uuid)},
				callback(r) {
					const tally = ((r && r.message) || {}).tallies || {};
					frappe.show_alert({
						message: __("Phase 2: {0} queued or running; {1} refused or failed.",
							[tally.Analyzing || 0, (tally.Failed || 0) + (tally.Skipped || 0)]),
						indicator: tally.Failed ? "orange" : "blue",
					});
					_reload_clean_form(frm);
				},
			});
		}, __("Phase 2"));
	}
	stuck_runs.forEach(row => {
		frm.add_custom_button(__("Retry Phase 2 Analyze ({0})", [row.run_uuid.slice(0, 8)]), () => {
			if (!clean_form()) return;
			frappe.call({
				method: "optimus.api.retry_phase2_analyze",
				args: {run_uuid: row.run_uuid},
				callback(r) {
					const accepted = r && r.message && r.message.status === "Analyzing";
					frappe.show_alert({
						message: accepted ? __("Phase 2 analysis is queued or running. Results will appear when it finishes.")
							: __("Phase 2 admission was not confirmed. Reload the session to check its status."),
						indicator: accepted ? "blue" : "orange",
					});
					_reload_clean_form(frm);
				},
			});
		}, __("Phase 2"));
	});

	frm.add_custom_button(__("Run Line-Profile Pass"), function () {
		open_phase2_picker(frm);
	}, __("Phase 2"));

	// Recovery hatch: force-clear a stuck phase-2 active flag if a
	// previous run never reached Stop (worker crash, tab close, etc.).
	// Idempotent safe to click when nothing is stuck.
	frm.add_custom_button(__("Force Stop Stuck Run"), function () {
		frappe.confirm(
			__(
				"Clear any in-flight phase-2 active flag for your user " +
				"and mark stuck Recording rows as Failed? Use this if " +
				"the picker keeps reporting 'phase-2 already active'."
			),
			function () {
				frappe.call({
					method: "optimus.api.force_stop_phase2",
					callback: function (r) {
						var msg = r && r.message ? r.message : {};
						frappe.show_alert({
							message: __(
								"Phase 2 cleared flag was " +
								(msg.cleared_active_flag ? "set" : "already clear") +
								"; " +
								(msg.rows_marked_failed || 0) +
								" rows marked Failed."
							),
							indicator: "blue",
						});
						frm.reload_doc();
					},
				});
			}
		);
	}, __("Phase 2"));
}

function open_phase2_picker(frm) {
	// First fetch the candidate list from the API. We wait for the data
	// before opening the dialog so the MultiCheck field populates with
	// real options rather than rendering empty and re-rendering later.
	frappe.call({
		method: "optimus.api.get_phase2_candidates",
		args: { session_uuid: frm.doc.session_uuid },
		freeze: true,
		freeze_message: __("Loading phase-1 hot frames..."),
		callback: function (r) {
			var data = r.message || {};
			if (!data.line_profiler_available) {
				frappe.msgprint({
					title: __("line_profiler not installed"),
					message: __(
						"Phase 2 needs the line_profiler package. Install it via " +
						"<code>bench pip install line_profiler</code> and restart."
					),
					indicator: "red",
				});
				return;
			}
			show_phase2_dialog(frm, data);
		},
	});
}

function show_phase2_dialog(frm, data) {
	var primary = data.candidates || [];
	var framework = data.observations || [];

	// Phase K v0.7 GA: build a collapsible <details> tree from the
	// flat DFS pre-order candidate list. Each candidate's ``depth``
	// field tells us where to nest it; the stack-based walk re-builds
	// parent-child structure in one pass. Browser-native <details>
	// handles expand/collapse - no custom toggle JS needed. Children
	// start collapsed (closed <details>) so the user sees just the
	// top-level entries by default and drills in on demand.
	function build_tree_html(candidates) {
		if (!candidates.length) return "";
		var roots = [];
		var stack = [{depth: -1, children: roots}];
		candidates.forEach(function (c) {
			var depth = c.depth || 0;
			while (stack[stack.length - 1].depth >= depth) stack.pop();
			var node = {c: c, children: []};
			stack[stack.length - 1].children.push(node);
			stack.push({depth: depth, children: node.children});
		});

		function esc(s) {
			return frappe.utils.escape_html(String(s == null ? "" : s));
		}
		function meta(c) {
			return (
				" <span style='color:#6b7280;font-size:0.85em;'>(" +
				optimus_fmt_ms(c.cumulative_ms || 0, 1) + " &middot; " +
				(c.hit_count || 0) + "&times; hits &middot; " +
				esc(c.app) +
				")</span>"
			);
		}
		function row(node) {
			var c = node.c;
			var dotted = esc(c.dotted_path);
			// v0.7.x (P2): pre-tick the recommended hot paths so the user can
			// run a line-profile pass in one click without hunting for them.
			var cb = (
				"<input type='checkbox' class='fp-pick'" +
				" data-pick=\"" + dotted + "\"" +
				(c.recommended ? " checked" : "") +
				" onclick='event.stopPropagation()'" +
				" style='margin-right:6px;vertical-align:middle;'>"
			);
			var body = cb +
				"<span class='fp-pick-label' " +
				"style='font-family:var(--font-mono);font-size:0.85em;'>" +
				dotted + "</span>" + meta(c);

			// Every row leads with a fixed-width disclosure cell (.fp-toggle):
			// a chevron for expandable rows, an invisible spacer for leaves.
			// This keeps a row's checkbox flush at its OWN depth, so a
			// top-level (depth-0) leaf never renders indented as though it
			// were nested under the preceding sibling <details>. Depth is
			// conveyed purely by DOM nesting (.fp-children), not per-row pad.
			if (node.children.length === 0) {
				return (
					"<div class='fp-row fp-leaf'>" +
					"<label class='fp-rowline'>" +
					"<span class='fp-toggle fp-spacer'></span>" +
					body + "</label>" +
					"</div>"
				);
			}
			var inner = node.children.map(row).join("");
			return (
				"<details class='fp-row'>" +
				"<summary class='fp-summary'>" +
				"<span class='fp-toggle'></span>" +
				"<label class='fp-rowline' " +
				"onclick='event.stopPropagation()'>" +
				body + "</label>" +
				"</summary>" +
				"<div class='fp-children'>" +
				inner +
				"</div>" +
				"</details>"
			);
		}
		// Scoped styling: hide the native <details> disclosure marker and
		// draw our own fixed-width chevron, so leaf rows and parent rows
		// align column-for-column at every depth. Indentation comes only
		// from .fp-children nesting (one dashed guide per level). Idempotent
		// if injected more than once (primary + framework trees).
		var style = (
			"<style>" +
			".fp-tree summary{list-style:none;}" +
			".fp-tree summary::-webkit-details-marker{display:none;}" +
			".fp-tree summary::marker{content:'';}" +
			".fp-tree .fp-row{margin:0;}" +
			".fp-tree .fp-summary{cursor:pointer;padding:2px 0;" +
			"display:flex;align-items:center;}" +
			".fp-tree .fp-leaf{padding:2px 0;}" +
			".fp-tree .fp-rowline{cursor:pointer;display:flex;" +
			"align-items:center;flex:1;margin:0;}" +
			".fp-tree .fp-toggle{display:inline-block;width:16px;" +
			"min-width:16px;text-align:center;color:#9ca3af;" +
			"font-size:0.8em;user-select:none;}" +
			".fp-tree details>summary>.fp-toggle::before{content:'\\25B8';}" +
			".fp-tree details[open]>summary>.fp-toggle::before{content:'\\25BE';}" +
			".fp-tree .fp-spacer{visibility:hidden;}" +
			".fp-tree .fp-children{padding-left:16px;" +
			"border-left:1px dashed #d1d5db;margin-left:7px;}" +
			"</style>"
		);
		return (
			style +
			"<div class='fp-tree' style='max-height:340px;" +
			"overflow-y:auto;border:1px solid var(--border-color, #e5e7eb);" +
			"border-radius:4px;padding:8px 12px;'>" +
			roots.map(row).join("") +
			"</div>"
		);
	}

	// When there are no user-app frames at all (vanilla ERPNext or a
	// site without custom apps), the framework list IS the primary
	// list the customer is profiling erpnext / frappe code. Promote
	// it to default-expanded so the dialog shows usable candidates
	// instead of an empty primary section.
	var no_user_app = primary.length === 0 && framework.length > 0;

	var fields = [
		{
			fieldname: "intro_html",
			fieldtype: "HTML",
			options:
				"<p style='margin-bottom:8px;'>Tick functions from phase-1's " +
				"top hot frames, or paste a dotted path below. " +
				"Phase 2 will instrument <strong>only</strong> these " +
				"functions during your next reproduction of the flow.</p>",
		},
	];

	// Phase K v0.7 GA: when the picker has zero curated candidates, show
	// a yellow callout explaining why (no actions / no call trees / all
	// filtered) instead of a silently empty dialog. The diagnostic dict
	// is populated by api.get_phase2_candidates; older callers without
	// it fall back to a generic message.
	if (primary.length === 0 && framework.length === 0) {
		var diag = data.diagnostic || {};
		var hint = diag.hint || (
			"No curated picks were found. Use the freeform textbox below " +
			"to type the dotted path of the function you want to profile."
		);
		fields.push({
			fieldname: "empty_state_html",
			fieldtype: "HTML",
			options: (
				"<div style='padding:10px 14px;background:#fef3c7;" +
				"border:1px solid #fbbf24;border-radius:6px;" +
				"margin-bottom:12px;'>" +
				"<strong>No curated functions available</strong><br>" +
				"<span style='font-size:0.85em;color:#92400e'>" +
				hint +
				"</span><br><br>" +
				"<code style='font-size:0.78em;color:#6b7280'>" +
				"actions=" + (diag.action_count || 0) +
				" &middot; with_tree=" + (diag.actions_with_call_tree_json || 0) +
				" &middot; parsed=" + (diag.trees_parsed_ok || 0) +
				" &middot; pre_filter=" + (diag.raw_candidates_before_filter || 0) +
				"</code></div>"
			),
		});
	}

	if (primary.length) {
		fields.push({
			fieldname: "curated_section",
			fieldtype: "Section Break",
			label: __("Hot frames from your apps"),
			description: __(
				"Click a row's chevron to expand its nested calls. " +
				"Tick the boxes you want to line-profile."
			),
		});
		fields.push({
			fieldname: "curated_html",
			fieldtype: "HTML",
			options: build_tree_html(primary),
		});
	}

	if (framework.length) {
		fields.push({
			fieldname: "framework_section",
			fieldtype: "Section Break",
			label: no_user_app
				? __("Hot frames (frappe / erpnext / framework code)")
				: __(
					"+ " +
					framework.length +
					" framework frames (frappe / erpnext) actionable for " +
					"customizations or framework-level fixes"
				),
			collapsible: !no_user_app,
			collapsible_depends_on: no_user_app ? "" : "0",
		});
		fields.push({
			fieldname: "framework_html",
			fieldtype: "HTML",
			options: build_tree_html(framework),
		});
	}

	fields.push({
		fieldname: "section_break_freeform",
		fieldtype: "Section Break",
		label: __("Additional dotted paths"),
	});
	fields.push({
		fieldname: "freeform",
		fieldtype: "Small Text",
		label: __("One dotted path per line"),
		description: __(
			"e.g. <code>my_app.tasks.heavy_helper</code>. Paths that " +
			"can't be imported are rejected before phase 2 starts. Use " +
			"this when the curated list above doesn't surface the " +
			"function you want, or to disambiguate a class method."
		),
	});
	fields.push({
		fieldname: "section_break_options",
		fieldtype: "Section Break",
	});
	// v0.6.0 Round 6: default reads from Optimus Settings via the
	// candidates endpoint, so admins can flip the dialog default
	// without code changes.
	var auto_expand_default = data && data.default_auto_expand !== false ? 1 : 0;
	fields.push({
		fieldname: "auto_expand",
		fieldtype: "Check",
		label: __("Auto-expand hot chain (recommended)"),
		default: auto_expand_default,
		description: __(
			"For each curated pick, walks phase-1's call tree downward " +
			"following the hottest user-code child until it hits an ORM " +
			"call or framework wrapper. The run instruments the entire " +
			"chain so you see exactly which descendant line is the time " +
			"sink no need to re-pick and re-record level by level."
		),
	});

	var d = new frappe.ui.Dialog({
		title: __("Phase 2: Pick Functions to Line-Profile"),
		size: "large",
		fields: fields,
		primary_action_label: __("Run Line-Profile Pass"),
		primary_action: function (values) {
			var picks = [];
			// Phase K v0.7 GA: collect ticked checkboxes from the
			// custom HTML trees (curated + framework). The legacy
			// MultiCheck arrays (values.curated / values.framework_picks)
			// no longer exist - the dialog now uses an HTML field
			// per section and selected state lives on the DOM.
			d.$wrapper.find(".fp-tree input.fp-pick:checked").each(function () {
				var path = $(this).data("pick");
				if (path) picks.push({ dotted_path: String(path), source: "curated" });
			});
			(values.freeform || "")
				.split("\n")
				.map(function (line) {
					return line.trim();
				})
				.filter(function (line) {
					return line.length > 0;
				})
				.forEach(function (path) {
					picks.push({ dotted_path: path, source: "freeform" });
				});

			if (!picks.length) {
				frappe.msgprint(__("Pick at least one function to line-profile."));
				return;
			}

			var auto_expand = values.auto_expand !== 0;
			d.hide();
			start_phase2(frm, picks, auto_expand);
		},
	});

	d.show();
}

function start_phase2(frm, picks, auto_expand) {
	frappe.call({
		method: "optimus.api.start_line_profile_pass",
		args: {
			session_uuid: frm.doc.session_uuid,
			picks: JSON.stringify(picks),
			auto_expand: auto_expand ? 1 : 0,
		},
		callback: function (r) {
			if (!r || !r.message) return;
			var run_uuid = r.message.run_uuid;
			var resolved = r.message.resolved_picks || [];
			var instrumented = resolved.filter(function (p) { return p.eligible; }).length;
			var expansions = r.message.expansions || [];

			var msg = __(
				"Phase 2 recording started instrumenting " +
				instrumented +
				" function" + (instrumented === 1 ? "" : "s") +
				". Reproduce your flow now, then click Stop on the floating widget."
			);
			if (expansions.length) {
				// Show the first expansion inline so the dev sees what was
				// added; remaining expansions appear in the form's run row.
				var first = expansions[0];
				msg += __(
					" Auto-expanded " + first.original.split(".").pop() +
					" → " + first.chain.length + " functions" +
					(expansions.length > 1 ? " (+ " + (expansions.length - 1) + " more)" : "")
				);
			}
			frappe.show_alert({ message: msg, indicator: "blue" });
			frm.dashboard.add_indicator(
				__("Phase 2 recording run " + run_uuid.slice(0, 8) + "..."),
				"blue"
			);
		},
		error: function (xhr) {
			// Frappe surfaces validation errors through frappe.throw they
			// already render as a modal; we just re-enable the button.
		},
	});
}

// Listen for phase-2 realtime events on this session and refresh the form
// so the Phase 2 Runs child table picks up status transitions without the
// user having to reload manually.
function subscribe_phase2_events(frm) {
	if (frm.is_new()) return;
	if (frm._phase2_subscribed) return;
	frm._phase2_subscribed = true;

	["phase_2_run_recording", "phase_2_run_analyzing", "phase_2_run_ready", "phase_2_run_failed"].forEach(
		function (event) {
			frappe.realtime.on(event, function (payload) {
				if (!payload || payload.session_uuid !== frm.doc.session_uuid) return;
				if (event === "phase_2_run_ready") {
					frappe.show_alert({
						message: __("Phase 2 report ready"),
						indicator: "green",
					});
				} else if (event === "phase_2_run_failed") {
					frappe.show_alert({
						message: __("Phase 2 analyze failed: " + (payload.error || "unknown")),
						indicator: "red",
					});
				}
				_reload_clean_form(frm);
			});
		}
	);
}

function render_retry_button(frm) {
	if (frm.is_new()) return;
	if (frm.doc.status !== "Failed") return;

	frm.add_custom_button(__("Retry Analyze"), () => {
		frappe.confirm(
			__("Re-run the analyze pipeline for this session?"),
			() => {
				frappe.call({
					method: "optimus.api.retry_analyze",
					args: { session_uuid: frm.doc.session_uuid },
					callback: (r) => {
						const data = r.message || {};
						if (data.retried) {
							frappe.show_alert({
								message: __("Analyze retry enqueued"),
								indicator: "orange",
							});
							setTimeout(() => _reload_clean_form(frm), 2000);
						} else {
							frappe.show_alert({
								message: data.reason || __("Retry skipped"),
								indicator: "gray",
							});
						}
					},
				});
			},
		);
	});
}

// v0.5.3: Regenerate Reports button. Re-renders the safe + raw HTML
// from the stored session data without re-running the analyzer. Shown
// on Ready / Failed sessions. Typical use: the report template was
// upgraded (e.g. noise filters or exec summary added) and the admin
// wants existing sessions to reflect the new layout or the original
// render crashed and a fix was deployed.
function render_regenerate_report_button(frm) {
	if (frm.is_new()) return;
	// Only makes sense once the session has content to render.
	if (!["Ready", "Failed"].includes(frm.doc.status)) return;

	frm.add_custom_button(__("Regenerate Reports"), () => {
		frappe.confirm(
			__(
				"Re-render the HTML report from stored session data. Saved AI suggestions are retained. Use Refresh AI suggestions to request new answers."
			),
			() => {
				frappe.call({
					method: "optimus.api.regenerate_reports",
					args: { session_uuid: frm.doc.session_uuid },
					freeze: true,
					freeze_message: __("Regenerating the report…"),
					callback: (r) => {
						const data = (r && r.message) || {};
						if (data.regenerated) {
							const rec = data.recordings_available;
							const total = data.actions_total;
							let msg = __("Reports regenerated.");
							if (total && rec < total) {
								msg += " "
									+ __(
										"Only {0} of {1} recordings were "
										+ "available (others expired from "
										+ "Redis); per-query drill-down "
										+ "may be partial.",
										[rec, total],
									);
							}
							frappe.show_alert({
								message: msg,
								indicator: "green",
							});
							setTimeout(() => _reload_clean_form(frm), 1500);
						} else {
							frappe.show_alert({
								message: __("Regeneration skipped"),
								indicator: "gray",
							});
						}
					},
				});
			},
		);
	});
}

// Parent-level, NON-status hint that an additive phase-2 line-profile drill-down
// is still computing. The session's own `status` intentionally stays "Ready" (the
// phase-1 report is rendered and available and a session can have many phase-2
// passes) this just resolves the "parent Ready but a Phase 2 run says Analyzing"
// confusion without flapping the real status. Driven by the already-loaded child
// rows, so no extra round-trip; cleared automatically on the next refresh once the
// run finishes (the phase_2_run_ready realtime event reloads the form).
function render_phase2_progress(frm) {
	if (frm.is_new()) return;
	const in_flight = (frm.doc.phase_2_runs || []).filter(
		(r) => r.status === "Recording" || r.status === "Analyzing"
	);
	if (!in_flight.length) return;
	frm.dashboard.add_indicator(
		__("Phase 2 analyzing… ({0} run{1} in progress)", [
			in_flight.length,
			in_flight.length === 1 ? "" : "s",
		]),
		"orange"
	);
}

function render_status_indicator(frm) {
	if (frm.is_new()) return;
	const status = frm.doc.status || "Recording";
	const colors = {
		Recording: "green",
		Stopping: "orange",
		"Capturing Background Jobs": "orange",
		Analyzing: "orange",
		Ready: "blue",
		Failed: "red",
	};
	frm.page.set_indicator(status, colors[status] || "gray");
}

function _fmt_drain_window(secs) {
	if (secs == null) return null;
	if (secs <= 0) return __("almost done");
	if (secs >= 60) return __("~{0} min", [Math.ceil(secs / 60)]);
	return __("~{0}s", [secs]);
}

// " · up to ~Xm" suffix for the drain banner (or "" if the window is unknown).
function _drain_suffix(d) {
	const secs = d.remaining_seconds != null ? d.remaining_seconds : d.window_seconds;
	const win = _fmt_drain_window(secs);
	return win ? __(" · up to {0}", [win]) : "";
}

// The background-jobs drain banner. Same single-element mechanism as the analyze
// banner (see _single_banner); it keeps its own orange inline styling rather than
// a .form-message theme class. Pass html=null to remove it.
function _drain_banner(frm, html) {
	_single_banner(frm, "optimus-drain-banner", "", html, ($b) =>
		$b.css({
			padding: "10px 14px",
			margin: "8px",
			background: "#fff7ed",
			border: "1px solid #fed7aa",
			"border-radius": "6px",
			color: "#9a3412",
			"font-size": "0.9rem",
		})
	);
}

// While the session drains the flow's background jobs after Stop, poll the
// pending count + remaining window and show a live "Capturing background
// jobs… N left · up to ~Xm" intro so it doesn't look stuck at "Stopping".
// Reloads the form once the status moves on (analyze.run takes over → Analyzing).
function render_drain_progress(frm) {
	if (frm._optimus_drain_timer) {
		clearInterval(frm._optimus_drain_timer);
		frm._optimus_drain_timer = null;
	}
	if (frm.is_new() || frm.doc.status !== "Capturing Background Jobs") {
		// Clear any stale banner left over from a soft refresh after the status
		// already moved on.
		_drain_banner(frm, null);
		return;
	}

	const stop = () => {
		clearInterval(frm._optimus_drain_timer);
		frm._optimus_drain_timer = null;
	};
	const tick = () => {
		// Stop polling if the user navigated away from this form (no clean
		// per-form unload hook in Frappe cur_frm is the active form).
		if (window.cur_frm !== frm) {
			stop();
			return;
		}
		frappe.call({
			method: "optimus.api.drain_progress",
			args: { session_uuid: frm.doc.session_uuid },
			callback: (r) => {
				const d = (r && r.message) || {};
				if (d.status && d.status !== "Capturing Background Jobs") {
					stop();
					_drain_banner(frm, null);
					frm.reload_doc();
					return;
				}
				const n = d.pending != null ? d.pending : 0;
				// One self-managed banner updated in place set_intro /
				// set_headline both APPEND a dismissible .form-message in this
				// Frappe version, so polling them stacked a new bar each tick.
				_drain_banner(
					frm,
					__("⏳ Capturing background jobs… {0} left{1}", [n, _drain_suffix(d)])
				);
			},
			error: () => {
				// Persistent 403/500 → stop polling so we don't hammer the
				// server forever; a form refresh re-establishes the poll.
				stop();
			},
		});
	};
	tick();
	frm._optimus_drain_timer = setInterval(tick, 4000);
}

function render_download_buttons(frm) {
	if (frm.is_new()) return;
	if (frm.doc.status !== "Ready") return;

	// v0.6.0 Round 7: safe-mode reporting removed. Single admin-scoped
	// report the raw HTML plus a lazy-generated PDF. Server-side
	// permission gating still applies (Optimus User role + per-File
	// permission hook).
	if (frm.doc.raw_report_file) {
		frm.add_custom_button(
			__("Download Report"),
			() => {
				frappe.confirm(
					__(
						"The report will be saved to your downloads folder and contains literal SQL values, request headers and stack traces. Do not share it externally without redacting it yourself. Continue?",
					),
					() => {
						// Programmatic <a download="..."> click forces
						// the browser to save the file rather than navigate
						// to it. window.open serves the HTML inline because
						// the file's Content-Type is text/html that's the
						// "Open Report" flow below; this button needs a
						// real save-to-disk.
						const link = document.createElement("a");
						link.href = frm.doc.raw_report_file;
						link.download = "";
						document.body.appendChild(link);
						link.click();
						document.body.removeChild(link);
					},
				);
			},
			__("Reports"),
		);

		frm.add_custom_button(
			__("Open Report"),
			() => {
				frappe.confirm(
					__(
						"The report opens in a new tab and contains literal SQL values, request headers and stack traces. Do not share it externally without redacting it yourself. Continue?",
					),
					() => {
						// Frappe serves /private/files/*.html with
						// Content-Disposition: attachment, which triggers a
						// download dialog instead of rendering inline. Fetch
						// the content, wrap it in a blob URL with the right
						// MIME type and window.open that blob URLs are
						// not governed by the original response's
						// Content-Disposition, so the browser renders the
						// HTML inline. Works because the report HTML is
						// self-contained (no external asset references); see
						// product-thesis "safe report" guarantee.
						const showError = (msg) =>
							frappe.show_alert({
								message: __(msg),
								indicator: "red",
							});
						fetch(frm.doc.raw_report_file, {
							credentials: "same-origin",
						})
							.then((r) => {
								if (!r.ok) {
									throw new Error("HTTP " + r.status);
								}
								return r.text();
							})
							.then((html) => {
								const blob = new Blob([html], {
									type: "text/html",
								});
								const url = URL.createObjectURL(blob);
								const win = window.open(url, "_blank");
								if (!win) {
									showError(
										"Pop-up blocked; allow pop-ups for this site to open the report inline.",
									);
									URL.revokeObjectURL(url);
									return;
								}
								// Revoke the blob URL once the new tab has had
								// a chance to load it. The tab keeps a DOM
								// reference to the rendered content
								// independent of the URL.
								setTimeout(
									() => URL.revokeObjectURL(url),
									60000,
								);
							})
							.catch(() => {
								showError(
									"Could not load the report; try Download Report instead.",
								);
							});
					},
				);
			},
			__("Reports"),
		);
	}
}

function render_findings_summary(frm) {
	if (frm.is_new()) return;
	if (!frm.doc.findings || frm.doc.findings.length === 0) return;

	// Add a small color-coded badge dashboard above the form fields.
	const high = frm.doc.findings.filter((f) => f.severity === "High").length;
	const medium = frm.doc.findings.filter((f) => f.severity === "Medium").length;
	const low = frm.doc.findings.filter((f) => f.severity === "Low").length;

	const badges = [];
	if (high) badges.push(`<span class="indicator-pill red">${high} High</span>`);
	if (medium) badges.push(`<span class="indicator-pill orange">${medium} Medium</span>`);
	if (low) badges.push(`<span class="indicator-pill blue">${low} Low</span>`);

	if (badges.length === 0) return;

	frm.dashboard.add_section(
		`<div style="padding: 8px 0; font-size: 0.9rem;">
			<strong>${__("Findings")}:</strong> ${badges.join(" ")}
		</div>`,
		__("Performance issues"),
	);
}
