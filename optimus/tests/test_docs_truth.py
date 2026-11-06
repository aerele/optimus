# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The docs say what the code does: the README's preset reference values equal the shipped
presets, the form help never promises what the presets do not do, and no doc describes a
feature that was removed (Safe report, baseline comparison, the old renderer module)."""

import glob
import json
import re
from pathlib import Path

import pytest

from optimus import settings

_APP = Path(__file__).resolve().parents[2]
_SETTINGS_JSON = _APP / "optimus" / "optimus" / "doctype" / "optimus_settings" / "optimus_settings.json"
_TRIPLET = re.compile(
	r"Strict\s+(-?\d+(?:\.\d+)?)\s*·\s*Recommended\s+(-?\d+(?:\.\d+)?)\s*·\s*Relaxed\s+(-?\d+(?:\.\d+)?)"
)
_ROW = re.compile(r"^\| \*\*(?P<label>.+?)\*\* \| (?P<default>[^|]+) \|.*$", re.M)

_REMOVED_FEATURE_PATTERNS = {
	"Safe report or mode": r"(?<![-\w])safe[ -](report|mode)",
	"Safe/Raw pairing": r"\b(safe|raw) (and|or|vs\.?) (safe|raw)\b",
	"Raw report": r"\braw report\b",
	"customer-safe export": r"customer-safe",
	"baseline comparison": r"\bbaseline comparison\b|\bpin(ned)? .{0,25}baseline|unpin baseline",
	"safe_report_file field": r"(?<![\w])safe_report_file",
	"missing test file": r"test_safe_report_self_contained\.py",
	"removed modules": r"(?<!\w)(renderer|recorder)\.py",
	"personal path": r"/Users/navin",
	"archived line-profile runs": r"moved to a private file|archived to a private",
}


def _doc_paths():
	paths = [_APP / "README.md", _APP / "CONTRIBUTING.md", _APP / "SECURITY.md"]
	for pattern in ("docs/*.md", "optimus/renderer/*.md", "tests_integration/*.md"):
		paths += [Path(p) for p in glob.glob(str(_APP / pattern))]
	return [p for p in paths if p.exists()]


def _fields():
	return {f["fieldname"]: f for f in json.loads(_SETTINGS_JSON.read_text())["fields"]}


def _readme_rows():
	rows = {}
	for m in _ROW.finditer((_APP / "README.md").read_text()):
		rows.setdefault(m["label"].replace("`", ""), []).append(m)
	return rows


@pytest.mark.parametrize("key", settings._SENSITIVITY_KEYS)
def test_readme_reference_values_match_profiles(key):
	label = _fields()[key]["label"].replace("`", "")
	rows = _readme_rows().get(label, [])
	assert len(rows) == 1, f"README must have exactly one row for {label!r}, found {len(rows)}"
	triplet = _TRIPLET.search(rows[0].group(0))
	assert triplet, f"README row {label!r} has no Strict/Recommended/Relaxed values"
	shown = tuple(float(v) for v in triplet.groups())
	actual = tuple(float(settings._PROFILES[p][key]) for p in ("Strict", "Recommended", "Relaxed"))
	assert shown == actual, f"README row {label!r}: {shown} != presets {actual}"


@pytest.mark.parametrize("key", settings._SENSITIVITY_KEYS)
def test_readme_default_column_matches_defaults(key):
	label = _fields()[key]["label"].replace("`", "")
	row = _readme_rows()[label][0]
	shown = row["default"].replace("`", "").strip()
	assert float(shown) == float(settings._DEFAULTS[key]), f"README default of {label!r}"


@pytest.mark.parametrize("name", sorted(_REMOVED_FEATURE_PATTERNS))
def test_docs_do_not_describe_removed_features(name):
	pattern = re.compile(_REMOVED_FEATURE_PATTERNS[name], re.I)
	hits = []
	for path in _doc_paths():
		for number, line in enumerate(path.read_text().splitlines(), 1):
			if pattern.search(line):
				hits.append(f"{path.relative_to(_APP)}:{number}")
	assert not hits, f"{name} still described at {hits}"


def test_settings_help_does_not_describe_removed_features():
	pattern = re.compile(_REMOVED_FEATURE_PATTERNS["archived line-profile runs"], re.I)
	stale = [
		k for k, f in _fields().items() if pattern.search(f.get("description", "") + f.get("options", ""))
	]
	assert not stale, f"help text still promises archiving: {stale}"


def test_preset_intro_matches_retention_presets():
	profiles = settings._PROFILES
	assert profiles["Strict"]["session_retention_days"] < profiles["Relaxed"]["session_retention_days"]
	intro = _fields()["sensitivity_description"]["options"]
	assert not re.search(r"Strict</b>[^;]*keeps data longer", intro)
	assert not re.search(r"Relaxed</b>[^;]*prunes faster", intro)
	for days in (profiles["Strict"]["session_retention_days"], profiles["Relaxed"]["session_retention_days"]):
		assert f"{days} day" in intro


_NUMBER_WORDS = {"Nine": 9, "Ten": 10, "Eleven": 11, "Twelve": 12, "Thirteen": 13, "Fourteen": 14}


def _finding_type_count():
	path = _APP / "optimus" / "optimus" / "doctype" / "optimus_finding" / "optimus_finding.json"
	field = next(f for f in json.loads(path.read_text())["fields"] if f["fieldname"] == "finding_type")
	return len(field["options"].split("\n"))


def test_readme_analyzer_count_matches_code():
	from optimus import analyze

	text = (_APP / "README.md").read_text()
	word = re.search(r"\*\*(\w+) analyzers, all pure functions", text).group(1)
	assert _NUMBER_WORDS[word] == len(analyze._BUILTIN_ANALYZERS)
	assert f"the {len(analyze._BUILTIN_ANALYZERS)} built-in analyzers" in text


def test_readme_preset_field_count_matches_code():
	text = (_APP / "README.md").read_text()
	count = int(re.search(r"One-knob preset that sets (\d+) fields", text).group(1))
	assert count == len(settings._SENSITIVITY_KEYS)


def test_readme_finding_type_counts_match_enum():
	text = (_APP / "README.md").read_text()
	section = text.split("## Finding types", 1)[1].split("\n## ", 1)[0]
	total = int(re.search(r"emits (\d+) finding types", section).group(1))
	assert total == _finding_type_count()
	documented = 0
	for heading in re.finditer(r"^### [^\n]*\((\d+) types\)\n(.*?)(?=^### |\Z)", section, re.M | re.S):
		rows = len(re.findall(r"^\| \*\*", heading.group(2), re.M))
		assert rows == int(heading.group(1)), heading.group(0).splitlines()[0]
		documented += rows
	assert documented == total
