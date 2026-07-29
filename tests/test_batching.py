"""Deterministic tests for bounded batch verification and merge validation.

The Anthropic boundary is mocked at client.messages.create only, with a brain
that speaks the real tool protocol (find -> select -> compare -> log) so the
actual batch orchestration, shared workbook, evidence prefixing, and merge
validation all run. A production-scale synthetic document proves the per-call
payload stays bounded and independent of total document candidate count.
Run with:  python -m tests.test_batching
"""

import contextlib
import io
import json
import os
import re
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic
import openpyxl

from verifier import agent, cli, engine
from verifier.engine import EngineOptions, verify_report


# ---------------------------------------------------------------------------
# Fake Anthropic response objects and a protocol-speaking brain
# ---------------------------------------------------------------------------

def _tool_use(name, tool_input, tool_id):
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=tool_id)


def _text(value):
    return SimpleNamespace(type="text", text=value)


def _response(blocks, stop_reason):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason)


class Brain:
    """A deterministic stand-in for the model that drives the tool chain."""

    def __init__(self, never_end=False, fail_on_batch=None, error=None, secret=None,
                 truncate_on_batch=None):
        self.never_end = never_end
        self.fail_on_batch = fail_on_batch
        self.error = error
        self.secret = secret  # optional string to leak into the final text
        self.truncate_on_batch = truncate_on_batch  # 1-based batch to run to limit
        self.payloads = []
        self.batch_prompts = []
        self.batch_index = 0  # completed batches so far

    def __call__(self, **kwargs):
        messages = kwargs["messages"]
        self.payloads.append(len(json.dumps(messages, default=str)))
        stage = sum(1 for m in messages if m["role"] == "assistant")
        first = messages[0]["content"]
        claims = json.loads(first[first.index("["):])

        # A brain that never ends its turn: always emit one harmless tool call so
        # the batch runs to its turn limit and is reported as truncated. never_end
        # truncates every batch; truncate_on_batch truncates only the named one
        # (so an earlier batch can complete first).
        truncating = (self.truncate_on_batch is not None
                      and self.batch_index + 1 == self.truncate_on_batch)
        if self.never_end or truncating:
            return _response([_tool_use("list_sheets", {}, "t")], "tool_use")

        if stage == 0:
            self.batch_prompts.append(first)
            if self.fail_on_batch is not None and self.batch_index + 1 == self.fail_on_batch:
                raise self.error
            blocks = [_tool_use("find_in_spreadsheet", {"query": _query(c["sentence"])},
                                f"tf{i}") for i, c in enumerate(claims)]
            return _response(blocks, "tool_use")

        results = [json.loads(tr["content"]) for tr in messages[-1]["content"]]

        if stage == 1:
            blocks = []
            for i, _c in enumerate(claims):
                match = results[i]["matches"][0]
                value_cell = next(cc["cell"] for cc in match["cells"]
                                  if isinstance(cc["value"], (int, float)))
                blocks.append(_tool_use("select_source_cell",
                                        {"match_id": match["match_id"], "cell": value_cell},
                                        f"ts{i}"))
            return _response(blocks, "tool_use")

        if stage == 2:
            blocks = []
            for i, c in enumerate(claims):
                blocks.append(_tool_use("compare_values",
                                        {"reported_value": c["figure"],
                                         "source_value_id": results[i]["source_value_id"]},
                                        f"tc{i}"))
            return _response(blocks, "tool_use")

        if stage == 3:
            # the source_value_id used is read back from this batch's compare calls
            compare_blocks = [b for b in messages[-2]["content"]
                              if getattr(b, "type", None) == "tool_use"]
            blocks = []
            for i, c in enumerate(claims):
                r = results[i]
                blocks.append(_tool_use("log_finding", {
                    "claim_id": c["claim_id"], "sentence": c["sentence"],
                    "reported_value": c["figure"], "source_value": str(r["source_value"]),
                    "source_location": f"{r['sheet']} sheet, {r['row_label']}",
                    "verdict": r["verdict"],
                    "source_value_id": compare_blocks[i].input["source_value_id"],
                }, f"tl{i}"))
            return _response(blocks, "tool_use")

        self.batch_index += 1
        text = "Batch complete."
        if self.secret:
            text += f" (leaked {self.secret})"
        return _response([_text(text)], "end_turn")


