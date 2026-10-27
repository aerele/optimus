#!/usr/bin/env python3
# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Score one AI eval run against the owner's quality bar and print a Markdown table.

    python scripts/ai_eval/report.py RUN_DIR [--baseline RUN_DIR|stored] [--out FILE]
    python scripts/ai_eval/report.py stored            # the 2026-09-09 answers in the fixture
    python scripts/ai_eval/report.py RUN_DIR --label-sheet

RUN_DIR is what scripts/ai_eval/live.py wrote (run.json). Labels for a live run live in
RUN_DIR/labels.json ({case: {"label", "reason"}}); the executor drafts them from the
--label-sheet with reasons starting "DRAFT:", and a DRAFT reason counts as unlabelled
until the owner confirms it. RUN_DIR/accepted_losses.json ({case: reason}) names the
coverage losses the owner accepted; each reason starts with "owner:" (any other value
accepts nothing). The "stored" run uses the owner labels committed
in the fixture.

Per case: disposition (recipe / gated / AI clean / AI fallback / AI with notes), label,
whether code is rendered, fabricated diff lines, still-in-loop for N+1 Query and
Redundant Call, pinned-semgrep hits on the code the answer introduces, LLM calls,
tokens and wall time. The bar (owner decision Q1): 0 harmful and 0 fabricated rendered
as code, 0 semgrep hits, at most 2 wrong rendered as code, and no case that was correct
in the baseline gets worse. INCOMPLETE until every AI answer carries a confirmed label,
every still-in-loop answer is labelled wrong or harmful, and every coverage loss (a case
that reached the AI in the baseline but is now gated or recipe) is the pre-L5 Redundant
Call gate or is accepted by the owner. Needs no Frappe and no network (semgrep runs locally when
OPTIMUS_SEMGREP_RULES_DIR is set).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import _answer
import _corpus
import _semgrep

DISPOSITIONS = ("recipe", "gated", "AI clean", "AI fallback", "AI with notes")
AI_DISPOSITIONS = ("AI clean", "AI fallback", "AI with notes")
NOTE_MARKER = "> **Profiler note:**"
PRE_L5_NOTE = "re-record the flow"  # PR-L1's gate note for Redundant Calls analyzed before the L5 fix
MAX_WRONG_AS_CODE = 2


def load_run(ref: str, corpus: dict) -> dict:
	"""A live run directory, or "stored" for the fixture's 2026-09-09 answers."""
	if ref == "stored":
		return {
			"meta": {"tag": "stored-2026-09-09", "model": corpus["model"], "source": "fixture"},
			"cases": [
				{"name": c["name"], "finding_type": c["finding_type"], "result": {"suggestion": c["suggestion"]}}
				for c in corpus["cases"]
			],
			"labels": {c["name"]: c["label"] for c in corpus["cases"] if c.get("label")},
		}
	with open(os.path.join(ref, "run.json"), encoding="utf-8") as fh:
		run = json.load(fh)
	for key, name in (("labels", "labels.json"), ("accepted_losses", "accepted_losses.json"), ("server", "server.json")):
		path = os.path.join(ref, name)
		run[key] = {}
		if os.path.exists(path):
			with open(path, encoding="utf-8") as fh:
				run[key] = json.load(fh)
	return run


def is_draft(entry: dict | None) -> bool:
	"""A label whose reason starts with DRAFT is the executor's, not the owner's."""
	return str((entry or {}).get("reason") or "").lstrip().upper().startswith("DRAFT")


def disposition(record: dict) -> str | None:
	"""The product outcome for one case, or None when the call failed."""
	if record.get("outcome") in ("recipe", "gated"):
		return record["outcome"]
	result = record.get("result")
	if not result or not (result.get("suggestion") or "").strip():
		return None
	guardrail = result.get("guardrail") or {}
	if guardrail.get("fallback"):
		return "AI fallback"
	if NOTE_MARKER in result["suggestion"]:
		return "AI with notes"
	return "AI clean"


def calls(record: dict) -> int:
	"""LLM calls this case cost: none for a recipe or a gate, one per attempt otherwise
	(a failed call still went out), plus the re-ask when the guardrail made one."""
	if record.get("outcome") in ("recipe", "gated"):
		return 0
	result = record.get("result")
	if not result:
		return 1 if record.get("error") else 0
	return 1 + int(bool((result.get("guardrail") or {}).get("reasked")))


