"""Deterministic tests for numeric-candidate relevance classification.

A realistic extracted-text fixture mixes document-structure noise with genuine
quantitative claims. We run the real extractor over it and assert the classifier
excludes structure, definitions, and scoping dates with correct categories and
reasons, while accepting every genuine quantity, and telling a scale definition
apart from a result that happens to span the same range.
Run with:  python -m tests.test_classify
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from verifier.tools import extract_numeric_claims
from verifier.classify import classify, classify_candidates, exclusion_counts

FIXTURE = """Contents

Introduction .......... 3

Methods .......... 7

3.2 Data cleaning

Figure 4 shows the distribution of scores.

Table 2 summarises the results.

Student ID: 20481234

Page 12

The scale ranges from 0 to 4, anchoring the rating instrument.

Scores ranged from 0 to 4 after handling missing values.

In 2019, data collection began.

On 5 July, the survey closed.

Adoption reached 62% among the 480 respondents.

The mean score was 3.7 and total revenue was 5.2 billion.
"""


def _split():
    candidates = extract_numeric_claims(FIXTURE)
    accepted, excluded = classify_candidates(candidates)
    return candidates, accepted, excluded


def _by_sentence(items, needle):
    return [i for i in items if needle in i["sentence"]]


# --- unit rules --------------------------------------------------------------

def test_rule_examples():
    assert classify("3", "Introduction .......... 3")[0] == "structural"
    assert classify("3.2", "3.2 Data cleaning")[0] == "structural"
    assert classify("4", "Figure 4 shows the distribution.")[0] == "structural"
    assert classify("2", "Table 2 summarises the results.")[0] == "structural"
    assert classify("20481234", "Student ID: 20481234")[0] == "structural"
    assert classify("12", "Page 12")[0] == "structural"
    assert classify("2019", "In 2019, data collection began.")[0] == "contextual"
    assert classify("5", "On 5 July, the survey closed.")[0] == "contextual"
    assert classify("62", "Adoption reached 62% among respondents.")[0] == "accepted"


# --- definitional vs result --------------------------------------------------

def test_scale_definition_excluded_result_accepted():
    _c, accepted, excluded = _split()
    # the definitional "0 to 4" is excluded as definitional
    defn = _by_sentence(excluded, "The scale ranges from 0 to 4")
    assert defn, "scale definition candidates were not excluded"
    assert all(e["category"] == "definitional" for e in defn)
    # the result "0 to 4" is accepted
    result = _by_sentence(accepted, "Scores ranged from 0 to 4 after handling")
    assert result, "result range was not accepted"
    assert {r["figure"] for r in result} == {"0", "4"}


# --- every excluded candidate keeps a category and reason --------------------

def test_all_exclusions_have_category_and_reason():
    _c, _accepted, excluded = _split()
    for item in excluded:
        assert item["category"] in ("structural", "definitional", "contextual")
        assert item["reason"], f"missing reason for {item}"
        assert "claim_id" in item and "sentence" in item and "figure" in item
    # nothing silently dropped: accepted + excluded == all candidates
    candidates, accepted, excluded = _split()
    assert len(accepted) + len(excluded) == len(candidates)


# --- genuine quantities are never filtered ----------------------------------

def test_genuine_quantities_accepted():
    _c, accepted, _excluded = _split()
    figures = {a["figure"] for a in accepted}
    for genuine in ("62%", "480", "3.7", "5.2 billion"):
        assert genuine in figures, f"{genuine} was wrongly filtered"


# --- structure, definitions, and scoping dates are all excluded -------------

def test_structure_and_dates_excluded():
    _c, accepted, excluded = _split()
    excluded_figs = [(e["figure"], e["category"]) for e in excluded]
    # ToC page numbers, section number, figure/table labels, id, page, year, date
    cats = {e["category"] for e in excluded}
    assert {"structural", "definitional", "contextual"} <= cats
    counts = exclusion_counts(excluded)
    assert counts["structural"] >= 5      # toc x2, section, figure, table, id, page
    assert counts["definitional"] >= 2    # 0 and 4 of the scale definition
    assert counts["contextual"] >= 2      # 2019 and the 5 March day
    accepted_figs = {a["figure"] for a in accepted}
    # none of these structural tokens leaked into accepted
    assert "20481234" not in accepted_figs
    assert "2019" not in accepted_figs


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
