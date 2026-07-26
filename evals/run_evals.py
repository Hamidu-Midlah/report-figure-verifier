"""Eval harness: run the agent on the sample data and score it.

Usage:  python -m evals.run_evals
Requires ANTHROPIC_API_KEY. Prints a pass/fail table and exits non-zero if
any case fails, so this can run in CI.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from verifier.agent import run_verification  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    cases = json.loads((ROOT / "evals" / "cases.json").read_text())["cases"]
    report_text = (ROOT / "sample_data" / "draft_report.md").read_text()
    xlsx = str(ROOT / "sample_data" / "source_data.xlsx")

    print(f"Running agent on sample data ({len(cases)} eval cases)…\n")
    result = run_verification(report_text, xlsx)
    findings = result["findings"]

    passed = 0
    rows = []
    for case in cases:
        # Find the agent's finding for this claim
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
    print(f"Agent summary: {result['summary']}")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
