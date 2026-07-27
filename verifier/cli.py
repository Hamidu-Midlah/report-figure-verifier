"""Command-line entry point for the FigureAudit verification engine.

Example:
  python -m verifier.cli --report report.docx --workbook source.xlsx --output result.json

JSON goes to the output file, or to stdout when no output path is given. All
progress, warnings, and diagnostics go to stderr, so stdout stays valid JSON.
Exit codes:
  0  completed successfully
  1  internal failure
  2  invalid input / usage
  3  ingestion failure
  4  authentication failure
  5  incomplete or truncated verification
"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import (
    EngineAuthError,
    EngineIngestionError,
    EngineInputError,
    EngineOptions,
    SCHEMA_VERSION,
    verify_report,
)

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_INPUT = 2
EXIT_INGESTION = 3
EXIT_AUTH = 4
EXIT_INCOMPLETE = 5


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m verifier.cli",
        description=(
            "Verify every numeric claim in a report against its source .xlsx "
            "workbook and emit a versioned JSON result. The API key is read from "
            "the ANTHROPIC_API_KEY environment variable and is never a CLI argument."
        ),
    )
    parser.add_argument("--report", required=True,
                        help="Path to the report (.md, .txt, .docx, or text-based .pdf).")
    parser.add_argument("--workbook", required=True,
                        help="Path to the source-of-truth spreadsheet (.xlsx).")
    parser.add_argument("--output",
                        help="Write JSON here. If omitted, JSON is written to stdout.")
    parser.add_argument("--model",
                        help="Override the verifier model id (no API key is accepted here).")
    parser.add_argument(
        "--include-transcript", action="store_true",
        help=("Include the full model and tool-call transcript in the JSON as "
              "supplementary audit evidence. WARNING: the transcript may contain "
              "report text and spreadsheet values and should be treated as "
              "potentially confidential. Secrets are always redacted."),
    )
    parser.add_argument("--schema-version", action="version", version=SCHEMA_VERSION,
                        help="Print the JSON schema version and exit.")
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    def log(message: str) -> None:
        print(message, file=sys.stderr)

    def progress(tool_name, tool_input):
        log(f"tool: {tool_name}")

    try:
        run = verify_report(
            args.report, args.workbook,
            EngineOptions(model=args.model, progress=progress),
        )
    except EngineInputError as exc:
        log(f"Input error: {exc}")
        return EXIT_INPUT
    except EngineIngestionError as exc:
        log(f"Ingestion error: {exc}")
        return EXIT_INGESTION
    except EngineAuthError as exc:
        log(f"Authentication error: {exc}")
        return EXIT_AUTH
    except Exception as exc:  # noqa: BLE001  - never leak a stack trace to the user
        log(f"Internal error: {type(exc).__name__}: {exc}")
        return EXIT_INTERNAL

    document = run.to_dict(include_transcript=args.include_transcript)
    text = json.dumps(document, indent=2, default=str)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        log(f"Wrote JSON result to {args.output}")
    else:
        print(text)  # stdout stays valid JSON

    if run.status != "completed":
        log("Verification incomplete: " + "; ".join(run.completion_issues))
        return EXIT_INCOMPLETE
    log("Verification completed.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
