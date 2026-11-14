// Pure floating-pill state logic (no DOM, no frappe); tested in node.
// Reducers return {state, ui, alert, effect?}, or null to ignore the input.

(function (root, factory) {
	"use strict";
	const api = factory();
	if (typeof module === "object" && module.exports) {
		module.exports = api;
	}
	if (root) {
		root.OptimusPillState = api;
	}
})(typeof window !== "undefined" ? window : null, function () {
	"use strict";

	// Desk's translator when present (labels are translated at the point they are built); identity in node tests.
	function __(text, args) {
		const t = typeof globalThis.__ === "function" ? globalThis.__ : null;
		return t ? t(text, args) : (args ? text.replace(/\{(\d+)\}/g, (m, i) => args[i]) : text);
	}

	// Safety poll: backoff capped at 30s, hard stop at 15 min.
	const POLL = { baseMs: 3000, factor: 1.6, maxMs: 30000, hardStopMs: 15 * 60 * 1000 };
	// An "analyzing" event right after a terminal one is a reordered delivery.
	const STALE_EVENT_WINDOW_MS = 5000;
	const TRANSIENT = ["stopping", "analyzing"];
	// A visible recording tab asks the server this often whether the session ended by itself.
	// Must equal timing.PILL_RECORDING_CHECK_SECONDS (pinned by test_timing_relations.py).
	const RECORDING_CHECK_MS = 60000;

	const EVENTS = {
		optimus_session_analyzing: { kind: "analyzing", phase2: false },
		optimus_progress: { kind: "progress", phase2: false },
		optimus_session_ready: { kind: "ready", phase2: false },
		optimus_session_failed: { kind: "failed", phase2: false },
		phase_2_run_analyzing: { kind: "analyzing", phase2: true },
		phase_2_run_ready: { kind: "ready", phase2: true },
		phase_2_run_failed: { kind: "failed", phase2: true },
	};

	function nextDelay(attempt) {
		const raw = POLL.baseMs * Math.pow(POLL.factor, Math.max(0, attempt));
		return Math.min(POLL.maxMs, Math.round(raw));
	}

	function pollExpired(elapsedMs) {
		return elapsedMs >= POLL.hardStopMs;
	}

	function isTransient(display) {
		return TRANSIENT.indexOf(display) !== -1;
	}

	// What a poll timer tick should do: stop, wait (tab hidden), expire or fetch.
	function pollAction(display, elapsedMs, hidden) {
		if (!isTransient(display)) {
			return "stop";
		}
		if (hidden) {
			return "wait";
		}
		return pollExpired(elapsedMs) ? "expire" : "fetch";
	}

	// What a recording check tick should do: stop (not a phase-1 recording), wait (tab hidden) or fetch.
	function recordingCheckAction(display, phase2, hidden) {
		if (display !== "recording" || phase2) {
			return "stop";
		}
		return hidden ? "wait" : "fetch";
	}

	function terminalKey(data, state) {
		return (data && data.run_uuid) || (data && data.session_uuid) || state.run_uuid || state.session_uuid;
	}

	function terminalLabel(kind, phase2, pending, empty) {
		if (kind === "ready") {
			if (phase2 && pending) {
				return __("Line profiling results saved");
			}
			if (empty) {
				return __("No data");
			}
			return phase2 ? __("Line profiling report ready") : __("Report ready");
		}
		return phase2 ? __("Line profiling failed") : __("Analyze failed");
	}

	function withState(state, patch) {
		return Object.assign({}, state, patch);
	}

	function markTerminal(state, key, now) {
		const terminal = Object.assign({}, state.terminal);
		if (key) {
			terminal[key] = now;
		}
		return terminal;
	}

	function isStaleAnalyzing(state, key, now) {
		const at = key && state.terminal && state.terminal[key];
		return typeof at === "number" && now - at < STALE_EVENT_WINDOW_MS;
	}

	function matchesSession(state, data) {
		if (!data || !data.session_uuid) {
			return false;
		}
		return !state.session_uuid || data.session_uuid === state.session_uuid;
	}

	function finish(state, kind, phase2, data, now) {
		const key = terminalKey(data, state);
		const failed = kind === "failed";
		const already = state.display === "ready";
		// A phase-2 run whose report update is still owed (use Regenerate Reports).
		const pending = !failed && phase2 && !!data.render_pending;
		return {
			state: withState(state, {
				display: "ready",
				phase2: false,
				run_uuid: null,
				session_uuid: data.session_uuid || state.session_uuid,
				docname: data.docname || data.parent || state.docname,
				terminal: markTerminal(state, key, now),
			}),
			ui: { display: "ready", label: terminalLabel(kind, phase2, pending, !!data.empty), elapsed: __("click to view"), tone: failed ? "failed" : null },
			alert: already ? null : {
				message: failed
					? (phase2 ? (data.error ? __("Line profiling analyze failed: {0}") : __("Line profiling analyze failed, open the session for details")) : __("Profiler analyze failed, check Error Log"))
					: (pending ? __("Line profiling results saved. Use Regenerate Reports to update the report.")
						: (data.empty ? __("Profiling finished, but no requests were recorded")
							: (phase2 ? __("Line profiling report ready") : __("Profiler report ready")))),
				args: failed && phase2 && data.error ? [data.error] : undefined,
				indicator: failed ? "red" : (pending || data.empty ? "orange" : "blue"),
			},
		};
	}

	function reduceEvent(state, name, data, now) {
		const spec = EVENTS[name];
		if (!spec || !matchesSession(state, data)) {
			return null;
		}
		const key = terminalKey(data, state);
		if (spec.kind === "ready" || spec.kind === "failed") {
			return finish(state, spec.kind, spec.phase2, data || {}, now);
		}
		if (isStaleAnalyzing(state, key, now)) {
			return null;
		}
		if (spec.kind === "progress") {
			if (state.display !== "analyzing") {
				return null;
			}
			const pct = typeof data.percent === "number" ? Math.round(data.percent) : 0;
			return { state: state, ui: { display: "analyzing", label: __("Analyzing {0}%", [pct]), elapsed: data.description || "" }, alert: null };
		}
		const next = withState(state, {
			display: "analyzing",
			phase2: spec.phase2,
			session_uuid: data.session_uuid || state.session_uuid,
			docname: data.docname || state.docname,
			run_uuid: spec.phase2 ? (data.run_uuid || state.run_uuid) : state.run_uuid,
		});
		const label = spec.phase2 ? __("Line profiling analyzing…") : __("Analyzing…");
		return { state: next, ui: { display: "analyzing", label: label, elapsed: spec.phase2 ? "" : "0%" }, alert: null };
	}

	function inactiveUi() {
		return { display: "inactive", label: __("Profiler"), elapsed: "" };
	}

	// Server wins for terminal states; a stale recording snapshot is ignored.
	function reduceServer(state, srv, now) {
		if (!srv || !srv.phase) {
			return null;
		}
		const display = state.display;
		if (srv.phase === "recording") {
			if (isTransient(display) || display === "ready") {
				return null;
			}
			const phase2 = srv.scope === "phase2";
			const ui = phase2
				? { display: "recording", label: __("Line profiling recording…"), elapsed: __("click pill or form to stop") }
				: { display: "recording", label: __("Recording"), elapsed: "" };
			return {
				state: withState(state, {
					active: true, display: "recording", session_uuid: srv.session_uuid,
					docname: srv.docname, label: srv.label, started_at: srv.started_at,
					phase2: phase2, run_uuid: phase2 ? srv.run_uuid : null,
				}),
				// Already recording: keep the running elapsed text.
				ui: display === "recording" ? null : ui,
				alert: null,
			};
		}
		if (srv.phase === "analyzing") {
			return serverAnalyzing(state, srv);
		}
		if (srv.phase === "ready" || srv.phase === "failed") {
			const data = { session_uuid: srv.session_uuid, docname: srv.docname, run_uuid: srv.run_uuid };
			return finish(state, srv.phase, srv.scope === "phase2", data, now);
		}
		if (srv.phase === "ended") {
			return serverEnded(state, srv);
		}
		return serverIdle(state);
	}

	// The tracked session ended by itself: analyze what was captured, once per session.
	// No alert here: the stop reply says whether this tab won the claim, and only the winner tells the user.
	function serverEnded(state, srv) {
		const display = state.display;
		if (state.phase2 || display === "stopping" || display === "analyzing" || display === "ready") {
			return null;
		}
		if (state.session_uuid && state.session_uuid !== srv.session_uuid) {
			return null;
		}
		if (state.autoStopped && state.autoStopped[srv.session_uuid]) {
			// The automatic stop already failed: stop claiming a recording.
			return display === "recording" ? { state: resetState(state), ui: inactiveUi(), alert: null } : null;
		}
		const autoStopped = Object.assign({}, state.autoStopped);
		autoStopped[srv.session_uuid] = true;
		return {
			state: withState(state, {
				active: false, display: "stopping", session_uuid: srv.session_uuid,
				docname: srv.docname, autoStopped: autoStopped,
			}),
			ui: { display: "stopping", label: __("Stopping…"), elapsed: "" },
			alert: null,
			effect: { stop: srv.session_uuid, capHit: !!srv.cap_hit },
		};
	}

	function serverAnalyzing(state, srv) {
		if (state.display === "ready") {
			// Never show "analyzing" over a finished report.
			return null;
		}
		const phase2 = srv.scope === "phase2";
		return {
			state: withState(state, {
				display: "analyzing", phase2: phase2, session_uuid: srv.session_uuid,
				docname: srv.docname, run_uuid: phase2 ? srv.run_uuid : state.run_uuid,
			}),
			ui: { display: "analyzing", label: phase2 ? __("Line profiling analyzing…") : __("Analyzing…"), elapsed: "" },
			alert: null,
		};
	}

	// Nothing in flight: clear "analyzing"; "stopping" waits for its own request.
	function serverIdle(state) {
		if (state.phase2 && state.display === "recording") {
			// A run on another user's session reads as idle in the tracked lookup.
			return null;
		}
		if (state.display === "recording" || state.display === "analyzing") {
			return { state: resetState(state), ui: { display: "inactive", label: __("Profiler"), elapsed: "" }, alert: null };
		}
		return null;
	}

	// Poll gave up: stop claiming progress.
	function reduceHardStop(state) {
		if (!isTransient(state.display)) {
			return null;
		}
		if (state.docname) {
			return {
				state: withState(state, { display: "ready", phase2: false, run_uuid: null }),
				ui: { display: "ready", label: __("Check report"), elapsed: __("click to view"), tone: "stale" },
				alert: { message: __("Profiler is taking unusually long open the session to check its status"), indicator: "orange" },
			};
		}
		return {
			state: resetState(state),
			ui: { display: "inactive", label: __("Profiler"), elapsed: "" },
			alert: { message: __("Profiler status could not be confirmed check the Optimus Session list"), indicator: "orange" },
		};
	}

	function resetState(state) {
		return withState(state, {
			active: false, display: "inactive", phase2: false, run_uuid: null, session_uuid: null,
		});
	}

	function initialState() {
		return {
			active: false, session_uuid: null, docname: null, label: null, started_at: null,
			display: "inactive", phase2: false, run_uuid: null, terminal: {}, autoStopped: {},
		};
	}

	return {
		POLL: POLL, STALE_EVENT_WINDOW_MS: STALE_EVENT_WINDOW_MS, EVENT_NAMES: Object.keys(EVENTS),
		initialState: initialState, nextDelay: nextDelay, pollExpired: pollExpired, pollAction: pollAction,
		RECORDING_CHECK_MS: RECORDING_CHECK_MS, recordingCheckAction: recordingCheckAction,
		isTransient: isTransient, reduceEvent: reduceEvent, reduceServer: reduceServer,
		reduceHardStop: reduceHardStop, resetState: resetState,
	};
});
