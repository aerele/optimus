# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Redis lifetimes and stale-session limits in one place.

They only work in relation to each other (see ``tests/test_timing_relations.py``),
so change them here, never in the modules that read them. Pure constants: no
Frappe import.
"""

# The active-session pointer slides: it expires this long after the last
# captured request. Matches frappe.recorder.RECORDER_AUTO_DISABLE.
SESSION_TTL_SECONDS = 10 * 60

# Per-recording tree, sidecar, lock-event, infra and resolved-doc keys, and a
# session's frontend metric lists. A janitor-stopped session is analyzed up to
# an idle expiry, a janitor sweep and the analyze waits after its last request,
# and these keys must still be there then. Memory held per session is up to
# 200 recordings x 50 KB-2 MB, evicted oldest-first by the cache LRU.
RECORDING_DATA_TTL_SECONDS = 6 * 60 * 60

# Bound on the keys a late background job could re-create after cleanup.
JOB_KEYS_TTL_SECONDS = 24 * 60 * 60

# The janitor force-stops a Recording row older than this once its pointer is
# gone; longer than SESSION_TTL_SECONDS so the natural expiry comes first.
STALE_RECORDING_MINUTES = 11

# Analyzing rows older than this are failed as a crashed worker. The analyze
# budget and the RQ long-queue timeout (25 minutes) both end before it.
STALE_ANALYZING_MINUTES = 30

# How often a visible pill asks the server whether its recording ended by itself.
# Shorter than SESSION_TTL_SECONDS so an expired session is noticed within a minute.
PILL_RECORDING_CHECK_SECONDS = 60

JANITOR_INTERVAL_MINUTES = 5

# Ceiling on how long analyze waits for the flow's background jobs.
MAX_BG_JOB_WAIT_SECONDS = 300

# Ceiling on how long analyze yields to another session's heavy phase.
SINGLEFLIGHT_MAX_WAIT_SECONDS = 600

# Cumulative wall-clock budget of the analyzer pipeline.
ANALYZE_TOTAL_BUDGET_SECONDS = 20 * 60
