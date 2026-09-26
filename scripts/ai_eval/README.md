# AI suggestion evaluation

This kit compares stored or newly generated suggestions against the same source
windows and report renderer. It measures code shown to the reader, invented diff
context, calls left inside loops, introduced-code Semgrep findings, model calls,
tokens and elapsed time. It does not execute model-generated code.

The corpus contains 15 test findings and their captured answers. Framework source
windows are masked except for the target and quoted lines. Home paths and the source
session identifier are omitted. A local run can hydrate matching framework source;
source drift is recorded instead of silently replacing the captured window.

## Score captured answers

Run from the checkout using the bench Python, or install Optimus and its test
dependencies in a separate environment:

```sh
python scripts/ai_eval/report.py stored --label-sheet
python scripts/ai_eval/report.py stored --json /tmp/optimus-stored-report.json
```

Each label is `correct`, `safe-directional`, `wrong`, or `harmful`, with a reason.
The owner confirms labels and the proposed correct fixes. A reason starting with
`DRAFT:` is unconfirmed. The fixture starts with draft labels and empty owner labels.
Live-run decisions go in `<run-dir>/labels.json` as `{case: {label, reason}}`.

A passing report requires the complete corpus, confirmed labels for AI answers,
no unresolved loop/label conflicts, a complete Semgrep scan, no harmful or fabricated
code, no Semgrep findings, at most two wrong answers shown as code, and no regression
from a previously correct answer. A partial run cannot pass the full corpus check.
A known quality failure remains `FAIL` even when some evidence is incomplete;
otherwise missing evidence gives `INCOMPLETE`. Only `PASS` exits zero.

## Pinned Semgrep

Use Semgrep and the Frappe rules revision in `semgrep_rule_map.json`. Install Semgrep
in its own virtualenv so its dependencies do not alter the bench. Put only its CLI
on `PATH`, and set `OPTIMUS_SEMGREP_RULES_DIR` to the pinned checkout's `rules` directory.
No registry rules or automatic configuration are used.

```sh
REQUIRE_SEMGREP=1 python -m pytest optimus/tests -k 'semgrep or rq'
```

Missing tooling fails this check. Local runs without `REQUIRE_SEMGREP=1` may skip
Semgrep tests. Scanner parse errors and skipped files are incomplete evidence.
Only introduced Python/JavaScript lines are scanned: moved lines and quoted source
are excluded. Index DDL still counts as displayed code for owner labelling.

## Run the reference model

First complete the hotfix merge/release checkpoint and back up the approved test
site. Use the prescribed reference provider/model and confirm that no API key is
configured. The runner reads settings without changing them. Do not substitute a
different provider for an unavailable reference host.

```sh
python scripts/ai_eval/live.py --site optimus.local \
  --optimus-src /absolute/path/to/checkout \
  --out /absolute/path/outside/the/repository/new-run --tag before-develop
python scripts/ai_eval/report.py /absolute/path/outside/the/repository/new-run --label-sheet
```

These are real model calls. The runner accepts `optimus.local` by default; another
test site requires an explicit `OPTIMUS_EVAL_ALLOW_SITE` override. It never attributes
spend to a recorded session and rolls back each case. Keep run artifacts outside the
repository: they include model answers and local environment metadata. URL credentials
are omitted from provider metadata. Never commit run directories.

The baseline remains unfinished until the owner labels its answers. The CI workflow
checks new Frappe Semgrep findings against the PR base and reports existing findings
separately. It also runs Semgrep/RQ tests; it never contacts the reference model.

## Limits

The corpus represents one test flow. It carries source windows rather than full
recordings, so recordings-derived example queries and full enclosing functions are
not measured. Hosted-provider behavior is outside this reference comparison.
