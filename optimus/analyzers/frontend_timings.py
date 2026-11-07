# optimus/analyzers/frontend_timings.py
# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Browser-side timing join + Web Vitals analyzer.

Reads ``context.frontend_data``, joins XHR timings to Profiler Actions by
recording_id and dedupes multi-fire LCP per page. Emits three finding types:

- Slow Frontend Render    (LCP > 2500ms on any page)
- Network Overhead        (XHR - backend > 500ms AND > backend * 1.5)
- Heavy Response          (single response > 500KB)
"""

import json
import math

from optimus.analyzers.base import SEVERITY_ORDER, AnalyzerResult, dur

LCP_MEDIUM_MS = 2500
LCP_HIGH_MS = 4000
NETWORK_DELTA_MIN_MS = 500
NETWORK_DELTA_MULTIPLIER = 1.5
NETWORK_DELTA_HIGH_MS = 1000
HEAVY_RESPONSE_BYTES = 500_000
# A browser timing outside these bounds is a bad row, not a slow page: it is dropped rather than
# reported, so no finding title, cell or estimated_impact_ms (a Float column) can carry an absurd value.
MAX_TIMING_MS = 600_000
MAX_RESPONSE_BYTES = 2**31
MAX_CLS = 100
MAX_TIMESTAMP = 1e15


def analyze(recordings: list[dict], context) -> AnalyzerResult:
    frontend_data = getattr(context, "frontend_data", None) or {"xhr": [], "vitals": []}
    xhr_entries = _dict_rows(frontend_data.get("xhr"))
    vitals_entries = _dict_rows(frontend_data.get("vitals"))
    dropped = _count_rows(frontend_data.get("xhr")) - len(xhr_entries)
    dropped += _count_rows(frontend_data.get("vitals")) - len(vitals_entries)

    # Build a recording_id → action_idx map.
    rec_index = {}
    for idx, rec in enumerate(recordings):
        rec_id = rec.get("uuid") or rec.get("recording_uuid")
        if rec_id:
            rec_index[rec_id] = idx

    # v0.5.1: humanized labels (e.g. "Save Sales Invoice" instead of
    # "POST /api/method/frappe.client.save") are built by
    # per_action._build_action and live on context.actions, NOT on the
    # raw `recordings` list. Pre-v0.5.1 this analyzer read from
    # recordings[action_idx] so action.get("action_label") always
    # returned None and every XHR row showed up as "action_0",
    # "action_1", etc. in the report. Pull the label from
    # context.actions instead, fall back through the recording's
    # raw method+path and only then to the synthetic "action_N".
    ctx_actions = getattr(context, "actions", None) or []

    def _label_for(idx: int) -> str:
        # 1. Preferred: the humanized label per_action.analyze built
        #    on context.actions.
        if idx < len(ctx_actions):
            lbl = ctx_actions[idx].get("action_label")
            if lbl:
                return lbl
        # 2. Fallback: synthesize from the recording's method + path,
        #    which IS present on the raw recording dict. Still
        #    readable if per_action hasn't run yet or the actions
        #    list is shorter than recordings for any reason.
        if idx < len(recordings):
            rec = recordings[idx]
            method = rec.get("method") or ""
            path = (rec.get("path") or "").split("?", 1)[0]
            if path:
                return f"{method} {path}".strip()
            cmd = rec.get("cmd")
            if cmd:
                return str(cmd)
        # 3. Last resort: synthetic marker so the report never
        #    shows an empty string.
        return f"action_{idx}"

    matched = []
    orphans = []
    total_backend_ms = 0
    total_xhr_ms = 0
    network_overhead_ms = 0
    slowest_xhr = None

    for x in xhr_entries:
        rid = x.get("recording_id")
        if not isinstance(rid, str) or rid not in rec_index:
            orphans.append({
                "recording_id": rid if isinstance(rid, str) else None,
                "url": _text(x.get("url")),
                "duration_ms": _ms(x.get("duration_ms")),
                "timestamp": _stamp(x.get("timestamp")),
                "reason": "no_matching_recording",
            })
            continue

        action_idx = rec_index[rid]
        action = recordings[action_idx]
        # v0.5.1: backend time lives on the per_action row
        # (context.actions[idx].duration_ms), NOT on the raw
        # recording dict. Pre-v0.5.1 this read `action.get("duration_ms")`
        # from the recording and always got None, so backend_ms was
        # always 0 in production making every XHR look like 100%
        # network overhead. Prefer context.actions[idx].duration_ms,
        # then fall back through the recording's duration_ms (test
        # fixtures occasionally put it here) and finally the
        # recording's `duration` (ms, per recorder convention).
        backend_ms = 0.0
        if action_idx < len(ctx_actions):
            backend_ms = _num(ctx_actions[action_idx].get("duration_ms")) or 0.0
        if not backend_ms:
            backend_ms = (
                _num(action.get("duration_ms"))
                or _num(action.get("duration"))
                or 0.0
            )
        xhr_ms = _ms(x.get("duration_ms"))
        if xhr_ms is None:
            dropped += 1
            continue
        delta = xhr_ms - backend_ms
        if delta < 0:
            delta = 0

        entry = {
            "action_idx": action_idx,
            "action_label": _label_for(action_idx),
            "backend_ms": backend_ms,
            "xhr_ms": xhr_ms,
            "network_delta_ms": delta,
            "response_size_bytes": _int(_num(x.get("response_size_bytes"), 0, MAX_RESPONSE_BYTES)) or 0,
            "status": _int(_num(x.get("status"), 0, 999)),
            "url": _text(x.get("url")),
            "transport": _text(x.get("transport")),
        }
        matched.append(entry)

        total_backend_ms += backend_ms
        total_xhr_ms += xhr_ms
        network_overhead_ms += delta
        if slowest_xhr is None or xhr_ms > slowest_xhr["duration_ms"]:
            slowest_xhr = {
                "url": entry["url"],
                "duration_ms": xhr_ms,
                "action_idx": action_idx,
            }

    # Dedupe multi-fire LCP per page. Keep the highest-timestamp LCP entry
    # for each page_url. Group FCP/CLS/navigation timings by page too.
    vitals_by_page: dict[str, dict] = {}
    lcp_last_ts: dict[str, int] = {}

    for v in vitals_entries:
        page = v.get("page_url")
        fields = _vital_fields(v)
        if not isinstance(page, str) or not page or not fields:
            dropped += 1
            continue
        ts = _stamp(v.get("timestamp")) or 0
        bucket = vitals_by_page.setdefault(page, {})

        if "lcp_ms" in fields:
            if ts >= lcp_last_ts.get(page, -1):
                bucket["lcp_ms"] = fields["lcp_ms"]
                lcp_last_ts[page] = ts
        elif "cls" in fields:
            # CLS accumulates across entries keep the max seen per page.
            if fields["cls"] > bucket.get("cls", 0):
                bucket["cls"] = fields["cls"]
        else:
            bucket.update(fields)

    findings = []

    # ---- Slow Frontend Render -------------------------------------------
    for page, vitals in vitals_by_page.items():
        lcp = vitals.get("lcp_ms")
        if lcp is None or lcp <= LCP_MEDIUM_MS:
            continue
        severity = "High" if lcp > LCP_HIGH_MS else "Medium"
        findings.append({
            "finding_type": "Slow Frontend Render",
            "severity": severity,
            "title": f"LCP {dur(lcp)} on {page}",
            "customer_description": (
                f"The page '{page}' took {dur(lcp)} for its largest "
                "content element to paint. Users typically perceive pages "
                "as slow beyond 2.5 seconds."
            ),
            "technical_detail_json": json.dumps({
                "lcp_ms": lcp,
                "fcp_ms": vitals.get("fcp_ms"),
                "cls": vitals.get("cls"),
                "ttfb_ms": vitals.get("ttfb_ms"),
                "page_url": page,
                "fix_hint": (
                    "Look at TTFB: if it's large, the backend is slow "
                    "(see Slow Query / N+1 findings). If TTFB is small, "
                    "the browser spent time downloading or rendering. "
                    "Check response size and JavaScript execution."
                ),
            }, default=str),
            # Round to 0.01ms so the badge (fmt_ms of this value) and the title
            # (dur(lcp), which rounds to 0.01ms internally) decide the ms-vs-seconds
            # rollover from the SAME number and can't disagree at a boundary.
            "estimated_impact_ms": round(lcp, 2),
            "affected_count": 1,
            "action_ref": "0",
        })

    # ---- Network Overhead -----------------------------------------------
    for m in matched:
        delta = m["network_delta_ms"]
        backend = m["backend_ms"]
        if delta > NETWORK_DELTA_MIN_MS and delta > backend * NETWORK_DELTA_MULTIPLIER:
            severity = "Medium" if delta > NETWORK_DELTA_HIGH_MS else "Low"
            findings.append({
                "finding_type": "Network Overhead",
                "severity": severity,
                "title": f"{dur(delta)} network overhead on {m['action_label']}",
                "customer_description": (
                    f"The browser waited {dur(delta)} longer than the "
                    "server spent processing this request. That extra time "
                    "is network, TLS, serialization, or response download."
                ),
                "technical_detail_json": json.dumps({
                    "backend_ms": backend,
                    "xhr_ms": m["xhr_ms"],
                    "delta_ms": delta,
                    "response_size_bytes": m["response_size_bytes"],
                    "url": m["url"],
                    "fix_hint": (
                        "Large response sizes cause this. Check the "
                        "Heavy Response finding. If response is small, "
                        "suspect network path: CDN, TLS handshake, proxy."
                    ),
                }, default=str),
                # Round to 0.01ms so the badge and the dur(delta) title roll over
                # from the same number (see the LCP note above).
                "estimated_impact_ms": round(delta, 2),
                "affected_count": 1,
                "action_ref": str(m["action_idx"]),
            })

    # ---- Heavy Response --------------------------------------------------
    for m in matched:
        size = m.get("response_size_bytes") or 0
        if size > HEAVY_RESPONSE_BYTES:
            findings.append({
                "finding_type": "Heavy Response",
                "severity": "Low",
                "title": f"{size // 1024}KB response on {m['action_label']}",
                "customer_description": (
                    f"The server returned {size // 1024}KB for this request. "
                    "Large responses are sometimes correct, sometimes a sign "
                    "of returning more data than the UI needs."
                ),
                "technical_detail_json": json.dumps({
                    "response_size_bytes": size,
                    "url": m["url"],
                    "fix_hint": (
                        "Check whether the UI actually uses all the fields "
                        "returned. Paginate or limit field lists if not."
                    ),
                }, default=str),
                "estimated_impact_ms": 0,
                "affected_count": 1,
                "action_ref": str(m["action_idx"]),
            })

    findings.sort(
        key=lambda f: (SEVERITY_ORDER.get(f["severity"], 3), -f["estimated_impact_ms"])
    )

    summary = {
        "total_xhrs": len(matched),
        "total_xhr_ms": round(total_xhr_ms, 2),
        "total_backend_ms": round(total_backend_ms, 2),
        "network_overhead_ms": round(network_overhead_ms, 2),
        "slowest_xhr": slowest_xhr,
        "dropped_rows": dropped,
    }

    return AnalyzerResult(
        findings=findings,
        aggregate={
            "frontend_xhr_matched": matched,
            "frontend_vitals_by_page": vitals_by_page,
            "frontend_orphans": orphans,
            "frontend_summary": summary,
        },
    )


def _num(val, low=None, high=None):
    """A finite float inside [low, high], or None for anything the browser payload should not have sent."""
    if val is None or isinstance(val, bool):
        return None
    try:
        number = float(val)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    if (low is not None and number < low) or (high is not None and number > high):
        return None
    return number


def _ms(val):
    return _num(val, 0, MAX_TIMING_MS)


def _stamp(val):
    return _num(val, 0, MAX_TIMESTAMP)


_NAVIGATION_FIELDS = ("ttfb_ms", "dom_content_loaded_ms", "load_ms", "dns_ms", "tcp_ms")


def _vital_fields(row):
    """The valid fields of one vitals row, keyed as the page bucket stores them; empty if none."""
    name = row.get("name")
    if name in ("lcp", "fcp"):
        value = _ms(row.get("value_ms"))
        return {} if value is None else {f"{name}_ms": value}
    if name == "cls":
        value = _num(row.get("value"), 0, MAX_CLS)
        return {} if value is None else {"cls": value}
    if name == "navigation":
        fields = {key: _ms(row.get(key)) for key in _NAVIGATION_FIELDS}
        return {key: value for key, value in fields.items() if value is not None}
    return {}


def _count_rows(rows):
    return len(rows) if isinstance(rows, list) else 0


def _text(val):
    return val if isinstance(val, str) else None


def _int(val):
    number = _num(val)
    return None if number is None else int(number)


def _dict_rows(rows):
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]