def violation_codes(result: dict | None) -> list[str]:
	"""Guardrail codes of one result. #54 stores ``guardrail.violations`` as code strings;
	a dict with a "code" key is tolerated. Absent on develop (empty list)."""
	codes = []
	for v in ((result or {}).get("guardrail") or {}).get("violations") or []:
		code = v if isinstance(v, str) else (v.get("code") if isinstance(v, dict) else None)
		if code and code not in codes:
			codes.append(code)
	return codes


def truncated(record: dict) -> bool:
	"""The model ran out of room: finish_reason length/max_tokens, or a truncated or
	context-truncated guardrail code."""
	result = record.get("result") or {}
	return result.get("finish_reason") in ("length", "max_tokens") or bool(
		set(violation_codes(result)) & {"truncated", "context-truncated"}
	)


def score(run: dict, corpus: dict, *, semgrep_rules: str | None = None) -> list[dict]:
	by_name = {c["name"]: c for c in corpus["cases"]}
	records = list(run["cases"])
	names = [r["name"] for r in records]
	if len(names) != len(set(names)):
		raise ValueError("duplicate case records")
	if set(names) - set(by_name):
		raise ValueError("unknown case records")
	# A subset run is useful for diagnosis, but cannot pass the full corpus bar.
	records.extend(
		{"name": name, "error": {"kind": "missing_case"}}
		for name in by_name if name not in names
	)
	texts = {}
	rows = []
	for record in records:
		case = by_name[record["name"]]
		text = ((record.get("result") or {}).get("suggestion") or "") if record.get("outcome") not in ("recipe", "gated") else ""
		disp = disposition(record)
		entry = run["labels"].get(record["name"]) if disp not in ("recipe", "gated") else None
		label = None if is_draft(entry) else (entry or {}).get("label")
		if label not in _corpus.LABELS or not str((entry or {}).get("reason") or "").strip():
			label = None
		code = _answer.has_code(text)
		tokens = (record.get("result") or {}).get("tokens") or {}
		rows.append({
			"name": record["name"],
			"finding_type": case["finding_type"],
			"disposition": disp,
			"error": (record.get("error") or {}).get("kind") if disp is None else None,
			"gate_note": record.get("gate_note"),
			"label": label,
			"draft_label": (entry or {}).get("label") if is_draft(entry) else None,
			"code": code,
			"fabricated": _answer.fabricated(text, case["source_lines"]) if code else False,
			"still_in_loop": _answer.still_in_loop(case, text) if code else None,
			"semgrep": None,
			"unparsed": 0,
			"violations": violation_codes(record.get("result")),
			"calls": calls(record),
			"prompt_tokens": tokens.get("prompt_tokens"),
			"completion_tokens": tokens.get("completion_tokens"),
			"elapsed_s": record.get("elapsed_s"),
			"finish_reason": (record.get("result") or {}).get("finish_reason"),
			"truncated": truncated(record),
		})
		if code:
			texts[record["name"]] = text
	rules = semgrep_rules if semgrep_rules is not None else _semgrep.rules_dir()
	if _semgrep.available(rules):
		scanned = _semgrep.scan_texts(texts, rules, {name: by_name[name]["source_lines"] for name in texts})
		for row in rows:
			res = scanned.get(row["name"])
			row["semgrep"] = [h["rule"] for h in res.hits] if res else []
			row["unparsed"] = res.unparsed if res else 0
	return rows


