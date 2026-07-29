"""Deterministic, offline tests for the verifier CLI and JSON schema (1.1).

The Anthropic boundary is mocked at client.messages.create (via the batching
test's protocol-speaking brain), so the real engine, classification, batching,
and evidence flow run without any network. These tests cover CLI argument
handling, JSON-on-stdout / logs-on-stderr separation, exit codes, the versioned
schema and its new batch fields, and the optional per-batch transcript.
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
from tests.test_batching import Brain, _patched, _synthetic, _write
from tests.test_ingestion import _make_pdf, _docx_paragraphs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp()
BLANK_PDF = os.path.join(_TMP, "blank.pdf")
with open(BLANK_PDF, "wb") as _fh:
    _fh.write(_make_pdf(["", ""]))


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    code = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as exc:  # argparse --help / --schema-version
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def _complete(fmt="txt", extra=None, genuine=6, structural=4, brain=None):
    """Run the CLI on a small synthetic report of the given format, mocked."""
    text, xlsx = _synthetic(genuine=genuine, structural=structural)
    genuine_lines = [l for l in text.split("\n\n") if l.startswith("Metric")]
    if fmt == "txt":
        report_bytes = text.encode("utf-8")
    elif fmt == "docx":
        report_bytes = _docx_paragraphs(genuine_lines)
    else:
        report_bytes = _make_pdf([" ".join(genuine_lines)])
    with tempfile.TemporaryDirectory() as tmp:
        report = _write(tmp, f"report.{fmt}", report_bytes)
        wb = _write(tmp, "w.xlsx", xlsx)
        argv = ["--report", report, "--workbook", wb, "--batch-size", "5"] + (extra or [])
        out, err = io.StringIO(), io.StringIO()
        with _patched(brain or Brain()):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


# --- CLI basics --------------------------------------------------------------

def test_help_mentions_transcript_confidentiality():
    code, out, _ = _run(["--help"])
    assert code == 0
    assert "usage" in out.lower() and "confidential" in out.lower()


def test_schema_version_is_1_1():
    code, out, _ = _run(["--schema-version"])
    assert code == 0 and out.strip() == "1.1"


def test_valid_json_on_stdout_logs_on_stderr():
    code, out, err = _complete()
    assert code == 0
    data = json.loads(out)
    assert data["schema_version"] == "1.1" and data["status"] == "completed"
    assert "Batch" in err and "tool:" not in out


def test_output_file_keeps_stdout_clean():
    text, xlsx = _synthetic(genuine=6, structural=4)
    with tempfile.TemporaryDirectory() as tmp:
        report = _write(tmp, "r.txt", text.encode("utf-8"))
        wb = _write(tmp, "w.xlsx", xlsx)
        target = os.path.join(tmp, "result.json")
        out = io.StringIO()
        with _patched(Brain()):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["--report", report, "--workbook", wb,
                                 "--batch-size", "5", "--output", target])
        assert code == 0 and out.getvalue().strip() == ""
        with open(target) as fh:
            assert json.load(fh)["schema_version"] == "1.1"


# --- Exit codes --------------------------------------------------------------

def test_invalid_input_exit_2():
    code, _out, err = _run(["--report", os.path.join(_TMP, "missing.txt"),
                            "--workbook", os.path.join(ROOT, "sample_data", "source_data.xlsx")])
    assert code == 2 and "not found" in err.lower()


def test_ingestion_failure_exit_3():
    code, _out, err = _run(["--report", BLANK_PDF,
                            "--workbook", os.path.join(ROOT, "sample_data", "source_data.xlsx")])
    assert code == 3 and "scanned" in err.lower()


# --- Ingestion reaches the shared layer -------------------------------------

def test_docx_and_pdf_reach_shared_ingestion():
    for fmt in ("docx", "pdf"):
        code, out, _ = _complete(fmt=fmt)
        assert code == 0
        assert json.loads(out)["input"]["report_type"] == fmt


# --- Schema 1.1 fields -------------------------------------------------------

def test_schema_carries_batch_and_accounting_fields():
    _code, out, _ = _complete(genuine=8, structural=6)
    data = json.loads(out)
    assert data["candidates_extracted"] >= data["accepted_claim_count"]
    assert "excluded_candidates" in data and "counts" in data["excluded_candidates"]
    assert "batches" in data and data["batches"]
    assert "evidence_index" in data
    assert data["settings"]["batch_size"] == 5
    for f in data["findings"]:
        assert f["batch_id"], "finding missing batch_id"
        for key in ("match_id", "source_value_id", "comparison_id", "source_cell"):
            assert f[key], f"verifiable finding missing {key}"


# --- Transcript --------------------------------------------------------------

def test_transcript_absent_by_default():
    _code, out, _ = _complete()
    data = json.loads(out)
    assert data["transcript_included"] is False and "transcript" not in data


def test_transcript_present_grouped_by_batch_with_flag():
    _code, out, _ = _complete(genuine=8, extra=["--include-transcript"])
    data = json.loads(out)
    assert data["transcript_included"] is True
    assert data["transcript"] and all("batch_id" in g and "entries" in g
                                       for g in data["transcript"])


def test_transcript_redacts_secrets():
    _code, out, _ = _complete(extra=["--include-transcript"],
                              brain=Brain(secret="sk-ant-LEAKED123"))
    assert "sk-ant-LEAKED123" not in out and "[redacted]" in out


def test_transcript_does_not_change_findings():
    _c1, plain, _ = _complete()
    _c2, witht, _ = _complete(extra=["--include-transcript"])
    assert json.loads(plain)["findings"] == json.loads(witht)["findings"]


# --- Same engine entry point -------------------------------------------------

def test_streamlit_and_cli_use_same_engine():
    assert cli.verify_report is engine.verify_report
    import app
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
