#!/usr/bin/env python3
# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Run the 15-case AI eval corpus against the AI provider configured on a TEST site.

    cd <bench>
    ./env/bin/python apps/<checkout>/scripts/ai_eval/live.py --site optimus.local \\
        --out <dir outside the repo> --tag before-develop [--optimus-src apps/optimus-before]

For every corpus case it builds the finding exactly as the product's refresh path does
(scripts/ai_eval/_corpus.build_finding -> analyze._ai_payload_for_finding) and calls
ai_fix.suggest_fix, the library function the product uses, so the same runner measures
develop and every later PR. It records the result as returned (suggestion, guardrail
when present, tokens, finish_reason), wall time and any error, into OUT/run.json after
each case. Score it with scripts/ai_eval/report.py OUT.

Makes real, possibly billed, LLM calls. Refuses any site other than optimus.local unless
OPTIMUS_EVAL_ALLOW_SITE names it. Reads Optimus Settings but never changes them, never
charges a session's AI spend and rolls back after every case. Never records the API key.
Not collected by any test runner (scripts/ has no tests and no package markers).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import _corpus

DEFAULT_SITE = "optimus.local"
ALLOW_ENV = "OPTIMUS_EVAL_ALLOW_SITE"
PROVIDER_FIELDS = ("name", "protocol", "model", "context_tokens", "max_output_tokens")


class EvalRefused(SystemExit):
	pass


def check_site(site: str, env: dict | None = None) -> str:
	"""Return ``site`` when the eval may run there, else raise EvalRefused."""
	env = os.environ if env is None else env
	allowed = {DEFAULT_SITE, (env.get(ALLOW_ENV) or "").strip()} - {""}
	if (site or "").strip() not in allowed:
		raise EvalRefused(
			f"refusing to run the AI eval on {site!r}: it makes real LLM calls and must use a "
			f"test site ({DEFAULT_SITE}, or set {ALLOW_ENV}={site} for another test site)"
		)
	return site.strip()


def provider_meta(provider: dict) -> dict:
	"""The provider facts worth recording. Allow-list only: the develop-era provider dict
	still carries the API key, so nothing outside PROVIDER_FIELDS is ever copied."""
	meta = {k: provider.get(k) for k in PROVIDER_FIELDS if provider.get(k) is not None}
	try:
		parsed = urlparse(provider.get("base_url") or "")
		host = parsed.hostname or ""
		if ":" in host:
			host = f"[{host}]"
		meta["base_url_host"] = f"{host}:{parsed.port}" if parsed.port else host
	except ValueError:
		meta["base_url_host"] = ""
	meta["has_key"] = bool(provider.get("has_key") or provider.get("api_key"))
	return meta


def git_facts(path: str) -> dict:
	def git(*args):
		proc = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, check=False)
		return proc.stdout.strip() if proc.returncode == 0 else ""

	return {"git_head": git("rev-parse", "HEAD"), "git_dirty": bool(git("status", "--porcelain", "--untracked-files=no"))}


def run_case(case: dict, ai_fix, *, source_lines: list[str] | None = None) -> dict:
	"""Evaluate one case with the product's own eligibility, recipe and gate checks."""
	record = {"name": case["name"], "finding_type": case["finding_type"], "outcome": None,
		"result": None, "error": None, "elapsed_s": 0.0}
	try:
		from optimus.renderer import fix_recipes  # added by PR-L1
	except ImportError:
		fix_recipes = None
	finding = _corpus.build_finding(case, source_lines=source_lines)
	ftype = case["finding_type"]
	if fix_recipes is not None and ftype in getattr(fix_recipes, "INDEX_FINDING_TYPES", ()):
		record["outcome"] = "recipe"
		return record
	gate = getattr(ai_fix, "llm_gate_note", None)  # added by PR-L1
	note = gate(finding) if gate else None
	if ftype not in ai_fix.AI_ELIGIBLE_FINDING_TYPES or note:
		record["outcome"], record["gate_note"] = "gated", note or "not AI-eligible"
		return record
	t0 = time.monotonic()
	failure = None
	try:
		record["result"] = ai_fix.suggest_fix(finding)
	except ai_fix.AiFixError as exc:
		failure = {"kind": getattr(exc, "kind", "") or "unknown", "message": str(exc)[:300]}
	except Exception as exc:
		failure = {"kind": "internal", "message": type(exc).__name__}
	record["elapsed_s"] = round(time.monotonic() - t0, 1)
	if failure and failure["kind"] == "not_eligible":
		record["outcome"], record["gate_note"] = "gated", failure["message"]
	elif failure:
		record["error"] = failure
	return record


