"""Deterministic tests for structured source evidence and sheet counting.

Run with:  python -m tests.test_source_evidence   (from the repo root)
No network or API access is required.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openpyxl

from verifier.agent import _apply_log_finding
from verifier.tools import (
    SourceWorkbook,
    FindingsLog,
    _excel_cell_ref,
    resolve_source_sheet,
    sheets_referenced,
    with_cell_prefix,
    mismatch_line,
)


def _make_workbook() -> str:
    """A small workbook with plain, spaced, and apostrophe'd sheet names."""
    wb = openpyxl.Workbook()
    sales = wb.active
    sales.title = "Sales"
    sales.append(["Region", "2024", "2025", "2026"])
    sales.append(["Manchester", 90, 93, 95])

    customers = wb.create_sheet("Customers")
    customers.append(["Segment", "Count"])
    customers.append(["Enterprise", 1200])

    regional = wb.create_sheet("Regional Sales")
    regional.append(["Area", "Value"])
    regional.append(["North", 42])

    path = os.path.join(tempfile.mkdtemp(), "test_book.xlsx")
    wb.save(path)
    return path


WB_PATH = _make_workbook()


def _finding(**kw) -> dict:
    base = {
        "verdict": "match",
        "sheet": "",
        "cell": "",
        "source_location": "",
        "reported_value": "",
        "source_value": "",
    }
    base.update(kw)
    return base


# --- 1 & 2: structured fields present via valid source_match_id resolution ----

def test_valid_match_id_populates_structured_fields():
    wb = SourceWorkbook(WB_PATH)
    hits = wb.find_value("Manchester")
    assert hits, "expected a match for Manchester"
    hit = hits[0]
    for key in ("match_id", "sheet", "cell", "row_context", "header_row"):
        assert key in hit, f"find_in_spreadsheet result missing {key}"
    assert hit["sheet"] == "Sales"
    assert hit["cell"] == "Sales!A2"

    log = FindingsLog()
    res = _apply_log_finding(
        {
            "claim_id": "C1", "sentence": "Manchester total 95",
            "reported_value": "95", "source_value": "95",
            "source_location": "Sales sheet, row 2 (Manchester)",
            "verdict": "match", "source_match_id": hit["match_id"],
        },
        wb, log,
    )
    assert res.get("logged"), res
    f = log.to_dicts()[0]
    assert f["sheet"] == "Sales"
    assert f["cell"] == "Sales!A2"
    assert f["source_mapping_status"] == "structured"
    # authoritative cell prepended to the human-readable explanation
    assert f["source_location"].startswith("Sales!A2;")


# --- 3: rejection of an unknown / fabricated source_match_id -----------------

def test_unknown_match_id_is_rejected():
    wb = SourceWorkbook(WB_PATH)
    wb.find_value("Manchester")  # issues at least one real match id
    log = FindingsLog()
    res = _apply_log_finding(
        {
            "claim_id": "C1", "sentence": "x", "reported_value": "95",
            "source_value": "95", "source_location": "Sales sheet",
            "verdict": "match", "source_match_id": "match_9999",
        },
        wb, log,
    )
    assert "error" in res, "fabricated match_id should be rejected"
    assert len(log.findings) == 0, "rejected finding must not be logged"


# --- 4: sheet names with spaces and apostrophes ------------------------------

def test_special_sheet_names_are_quoted():
    assert _excel_cell_ref("Regional Sales", 3, 4) == "'Regional Sales'!D3"
    assert _excel_cell_ref("O'Brien", 1, 1) == "'O''Brien'!A1"
    wb = SourceWorkbook(WB_PATH)
    hits = wb.find_value("North")
    assert hits and hits[0]["cell"].startswith("'Regional Sales'!"), hits


# --- 5: multiple findings from one sheet count that sheet once ----------------

def test_same_sheet_counted_once():
    findings = [
        _finding(sheet="Sales", source_location="Sales!A2"),
        _finding(sheet="Sales", source_location="Sales!D2"),
    ]
    assert sheets_referenced(findings) == ["Sales"]


# --- 6: Sales + Customers give a sheet count of 2 (production example) --------

def test_two_distinct_sheets_count_two():
    findings = [
        _finding(sheet="Sales"),
        _finding(sheet="Sales"),
        _finding(sheet="Customers"),
        _finding(sheet="Customers"),
        _finding(sheet="Sales"),
        _finding(sheet="Customers"),
        _finding(sheet="Sales"),
        _finding(verdict="unverifiable", sheet="",
                 source_location="Not present in spreadsheet"),
    ]
    assert len(findings) == 8
    assert len(sheets_referenced(findings)) == 2


# --- 7: unverifiable findings contribute no source sheet ---------------------

def test_unverifiable_not_counted():
    findings = [
        _finding(verdict="unverifiable", sheet="",
                 source_location="Not present in spreadsheet"),
    ]
    assert sheets_referenced(findings) == []
    assert resolve_source_sheet(findings[0]) == ("", "n/a")


# --- 8: legacy finding with parseable source_location uses fallback ----------

def test_legacy_source_location_fallback():
    legacy = {
        "verdict": "match",
        "source_location": "Sales sheet, row 3 (Manchester GBP k), 2026 column",
    }
    sheet, status = resolve_source_sheet(legacy)
    assert (sheet, status) == ("Sales", "fallback")
    assert sheets_referenced([legacy]) == ["Sales"]


# --- 9: no structured and no parseable evidence -> under_specified ------------

def test_under_specified_when_no_evidence():
    finding = {"verdict": "match", "source_location": "row 3, 2026 column"}
    assert resolve_source_sheet(finding) == ("", "under_specified")
    assert sheets_referenced([finding]) == []


# --- 10: cell prefix is not duplicated ---------------------------------------

def test_cell_prefix_not_duplicated():
    already = "Sales!D3; Sales sheet, row 3"
    assert with_cell_prefix("Sales!D3", already) == already
    prefixed = with_cell_prefix("Sales!D3", "Sales sheet, row 3")
    assert prefixed == "Sales!D3; Sales sheet, row 3"
    # idempotent
    assert with_cell_prefix("Sales!D3", prefixed) == prefixed
    # a different leading cell is replaced, not stacked
    assert with_cell_prefix("Sales!D3", "Sales!D4; Sales sheet") == "Sales!D3; Sales sheet"


# --- 11: scaled units are preserved in mismatch banner text ------------------

def test_scaled_units_preserved_in_banner():
    from_reported = mismatch_line(_finding(
        verdict="mismatch", reported_value="99k", source_value="95",
        source_location="Sales!D3; Sales sheet, row 3 (Manchester GBP k), 2026 column",
    ))
    assert "spreadsheet says 95k." in from_reported, from_reported

    from_context = mismatch_line(_finding(
        verdict="mismatch", reported_value="99", source_value="95",
        source_location="Sales!D3; Sales sheet, row 3 (Manchester GBP k), 2026 column",
    ))
    assert "spreadsheet says 95k." in from_context, from_context


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
