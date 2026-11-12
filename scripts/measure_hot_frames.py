"""Measure the hot-frames gate on a stored recordings bundle (offline, no site needed).

Usage, run once per checkout, then compare the two JSON files:

    python3 scripts/measure_hot_frames.py <bundle.json.gz> <out.json>
    python3 scripts/measure_hot_frames.py --compare <develop_out.json> <pr_out.json>

``<bundle.json.gz>`` is a ``private/files/optimus_recordings_<session>.json.gz`` file
from a test site, with 20+ actions. Run the first command inside a checkout of
``develop`` and again inside this branch (it analyzes with the code of the checkout
it is run from), then run ``--compare``. The bundle holds pickled call trees: use only
a bundle from your own test site.

Gate (PR body, Before merge): Repeated Hot Frame findings <= 5 with <= 2 from
framework code, and per-action findings change <= 10 percent against develop.
Off a bench there is no installed-app list, so framework code is told apart by the
analyzer's built-in path rules.
"""

import base64
import collections
import gzip
import json
import os
import pathlib
import pickle
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

PER_ACTION_TYPES = {"Slow Hot Path", "Hook Bottleneck", "Slow Background Job"}


def _load_tree(blob_b64):
	raw = base64.b64decode(blob_b64)
	for payload in (raw, raw[32:]):
		try:
			return pickle.loads(payload)
		except Exception:
			continue
	return None


def _recordings(path):
	with gzip.open(path) as fh:
		bundle = json.load(fh)
	entries = bundle["recordings"] if "recordings" in bundle else bundle
	recs, actions = [], []
	for entry in entries.values():
		rec = dict(entry["rec"])
		rec["pyi_session"] = _load_tree(entry["tree_b64"]) if entry.get("tree_b64") else None
		recs.append(rec)
		actions.append(
			{
				"action_label": rec.get("cmd") or rec.get("path") or "?",
				"duration_ms": rec.get("duration") or 0,  # the recorder stores milliseconds
			}
		)
	return recs, actions


def measure(path):
	if "frappe" not in sys.modules:
		try:
			import frappe  # noqa: F401
		except ImportError:
			sys.modules["frappe"] = types.ModuleType("frappe")
	from optimus.analyzers.base import AnalyzeContext
	from optimus.analyzers.call_tree import analyze

	recs, actions = _recordings(path)
	ctx = AnalyzeContext(session_uuid="measure", docname="measure", actions=actions)
	result = analyze(recs, ctx)
	by_type = collections.Counter(f.get("finding_type") for f in result.findings)
	repeated = [f for f in result.findings if f.get("finding_type") == "Repeated Hot Frame"]
	hot = result.aggregate.get("hot_frames") or []
	return {
		"actions": len(recs),
		"findings_by_type": dict(by_type),
		"per_action_findings": sum(by_type[t] for t in PER_ACTION_TYPES),
		"per_action_titles": sorted(
			f"{f.get('action_ref')}|{f.get('finding_type')}|{f.get('title')}"
			for f in result.findings
			if f.get("finding_type") in PER_ACTION_TYPES
		),
		"repeated_hot_frame": len(repeated),
		"repeated_titles": [f.get("title") for f in repeated],
		"hot_frames_rows": len(hot),
		"warnings": ctx.warnings,
	}


def compare(base_path, pr_path):
	base, pr = (json.loads(pathlib.Path(p).read_text()) for p in (base_path, pr_path))
	b, p = base["per_action_findings"], pr["per_action_findings"]
	change = abs(p - b) / b * 100 if b else (0 if p == 0 else 100)
	print(f"actions: {pr['actions']}")
	print(
		f"Repeated Hot Frame: develop {base['repeated_hot_frame']}, PR {pr['repeated_hot_frame']}  (gate <= 5)"
	)
	for t in pr["repeated_titles"]:
		print("   ", t)
	print("  count the framework ones above by hand: gate <= 2")
	print(f"Per-action findings: develop {b}, PR {p}, change {change:.1f}%  (gate <= 10%)")
	same = len(set(base.get("per_action_titles", [])) & set(pr.get("per_action_titles", [])))
	print(f"Per-action findings identical (same action, type and title): {same} of {max(b, p)}")
	print("by type develop:", base["findings_by_type"])
	print("by type PR     :", pr["findings_by_type"])


if __name__ == "__main__":
	if sys.argv[1] == "--compare":
		compare(sys.argv[2], sys.argv[3])
	else:
		out = measure(sys.argv[1])
		pathlib.Path(sys.argv[2]).write_text(json.dumps(out, indent=1))
		print(json.dumps({k: v for k, v in out.items() if k != "repeated_titles"}, indent=1))
