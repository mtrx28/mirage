"""The E1 scoring script (mirage/e1.py): compares expected spoken numbers with the findings a call LOGGED. The calls here are INVENTED
folders with a findings.json; nothing is re-extracted and nothing touches the network."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import mirage.extract as extract_module
from mirage import e1

TEMPLATE = Path(__file__).parent.parent / "docs" / "e1_expected_template.txt"


def finding(kind, value, confidence="keyword", quote=None, t=10.0):
    return {"kind": kind, "value": value, "confidence": confidence, "valid": True, "reason": "", "needs_confirmation": False,
            "quote": quote or value, "t": t, "suggested": None, "candidates": []}


def make_folder(tmp_path: Path, call_id: str, findings: dict) -> None:
    d = tmp_path / "calls" / call_id
    d.mkdir(parents=True)
    (d / "findings.json").write_text(json.dumps(findings), encoding="utf-8")


def expected_file(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "expected.txt"
    p.write_text(text, encoding="utf-8")
    return p


def run(tmp_path, expected_text, calls):
    expected, warnings = e1.parse_expected(expected_file(tmp_path, expected_text))
    findings = [f for c in calls for f in e1.load_findings(c, tmp_path / "calls")]
    return e1.score(expected, findings), warnings


# ---------------------------------------------------------------- the four verdicts

def test_exact_wrong_label_partial_and_missed(tmp_path):
    make_folder(tmp_path, "c1", {
        "phone": [finding("phone", "0000274196")],
        "ambiguous": [finding("ambiguous", "0000358261", "none")],                      # right digits, unlabelled
        "case": [finding("case", "4729")],                                              # 47291 with a digit lost
        "account": [finding("account", "9998887776665")],                               # unrelated
    })
    results, _ = run(tmp_path, """
        0000274196 | phone | natural_groups | owner
        0000358261 | phone | one_digit_per_pause | friend
        47291 | case | natural_groups | owner
        71936204853172 | account | natural_groups | friend
    """, ["c1"])
    assert [r.verdict for r in results] == ["exact", "wrong_label", "partial", "missed"]
    assert "ambiguous" in results[1].detail and "phone" in results[1].detail
    assert results[2].found.value == "4729" and "edit distance 1" in results[2].detail
    assert results[3].found is None


def test_only_the_full_value_counts_as_captured(tmp_path):
    make_folder(tmp_path, "c1", {"phone": [finding("phone", "0000274196")], "case": [finding("case", "991234")]})
    results, _ = run(tmp_path, """
        0000274196 | phone | natural_groups | owner
        1234 | case | natural_groups | owner
    """, ["c1"])
    assert results[0].verdict == "exact"
    assert results[1].verdict == "partial" and results[1].found.value == "991234"           # a longer string that merely contains it is not a capture
    t = e1.tally(results)["all"]
    assert t["full_value"] == 1 and t["attempted"] == 2
    longer, _ = run(tmp_path, "00002741961 | phone | natural_groups | owner\n", ["c1"])
    assert longer[0].verdict == "partial" and longer[0].found.value == "0000274196"        # nor is a shorter piece of a longer number


def test_leading_zeros_matter_and_spaces_and_hyphens_in_the_expected_number_are_ignored(tmp_path):
    make_folder(tmp_path, "c1", {"phone": [finding("phone", "0000274196")], "ambiguous": [finding("ambiguous", "274196")]})
    results, _ = run(tmp_path, "0000 274-196 | phone | natural_groups | owner\n", ["c1"])
    assert results[0].verdict == "exact"
    results2, _ = run(tmp_path, "00274196 | phone | natural_groups | owner\n", ["c1"])
    assert results2[0].verdict == "partial"                                                  # a different number: not the same value


def test_a_piece_of_less_than_half_the_digits_is_a_miss(tmp_path):
    make_folder(tmp_path, "c1", {"ambiguous": [finding("ambiguous", "7419", "none")]})
    results, _ = run(tmp_path, "0000274196 | phone | one_digit_per_pause | owner\n", ["c1"])
    assert results[0].verdict == "missed"                                                    # 4 shared digits of 10 (needs 5)


# ---------------------------------------------------------------- searching, using each finding once

def test_the_number_is_looked_for_in_every_call_given_or_only_the_named_one(tmp_path):
    make_folder(tmp_path, "c1", {"phone": [finding("phone", "0000274196")]})
    make_folder(tmp_path, "c2", {"case": [finding("case", "47291")]})
    results, _ = run(tmp_path, "47291 | case | natural_groups | owner\n", ["c1", "c2"])
    assert results[0].verdict == "exact" and results[0].found.call == "c2"
    results, _ = run(tmp_path, "47291 | case | natural_groups | owner | c1\n", ["c1", "c2"])
    assert results[0].verdict == "missed"


def test_a_number_listed_twice_in_one_call_matches_once_and_says_why(tmp_path):
    make_folder(tmp_path, "c1", {"case": [finding("case", "47291")]})
    results, warnings = run(tmp_path, "47291 | case | natural_groups | owner\n47291 | case | natural_groups | friend\n", ["c1"])
    assert [r.verdict for r in results] == ["exact", "missed"] and warnings and "only one of them can be matched" in warnings[0]


# ---------------------------------------------------------------- the counts

def test_counts_by_style_speaker_and_label_with_failures_listed(tmp_path):
    make_folder(tmp_path, "c1", {"phone": [finding("phone", "0000274196"), finding("phone", "0000111222")],
                                 "ambiguous": [finding("ambiguous", "8120364", "none")], "case": [finding("case", "47291")]})
    text = """
        0000274196 | phone | natural_groups | owner
        0000111222 | phone | one_digit_per_pause | owner
        8120364 | case | one_digit_per_pause | friend
        47291 | case | natural_groups | friend
        71936204853172 | account | natural_groups | friend
    """
    results, warnings = run(tmp_path, text, ["c1"])
    t = e1.tally(results)
    assert t["all"]["attempted"] == 5 and t["all"]["exact"] == 3 and t["all"]["wrong_label"] == 1
    assert t["all"]["missed"] == 1 and t["all"]["full_value"] == 4
    assert t["style"]["natural_groups"]["attempted"] == 3 and t["style"]["natural_groups"]["exact"] == 2
    assert t["style"]["one_digit_per_pause"]["attempted"] == 2 and t["style"]["one_digit_per_pause"]["exact"] == 1
    assert t["speaker"]["owner"]["exact"] == 2 and t["speaker"]["friend"]["attempted"] == 3 and t["speaker"]["friend"]["full_value"] == 2
    out = e1.report(results, warnings, ["c1"])
    assert out.startswith("STAGED, SMALL-SAMPLE DEV DATA") and "Not captured exactly:" in out and "8120364" in out and "71936204853172" in out
    assert "full value 4 of 5 attempted; exact 3 of 5" in out and "by style:" in out and "by speaker:" in out
    assert "%" not in out                                                                     # counts only, no rate on a sample this small


def test_a_run_with_every_number_exact_says_so(tmp_path):
    make_folder(tmp_path, "c1", {"case": [finding("case", "47291")]})
    results, w = run(tmp_path, "47291 | case | natural_groups | owner\n", ["c1"])
    assert "Every number was captured exactly." in e1.report(results, w, ["c1"])


# ---------------------------------------------------------------- only the logged findings are used

def test_the_extractor_is_not_run_again(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("E1 scoring must read the logged findings, not re-run the extractor")
    monkeypatch.setattr(extract_module, "extract_intel", boom)
    make_folder(tmp_path, "c1", {"case": [finding("case", "47291")]})
    results, _ = run(tmp_path, "47291 | case | natural_groups | owner\n", ["c1"])
    assert results[0].verdict == "exact"
    imports = [l for l in Path(e1.__file__).read_text(encoding="utf-8").splitlines() if l.startswith(("import ", "from "))]
    assert not any("mirage.extract" in l or "mirage.fsm" in l for l in imports)           # the module does not even import the extractor


def test_non_numeric_findings_are_ignored(tmp_path):
    make_folder(tmp_path, "c1", {"vpa": [finding("vpa", "ravi@mirageupi", "heard")], "ifsc": [finding("ifsc", "MIRG0000456", "heard")]})
    assert e1.load_findings("c1", tmp_path / "calls") == []


# ---------------------------------------------------------------- bad input and the template

@pytest.mark.parametrize("line", [
    "47291 | case | natural_groups",                         # missing speaker
    "47x91 | case | natural_groups | owner",                 # not digits
    "47291 | caze | natural_groups | owner",                 # unknown label
    "47291 | case | | owner",                                # empty style
])
def test_bad_lines_are_rejected_with_the_line_number(tmp_path, line):
    with pytest.raises(ValueError, match="line 1"):
        e1.parse_expected(expected_file(tmp_path, line + "\n"))


def test_the_template_has_no_active_lines_and_its_examples_parse_once_uncommented(tmp_path):
    expected, warnings = e1.parse_expected(TEMPLATE)
    assert expected == [] and warnings == []                                                  # every example is commented out
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "number | label | style | speaker" in text and "one_digit_per_pause" in text and "natural_groups" in text
    examples = [l[1:].strip() for l in text.splitlines() if l.startswith("# ") and l.count("|") >= 3 and "number | label" not in l]
    assert len(examples) == 6
    parsed, _ = e1.parse_expected(expected_file(tmp_path, "\n".join(examples) + "\n"))
    assert [e.label for e in parsed] == ["phone", "phone", "case", "case", "account", "account"] and parsed[-1].call == "20261104_101500_ab12"


def test_a_missing_findings_file_names_the_path(tmp_path):
    with pytest.raises(FileNotFoundError, match="findings.json"):
        e1.load_findings("nope", tmp_path / "calls")


# ---------------------------------------------------------------- the command line

def test_command_line_prints_the_banner_and_writes_json(tmp_path, capsys):
    make_folder(tmp_path, "c1", {"case": [finding("case", "47291")]})
    exp = expected_file(tmp_path, "47291 | case | natural_groups | owner\n8120364 | case | one_digit_per_pause | friend\n")
    out = tmp_path / "r.json"
    assert e1.main([str(exp), "c1", "--calls-dir", str(tmp_path / "calls"), "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "STAGED, SMALL-SAMPLE DEV DATA" in text and "full value 1 of 2 attempted" in text
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["counts"]["all"]["attempted"] == 2 and "STAGED" in data["label"] and [r["verdict"] for r in data["results"]] == ["exact", "missed"]


def test_a_call_named_in_the_file_but_not_on_the_command_line_is_refused(tmp_path, capsys):
    make_folder(tmp_path, "c1", {"case": [finding("case", "47291")]})
    exp = expected_file(tmp_path, "47291 | case | natural_groups | owner | c9\n")
    assert e1.main([str(exp), "c1", "--calls-dir", str(tmp_path / "calls")]) == 1
    assert "c9" in capsys.readouterr().out


def test_an_empty_expected_file_is_refused(tmp_path, capsys):
    make_folder(tmp_path, "c1", {})
    assert e1.main([str(expected_file(tmp_path, "# nothing yet\n")), "c1", "--calls-dir", str(tmp_path / "calls")]) == 1
    assert "no numbers yet" in capsys.readouterr().out
