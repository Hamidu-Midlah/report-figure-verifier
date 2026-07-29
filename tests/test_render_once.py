"""Structural test: a completed run renders each results element exactly once.

Uses Streamlit AppTest with a completed run injected into session_state (no API
call), and confirms that changing the findings filter does not duplicate the
table or download controls. Run with:  python -m tests.test_render_once
"""

import contextlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic
from streamlit.testing.v1 import AppTest

import verifier.engine as engine_mod
from verifier.engine import verify_report, EngineOptions
from tests.test_batching import Brain, _patched, _synthetic, _write

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")
SECRET = "sk-ant-SECRET"


def _finding(claim_id, verdict, sheet, source_cell):
    return {
        "claim_id": claim_id, "sentence": f"claim {claim_id}",
        "reported_value": "36", "source_value": "39",
        "source_location": f"{source_cell}; sheet" if source_cell else "Not present in spreadsheet",
        "verdict": verdict, "note": "",
        "sheet": sheet, "source_cell": source_cell, "label_cell": "",
        "source_value_id": "srcval_0001" if verdict != "unverifiable" else "",
        "comparison_id": "cmp_0001" if verdict != "unverifiable" else "",
        "source_mapping_status": "structured" if verdict != "unverifiable" else "n/a",
    }


FAKE_RUN = {
    "run_id": "abcd1234",
    "timestamp": "2026-07-27 00:00:00 UTC",
    "model": "claude-sonnet-4-6",
    "report_name": "sample_data/draft_report.md",
    "xlsx_name": "sample_data/source_data.xlsx",
    "report_text": "# Demo report\n\nIn 2026, 36% of firms scaled AI.",
    "findings": [
        _finding("C001", "mismatch", "Adoption", "Adoption!D2"),
        _finding("C002", "match", "Adoption", "Adoption!B2"),
        _finding("C011", "unverifiable", "", ""),
    ],
    "summary_text": "1 mismatch, 1 match, 1 unverifiable.",
    "summary_counts": {"mismatch": 1, "match": 1, "unverifiable": 1},
    "transcript": [{"role": "assistant", "text": "done"}],
}


def _fresh_app():
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["run"] = FAKE_RUN
    at.run()
    return at


def _subheader_values(at):
    return [s.value for s in at.subheader]


def _expander_labels(at):
    return [e.label for e in at.get("expander")]


def _assert_single(at, context):
    assert len(at.exception) == 0, f"{context}: exception {list(at.exception)}"
    assert len(at.get("download_button")) == 4, \
        f"{context}: {len(at.get('download_button'))} download buttons, expected 4"
    assert len(at.get("radio")) == 1, \
        f"{context}: {len(at.get('radio'))} filter radios, expected 1"
    assert len(at.get("metric")) == 4, \
        f"{context}: {len(at.get('metric'))} metrics, expected 4"
    assert len(at.dataframe) == 1, \
        f"{context}: {len(at.dataframe)} dataframes, expected 1"

    subs = _subheader_values(at)
    assert subs.count("Summary") == 1, f"{context}: Summary x{subs.count('Summary')}"
    assert subs.count("Findings (human review required)") == 1, \
        f"{context}: Findings x{subs.count('Findings (human review required)')}"
    assert subs.count("Evidence-grounded report verification") == 1, \
        f"{context}: tagline x{subs.count('Evidence-grounded report verification')}"

    exps = _expander_labels(at)
    for label in ("Run information", "Report under review",
                  "Agent transcript (for audit)"):
        assert exps.count(label) == 1, f"{context}: {label} x{exps.count(label)}"


def test_single_render_on_completed_run():
    _assert_single(_fresh_app(), "initial render")


def test_filter_change_does_not_duplicate():
    at = _fresh_app()
    at.get("radio")[0].set_value("All findings").run()
    _assert_single(at, "after 'All findings'")
    at.get("radio")[0].set_value("Mismatches").run()
    _assert_single(at, "after 'Mismatches'")
    at.get("radio")[0].set_value("Issues only").run()
    _assert_single(at, "back to 'Issues only'")


def test_no_results_without_a_run():
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()  # no completed run in session_state
    assert len(at.exception) == 0
    assert len(at.get("download_button")) == 0
    assert len(at.dataframe) == 0
    assert "Findings (human review required)" not in _subheader_values(at)


# --- Upstream failure surfaces cleanly and keeps completed-batch findings -----

def _failed_upstream_run():
    """A real run where b01 (3 claims) completes and b02 fails upstream."""
    text, xlsx = _synthetic(genuine=6, structural=4)
    err = anthropic.RateLimitError.__new__(anthropic.RateLimitError)
    Exception.__init__(err, f"rate limited {SECRET}")
    with tempfile.TemporaryDirectory() as tmp:
        report = _write(tmp, "r.txt", text)
        wb = _write(tmp, "w.xlsx", xlsx)
        with _patched(Brain(fail_on_batch=2, error=err)):
            return verify_report(report, wb, EngineOptions(batch_size=3))


@contextlib.contextmanager
def _engine_returns(run):
    """Replace the app's engine call with a counting stub returning `run`."""
    original = engine_mod.verify_report_from_bytes
    calls = {"n": 0}

    def stub(*args, **kwargs):
        calls["n"] += 1
        return run

    engine_mod.verify_report_from_bytes = stub
    try:
        yield calls
    finally:
        engine_mod.verify_report_from_bytes = original


def test_upstream_failure_is_clean_and_preserves_completed_findings():
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    run = _failed_upstream_run()
    assert run.status == "failed" and run.error_kind == "upstream"
    assert sum(1 for f in run.findings if f["batch_id"] == "b01") == 3

    with _engine_returns(run) as calls:
        at = AppTest.from_file(APP, default_timeout=60)
        at.run()
        at.checkbox[0].set_value(True)  # explicitly select bundled sample data
        assert at.checkbox[0].value is True
        at.button[0].click()
        at.run()

    assert calls["n"] == 1, f"engine called {calls['n']} times"
    assert len(at.exception) == 0, f"raw traceback surfaced: {list(at.exception)}"

    errors = [e.value for e in at.error]
    status_labels = [s.label or "" for s in at.status]
    assert any("Verification failed" in e and "upstream" in e for e in errors), errors
    assert all("Verification complete" not in l for l in status_labels), status_labels

    sess = at.session_state["run"]
    assert sess["run_status"] == "failed"
    sess_b01 = [f for f in sess["findings"] if f["batch_id"] == "b01"]
    assert len(sess["findings"]) == 3 and len(sess_b01) == 3, sess["findings"]

    visible = errors + status_labels
    visible += [e.value for e in at.warning]
    visible += [e.value for e in at.markdown]
    visible += [e.value for e in at.caption]
    assert all(SECRET not in t for t in visible), "secret leaked into visible text"


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
