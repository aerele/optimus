// Optimus Floating start/stop widget
//
// Injected into every Desk page via app_include_js in hooks.py.
// Renders a small floating button bottom-right that lets a user with the
// Optimus User (or System Manager) role start and stop a profiling session.
//
// State machine:
//
//   inactive  →  click → start dialog → start API → recording
//   recording →  click → stop API → stopping → analyzing → ready
//   ready     →  click → navigate to the Optimus Session detail view
//
// State is driven by realtime events, plus optimus.api.pill_state on load, on
// tab focus, on socket reconnect and a backed-off fallback poll while a session
// is stopping or analyzing.

(function () {
	"use strict";

	// v0.5.1 diagnostic: hardcoded build ID bumped on every widget fix.
	// This lets us confirm (from browser devtools) that the user is
	// actually running the latest JS, vs a stale cached copy from an
	// earlier page load. If the build ID in the console / in the
	// widget's title attribute doesn't match what we think is current,
	// the user is on cached JS and needs a hard refresh + bench restart.
	const WIDGET_BUILD_ID = "2026-10-18-role-gate-mount";

	// Developer-only diagnostic. Defaults to silent in production so the
	// browser console isn't spammed on every Desk page. Set
	// ``window.OPTIMUS_DEBUG = true`` before page load (or from devtools)
	// to surface the per-step trace. Replaces the prior unconditional
	// ``console.log`` / ``console.warn`` / ``console.error`` calls.
	function _diag() {
		if (window.OPTIMUS_DEBUG !== true) return;
		if (typeof console === "undefined" || !console.log) return;
		try {
			console.log.apply(
				console,
				["[optimus]"].concat([].slice.call(arguments))
			);
		} catch (e) { /* swallow */ }
	}

	_diag(
		"floating_widget.js LOADED",
		"build=" + WIDGET_BUILD_ID,
		"at", new Date().toISOString()
	);

	// Only run inside Desk (not on web pages or guest sessions).
	if (typeof frappe === "undefined" || typeof frappe.session === "undefined") {
		_diag("[optimus] no frappe global, script exiting");
		return;
	}
	if (frappe.session.user === "Guest") {
		_diag("[optimus] user is Guest, script exiting");
		return;
	}
	_diag("[optimus] proceeding for user:", frappe.session.user);

	// There is no fixed-interval poll; the fallback poll below only runs while
	// analyzing. POLL_INTERVAL_MS is kept as a legacy constant.
	const POLL_INTERVAL_MS = 0;

	let widget = null;
	let elapsedHandle = null;
	// Asks the server every RECORDING_CHECK_MS whether the recording ended by itself.
	let recordingTimer = null;
	// The start dialog's label, kept in this tab's memory so a failed start or Escape does not lose it.
	let rememberedLabel = null;
	let startInFlight = false;
	// State logic is in optimus_pill_state.js.
	const Pill = window.OptimusPillState;
	if (!Pill) {
		_diag("[optimus] optimus_pill_state.js missing, widget will not mount");
		return;
	}
	let currentState = Pill.initialState();
	let fallbackTimer = null;
	let pollStartedAt = 0;
	let pollAttempt = 0;
	// True from entering a waiting state until it ends; replies must not restart the clock.
	let pollActive = false;
	// Bumped by user actions so an older in-flight resync reply is dropped.
	let resyncEpoch = 0;

	function init() {
		// v0.5.2 round 4: hide widget entirely when Optimus Settings ▸
		// Profiler Enabled is off. A disabled widget is a dead button
		// (before_request / before_job short-circuit), so showing it
		// misleads the user into thinking they can still record.
		// `frappe.boot.optimus_enabled` is populated by boot_session
		// and defaults to True on error matching the settings
		// module's fail-open posture.
		const bootEnabled = (
			frappe.boot && frappe.boot.optimus_enabled
		);
		if (bootEnabled === false) {
			_diag(
				"[optimus] profiler is disabled in settings "
				+ "widget will not mount"
			);
			return;
		}

		// Skip non-profilers: api.status would 403 and pop an error modal.
		// boot.py sets the flag with the API's role check; missing means an old cached boot.
		if (frappe.boot && frappe.boot.optimus_can_profile === false) {
			_diag("[optimus] user lacks a profiler role, widget will not mount");
			return;
		}

		mountWidget();
		// v0.5.1: one-shot rehydrate on page load. Covers the case
		// where the user reloaded / navigated back to the Desk while
		// a session was already active. After this initial call,
		// state is driven entirely by realtime events + visibility
		// changes. No interval polling.
		try { refreshStatus(); } catch (e) { /* noop */ }
		try { subscribeRealtime(); } catch (e) { /* noop */ }
		try { subscribeVisibility(); } catch (e) { /* noop */ }
		try { maybeShowOnboardingToast(); } catch (e) { /* noop */ }
	}

	function maybeShowOnboardingToast() {
		// v0.4.0: one-time dismissible toast on first Desk visit after install.
		// Server decides whether to show (via api.check_onboarding_seen),
		// which suppresses for experienced users automatically.
		frappe.call({
			method: "optimus.api.check_onboarding_seen",
			callback: function (r) {
				var data = (r && r.message) || {};
				if (data.seen) return;
				renderOnboardingToast();
			},
		});
	}

	function renderOnboardingToast() {
		// Avoid double-rendering if init runs twice
		if (document.getElementById("frappe-profiler-onboarding-toast")) return;
		var toast = document.createElement("div");
		toast.id = "frappe-profiler-onboarding-toast";
		toast.className = "optimus-onboarding-toast";
		toast.setAttribute("role", "status");
		toast.innerHTML = [
			'<div class="optimus-onboarding-title">' + __("Optimus is installed") + "</div>",
			'<div class="optimus-onboarding-body">',
			__("Click the {0} pill in the bottom-right corner to record your first slow flow.", [
				"<strong>" + __("Profiler") + "</strong>",
			]),
			"</div>",
			'<button type="button" id="frappe-profiler-onboarding-dismiss" ',
			'class="optimus-onboarding-dismiss">' + __("Got it") + "</button>",
		].join("");
		document.body.appendChild(toast);

		var dismissed = false;
		function dismiss() {
			if (dismissed) return;
			dismissed = true;
			document.removeEventListener("keydown", onEscape);
			try {
				toast.remove();
			} catch (e) { /* noop */ }
			try {
				frappe.call({ method: "optimus.api.mark_onboarding_seen" });
			} catch (e) { /* noop */ }
		}
		function onEscape(e) {
			if (e.key === "Escape") dismiss();
		}
		document.addEventListener("keydown", onEscape);
		var btn = document.getElementById("frappe-profiler-onboarding-dismiss");
		if (btn) btn.addEventListener("click", dismiss);
		// Auto-dismiss after 15 seconds
		setTimeout(dismiss, 15000);
	}

	function subscribeVisibility() {
		// v0.5.1: no more polling but we still want to refresh state
		// ONCE when the tab becomes visible again, to catch TTL-based
		// auto-stop that happens silently (Redis active-pointer expired
		// while the tab was hidden) and any realtime events the Socket
		// .IO client might have missed during tab sleep. Browser
		// Background Fetch / Socket.IO idle throttling can occasionally
		// drop events; a one-shot rehydrate on return-to-tab covers it.
		document.addEventListener("visibilitychange", () => {
			if (!document.hidden) {
				refreshStatus();
			}
		});
	}

	function mountWidget() {
		_diag("[optimus] mountWidget() called");
		// Idempotent: don't double-mount on accidental re-init.
		const existing = document.getElementById("frappe-profiler-widget");
		if (existing) {
			_diag("[optimus] widget already in DOM, skipping mount");
			widget = existing;
			return;
		}
		widget = document.createElement("button");
		widget.type = "button";
		widget.id = "frappe-profiler-widget";
		widget.className = "fp-state-inactive";
		widget.title = pillTitle("inactive", false);
		// Lets a dev-tools inspector confirm which JS is running; not shown to users.
		widget.setAttribute("data-build-id", WIDGET_BUILD_ID);
		// The per-second elapsed timer stays out of the accessible name.
		widget.innerHTML = `
			<span class="fp-dot" aria-hidden="true"></span>
			<span class="fp-label">${__("Profiler")}</span>
			<span class="fp-elapsed" aria-hidden="true"></span>
		`;
		widget.addEventListener("click", onClick);
		// Inline-style only the layout primitives that need to override
		// any Desk-side CSS regression (positioning + force-visible).
		// Visual styling (size, padding, border, colour, font) lives in
		// floating_widget.css so the pill stays mobile-overlay-sized.
		widget.style.cssText = [
			"position: fixed",
			"right: 20px",
			"bottom: 20px",
			"display: block !important",
			"visibility: visible !important",
			"opacity: 1 !important",
			"pointer-events: auto",
		].join("; ");
		document.body.appendChild(widget);
		_diag("[optimus] widget appended to body, id=#frappe-profiler-widget");
		_diag("[optimus] widget element:", widget);
		_diag("[optimus] body has child count:", document.body.children.length);
	}

	function pillTitle(display, phase2) {
		if (display === "recording") {
			return phase2 ? __("Stop line profiling") : __("Stop recording and analyze");
		}
		if (display === "stopping" || display === "analyzing") return __("Analysis in progress");
		if (display === "ready") return __("Open the session");
		return __("Start a profiling session");
	}

	function setDisplay(display, label, elapsed, tone) {
		if (!widget) return;
		widget.classList.remove(
			"fp-state-inactive",
			"fp-state-recording",
			"fp-state-stopping",
			"fp-state-analyzing",
			"fp-state-ready",
			"fp-tone-failed",
			"fp-tone-stale",
		);
		widget.classList.add(`fp-state-${display}`);
		if (tone) widget.classList.add(`fp-tone-${tone}`);
		widget.querySelector(".fp-label").textContent = label;
		widget.querySelector(".fp-elapsed").textContent = elapsed || "";
		widget.title = pillTitle(display, currentState.phase2);
		widget.setAttribute("aria-label", label);
		if (Pill.isTransient(display)) {
			widget.setAttribute("aria-disabled", "true");
		} else {
			widget.removeAttribute("aria-disabled");
		}
		if (display !== "recording") stopElapsedTimer();
		syncRecordingCheck(display);
		syncPoll(display);
	}

	// Apply a {state, ui, alert} result from the pill reducers.
	function applyResult(res) {
		if (!res) return;
		currentState = res.state;
		if (res.ui) setDisplay(res.ui.display, res.ui.label, res.ui.elapsed, res.ui.tone);
		if (res.alert) {
			frappe.show_alert({ message: __(res.alert.message, res.alert.args), indicator: res.alert.indicator });
		}
		if (widget && !currentState.session_uuid) {
			widget.removeAttribute("data-session-uuid");
		}
		if (res.effect && res.effect.stop) {
			confirmAndStop({ sessionUuid: res.effect.stop, auto: true, capHit: res.effect.capHit });
		}
	}

	// While recording, ask the server once a minute whether the session ended by itself.
	function syncRecordingCheck(display) {
		if (display !== "recording") {
			stopRecordingCheck();
		} else if (!recordingTimer) {
			recordingTimer = setTimeout(recordingTick, Pill.RECORDING_CHECK_MS);
		}
	}

	function stopRecordingCheck() {
		if (recordingTimer) clearTimeout(recordingTimer);
		recordingTimer = null;
	}

	function recordingTick() {
		recordingTimer = null;
		const action = Pill.recordingCheckAction(currentState.display, currentState.phase2, document.hidden);
		if (action === "stop") return;
		if (action === "wait") {
			// No request while hidden; the visibility handler resyncs.
			recordingTimer = setTimeout(recordingTick, Pill.RECORDING_CHECK_MS);
			return;
		}
		refreshStatus(() => {
			if (currentState.display === "recording" && !recordingTimer) {
				recordingTimer = setTimeout(recordingTick, Pill.RECORDING_CHECK_MS);
			}
		});
	}

	// Fallback poll for missed realtime events, with backoff and a hard stop.
	// The clock starts when a waiting state begins, not on every reply.
	function syncPoll(display) {
		if (!Pill.isTransient(display)) {
			stopPoll();
		} else if (!pollActive) {
			pollActive = true;
			pollStartedAt = Date.now();
			pollAttempt = 0;
			schedulePoll();
		}
	}

	function schedulePoll() {
		fallbackTimer = setTimeout(pollTick, Pill.nextDelay(pollAttempt));
	}

	function stopPoll() {
		if (fallbackTimer) clearTimeout(fallbackTimer);
		fallbackTimer = null;
		pollActive = false;
	}

	function pollTick() {
		fallbackTimer = null;
		const action = Pill.pollAction(currentState.display, Date.now() - pollStartedAt, document.hidden);
		if (action === "stop") {
			stopPoll();
			return;
		}
		if (action === "wait") {
			// No request while hidden; the visibility handler resyncs.
			schedulePoll();
			return;
		}
		if (action === "expire") {
			// One last server check before giving up.
			refreshStatus(() => applyResult(Pill.reduceHardStop(currentState)));
			return;
		}
		pollAttempt += 1;
		refreshStatus(() => {
			if (Pill.isTransient(currentState.display) && !fallbackTimer) schedulePoll();
		});
	}

	function refreshStatus(done) {
		// Resync the pill from api.pill_state.
		const epoch = resyncEpoch;
		frappe.call({
			method: "optimus.api.pill_state",
			// Background resyncs must not pop an error dialog.
			silent: true,
			args: {
				session_uuid: currentState.session_uuid || undefined,
				run_uuid: currentState.run_uuid || undefined,
			},
			callback: (r) => {
				// A click since the request was sent makes the reply obsolete.
				if (epoch !== resyncEpoch) {
					if (done) done(false);
					return;
				}
				const prev = currentState.display;
				applyResult(Pill.reduceServer(currentState, (r && r.message) || {}, Date.now()));
				if (currentState.display === "recording" && prev !== "recording") {
					if (currentState.phase2) {
						setDisplay("recording", __("Line profiling recording…"), __("click pill or form to stop"));
					} else {
						startElapsedTimer();
						setDisplay("recording", __("Recording"), computeElapsed());
					}
					if (widget) widget.setAttribute("data-session-uuid", currentState.session_uuid || "");
				}
				if (done) done(true);
			},
			error: (xhr) => {
				_diag("[optimus] pill_state poll failed:", xhr && xhr.status);
				if (done) done(false);
			},
		});
	}

	function onClick() {
		if (currentState.display === "inactive") {
			openStartDialog();
		} else if (currentState.display === "recording") {
			// v0.6.0: phase-2 recording uses a different stop API and a
			// different active-flag in Redis. Route the click accordingly
			// confirmAndStop calls api.stop which only knows about
			// phase-1's flag and would no-op silently for line profiling.
			if (currentState.phase2 && currentState.run_uuid) {
				stopPhase2();
			} else {
				confirmAndStop();
			}
		} else if (currentState.display === "ready") {
			// Navigate to the session detail view
			if (currentState.docname) {
				frappe.set_route("Form", "Optimus Session", currentState.docname);
			}
			// Drop the finished session so a resync cannot revive the pill.
			resyncEpoch += 1;
			currentState = Pill.resetState(currentState);
			if (widget) widget.removeAttribute("data-session-uuid");
			setDisplay("inactive", __("Profiler"), "");
		}
		// stopping/analyzing: clicks are no-op until the state resolves
	}

	function stopPhase2() {
		const run_uuid = currentState.run_uuid;
		if (!run_uuid) return;
		resyncEpoch += 1;
		// Optimistic UI: the realtime phase_2_run_analyzing event will
		// arrive within ~1s and confirm the transition.
		currentState.display = "stopping";
		setDisplay("stopping", __("Stopping line profiling…"), "");
		frappe.call({
			method: "optimus.api.stop_line_profile_pass",
			args: { run_uuid: run_uuid },
			callback: () => {
				frappe.show_alert({
					message: __(
						"Line profiling stopped analyzing now. Open the session to see the line-level report when it's ready."
					),
					indicator: "blue",
				});
			},
			error: () => {
				// The stop may have landed server-side; resync after reverting.
				currentState.display = "recording";
				setDisplay("recording", __("Line profiling recording…"), __("click form to stop"));
				refreshStatus();
			},
		});
	}

	/**
	 * Derive a contextual default for the Session label field from
	 * the current Frappe Desk route. Used by openStartDialog() so the
	 * user can click Start without typing the most common "what am
	 * I profiling" answer is already on screen.
	 *
	 * Route shapes handled (Frappe v16):
	 *   Form      ["Form", "Sales Invoice", "SINV-2026-001"]
	 *   List      ["List", "Sales Invoice"]
	 *   Report    ["List", "Sales Invoice", "Report"]
	 *   Kanban    ["List", "Sales Invoice", "Kanban", "<name>"]
	 *   Dashboard ["List", "Sales Invoice", "Dashboard"]
	 *   Tree      ["Tree", "Account"]
	 *   Query Rpt ["query-report", "<report name>"]
	 *   anything else → fallback "Profiling session"
	 */
	function getDefaultSessionLabel() {
		// Compose the route-derived label first, then suffix the
		// current time (HH:MM) so two sessions captured on the same
		// route are easy to tell apart at a glance in the list view.
		function _routeLabel() {
			try {
				const route = (typeof frappe !== "undefined" && frappe.get_route)
					? frappe.get_route()
					: [];
				if (!route || !route.length) {
					return "Profiling session";
				}
				const head = route[0];
				const doctype = route[1];
				const sub = route[2];
				const leaf = route[3];
				switch (head) {
					case "Form":
						return doctype && leaf
							? `${doctype} ${leaf}`
							: doctype || "Form";
					case "List":
						if (!doctype) return "List";
						if (sub === "Kanban") {
							return leaf
								? `${doctype} kanban ${leaf}`
								: `${doctype} kanban`;
						}
						if (sub === "Report") return `${doctype} report`;
						if (sub === "Dashboard") return `${doctype} dashboard`;
						if (sub === "Calendar") return `${doctype} calendar`;
						if (sub === "Gantt") return `${doctype} gantt`;
						return `${doctype} list`;
					case "Tree":
						return doctype ? `${doctype} tree` : "Tree";
					case "query-report":
						return doctype ? `Report ${doctype}` : "Report";
					case "dashboard-view":
						return doctype ? `Dashboard ${doctype}` : "Dashboard";
					case "modules":
					case "desk":
					case "app":
						return "Profiling session";
					default:
						if (head && doctype) return `${head} ${doctype}`;
						if (head) return String(head);
						return "Profiling session";
				}
			} catch (e) {
				return "Profiling session";
			}
		}

		function _datetimeStamp() {
			try {
				const now = new Date();
				const y = String(now.getFullYear());
				const mo = String(now.getMonth() + 1).padStart(2, "0");
				const d = String(now.getDate()).padStart(2, "0");
				const hh = String(now.getHours()).padStart(2, "0");
				const mm = String(now.getMinutes()).padStart(2, "0");
				return `${y}-${mo}-${d} ${hh}:${mm}`;
			} catch (e) {
				return "";
			}
		}

		const route = _routeLabel();
		const t = _datetimeStamp();
		return t ? `${route} · ${t}` : route;
	}

	function routeKey() {
		try {
			return JSON.stringify(frappe.get_route ? frappe.get_route() : []);
		} catch (e) {
			return "";
		}
	}

	function remember(label) {
		rememberedLabel = label ? { text: label, route: routeKey() } : null;
	}

	// A typed label survives a failed start or an Escape, but only on the page it was typed on.
	function labelForNewDialog() {
		if (rememberedLabel && rememberedLabel.route === routeKey()) return rememberedLabel.text;
		return getDefaultSessionLabel();
	}

	function startSession(d, values, onStarted) {
		if (startInFlight) return;
		startInFlight = true;
		d.disable_primary_action();
		return frappe.call({
			method: "optimus.api.start",
			args: {
				label: values.label || "",
				// v0.7 GA: tree capture is always on - users
				// who turn it off then file "no candidates"
				// bugs. The api.start kwarg still defaults
				// True so CLI / external callers can opt
				// out if they really need SQL-only capture.
				capture_python_tree: 1,
			},
			callback: (r) => {
				startInFlight = false;
				rememberedLabel = null;
				onStarted();
				d.hide();
				showStarted((r && r.message) || {});
			},
			error: (r) => {
				// frappe.call's success callback is NOT invoked on server errors
				// (4xx/5xx). The dialog stays open with the label so the user can retry.
				// A server message already opens its own dialog, so add a toast only without one.
				startInFlight = false;
				d.enable_primary_action();
				if (r && r._server_messages) return;
				frappe.show_alert({
					message: __("Profiler start failed. Check your role and try again."),
					indicator: "red",
				});
			},
		});
	}

	function showStarted(data) {
		if (!data.session_uuid) {
			// Server returned 200 but no session_uuid in the
			// response unexpected. Surface something so
			// the user knows the click didn't land.
			frappe.show_alert({
				message: __("Profiler start returned no session check Error Log"),
				indicator: "orange",
			});
			return;
		}
		resyncEpoch += 1;
		currentState.active = true;
		currentState.session_uuid = data.session_uuid;
		currentState.docname = data.docname;
		currentState.label = data.title;
		currentState.started_at = data.started_at;
		currentState.display = "recording";
		startElapsedTimer();
		setDisplay("recording", __("Recording"), "0:00");
		// v0.5.0: expose session UUID for optimus_frontend.js.
		if (widget) {
			widget.setAttribute("data-session-uuid", data.session_uuid || "");
		}
		frappe.show_alert({
			message: __("Profiler started: {0}", [data.title]),
			indicator: "green",
		});
	}

	function openStartDialog() {
		let started = false;
		const initialLabel = labelForNewDialog();
		const d = new frappe.ui.Dialog({
			title: __("Start profiling session"),
			fields: [
				{
					fieldname: "label",
					fieldtype: "Data",
					label: __("Session label"),
					reqd: 1,
					length: 140,
					default: initialLabel,
					description: __(
						"Give this session a name you'll recognize later e.g. 'Sales Invoice flow with 50 items'. (up to 140 characters)"
					),
				},
				{
					fieldname: "warning_html",
					fieldtype: "HTML",
					options: '<div class="optimus-start-note"><strong>' + __("Note:") + "</strong> "
						+ __("Recording adds ~1.5–2× wall-clock overhead per request while it's running. Only your traffic will be captured other users on this site are not affected. The session auto-stops after 10 minutes without activity, and stops recording once it reaches the per-session request limit. Captured data stays in Redis for up to 6 hours, until analysis completes.")
						+ "</div>",
				},
			],
			primary_action_label: __("Start"),
			primary_action: (values) => startSession(d, values, () => { started = true; }),
		});
		d.onhide = () => {
			const typed = d.get_value("label");
			if (!started && typed !== initialLabel) remember(typed);
			const active = document.activeElement;
			if (widget && (!active || active === document.body)) widget.focus();
		};
		d.show();
	}

	// opts.sessionUuid: stop that session (an automatic stop of an ended one); default is the session this tab shows.
	// opts.auto: no click behind it, so only an expired-session notice is shown.
	function confirmAndStop(opts) {
		// No confirmation modal keep it one-click. Just fire stop().
		// Update currentState.display BEFORE the API call so refreshStatus()'s
		// transient-state guard in the reducers kicks in; otherwise a resync
		// can race the stop API and flip the widget back to "Recording".
		//
		// v0.5.0: also flush any buffered frontend metrics before the stop
		// API fires, so analyze can join them to recordings. Best-effort
		// a failed flush never blocks stop.
		const options = opts || {};
		const sessionUuid = options.sessionUuid || currentState.session_uuid || undefined;
		_diag("[optimus] confirmAndStop: click received");
		resyncEpoch += 1;
		setDisplay("stopping", __("Stopping…"), "");
		currentState.display = "stopping";
		stopElapsedTimer();

		try {
			if (window.optimus_frontend && window.optimus_frontend.flush) {
				window.optimus_frontend.flush({ sync: false });
			}
		} catch (e) { /* noop frontend module missing or flush failed */ }

		frappe.call({
			method: "optimus.api.stop",
			// The session this tab shows, so an old tab never stops the one active in
			// another tab. Omitted only when the tab knows none.
			args: { session_uuid: sessionUuid },
			callback: (r) => {
				const data = (r && r.message) || {};
				_diag("[optimus] stop callback:", data);

				// The stop did not claim the session: it is already stopping or analyzing
				// (another tab won), or it is gone. Ask the server which.
				if (data.stopped === false) {
					resolveUnclaimedStop();
					return;
				}

				if (data.ran_inline) {
					// v0.5.0: scheduler was disabled and analyze ran
					// synchronously inside the stop request. The session
					// is already finalized (Ready or Failed) by the time
					// we get here branch on data.status so a failed
					// inline analyze doesn't show "Report ready" to the
					// user. In both branches we transition to "ready"
					// state so the user can click the pill and land on
					// the session form (which shows Ready/Failed clearly).
					currentState.display = "ready";
					currentState.docname = data.docname;
					if (data.status === "Failed") {
						setDisplay("ready", __("Analyze failed"), __("click to view"), "failed");
						frappe.show_alert({
							message: __("Profiler analyze failed click the pill to see details"),
							indicator: "red",
						});
					} else {
						const empty = !!data.empty;
						setDisplay("ready", empty ? __("No data") : __("Report ready"), __("click to view"));
						if (data.expired) {
							announceStopped(true, options);
						} else {
							frappe.show_alert({
								message: empty
									? __("Profiling finished, but no requests were recorded")
									: __("Profiler report ready"),
								indicator: empty ? "orange" : "blue",
							});
						}
					}
				} else if (currentState.display === "ready") {
					// The ready event arrived before the stop response.
					currentState.docname = data.docname || currentState.docname;
				} else {
					setDisplay("analyzing", __("Analyzing…"), "");
					currentState.display = "analyzing";
					currentState.docname = data.docname || currentState.docname;
					announceStopped(data.expired, options);
				}
			},
			error: () => recoverFromFailedStop(sessionUuid),
		});
	}

	// Only the stop that claimed an expired session tells the user why it ended.
	function announceStopped(expired, options) {
		if (expired) {
			const message = options.capHit
				? "Recording reached its request limit, so later requests were not captured. Analyzing what was captured."
				: "Recording stopped after 10 minutes without activity. Analyzing what was captured.";
			frappe.show_alert({ message: __(message), indicator: "orange" });
		} else if (!options.auto) {
			frappe.show_alert({
				message: __("Profiler stopped analyzing session…"),
				indicator: "orange",
			});
		}
	}

	// stopped:false is ambiguous, so resync from the server; only a confirmed "nothing there" resets the pill.
	function resolveUnclaimedStop() {
		refreshStatus((ok) => {
			if (!ok || currentState.display !== "stopping") return;
			currentState = Pill.resetState(currentState);
			if (widget) widget.removeAttribute("data-session-uuid");
			setDisplay("inactive", __("Profiler"), "");
			frappe.show_alert({
				message: __("Nothing to stop: this session had already ended."),
				indicator: "gray",
			});
		});
	}

	// The stop request failed, so the server may or may not have stopped the session: show Recording again
	// and let pill_state for that session say what is true.
	function recoverFromFailedStop(sessionUuid) {
		_diag("[optimus] stop failed for", sessionUuid);
		currentState.display = "recording";
		setDisplay("recording", __("Recording"), computeElapsed());
		startElapsedTimer();
		refreshStatus((ok) => {
			if (!ok) {
				frappe.show_alert({ message: __("Network error please retry"), indicator: "red" });
			} else if (currentState.display === "recording") {
				frappe.show_alert({ message: __("Failed to stop profiler try again"), indicator: "red" });
			} else if (currentState.display === "inactive") {
				frappe.show_alert({ message: __("Session already stopped"), indicator: "gray" });
			}
		});
	}

	// Completion popups: one claim per run (localStorage) across tabs, queued, shown in a visible tab when not typing.
	const popupQueue = [];
	const popupClaimed = new Set();
	const POPUP_TAB_ID = Math.random().toString(36).slice(2);
	let popupBusy = false;
	let popupTimer = null;
	let popupDeferrals = 0;
	const POPUP_MAX_DEFERRALS = 30;
	const TEXT_INPUT_TYPES = /^(text|search|email|url|tel|password|number)?$/i;

	// Write our id, then read it back: the last writer wins, so only one tab proceeds.
	function claimPopup(key, token) {
		const mine = token + "|" + POPUP_TAB_ID;
		try {
			const current = localStorage.getItem(key);
			if (current && current.split("|")[0] === token) return false;
			localStorage.setItem(key, mine);
			return localStorage.getItem(key) === mine;
		} catch (e) {
			if (popupClaimed.has(key + token)) return false;
			popupClaimed.add(key + token);
			return true;
		}
	}

	function releasePopup(key, token) {
		try {
			if (localStorage.getItem(key) === token + "|" + POPUP_TAB_ID) localStorage.removeItem(key);
		} catch (e) { /* storage unavailable */ }
		popupClaimed.delete(key + token);
	}

	function isTyping() {
		const el = document.activeElement;
		if (!el) return false;
		if (el.isContentEditable || el.tagName === "TEXTAREA") return true;
		return el.tagName === "INPUT" && TEXT_INPUT_TYPES.test(el.type || "");
	}

	function isOtherDialogOpen() {
		const open = frappe.ui && frappe.ui.open_dialogs;
		if (open && open.length) return true;
		return !!(document.querySelector && document.querySelector(".modal.show:not(.optimus-session-popup)"));
	}

	function openPopup(item) {
		const messages = {
			ready: __("Profiling is complete. Your Optimus Session is ready."),
			empty: __("Profiling finished, but no requests were recorded in this session."),
			failed: __("Analysis failed for this session. Open the session to review the error and retry."),
		};
		const titles = {
			ready: __("Optimus Session ready"),
			empty: __("Optimus Session has no data"),
			failed: __("Optimus analysis failed"),
		};
		const url = frappe.utils.get_form_link("Optimus Session", item.docname);
		const link = '<a class="optimus-session-link" href="' + frappe.utils.escape_html(url) + '">'
			+ frappe.utils.escape_html(item.title || item.docname) + "</a>";
		const d = new frappe.ui.Dialog({
			title: titles[item.kind],
			fields: [{
				fieldtype: "HTML",
				fieldname: "body",
				options: "<p>" + frappe.utils.escape_html(messages[item.kind]) + " " + link + "</p>",
			}],
		});
		if (d.$wrapper && d.$wrapper.addClass) d.$wrapper.addClass("optimus-session-popup");
		// A plain primary click navigates this tab to the session, so close the dialog behind it.
		// For a modified or non-primary click (ctrl / meta / shift / alt / middle) the browser may
		// open a new tab or window, so keep the popup. If Desk's router instead navigates this tab,
		// it closes the dialog itself (hide_open_dialog), so nothing is left dangling either way.
		if (d.$wrapper && d.$wrapper.on) {
			d.$wrapper.on("click", "a.optimus-session-link", (e) => {
				if (e && (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button)) return;
				d.hide();
			});
		}
		d.onhide = () => {
			popupBusy = false;
			flushPopups();
		};
		d.show();
	}

	function flushPopups() {
		clearTimeout(popupTimer);
		popupTimer = null;
		if (popupBusy || !popupQueue.length) return;
		// A hidden tab must not win the claim; it flushes again on visibilitychange.
		if (document.visibilityState !== "visible") return;
		// Don't steal focus or stack on another dialog; re-check, then open anyway after the cap.
		if ((isTyping() || isOtherDialogOpen()) && popupDeferrals < POPUP_MAX_DEFERRALS) {
			popupDeferrals += 1;
			popupTimer = setTimeout(flushPopups, 1000);
			return;
		}
		popupDeferrals = 0;
		const item = popupQueue.shift();
		// The user may have opened this session's own form while the popup waited; drop it, as
		// showSessionPopup does at enqueue time.
		if (isOnSessionForm(item.docname)) {
			flushPopups();
			return;
		}
		if (!claimPopup(item.key, item.token)) {
			flushPopups();
			return;
		}
		popupBusy = true;
		try {
			openPopup(item);
		} catch (e) {
			popupBusy = false;
			releasePopup(item.key, item.token);
			console.warn("[optimus] session popup failed", e);
			flushPopups();
		}
	}

	if (typeof document !== "undefined" && document.addEventListener) {
		document.addEventListener("visibilitychange", flushPopups);
	}

	function isOnSessionForm(docname) {
		const route = frappe.get_route ? frappe.get_route() : [];
		return !!route && route[0] === "Form" && route[1] === "Optimus Session" && route[2] === docname;
	}

	// Never throws: a popup problem must not break the widget's own state handling.
	function showSessionPopup(data, kind) {
		try {
			const docname = data && data.docname;
			if (!docname || !frappe.ui || !frappe.ui.Dialog) return;
			if (data.recording_user && data.recording_user !== frappe.session.user) return;
			if (isOnSessionForm(docname)) return;
			const key = "optimus_popup_shown:" + docname + ":" + kind;
			const token = String(data.event_id || data.session_uuid || "");
			popupQueue.push({ docname, kind, key, token, title: data.title || "" });
			flushPopups();
		} catch (e) {
			console.warn("[optimus] session popup failed", e);
		}
	}

	let realtimeSubscribed = false;
	let realtimeAttempts = 0;
	const REALTIME_RETRY_MS = 250;
	const REALTIME_MAX_ATTEMPTS = 120;

	function subscribeRealtime() {
		// Frappe creates the socket after this bootstrap runs, and
		// frappe.realtime.on is a silent no-op until it exists: retry
		// until it does, then register once.
		if (realtimeSubscribed) return;
		if (!frappe.realtime || !frappe.realtime.on) return;
		if (!frappe.realtime.socket) {
			if (++realtimeAttempts < REALTIME_MAX_ATTEMPTS) {
				setTimeout(subscribeRealtime, REALTIME_RETRY_MS);
			} else {
				// Always visible (not _diag): without the socket the widget never updates.
				try { console.warn("[optimus] realtime socket never appeared, giving up; widget updates need a page reload"); } catch (e) { /* noop */ }
			}
			return;
		}
		realtimeSubscribed = true;
		registerRealtimeHandlers();
	}

	function registerRealtimeHandlers() {
		// v0.5.1: realtime is now the PRIMARY state-transition channel
		// (polling is gone). Server emits:
		//
		//   optimus_session_stopping user clicked Stop on another tab
		//   optimus_session_analyzing analyze.run started
		//   optimus_progress percent + description during analyze
		//   optimus_session_ready analyze finished, report available
		//   optimus_session_failed analyze crashed
		//
		// The widget rehydrates state from these events so a session
		// driven from tab A is visible in tabs B, C, D without any tab
		// issuing an HTTP poll.
		// A socket can receive the same event through its user and document
		// rooms. Shared-form events must never take over this user's widget.
		const seenEvents = new Set();
		function matchesCurrentSession(data, phase2 = false) {
			if (!data || !data.session_uuid) return false;
			const principal = phase2 ? data.user : data.recording_user;
			if (principal && principal !== frappe.session.user) return false;
			if (!principal && (phase2 || data.session_uuid !== currentState.session_uuid)) return false;
			if (
				currentState.session_uuid
				&& data.session_uuid !== currentState.session_uuid
			) {
				return false;
			}
			if (data.event_id) {
				if (seenEvents.has(data.event_id)) return false;
				seenEvents.add(data.event_id);
				if (seenEvents.size > 128) seenEvents.delete(seenEvents.values().next().value);
			}
			currentState.session_uuid = data.session_uuid;
			return true;
		}

		frappe.realtime.on("optimus_session_stopping", (data) => {
			if (!matchesCurrentSession(data)) return;
			// Another tab clicked Stop. Transition this tab out of
			// Recording so the user doesn't see stale state.
			if (currentState.display === "recording") {
				currentState.display = "stopping";
				stopElapsedTimer();
				setDisplay("stopping", __("Stopping…"), "");
			}
		});

		// v0.6.0: phase-2 line-profile events. Widget reflects the same
		// state-machine slots as phase-1 (recording → analyzing → ready),
		// but the labels are prefixed with "Line profiling" so the user knows
		// which mode is running. Line profiling is started/stopped from the
		// Optimus Session form, not the floating widget clicking the
		// widget while line profiling is recording still means "stop" via the
		// existing Stop API path (api.stop_line_profile_pass), which the
		// form's history list reflects after refresh.
		frappe.realtime.on("phase_2_run_recording", (data) => {
			if (!matchesCurrentSession(data, true)) return;
			currentState.display = "recording";
			currentState.phase2 = true;
			currentState.run_uuid = data.run_uuid;
			currentState.session_uuid = data.session_uuid;
			setDisplay(
				"recording",
				__("Line profiling recording…"),
				__("click pill or form to stop")
			);
		});

		// analyzing / progress / ready / failed for both phases.
		Pill.EVENT_NAMES.forEach((name) => {
			frappe.realtime.on(name, (data) => {
				// The popup is independent of the pill: it also fires for
				// sessions this tab is not tracking.
				if (name === "optimus_session_failed") showSessionPopup(data, "failed");
				if (name === "optimus_session_ready") showSessionPopup(data, data && data.empty ? "empty" : "ready");
				if (!matchesCurrentSession(data, name.indexOf("phase_2_") === 0)) return;
				applyResult(Pill.reduceEvent(currentState, name, data, Date.now()));
			});
		});

		// Events may be lost across a reconnect.
		if (frappe.realtime.socket && frappe.realtime.socket.on) {
			frappe.realtime.socket.on("connect", () => refreshStatus());
		}
	}

	// started_at is naive server wall-clock time, so read it in the system time zone.
	function startedAtMs(startedAt) {
		const tz = (frappe.boot.time_zone && frappe.boot.time_zone.system)
			|| (frappe.sys_defaults && frappe.sys_defaults.time_zone);
		const m = window.moment;
		if (tz && m && m.tz) return m.tz(startedAt, tz).valueOf();
		return new Date(startedAt).getTime();
	}

	function computeElapsed() {
		if (!currentState.started_at) return "";
		const start = startedAtMs(currentState.started_at);
		if (isNaN(start)) return "";
		const seconds = Math.max(0, Math.floor((Date.now() - start) / 1000));
		const m = Math.floor(seconds / 60);
		const s = seconds % 60;
		return `${m}:${s.toString().padStart(2, "0")}`;
	}

	function startElapsedTimer() {
		if (elapsedHandle) return;
		elapsedHandle = setInterval(() => {
			if (currentState.display === "recording") {
				const el = widget && widget.querySelector(".fp-elapsed");
				if (el) el.textContent = computeElapsed();
			}
		}, 1000);
	}

	function stopElapsedTimer() {
		if (elapsedHandle) {
			clearInterval(elapsedHandle);
			elapsedHandle = null;
		}
	}

	// Bootstrap: wait for document.body to exist, then run init(), which
	// gates on the enabled flag and the user's roles before mounting.
	function bootstrap() {
		_diag("[optimus] bootstrap() called, document.body exists:", !!document.body);
		if (!document.body) {
			setTimeout(bootstrap, 50);
			return;
		}
		try {
			init();
		} catch (e) {
			_diag("[optimus] init failed:", e);
		}
	}

	_diag("[optimus] document.readyState:", document.readyState);
	if (document.readyState === "loading") {
		document.addEventListener("DOMContentLoaded", bootstrap);
	} else {
		bootstrap();
	}
})();