def bar(
	rows: list[dict],
	baseline_rows: list[dict] | None = None,
	*,
	context: int | None = None,
	accepted_losses: dict | None = None,
) -> dict:
	as_code = [r for r in rows if r["code"] and r["disposition"] not in ("recipe", "gated")]
	out = {
		"correct_as_code": [r["name"] for r in as_code if r["label"] == "correct"],
		"harmful_as_code": [r["name"] for r in as_code if r["label"] == "harmful"],
		"fabricated_as_code": [r["name"] for r in as_code if r["fabricated"]],
		"wrong_as_code": [r["name"] for r in as_code if r["label"] == "wrong"],
		"semgrep_hits": sum(len(r["semgrep"] or []) for r in rows),
		"semgrep_run": all(r["semgrep"] is not None for r in rows),
		"unparsed_blocks": sum(r["unparsed"] for r in rows),
		"unlabelled": [r["name"] for r in rows if r["disposition"] not in (None, "recipe", "gated") and not r["label"]],
		"errors": [r["name"] for r in rows if r["disposition"] is None],
		"blind_spots": [r["name"] for r in as_code if r["label"] in ("wrong", "harmful") and r["disposition"] == "AI clean"],
		"still_in_loop": [r["name"] for r in rows if r["still_in_loop"] is True],
		"loop_label_conflicts": [
			r["name"] for r in rows if r["still_in_loop"] is True and r["label"] in ("correct", "safe-directional")
		],
		"regressions": [],
		"coverage_losses": [],
		"unaccepted_losses": [],
		"llm_calls": sum(r["calls"] for r in rows),
		"prompt_tokens": sum(r["prompt_tokens"] or 0 for r in rows),
		"completion_tokens": sum(r["completion_tokens"] or 0 for r in rows),
		"wall_s": round(sum(r["elapsed_s"] or 0 for r in rows), 1),
		"truncated": [r["name"] for r in rows if r["truncated"]],
		"context": context,
		"within_context": None,
	}
	measured = [r for r in rows if r["prompt_tokens"] is not None]
	if context and measured:
		fits = sum(1 for r in measured if r["prompt_tokens"] + (r["completion_tokens"] or 0) <= context)
		out["within_context"] = f"{fits}/{len(measured)}"
	if baseline_rows is not None:
		before = {r["name"]: r for r in baseline_rows}
		# A recipe or a gate renders no AI code, so it is never a regression; it is a
		# coverage loss when the case reached the AI in the baseline (master Coverage line).
		out["regressions"] = [
			r["name"] for r in rows
			if (before.get(r["name"]) or {}).get("label") == "correct"
			and r["disposition"] not in ("recipe", "gated") and r["label"] != "correct"
		]
		losses = [
			r for r in rows
			if (before.get(r["name"]) or {}).get("disposition") in AI_DISPOSITIONS and r["disposition"] in ("recipe", "gated")
		]
		out["coverage_losses"] = [r["name"] for r in losses]
		out["unaccepted_losses"] = [
			r["name"] for r in losses
			if not (r["finding_type"] == "Redundant Call" and PRE_L5_NOTE in (r["gate_note"] or "").lower())
			and not str((accepted_losses or {}).get(r["name"]) or "").lstrip().lower().startswith("owner:")
		]
	incomplete = (
		not rows or out["unlabelled"] or out["errors"] or not out["semgrep_run"] or out["unparsed_blocks"]
		or out["loop_label_conflicts"] or out["unaccepted_losses"]
	)
	failed = (
		out["harmful_as_code"] or out["fabricated_as_code"] or out["semgrep_hits"]
		or len(out["wrong_as_code"]) > MAX_WRONG_AS_CODE or out["regressions"]
	)
	out["verdict"] = "FAIL" if failed else ("INCOMPLETE" if incomplete else "PASS")
	return out


def _cell(value) -> str:
	if value is None:
		return "-"
	if value is True:
		return "yes"
	if value is False:
		return "no"
	if isinstance(value, list):
		return ", ".join(value) if value else "0"
	return str(value)


def _label_cell(row: dict) -> str:
	if row["draft_label"]:
		return f"DRAFT {row['draft_label']}"
	return _cell(row["label"])


def markdown(run: dict, rows: list[dict], summary: dict) -> str:
	meta = run.get("meta") or {}
	server = run.get("server") or {}
	confirmed = sum(1 for r in rows if r["label"])
	drafts = sum(1 for r in rows if r["draft_label"])
	head = [
		f"### AI eval: {meta.get('tag', '?')}",
		"",
		f"Model `{meta.get('model', '?')}`, optimus `{meta.get('optimus_version', '?')}` "
		f"({meta.get('git_head', '?')[:10]}), context `{meta.get('context_tokens') or meta.get('num_ctx') or 'provider default'}`, "
		f"timeout `{meta.get('timeout_s', '?')}` s.",
		f"Server: {server.get('server', '?')} `{server.get('version', '?')}`, model digest `{str(server.get('digest') or '?')[:12]}`, "
		f"num_ctx `{server.get('num_ctx') or '?'}`. Labels: {confirmed} owner-confirmed, {drafts} DRAFT.",
		"",
		"| Case | Type | Disposition | Label | Code | Fabricated | Still in loop | Semgrep | Calls | Tokens in/out | Time s | Finish |",
		"|---|---|---|---|---|---|---|---|---|---|---|---|",
	]
	for r in rows:
		disp = r["disposition"] or f"error: {r['error'] or 'unknown'}"
		tokens = f"{_cell(r['prompt_tokens'])}/{_cell(r['completion_tokens'])}"
		head.append(
			f"| {r['name']} | {r['finding_type']} | {disp} | {_label_cell(r)} | {_cell(r['code'])} "
			f"| {_cell(r['fabricated'])} | {_cell(r['still_in_loop'])} | {_cell(r['semgrep'])} | {r['calls']} "
			f"| {tokens} | {_cell(r['elapsed_s'])} | {_cell(r['finish_reason'])} |"
		)
	s = summary
	head += [
		"",
		f"**Bar: {s['verdict']}.** Harmful as code: {_cell(s['harmful_as_code'])}. "
		f"Fabricated as code: {_cell(s['fabricated_as_code'])}. "
		f"Wrong as code ({len(s['wrong_as_code'])}, max {MAX_WRONG_AS_CODE}): {_cell(s['wrong_as_code'])}. "
		f"Semgrep hits: {s['semgrep_hits'] if s['semgrep_run'] else 'not run'} (unparsed blocks: {s['unparsed_blocks']}). "
		f"Regressions from correct: {_cell(s['regressions'])}. Correct as code: {len(s['correct_as_code'])} "
		f"({_cell(s['correct_as_code'])}).",
		"",
		f"Coverage losses: {_cell(s['coverage_losses'])} (not exempt and not accepted: {_cell(s['unaccepted_losses'])}). "
		f"Still in loop but labelled correct or safe-directional: {_cell(s['loop_label_conflicts'])}.",
		"",
		f"Blind spots (wrong or harmful, rendered as code, no guardrail note): {len(s['blind_spots'])} "
		f"({_cell(s['blind_spots'])}). Still in loop: {_cell(s['still_in_loop'])}. "
		f"Unlabelled: {_cell(s['unlabelled'])}. Errors: {_cell(s['errors'])}.",
		"",
		f"Efficiency: {s['llm_calls']} LLM calls, {s['prompt_tokens']} prompt + {s['completion_tokens']} completion "
		f"tokens, {s['wall_s']} s of model time, truncated: {_cell(s['truncated'])}, within the "
		f"context window ({s['context'] or 'size unknown'}): {s['within_context'] or 'not measured'}.",
	]
	return "\n".join(head) + "\n"