def _query(sentence):
    m = re.search(r"Metric [A-Z]+", sentence)
    return m.group(0) if m else sentence


@contextlib.contextmanager
def _patched(brain):
    original = anthropic.Anthropic
    anthropic.Anthropic = lambda *a, **k: SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kw: brain(**kw)))
    try:
        yield
    finally:
        anthropic.Anthropic = original


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------

def _label(i):
    """1-based index to a digit-free label: A, B, ..., Z, AA, AB, ..."""
    s = ""
    while i > 0:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _synthetic(genuine=50, structural=260):
    """A report with `genuine` accepted claims and `structural` excluded lines.

    Genuine claim labels are digit-free (Metric A, Metric B, ...) so the only
    numeric candidate they contribute is the reported percentage.
    """
    lines = []
    for i in range(1, genuine + 1):
        lines.append(f"Metric {_label(i)} reached {40 + i}% in the study.")
    for j in range(1, structural + 1):
        lines.append(f"Table {j} lists supporting items.")
    text = "\n\n".join(lines)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Metric", "Value"])
    for i in range(1, genuine + 1):
        ws.append([f"Metric {_label(i)}", 40 + i])
    buf = io.BytesIO()
    wb.save(buf)
    return text, buf.getvalue()


def _write(tmp, name, data):
    path = os.path.join(tmp, name)
    mode = "w" if isinstance(data, str) else "wb"
    with open(path, mode, encoding="utf-8" if isinstance(data, str) else None) as fh:
        fh.write(data)
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_batches_respect_size_and_prompt_is_scoped():
    text, xlsx = _synthetic(genuine=45, structural=20)
    brain = Brain()
    with tempfile.TemporaryDirectory() as tmp:
        report = _write(tmp, "r.txt", text)
        wb = _write(tmp, "w.xlsx", xlsx)
        with _patched(brain):
            run = verify_report(report, wb, EngineOptions(batch_size=20))
    # 45 accepted -> batches of 20, 20, 5
    assert [b["claim_count"] for b in run.batches] == [20, 20, 5]
    # each batch prompt carries only its own claims and no extraction instruction
    for prompt in brain.batch_prompts:
        claims = json.loads(prompt[prompt.index("["):])
        assert len(claims) <= 20
        assert "do not look for additional claims" in prompt
        assert "extract" not in prompt.lower()


def test_evidence_ids_are_batch_prefixed_and_globally_unique():
    text, xlsx = _synthetic(genuine=30, structural=10)
    with tempfile.TemporaryDirectory() as tmp:
        report = _write(tmp, "r.txt", text)
        wb = _write(tmp, "w.xlsx", xlsx)
        with _patched(Brain()):
            run = verify_report(report, wb, EngineOptions(batch_size=20))
    assert run.status == "completed", run.completion_issues
    seen = set()
    for f in run.findings:
        # all evidence ids are batch-prefixed
        for key in ("match_id", "source_value_id", "comparison_id"):
            assert f[key].startswith(f["batch_id"] + "_"), f[key]
        # the terminal per-finding evidence is globally unique (a match_id may
        # legitimately be shared by several claims from the same row)
        for key in ("source_value_id", "comparison_id"):
            assert f[key] not in seen, f"duplicate evidence id {f[key]}"
            seen.add(f[key])
    # a genuinely multi-batch run
    assert len({f["batch_id"] for f in run.findings}) >= 2


