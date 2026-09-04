// Copyright (c) 2026, Optimus contributors
// For license information, please see license.txt

// Fill the `app_name` Autocomplete on BOTH the Tracked Apps and Ignored Apps
// child tables with the bench's installed apps both share the field on the
// `Optimus Tracked App` child doctype, so both must be filled.
//
// Why on refresh: the Autocomplete options live on the grid docfield,
// which Frappe re-creates whenever the form rebinds. Setting options
// on refresh survives reloads and "refresh" Ctrl-S cycles.

// The full set of fields the Sensitivity Profile governs. Kept in sync with
// optimus.settings._SENSITIVITY_KEYS the preset numbers themselves are NOT
// duplicated here; they're fetched at runtime from
// optimus.api.get_config_profiles (single source of truth in settings.py).
// Fields missing from this list are silently NOT updated by the JS preset-
// write, even though the backend would still apply the preset at config-
// resolve time they'd appear locked under a non-Custom profile but show
// stale values. So this MUST match _SENSITIVITY_KEYS exactly.
const OPTIMUS_SENSITIVITY_FIELDS = [
	// General → Session retention
	"session_retention_days",
	// Capture capacity
	"max_queries_per_recording",
	"pyinstrument_sampler_interval_ms",
	"background_job_wait_seconds",
	// Display filters
	"min_action_duration_ms",
	"large_duration_threshold_ms",
	// Analyzer thresholds (detection the original nine)
	"redundant_doc_threshold",
	"redundant_cache_threshold",
	"redundant_perm_threshold",
	"n_plus_one_min_occurrences",
	"slow_query_threshold_ms",
	"slow_hot_path_pct_threshold",
	"slow_hot_path_min_ms",
	"hot_line_high_pct",
	"hot_line_high_min_ms",
	// Phase-2 UI knobs
	"phase2_max_runs_per_session",
	"auto_expand_max_depth",
	"auto_expand_min_ms",
	// AI auto-suggest cap
	"ai_auto_suggest_max",
];

