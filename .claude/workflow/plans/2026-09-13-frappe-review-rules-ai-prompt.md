# Plan: Frappe quality-code-review rules → Optimus AI-fix prompt
STATUS: APPROVED
Date: 2026-09-13
Owner: Fable (team leader)

## Goal
Every AI fix suggestion Optimus generates should obey standard Frappe framework
rules — correctness, security, concurrency, backward-compatibility, testing — not
just the performance rules the prompt already carries. The rule-set is maintained
in-repo (re-syncable from Frappe's upstream skill) so it stays useful over time.

## Context
Optimus builds AI suggestions in `optimus/ai_fix.py` from three module-level
system-prompt constants: `_SYSTEM_PROMPT` (line 175 — the fix-suggestion path that
produces the code a user pastes), `_STEPS_SYSTEM_PROMPT` (317 — steps-to-reproduce,
not code) and `_INDEX_SYSTEM_PROMPT` (397 — DBA index recs). `_build_messages`
(1088) returns `_SYSTEM_PROMPT` verbatim (line 1214) plus one humanized user
message; it is pure and already covered by ~17 substring assertions in
`optimus/tests/test_ai_fix.py`, so ADDING prompt content is safe and directly
testable.

`_SYSTEM_PROMPT` is already Frappe-idiomatic on the **performance** axis (no raw
SQL, `frappe.get_all`/`get_list`/`qb`, Search Index via Customize Form, no
metadata-column indexing, no ORM-in-loop). What's missing is the rest of Frappe's
`quality-code-review` skill: the correctness / security / concurrency /
backward-compat / testing rules. A suggestion today can be fast yet still violate a
standard framework rule (a stray `frappe.db.commit()`, `set_value(dt, None, …)`, a
check-then-act race, a breaking signature change, no regression test).

Source of the rules: Frappe's published skill
`github.com/frappe/skills/blob/main/skills/quality-code-review/SKILL.md`
(full text fetched this session).

**Approach chosen:** distil the code-suggestion-relevant rules into a compact
prompt block (kept token-lean for the user's local `qwen3-coder:30b`) AND keep the
full checklist as an in-repo doc for provenance/re-sync. Rejected alternatives:
(a) installing it as a Claude Code skill — user explicitly does not want that; the
rules must flow into Optimus's own generation. (b) Embedding the entire ~2,500-word
checklist in every LLM call — bloats every request, slows the local model, and
dilutes a tightly-focused prompt.

`ai_fix.py` is NOT part of the frozen capture/analyze pipeline (that freeze covers
v0.3.0 capture/analyze); AI-fix is a separate interactive feature, so editing it is
in-bounds. report.html and Optimus Settings stay untouched.

## Architecture / approach
- New doc `docs/frappe-quality-review.md`: full Frappe checklist verbatim + a
  provenance header (upstream URL, `synced 2026-09-13`, "re-sync when upstream
  changes"). Lives beside the existing `docs/AI-FIXING.md`.
- New constant `_FRAPPE_REVIEW_RULES` in `ai_fix.py`, same string-concat style as
  the sibling prompts, ~≤2,500 chars, grouped **Correctness / Security /
  Concurrency / Compatibility / Discipline**. A code comment points at
  `docs/frappe-quality-review.md` as the maintained source.
- Compose `_FRAPPE_REVIEW_RULES` into `_SYSTEM_PROMPT` **between** the
  "NEVER suggest indexing … metadata columns" block (~250) and the
  "OUTPUT Markdown, exactly these four headings" block (~252), so the rules sit
  with the other constraints and the output contract stays last and unchanged.
- `_STEPS_SYSTEM_PROMPT` and `_INDEX_SYSTEM_PROMPT` are unchanged (steps isn't
  code; index is DBA-specific and already carries its relevant rules).
- Distilled rules (highest-value, not already covered):
  - **Correctness**: never emit a mid-transaction `frappe.db.commit()`/`rollback()`;
    never `set_value`/`db.delete`/`get_value` with a None/empty/attacker-controlled
    name or filter (guard it — it hits every row); cast at boundaries with
    `cint`/`flt` instead of comparing mismatched types; prefer a DB unique
    constraint over `if not exists: insert` (check-then-act race).
  - **Security**: validate param *types* at `@frappe.whitelist` boundaries
    (`isinstance(x, str)` — Frappe accepts filter-lists, an injection vector even via
    the ORM); `safe_exec` only, never `eval`/`exec`; don't reach for
    `allow_guest=True`; escape at the DOM sink, don't strip characters.
  - **Concurrency**: `SELECT … FOR UPDATE` must filter an indexed column or it locks
    the table; keep query-building stateless (no class/module-global mutable state).
  - **Compatibility**: new params last as kwargs with safe defaults; on rename keep
    the old name as a shim; never monkey-patch core or copy a core file;
    schema/field changes need an idempotent, correctly-ordered data patch in the
    right app.
  - **Discipline**: pair the fix with a regression test that would have caught it;
    if the right move is "don't fix" / "needs a data patch" / "needs a test", say so
    rather than forcing a code diff.

## Task breakdown
| ID | Task | Weight | Assignee | Depends on | Acceptance criteria |
|----|------|--------|----------|------------|---------------------|
| T1 | Author `docs/frappe-quality-review.md` — full Frappe checklist verbatim + provenance header | Light | dev-haiku | — | Faithful to the upstream skill (no invented rules); header carries source URL, `synced 2026-09-13`, re-sync note; placed in `docs/`. |
| T2 | Add `_FRAPPE_REVIEW_RULES` constant and compose it into `_SYSTEM_PROMPT` | Heavy | Lead (Fable) | — | Constant grouped into the 5 sections above, ≤2,500 chars; concatenated into `_SYSTEM_PROMPT` at the specified point; four-heading output contract byte-unchanged; no existing prompt text removed/altered; no contradiction with existing perf rules; code comment cites `docs/frappe-quality-review.md`. |
| T3 | Add substring tests in `test_ai_fix.py` + a token-budget guard | Light | dev-haiku | T2 | New assertions that `_build_messages()` system output carries each rule group's tokens; a guard asserting `len(_FRAPPE_REVIEW_RULES) <= 2500`; all pre-existing ai_fix tests unchanged and green; full `test_ai_fix.py` passes. |

## Edge cases and failure modes (reviewer will verify each one)
1. **Prompt bloat / local-model budget** — the added block must be compact (≤2,500 chars, ~300 tokens). Behavior: T3's guard test fails if it grows past the cap; no duplication of rules already in `_SYSTEM_PROMPT`.
2. **Contradiction with existing rules** — the existing prompt endorses per-request memoization on `frappe.local` and `frappe.cache`. The new "keep query-building stateless / no module-global mutable state" must be phrased as *no static/shared query state*, compatible with request-scoped memoization. Behavior: wording reconciled; reviewer confirms no conflicting directives.
3. **Output contract drift** — the four headings (Diagnosis/Fix/Why it works/Verify) must remain the only output sections; the rules are constraints, not new sections. Behavior: `test_system_prompt_is_frappe_idiomatic_and_structured` still passes; no new heading token introduced.
4. **Existing-test breakage** — the 17+ substring assertions depend on specific text (e.g. "NO RAW SQL IN YOUR PROPOSED FIX", the two worked examples, metadata-column wording). Behavior: none of that text is removed or reordered; full suite green.
5. **Constant defined but not wired** — classic bug: `_FRAPPE_REVIEW_RULES` exists but isn't concatenated. Behavior: a test asserts the tokens appear in the actual `_build_messages()` output (not just that the constant exists).
6. **Provenance-doc fidelity** — `docs/frappe-quality-review.md` must represent Frappe's skill accurately (no fabricated rules) and name its source + sync date. Behavior: reviewer diffs content against the fetched source.
7. **Frozen-file boundary** — diff must touch only `ai_fix.py`, `docs/frappe-quality-review.md`, `test_ai_fix.py`. NOT report.html, Optimus Settings, or the capture/analyze pipeline. Behavior: reviewer checks diff scope.
8. **Discipline rule over-suppression** — the "don't fix / needs a patch / needs a test" clause must be a *fallback*, not a default that makes the model refuse normal fixes. Behavior: phrased conditionally ("if…"); manual spot-check in flow review, since LLM output isn't deterministically assertable.
9. **N/A categories** — no auth boundary, no DB write, no concurrency in *this* change itself (it is prompt text + a doc + tests). Data-integrity/partial-failure do not apply to a static string edit. Stated explicitly so the reviewer doesn't hunt for absent risk.

## Test plan
- **Unit tests** (`test_ai_fix.py`, new `TestFrappeReviewRules` or added methods), one per rule group (edge case 5 = wired-in check):
  - Correctness: system contains a mid-transaction `commit`/`rollback` prohibition; a `set_value`/`delete` None/empty-filter guard ("every row"); `cint`/`flt` boundary cast; "unique constraint" / check-then-act.
  - Security: `isinstance` at whitelist boundary; "safe_exec" and not-`eval`/`exec`; "allow_guest"; escape-at-sink.
  - Concurrency: "FOR UPDATE" + "index"; "stateless" / "module-global".
  - Compatibility: "keyword" / kwargs default; "shim" / keep old name; "monkey-patch"; "data patch" + "idempotent".
  - Discipline: "regression test"; "don't fix" / "needs a patch".
  - Budget guard (edge case 1): `len(ai_fix._FRAPPE_REVIEW_RULES) <= 2500`.
  - Contract intact (edge case 3/4): existing four-heading + NO-RAW-SQL tests still green.
- **Flow review** (honest scope): there is **no new UI** — this is a pure prompt-content change on a pure function, so Playwright/Chrome UI flow does not apply. The reviewer's flow check is a **bench-level smoke**: call `optimus.api.suggest_fix` (or `_build_messages`) on a sample finding in a real bench and confirm (a) the assembled system prompt carries the new rule tokens and (b) with a configured provider, one live "Suggest a fix" still returns the four-heading format with no regression. The break-attempt-that-must-fail-safe: a finding whose offending code isn't in the source window still yields a directional recommendation (no fabricated diff), unaffected by the new rules.

## Open questions
None blocking — scope was pre-agreed with the user:
- Inject into `_SYSTEM_PROMPT` only (not the index/steps prompts). (Decided.)
- Always inject, not gated behind a provider/setting. (Decided — simpler, universally beneficial.)
- Possible follow-up (out of scope here): thread the write-hot-table/patch subset into `_INDEX_SYSTEM_PROMPT` too. Noted, not planned now.

## Definition of done
- All tasks meet acceptance criteria
- Code review VERDICT: GREEN
- Flow review VERDICT: GREEN (bench smoke per Test plan)
- Committed only after both greens; PR raised only after flow review passed on the final state
- **Commit hygiene:** per this repo's standing rule, commits must NOT carry a `Co-Authored-By: Claude` trailer (the session harness currently injects one — omit it here; flag at commit time).