def label_sheet(run: dict, corpus: dict) -> str:
	by_name = {c["name"]: c for c in corpus["cases"]}
	parts = ["# Label sheet", "", "Write RUN_DIR/labels.json as {case: {\"label\": one of "
		+ ", ".join(_corpus.LABELS) + ", \"reason\": one line}}.", ""]
	for record in run["cases"]:
		case = by_name[record["name"]]
		disp = disposition(record)
		parts += [f"## {case['name']} ({case['finding_type']}): {disp or 'error'}", "",
			f"Correct fix: {case.get('correct_fix', '')}", "",
			f"Stored 2026-09-09 answer was labelled: {(case.get('label') or case.get('draft_label') or {}).get('label', '?')}", ""]
		if disp not in (None, "recipe", "gated"):
			parts += ["````markdown", record["result"]["suggestion"], "````", ""]
	drafts = {
		record["name"]: {
			"label": (by_name[record["name"]].get("label") or by_name[record["name"]].get("draft_label") or {}).get("label", ""),
			"reason": "DRAFT: ",
		}
		for record in run["cases"] if disposition(record) not in (None, "recipe", "gated")
	}
	parts += ["## labels.json draft (every reason stays DRAFT until the owner confirms it)", "",
		"```json", json.dumps(drafts, indent=1), "```", ""]
	return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
	ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
	ap.add_argument("run", help='run directory written by live.py, or "stored"')
	ap.add_argument("--baseline", help='run directory or "stored" to compute regressions against')
	ap.add_argument("--out", help="write the Markdown table here as well as stdout")
	ap.add_argument("--json", dest="json_out", help="write rows + summary as JSON")
	ap.add_argument("--label-sheet", action="store_true", help="print a labelling worksheet instead")
	args = ap.parse_args(argv)
	corpus = _corpus.load_corpus()
	run = load_run(args.run, corpus)
	if args.label_sheet:
		sys.stdout.write(label_sheet(run, corpus))
		return 0
	rows = score(run, corpus)
	baseline_rows = score(load_run(args.baseline, corpus), corpus, semgrep_rules="") if args.baseline else None
	meta = run.get("meta") or {}
	summary = bar(
		rows, baseline_rows,
		context=meta.get("context_tokens") or meta.get("num_ctx") or (run.get("server") or {}).get("num_ctx"),
		accepted_losses=run.get("accepted_losses"),
	)
	text = markdown(run, rows, summary)
	sys.stdout.write(text)
	if args.out:
		with open(args.out, "w", encoding="utf-8") as fh:
			fh.write(text)
	if args.json_out:
		with open(args.json_out, "w", encoding="utf-8") as fh:
			json.dump({"rows": rows, "summary": summary}, fh, indent=1)
	return 0 if summary["verdict"] == "PASS" else 1


if __name__ == "__main__":
	sys.exit(main())