def test_all_accepted_logged_once_and_chain_resolves():
    text, xlsx = _synthetic(genuine=25, structural=15)
    with tempfile.TemporaryDirectory() as tmp:
        report = _write(tmp, "r.txt", text)
        wb = _write(tmp, "w.xlsx", xlsx)
        with _patched(Brain()):
            run = verify_report(report, wb, EngineOptions(batch_size=10))
    assert run.status == "completed", run.completion_issues
    assert run.accepted_count == 25
    claim_ids = [f["claim_id"] for f in run.findings]
    assert len(claim_ids) == len(set(claim_ids)) == 25
    # evidence index resolves for every finding
    index = run.evidence_index["source_values"]
    for f in run.findings:
        assert f["source_value_id"] in index
        assert index[f["source_value_id"]]["batch_id"] == f["batch_id"]


def test_truncated_batch_makes_run_incomplete_and_cli_exit_5():
    text, xlsx = _synthetic(genuine=5, structural=5)
    with tempfile.TemporaryDirectory() as tmp:
        report = _write(tmp, "r.txt", text)
        wb = _write(tmp, "w.xlsx", xlsx)
        with _patched(Brain(never_end=True)):
            run = verify_report(report, wb, EngineOptions(batch_size=5, max_turns=2))
            assert run.status == "incomplete"
            assert any("turn limit" in i for i in run.completion_issues)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["--report", report, "--workbook", wb,
                                 "--batch-size", "5"])
    assert code == 5
    assert json.loads(out.getvalue())["status"] == "incomplete"


def _cli_json(argv, brain):
    out, err = io.StringIO(), io.StringIO()
    with _patched(brain):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def test_authentication_failure_exit_4_preserves_findings():
    text, xlsx = _synthetic(genuine=15, structural=5)
    err = anthropic.AuthenticationError.__new__(anthropic.AuthenticationError)
    Exception.__init__(err, "invalid x-api-key sk-ant-SECRET")
    with tempfile.TemporaryDirectory() as tmp:
        report = _write(tmp, "r.txt", text)
        wb = _write(tmp, "w.xlsx", xlsx)
        # fail on batch 2 so batch 1 findings are preserved
        code, out, err = _cli_json(
            ["--report", report, "--workbook", wb, "--batch-size", "10"],
            Brain(fail_on_batch=2, error=err))
    assert code == 4
    doc = json.loads(out)  # stdout is still valid JSON
    assert doc["status"] == "failed" and doc["error_kind"] == "authentication"
    assert len(doc["findings"]) == 10  # batch 1 completed and is preserved
    assert "sk-ant-SECRET" not in out and "sk-ant-SECRET" not in err  # no secret leaks


def test_other_upstream_failure_exit_6():
    text, xlsx = _synthetic(genuine=15, structural=5)
    err = anthropic.RateLimitError.__new__(anthropic.RateLimitError)
    Exception.__init__(err, "rate limited")
    with tempfile.TemporaryDirectory() as tmp:
        report = _write(tmp, "r.txt", text)
        wb = _write(tmp, "w.xlsx", xlsx)
        code, out, _err = _cli_json(
            ["--report", report, "--workbook", wb, "--batch-size", "10"],
            Brain(fail_on_batch=2, error=err))
    assert code == 6
    doc = json.loads(out)
    assert doc["status"] == "failed" and doc["error_kind"] == "upstream"
    assert len(doc["findings"]) == 10  # completed-batch evidence preserved


# --- A completed batch survives a later batch's failure (evidence retained) --

def _assert_b01_findings_and_evidence_preserved(doc, expected):
    """Every b01 finding and its full evidence-index chain must be retained."""
    b01 = [f for f in doc["findings"] if f["batch_id"] == "b01"]
    assert len(b01) == expected, f"{len(b01)} b01 findings, expected {expected}"
    index = doc["evidence_index"]
    for f in b01:
        assert f["match_id"] in index["match_ids"], f"match_id dropped: {f['match_id']}"
        assert f["source_value_id"] in index["source_values"], \
            f"source_value_id dropped: {f['source_value_id']}"
        assert f["comparison_id"] in index["comparison_ids"], \
            f"comparison_id dropped: {f['comparison_id']}"


