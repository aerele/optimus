# Frappe optimizer copies

The two `.txt` files in this folder are verbatim copies of Frappe Framework source,
used only by `optimus/tests/test_index_suggestions_db_calls.py` to run Frappe's real
index optimizer against a fake database. They are not part of the Optimus package.

| File here | Copied from (Frappe 16.54.0, `version-16`) |
|---|---|
| `db_optimizer.py.txt` | `frappe/core/doctype/recorder/db_optimizer.py`, the whole file |
| `recorder_optimizer.py.txt` | `frappe/core/doctype/recorder/recorder.py`, from `def _optimize_query` to the end of `get_doctype_name` |

The files must stay byte-identical to the source, because the tests check their
sha256 (`PINNED_SHA256` in the test file) and compare them with an installed Frappe.

## Refreshing

1. Check out the Frappe version you want to pin.
2. Copy the two sources as described in the table above over the `.txt` files.
3. Update `PINNED_SHA256` and `PINNED_FRAPPE_VERSION` in the test file, and this file.
4. Run `python -m pytest optimus/tests/test_index_suggestions_db_calls.py`.

When an installed Frappe differs from these copies the comparison test skips (different
version) or fails (same version) and names the files that differ.

## Licence

Frappe is distributed under the MIT License:

```
The MIT License

Copyright (c) 2016-2021 Frappe Technologies Pvt. Ltd. <developers@frappe.io>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```