def _connect(site: str):
	import frappe

	bench = os.path.realpath(os.path.join(os.path.dirname(frappe.__file__), "..", "..", ".."))
	os.chdir(os.path.join(bench, "sites"))  # what `bench execute` does; logs resolve correctly
	frappe.init(site=site, sites_path=".")
	frappe.connect()
	return frappe, os.path.join(bench, "apps")


def main(argv: list[str] | None = None) -> int:
	ap = argparse.ArgumentParser(description="Run the AI eval corpus on a test site.")
	ap.add_argument("--site", default=DEFAULT_SITE)
	ap.add_argument("--out", required=True, help="new directory for run.json (keep it out of the repo)")
	ap.add_argument("--tag", default="", help="run name shown in the report, e.g. before-develop")
	ap.add_argument("--cases", default="", help="comma-separated case names (default: all 15)")
	ap.add_argument("--optimus-src", default="", help="import optimus from this checkout (e.g. a develop worktree)")
	ap.add_argument("--num-ctx", type=int, default=0, help="the model server's context window, as `ollama show` reports it")
	ap.add_argument("--no-hydrate", action="store_true", help="send masked framework lines as masked")
	args = ap.parse_args(argv)

	site = check_site(args.site)
	out_path = Path(args.out).resolve()
	optimus_src = str(Path(args.optimus_src).resolve()) if args.optimus_src else ""
	checkouts = [_corpus.REPO_ROOT, *([optimus_src] if optimus_src else [])]
	if any(out_path.is_relative_to(Path(path).resolve()) for path in checkouts):
		raise EvalRefused("keep run artifacts outside the repository and selected checkout")
	if out_path.exists():
		raise EvalRefused("output directory exists; use a new --out directory per run")
	out_dir = str(out_path)
	if optimus_src:
		sys.path.insert(0, optimus_src)

	corpus = _corpus.load_corpus()
	wanted = {n.strip() for n in args.cases.split(",") if n.strip()}
	chosen = [c for c in corpus["cases"] if not wanted or c["name"] in wanted]
	if wanted - {c["name"] for c in chosen}:
		raise EvalRefused(f"unknown case names: {sorted(wanted - {c['name'] for c in chosen})}")

	os.makedirs(out_dir)
	frappe, apps_dir = _connect(site)
	try:
		import optimus

		if optimus_src and not Path(optimus.__file__).resolve().is_relative_to(Path(optimus_src)):
			raise EvalRefused("requested checkout was not imported; use a fresh process")
		from optimus import ai_fix

		frappe.local._optimus_spend_session = None  # never charge a session's AI spend
		if not ai_fix.is_available(section="findings"):
			raise EvalRefused("AI fix suggestions are not available on this site: check Optimus Settings")
		excluded = [c["finding_type"] for c in chosen if ai_fix.is_finding_type_excluded(c["finding_type"])]
		if excluded:
			raise EvalRefused(f"Optimus Settings excludes {sorted(set(excluded))}; clear it for the eval")
		provider = ai_fix._resolve_provider()
		resolve_timeout = getattr(ai_fix, "_resolve_timeout_seconds", None)
		run = {
			"meta": {
				"tag": args.tag or os.path.basename(out_dir),
				"site": site,
				"started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
				"optimus_version": optimus.__version__,
				"optimus_path": os.path.dirname(os.path.dirname(os.path.abspath(optimus.__file__))),
				**git_facts(os.path.dirname(os.path.abspath(optimus.__file__))),
				**provider_meta(provider),
				"timeout_s": resolve_timeout() if resolve_timeout else None,
				"num_ctx": args.num_ctx or None,
				"hydrated": not args.no_hydrate,
				"corpus_cases": len(chosen),
			},
			"cases": [],
		}
		for case in chosen:
			lines, drift = (None, [])
			if case.get("masked") and not args.no_hydrate:
				lines, drift = _corpus.hydrate(case, apps_dir)
			record = run_case(case, ai_fix, source_lines=lines)
			record["drift"] = drift
			frappe.db.rollback()
			run["cases"].append(record)
			run["meta"]["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
			with open(os.path.join(out_dir, "run.json"), "w", encoding="utf-8") as fh:
				json.dump(run, fh, indent=1, default=str)
			print(f"{case['name']}: {record['outcome'] or ('error ' + record['error']['kind'] if record['error'] else 'ok')} "
				f"({record['elapsed_s']} s)", flush=True)
	finally:
		frappe.destroy()
	print(f"wrote {out_dir}/run.json; score it with: python scripts/ai_eval/report.py {out_dir}")
	return 0


if __name__ == "__main__":
	sys.exit(main())