def test_completed_batch_survives_authentication_failure():
    text, xlsx = _synthetic(genuine=15, structural=5)
    err = anthropic.AuthenticationError.__new__(anthropic.AuthenticationError)
    Exception.__init__(err, "invalid x-api-key sk-ant-SECRET")
    with tempfile.TemporaryDirectory() as tmp:
        report = _write(tmp, "r.txt", text)
        wb = _write(tmp, "w.xlsx", xlsx)
        with _patched(Brain(fail_on_batch=2, error=err)):
            run = verify_report(report, wb, EngineOptions(batch_size=10))
    doc = run.to_dict()
    assert doc["status"] == "failed" and doc["error_kind"] == "authentication"
    _assert_b01_findings_and_evidence_preserved(doc, expected=10)


def test_completed_batch_survives_upstream_failure():
    text, xlsx = _synthetic(genuine=15, structural=5)
    err = anthropic.RateLimitError.__new__(anthropic.RateLimitError)
    Exception.__init__(err, "rate limited")
    with tempfile.TemporaryDirectory() as tmp:
        report = _write(tmp, "r.txt", text)
        wb = _write(tmp, "w.xlsx", xlsx)
        with _patched(Brain(fail_on_batch=2, error=err)):
            run = verify_report(report, wb, EngineOptions(batch_size=10))
    doc = run.to_dict()
    assert doc["status"] == "failed" and doc["error_kind"] == "upstream"
    _assert_b01_findings_and_evidence_preserved(doc, expected=10)


def test_completed_batch_survives_turn_limit_truncation():
    text, xlsx = _synthetic(genuine=15, structural=5)
    with tempfile.TemporaryDirectory() as tmp:
        report = _write(tmp, "r.txt", text)
        wb = _write(tmp, "w.xlsx", xlsx)
        # b01 (10 claims) completes its chain; b02 runs to the turn limit.
        with _patched(Brain(truncate_on_batch=2)):
            run = verify_report(report, wb, EngineOptions(batch_size=10, max_turns=6))
    doc = run.to_dict()
    assert doc["status"] == "incomplete"
    assert any("turn limit" in i for i in run.completion_issues), run.completion_issues
    _assert_b01_findings_and_evidence_preserved(doc, expected=10)
    b02 = next(b for b in doc["batches"] if b["batch_id"] == "b02")
    assert b02["complete"] is False


# --- Merge validation rejects each violation (shared match_id stays allowed) --

class _FakeWorkbook:
    """A minimal evidence index for exercising _merge_validate directly."""

    def __init__(self, matches, source_values, comparisons):
        self._matches = matches
        self._source_values = source_values
        self._comparisons = comparisons

    def resolve_match(self, match_id):
        return self._matches.get(match_id)

    def resolve_source_value(self, source_value_id):
        return self._source_values.get(source_value_id)

    def resolve_comparison(self, comparison_id):
        return self._comparisons.get(comparison_id)


def _mf(claim_id, match_id, svid, cmp_id, cell, sheet="Data", verdict="match"):
    return {"claim_id": claim_id, "verdict": verdict, "batch_id": "b01",
            "match_id": match_id, "source_value_id": svid, "comparison_id": cmp_id,
            "source_cell": cell, "sheet": sheet}


def _baseline():
    """A clean two-finding batch that shares one match_id (same source row)."""
    matches = {"b01_match_0001": {"sheet": "Data", "label_cell": "Data!A2"}}
    source_values = {"b01_srcval_0001": {"cell": "Data!B2"},
                     "b01_srcval_0002": {"cell": "Data!C2"}}
    comparisons = {
        "b01_cmp_0001": {"source_cell": "Data!B2", "source_value_id": "b01_srcval_0001"},
        "b01_cmp_0002": {"source_cell": "Data!C2", "source_value_id": "b01_srcval_0002"},
    }
    accepted = [{"claim_id": "C1"}, {"claim_id": "C2"}]
    findings = [
        _mf("C1", "b01_match_0001", "b01_srcval_0001", "b01_cmp_0001", "Data!B2"),
        _mf("C2", "b01_match_0001", "b01_srcval_0002", "b01_cmp_0002", "Data!C2"),
    ]
    batch_status = [{"batch_id": "b01", "complete": True}]
    wb = _FakeWorkbook(matches, source_values, comparisons)
    return accepted, findings, wb, batch_status, (matches, source_values, comparisons)


