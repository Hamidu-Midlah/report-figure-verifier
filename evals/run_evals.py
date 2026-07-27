"""Eval harness: run the sample data through the verifier CLI and score it.

Usage:  python -m evals.run_evals
Requires ANTHROPIC_API_KEY. Invokes the public CLI end to end, parses its JSON
output, scores the 11 expected verdicts, and enforces the completeness gates.
Exits non-zero unless every gate passes, so this is a CI-ready regression check.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    cases = json.loads((ROOT / "evals" / "cases.json").read_text())["cases"]
    report = ROOT / "sample_data" / "draft_report.md"
    xlsx = ROOT / "sample_data" / "source_data.xlsx"

    print(f"Running the verifier CLI on sample data ({len(cases)} eval cases)...",
          file=sys.stderr)
    proc = subprocess.run(
        [sys.executable, "-m", "verifier.cli",
         "--report", str(report), "--workbook", str(xlsx)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    if proc.returncode != 0:
        print(f"CLI exited {proc.returncode} (expected 0). stderr tail:", file=sys.stderr)
        sys.stderr.write(proc.stderr[-2000:])
        return 1

    try:
        data = json.loads(proc.stdout)  # stdout must be valid JSON
    except json.JSONDecodeError as exc:
        print(f"CLI stdout was not valid JSON: {exc}", file=sys.stderr)
        return 1

    findings = data.get("findings", [])

    passed = 0
    rows = []
    for case in cases:
        match = next(
            (f for f in findings
             if case["sentence_contains"].lower() in f["sentence"].lower()
             and case["figure"].replace("%", "") in f["reported_value"]),
            None,
        )
        got = match["verdict"] if match else "NO_FINDING"
        ok = got == case["expected_verdict"]
        passed += ok
        rows.append((case["id"], case["expected_verdict"], got, "PASS" if ok else "FAIL"))

    width = max(len(r[1]) for r in rows) + 2
    print(f"{'case':<6}{'expected':<{width}}{'got':<{width}}result")
    for r in rows:
        print(f"{r[0]:<6}{r[1]:<{width}}{r[2]:<{width}}{r[3]}")
    print(f"\n{passed}/{len(cases)} cases passed")

    # Regression gates beyond verdict scoring.
    claim_ids = [f["claim_id"] for f in findings]
    logged_once = len(claim_ids) == len(set(claim_ids)) == len(cases)
    verifiable = [f for f in findings if f["verdict"] != "unverifiable"]
    exact_cells = all(f.get("source_cell") for f in verifiable)
    schema_ok = data.get("schema_version") == "1.0"
    completed = data.get("status") == "completed" and data.get("completion", {}).get("complete")

    print(f"schema_version 1.0: {schema_ok}")
    print(f"all {len(cases)} claims logged exactly once: {logged_once}")
    print(f"every verifiable finding has an exact source cell: {exact_cells}")
    print(f"CLI status completed: {completed}")

    gates = (passed == len(cases) and logged_once and exact_cells
             and schema_ok and completed)
    return 0 if gates else 1


if __name__ == "__main__":
    sys.exit(main())