frappe.ui.form.on("Optimus Settings", {
	refresh(frm) {
		frm.set_intro(
			__(
				"Optimus app-wide settings. Changes apply to new sessions; in-flight recordings keep the values they started with."
			),
			"blue"
		);

		// The API Key is a secret token, not a human-chosen password, so the
		// password-strength meter is meaningless for it. It also POSTs the
		// value to `test_password_strength` (zxcvbn) on every keystroke and
		// for a long key zxcvbn returns a `guesses` integer larger than 64
		// bits, which the server's orjson serializer can't encode → a 500
		// "<!doctype" HTML page the form can't parse. Turning the check off
		// for this field sidesteps both the noise and the crash.
		const ak = frm.fields_dict.ai_api_key;
		if (ak && typeof ak.disable_password_checks === "function") {
			ak.disable_password_checks();
		}

		frappe.call({
			method: "optimus.api.get_installed_apps_for_tracking",
			callback(r) {
				if (!r || !r.message) {
					return;
				}
				// Options are a newline-separated string (Frappe splits on \n).
				const options = r.message.join("\n");
				// New rows rebuild their docfield from frappe.meta, so set the
				// base child-doctype meta too both docfield_map AND
				// docfield_list or a freshly-added row's dropdown is blank on
				// older Frappe (e.g. 15.98).
				try {
					const child_dt = "Optimus Tracked App";
					const base_df = frappe.meta.get_docfield(child_dt, "app_name");
					if (base_df) {
						base_df.options = options;
					}
					const list = (frappe.meta.docfield_list || {})[child_dt] || [];
					list.forEach((d) => {
						if (d.fieldname === "app_name") {
							d.options = options;
						}
					});
				} catch (e) {
					// meta not ready per-grid update below covers existing rows.
				}
				// Fill each grid that's present.
				["tracked_apps", "ignored_apps"].forEach((fieldname) => {
					const field = frm.fields_dict[fieldname];
					const grid = field && field.grid;
					if (grid) {
						grid.update_docfield_property(
							"app_name",
							"options",
							options
						);
					}
				});
			},
		});

		// "Test AI connection" only when the feature is on. Saves the
		// operator a profiling round-trip just to find out the key/model
		// are wrong.
		// Also re-evaluate the "Test AI connection" button visibility
		// when the operator toggles ai_enabled (see the ai_enabled
		// handler below it re-runs refresh() so this conditional
		// fires again).
		if (frm.doc.ai_enabled) {
			frm.add_custom_button(__("Test AI connection"), () => {
				if (frm.is_dirty()) {
					frappe.msgprint(
						__("Save your AI settings first, then test the connection.")
					);
					return;
				}
				frappe.show_alert({
					message: __("Pinging the AI provider…"),
					indicator: "blue",
				});
				frappe.call({
					method: "optimus.api.test_ai_connection",
					callback(r) {
						const m = (r && r.message) || {};
						frappe.msgprint({
							title: m.ok
								? __("AI connection OK")
								: __("AI connection failed"),
							indicator: m.ok ? "green" : "red",
							message:
								(m.model ? __("Model: {0}", [m.model]) + "<br>" : "") +
								frappe.utils.escape_html(m.message || ""),
						});
					},
					error() {
						frappe.show_alert({
							message: __("AI connection test failed"),
							indicator: "red",
						});
					},
				});
			});
		}

	},

	config_profile(frm) {
		// Sensitivity Profile changed. Under a named preset (Strict /
		// Recommended / Relaxed) the threshold fields are read-only and the
		// preset drives analysis at read time but we ALSO fill the fields
		// with the preset numbers so the operator can see what they're getting
		// (and so they become the starting point if they later pick Custom).
		// On Custom we just unlock the fields and leave their current values.
		const profile = frm.doc.config_profile;
		const refresh_fields = () =>
			OPTIMUS_SENSITIVITY_FIELDS.forEach((f) => frm.refresh_field(f));

		if (!profile || profile === "Custom") {
			refresh_fields();
			return;
		}

		frappe.call({
			method: "optimus.api.get_config_profiles",
			callback(r) {
				const preset = (r && r.message && r.message[profile]) || null;
				if (preset) {
					OPTIMUS_SENSITIVITY_FIELDS.forEach((f) => {
						if (preset[f] !== undefined) {
							frm.set_value(f, preset[f]);
						}
					});
				}
				// Re-evaluate read_only_depends_on regardless, so the fields
				// lock immediately without a save + reload.
				refresh_fields();
			},
		});
	},

	ai_enabled(frm) {
		// Force the form to re-evaluate `depends_on` directives so the
		// AI subfields (provider / base URL / model / API key / the
		// per-section toggles / the Automatic Suggestions block) show or
		// hide immediately when the master checkbox flips, without
		// requiring a save + reload. Also re-fires refresh() so the
		// "Test AI connection" custom button appears / disappears in
		// step with the toggle.
		frm.refresh_field("ai_provider");
		frm.refresh_field("ai_base_url");
		frm.refresh_field("ai_model");
		frm.refresh_field("ai_api_key");
		frm.refresh_field("ai_sections_break");
		frm.refresh_field("ai_suggest_findings");
		frm.refresh_field("ai_suggest_indexes");
		frm.refresh_field("ai_humanize_steps");
		frm.refresh_field("ai_auto_section");
		frm.refresh_field("ai_auto_suggest");
		frm.refresh_field("ai_auto_suggest_max");
		// Clear and re-attach the custom button.
		frm.clear_custom_buttons();
		frm.trigger("refresh");
	},

	ai_provider(frm) {
		// Base URL only applies to the "OpenAI-compatible" provider the
		// hosted providers (Anthropic / OpenAI / Kimi) use their built-in
		// default endpoint. Re-evaluate depends_on so Base URL hides/shows
		// immediately when the provider changes, without a save + reload.
		frm.refresh_field("ai_base_url");
	},
});