def test_merge_validate_allows_shared_match_id():
    accepted, findings, wb, batch_status, _ = _baseline()
    assert findings[0]["match_id"] == findings[1]["match_id"]  # same source row
    issues = engine._merge_validate(accepted, findings, wb, batch_status)
    assert issues == [], issues


def test_merge_validate_rejects_cross_batch_reference():
    accepted, findings, wb, batch_status, _ = _baseline()
    findings[1]["source_value_id"] = "b02_srcval_0002"  # evidence from another batch
    issues = engine._merge_validate(accepted, findings, wb, batch_status)
    assert any("is not from batch b01" in i for i in issues), issues


def test_merge_validate_rejects_missing_evidence_record():
    accepted, findings, wb, batch_status, raw = _baseline()
    _matches, source_values, _comparisons = raw
    source_values.pop("b01_srcval_0002")  # index no longer holds the record
    issues = engine._merge_validate(accepted, findings, wb, batch_status)
    assert any("record missing from evidence index" in i for i in issues), issues


def test_merge_validate_rejects_duplicate_source_value_id():
    accepted, findings, wb, batch_status, raw = _baseline()
    _matches, _source_values, comparisons = raw
    # C2 points at C1's source value through a distinct comparison.
    comparisons["b01_cmp_0002"] = {"source_cell": "Data!B2",
                                    "source_value_id": "b01_srcval_0001"}
    findings[1]["source_value_id"] = "b01_srcval_0001"
    findings[1]["source_cell"] = "Data!B2"
    issues = engine._merge_validate(accepted, findings, wb, batch_status)
    assert any("duplicate source_value_id b01_srcval_0001" in i for i in issues), issues


def test_merge_validate_rejects_duplicate_comparison_id():
    accepted, findings, wb, batch_status, _ = _baseline()
    findings[1]["comparison_id"] = "b01_cmp_0001"  # reuse C1's comparison id
    issues = engine._merge_validate(accepted, findings, wb, batch_status)
    assert any("duplicate comparison_id b01_cmp_0001" in i for i in issues), issues


def test_merge_validate_rejects_completed_batch_that_hit_turn_limit():
    accepted, findings, wb, batch_status, _ = _baseline()
    batch_status[0]["complete"] = False  # batch exhausted its turn limit
    issues = engine._merge_validate(accepted, findings, wb, batch_status)
    assert any("did not complete within its turn limit" in i for i in issues), issues


def test_transcript_grouped_by_batch_and_findings_unchanged():
    text, xlsx = _synthetic(genuine=25, structural=5)
    with tempfile.TemporaryDirectory() as tmp:
        report = _write(tmp, "r.txt", text)
        wb = _write(tmp, "w.xlsx", xlsx)
        with _patched(Brain()):
            run = verify_report(report, wb, EngineOptions(batch_size=10))
    plain = run.to_dict()
    with_t = run.to_dict(include_transcript=True)
    assert "transcript" not in plain
    assert plain["findings"] == with_t["findings"]
    groups = with_t["transcript"]
    assert [g["batch_id"] for g in groups] == ["b01", "b02", "b03"]


def test_payload_stays_bounded_and_independent_of_candidate_count():
    threshold = 200_000

    def max_payload(structural):
        brain = Brain()
        text, xlsx = _synthetic(genuine=50, structural=structural)
        with tempfile.TemporaryDirectory() as tmp:
            report = _write(tmp, "r.txt", text)
            wb = _write(tmp, "w.xlsx", xlsx)
            with _patched(brain):
                run = verify_report(report, wb, EngineOptions(batch_size=20))
        assert run.status == "completed", run.completion_issues
        return max(brain.payloads), run.candidates_extracted

    small_max, small_total = max_payload(260)   # 310 candidates
    large_max, large_total = max_payload(560)   # 610 candidates
    assert large_total > small_total >= 300
    assert small_max < threshold and large_max < threshold
    # excluded candidates never reach the model, so per-call payload is identical
    assert small_max == large_max


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
