"""Deterministic, offline tests for the verifier CLI and JSON schema.

The verification loop (the only part that needs the Anthropic API) is replaced
with a canned result, so these tests are fast and network-free. They exercise
the CLI argument handling, JSON-on-stdout / logs-on-stderr separation, exit
codes, schema serialisation, evidence chain, and the optional transcript.
Run with:  python -m tests.test_cli
"""

import contextlib
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from verifier import cli, engine
from tests.test_ingestion import _make_pdf, _docx_paragraphs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XLSX = os.path.join(ROOT, "sample_data", "source_data.xlsx")
_TMP = tempfile.mkdtemp()


def _fixture(name, data: bytes):
    path = os.path.join(_TMP, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


TXT_REPORT = _fixture("report.txt", b"In 2026, adoption reached 36%.")
DOCX_REPORT = _fixture("report.docx", _docx_paragraphs(["Adoption reached 36%."]))
PDF_REPORT = _fixture("report.pdf", _make_pdf(["Adoption reached 36% this year."]))
BLANK_PDF = _fixture("blank.pdf", _make_pdf(["", ""]))


def _result(hit_turn_limit=False):
    return {
        "findings": [
            {"claim_id": "C001", "sentence": "In 2026, 36% scaled AI",
             "reported_value": "36%", "source_value": "39",
             "source_location": "Adoption!D2; Adoption sheet, 2026 column",
             "verdict": "mismatch", "note": "", "sheet": "Adoption",
             "source_cell": "Adoption!D2", "label_cell": "Adoption!A2",
             "match_id": "match_0001", "source_value_id": "srcval_0001",
             "comparison_id": "cmp_0001", "difference": 3, "tolerance": 0.5,
             "source_mapping_status": "structured"},
            {"claim_id": "C002", "sentence": "USD 200 billion by 2028",
             "reported_value": "200 billion", "source_value": "",
             "source_location": "Not present in spreadsheet",
             "verdict": "unverifiable", "note": "external forecast", "sheet": "",
             "source_cell": "", "label_cell": "", "match_id": "",
             "source_value_id": "", "comparison_id": "", "difference": None,
             "tolerance": None, "source_mapping_status": "n/a"},
        ],
        "summary": {"mismatch": 1, "unverifiable": 1},
        "claims": [{"claim_id": "C001", "sentence": "s1", "figure": "36%"},
                   {"claim_id": "C002", "sentence": "s2", "figure": "200 billion"}],
        "claims_extracted": 2,
        "transcript": [
            {"tool": "find_in_spreadsheet",
             "input": {"query": "scaling", "api_key": "sk-ant-SHOULD-NOT-APPEAR"},
             "result_preview": "match_0001"},
            {"role": "assistant",
             "text": "Done. Leaked token sk-ant-SECRETVALUE123 must be redacted."},
        ],
        "tool_calls": 4, "turns": 3, "hit_turn_limit": hit_turn_limit,
        "evidence": {"comparisons": {
            "cmp_0001": {"source_cell": "Adoption!D2", "source_value_id": "srcval_0001"}}},
        "model": "claude-sonnet-4-6",
    }


@contextlib.contextmanager
def _patched(result):
    original = engine.run_verification

    def fake(*args, **kwargs):
        callback = kwargs.get("progress_callback")
        if callback:  # engine forwards its progress hook; exercise it
            callback("find_in_spreadsheet", {"query": "scaling"})
        return dict(result, model=kwargs.get("model") or result["model"])

    engine.run_verification = fake
    try:
        yield
    finally:
        engine.run_verification = original


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    code = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as exc:  # argparse --help / --schema-version
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def _complete(extra_argv=None, report=TXT_REPORT):
    argv = ["--report", report, "--workbook", XLSX] + (extra_argv or [])
    with _patched(_result()):
        return _run(argv)


# --- CLI basics --------------------------------------------------------------

def test_help_mentions_transcript_confidentiality():
    code, out, _ = _run(["--help"])
    assert code == 0
    assert "usage" in out.lower()
    assert "confidential" in out.lower()


def test_schema_version_flag():
    code, out, _ = _run(["--schema-version"])
    assert code == 0
    assert out.strip() == "1.0"


def test_valid_json_on_stdout_and_logs_on_stderr():
    code, out, err = _complete()
    assert code == 0
    data = json.loads(out)  # stdout is valid JSON
    assert data["schema_version"] == "1.0"
    assert data["status"] == "completed"
    # logs went to stderr only, never into stdout
    assert "tool:" in err and "completed" in err.lower()
    assert "tool:" not in out


def test_output_file_keeps_stdout_clean():
    target = os.path.join(_TMP, "result.json")
    code, out, _ = _complete(extra_argv=["--output", target])
    assert code == 0
    assert out.strip() == ""  # nothing on stdout when writing a file
    with open(target) as fh:
        assert json.load(fh)["schema_version"] == "1.0"


# --- Exit codes --------------------------------------------------------------

def test_invalid_input_exit_code():
    code, _, err = _run(["--report", os.path.join(_TMP, "missing.txt"),
                         "--workbook", XLSX])
    assert code == 2
    assert "not found" in err.lower()


def test_ingestion_failure_exit_code():
    code, _, err = _run(["--report", BLANK_PDF, "--workbook", XLSX])
    assert code == 3
    assert "scanned" in err.lower()


def test_incomplete_verification_exit_code():
    argv = ["--report", TXT_REPORT, "--workbook", XLSX]
    with _patched(_result(hit_turn_limit=True)):
        code, out, err = _run(argv)
    assert code == 5
    data = json.loads(out)  # still valid JSON documenting the incompletion
    assert data["status"] == "incomplete"
    assert data["completion"]["complete"] is False
    assert "incomplete" in err.lower()


# --- Ingestion reaches the shared layer -------------------------------------

def test_docx_and_pdf_reach_shared_ingestion():
    for report, expected in ((DOCX_REPORT, "docx"), (PDF_REPORT, "pdf")):
        code, out, _ = _complete(report=report)
        assert code == 0
        assert json.loads(out)["input"]["report_type"] == expected


# --- Schema and evidence chain ----------------------------------------------

def test_findings_schema_and_evidence_chain():
    _code, out, _ = _complete()
    data = json.loads(out)
    findings = {f["claim_id"]: f for f in data["findings"]}
    verifiable = findings["C001"]
    for key in ("match_id", "source_value_id", "comparison_id", "source_cell",
                "difference", "tolerance", "label_cell", "source_mapping_status"):
        assert verifiable.get(key) not in (None, ""), key
    assert verifiable["source_mapping_status"] == "structured"
    unverifiable = findings["C002"]
    for key in ("match_id", "source_value_id", "comparison_id", "source_cell",
                "label_cell"):
        assert not unverifiable.get(key), key


# --- Transcript --------------------------------------------------------------

def test_transcript_absent_by_default():
    _code, out, _ = _complete()
    data = json.loads(out)
    assert data["transcript_included"] is False
    assert "transcript" not in data


def test_transcript_present_only_with_flag():
    _code, out, _ = _complete(extra_argv=["--include-transcript"])
    data = json.loads(out)
    assert data["transcript_included"] is True
    assert isinstance(data["transcript"], list) and data["transcript"]


def test_transcript_never_serialises_secrets():
    _code, out, _ = _complete(extra_argv=["--include-transcript"])
    assert "sk-ant-" not in out
    assert "SHOULD-NOT-APPEAR" not in out
    assert "SECRETVALUE123" not in out
    assert "[redacted]" in out


def test_transcript_does_not_change_findings():
    _c1, out_default, _ = _complete()
    _c2, out_with, _ = _complete(extra_argv=["--include-transcript"])
    assert json.loads(out_default)["findings"] == json.loads(out_with)["findings"]
    assert json.loads(out_default)["verdict_counts"] == json.loads(out_with)["verdict_counts"]


# --- Same engine entry point for Streamlit and CLI --------------------------

def test_streamlit_and_cli_use_same_engine():
    assert cli.verify_report is engine.verify_report
    import app  # imports Streamlit in bare mode; render path is guarded by session state
    assert app.verify_report_from_bytes is engine.verify_report_from_bytes


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
