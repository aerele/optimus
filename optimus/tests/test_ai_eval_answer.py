# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""scripts/ai_eval/_answer.py: code, fabrication and still-in-loop checks on rendered answers."""

import pytest

from optimus.tests.ai_eval_support import load

H = "**Diagnosis**: x\n**Fix**\n{}\n**Why it works**: y\n**Verify**: z"
STORED_CODE = {
	"3q1dsfrmti", "3q1efl686s", "3q1fsdhl5p", "3q1j3utnoa", "3q1ln8bv2o",
	"3q1nfc4d2l", "3q1qg8btng", "5qih2paean", "5qimkr12p6", "5qirpu97ci",
}


@pytest.fixture(scope="module")
def corpus():
	return load("_corpus").load_corpus()


# ---------------------------------------------------------------- answer checks on the real corpus
def test_stored_answers_code_and_fabrication(corpus):
	ans = load("_answer")
	cases = corpus["cases"]
	assert {c["name"] for c in cases if ans.has_code(c["suggestion"])} == STORED_CODE
	assert {c["name"] for c in cases if ans.fabricated(c["suggestion"], c["source_lines"])} == {"3q1dsfrmti", "3q1j3utnoa"}


def test_stored_answers_still_in_loop(corpus):
	ans = load("_answer")
	got = {c["name"]: ans.still_in_loop(c, c["suggestion"]) for c in corpus["cases"]}
	assert got == {
		"3q1dsfrmti": None,  # fabricated diff: cannot be applied
		"3q1efl686s": False,  # get_doc hoisted out of the loop
		"3q1fsdhl5p": None,  # the flagged has_permission call is outside the window (pre-L5 anchor)
		"3q1gf7r9lq": None,  # no diff
		"3q1j3utnoa": None,
		"3q1ln8bv2o": True,  # get_all replaces the SQL inside the same while loop
		"3q1nfc4d2l": False,
		"3q1qg8btng": None,
		"5qi7eha1pf": None, "5qid8t8lu5": None, "5qiej4vhmk": None, "5qih2paean": None,
		"5qij9o50so": None, "5qimkr12p6": None, "5qirpu97ci": None,  # Hot Lines: not applicable
	}


# ---------------------------------------------------------------- answer checks, synthetic
LOOP = ["def f(names):", "    out = []", "    for n in names:", "        row = frappe.db.get_value('User', n, 'email')",
	"        out.append(row)", "    return out"]
CASE = {"finding_type": "N+1 Query", "source_lines": LOOP, "window_first_lineno": 10, "target_lineno": 13,
	"technical_detail": {}}


def test_still_in_loop_hoist_vs_in_place():
	ans = load("_answer")
	hoist = H.format("```diff\n     out = []\n+    rows = dict(frappe.get_all('User', filters={'name': ('in', names)}, fields=['name', 'email'], as_list=True))\n"
		"     for n in names:\n-        row = frappe.db.get_value('User', n, 'email')\n+        row = rows.get(n)\n```")
	in_place = H.format("```diff\n     for n in names:\n-        row = frappe.db.get_value('User', n, 'email')\n"
		"+        row = frappe.get_all('User', filters={'name': n}, pluck='email')\n```")
	batched = H.format("```diff\n-    for n in names:\n-        row = frappe.db.get_value('User', n, 'email')\n"
		"-        out.append(row)\n+    out = frappe.get_all('User', filters={'name': ('in', names)}, pluck='email')\n```")
	assert ans.still_in_loop(CASE, hoist) is False
	assert ans.still_in_loop(CASE, in_place) is True
	assert ans.still_in_loop(CASE, batched) is False  # the loop itself is gone
	assert ans.still_in_loop(CASE, H.format("No code.")) is None


def test_diff_parsing_edge_cases():
	ans = load("_answer")
	src = ["    x = 1", "    y = 2"]
	numbered = H.format("```diff\n-  12:     x = 1\n+    x = 3\n```")
	assert not ans.fabricated(numbered, src)
	long_fence = H.format("````diff\n-    x = 1\n```\n+    x = 3\n````")
	assert ans.fabricated(long_fence, src)  # the inner ``` does not close the ```` fence
	assert ans.fabricated(H.format("```diff\n-    z = 9\n+    z = 1\n```"), src)
	assert ans.fabricated(H.format("```diff\n-    x = 1\n+    x = 3\n```"), [])  # no source shown
	assert ans.has_code(H.format("~~~python\nx = 1\n~~~"))
	assert not ans.has_code(H.format("```sql\nSELECT 1\n```"))


# ---------------------------------------------------------------- the code a reader sees (rendered)
HOSTILE = {
	"list-nested fence (4 spaces)": "1. Add the index and commit:\n\n    ```python\n    frappe.db.commit()\n    ```\n",
	"indented code block": "Do this:\n\n    frappe.db.commit()\n",
	"tilde fence": "~~~python\nfrappe.db.commit()\n~~~\n",
	"four-backtick fence": "````python\nx = 1\n```\nfrappe.db.commit()\n````\n",
	"tilde fence holding a shorter tilde line": "~~~~python\nx = 1\n~~~\nfrappe.db.commit()\n~~~~\n",
	"code under Verify": "**Verify**:\n\n```python\nfrappe.db.commit()\n```\n",
}


@pytest.mark.parametrize("shape", sorted(HOSTILE))
def test_code_a_reader_sees_counts_in_every_shape(shape):
	ans = load("_answer")
	text = "**Diagnosis**: x\n\n**Fix**\n\n" + HOSTILE[shape]
	assert ans.has_code(text), shape
	assert any("frappe.db.commit()" in line for b in ans.code_blocks(text) for line in b.body), shape


def test_inline_code_is_not_a_code_block():
	assert not load("_answer").has_code("**Fix**: call `frappe.db.commit()` once.")


def test_fence_indented_outside_a_list_is_judged_as_its_code():
	"""markdown2 shows such a fence as an indented block holding the fence's HTML; the
	diff inside is still grounded."""
	ans = load("_answer")
	text = "**Fix**\n\n    ```diff\n    -    x = 1\n    +    x = \"a\" if y < 2 else 1\n    ```\n"
	assert [(b.info, b.body) for b in ans.code_blocks(text)] == [("diff", ["-    x = 1", '+    x = "a" if y < 2 else 1'])]
	assert not ans.fabricated(text, ["    x = 1"])


def test_index_ddl_counts_as_code_in_any_block():
	"""Correctness Minor 3: DDL in a sql / bash / text block is code a reader can paste."""
	ans = load("_answer")
	for info in ("sql", "bash", "shell", "text", "console", ""):
		text = f"**Fix**\n```{info}\nALTER TABLE `tabUser` ADD INDEX idx_email (email);\n```\n"
		assert ans.has_code(text), info
	assert ans.has_code("**Fix**\n```bash\nbench --site s mariadb -e \"CREATE INDEX i ON `tabUser` (email)\"\n```\n")
	for plain in ("```sql\nSELECT 1\n```", "```sql\nEXPLAIN SELECT name FROM `tabUser` WHERE email = 'x'\n```",
			"```console\n$ bench --site s migrate\n```"):
		assert not ans.has_code("**Fix**\n" + plain + "\n"), plain
