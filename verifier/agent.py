"""Agent orchestration: a plan → act → observe loop using Anthropic tool use.

The model is given four tools. It cannot see the spreadsheet directly; every
lookup and every comparison is a grounded tool call whose result is computed
in Python. The model's job is orchestration and judgement: deciding which
claims are verifiable, locating the right source cells, choosing a sensible
rounding tolerance, and writing findings to the structured log.
"""

from __future__ import annotations

import json
import os

import anthropic

from .tools import SourceWorkbook, extract_numeric_claims, compare_values, FindingsLog

MODEL = os.environ.get("VERIFIER_MODEL", "claude-sonnet-4-6")
MAX_TURNS = 40

SYSTEM_PROMPT = """You are a meticulous report verification agent. You are given \
a draft report and access to its source-of-truth spreadsheet via tools.

Your task: verify every numeric claim in the report against the spreadsheet.

Rules you must follow:
1. NEVER rely on your own memory or arithmetic. Every source value must come \
from a spreadsheet tool call, and every comparison must use compare_values.
2. For each numeric claim, either (a) verify it and log a finding whose \
source_location cites the exact spreadsheet cell, or (b) log it as \
'unverifiable' with a note explaining why (e.g. the figure comes from an \
external citation, not the source data). For unverifiable claims, set \
source_location to exactly "Not present in spreadsheet".
3. Match the claim to the RIGHT cell, not merely to any cell that happens to \
hold the same number. When a claim names a period (an explicit year such as \
"in 2026", or a relative term like "now", "current", or "today"), read the \
header row to find the matching column and compare against THAT column's \
value. A relative term with no explicit year refers to the most recent period \
in the data (the latest column). Double-check that the value you compared \
truly sits in that cell before logging.
4. For every spreadsheet-backed finding, source_location MUST begin with the \
exact Excel cell reference from the tool's `cell` field (A1 notation, \
sheet-qualified, e.g. Sales!D3), followed by the human-readable sheet name, \
row label, and column/period. For example: \
"Sales!D3; Sales sheet, row 3 (Manchester GBP k), 2026 column".
5. Use tolerance=0.5 by default for percentages that appear rounded to whole \
numbers; use tolerance=0 for exact counts. State the tolerance you used in \
the note when it matters.
6. Do not editorialise about the report's arguments. You verify numbers only.
7. Never use em dash characters in any text you write (source_location, notes, \
or your final summary). Use a colon, semicolon, comma, or hyphen instead.
8. When all claims are processed, reply with a short plain-text summary. Do \
not restate every finding; they are already in the log.

Work through claims systematically. Batch related lookups where possible."""

TOOL_SCHEMAS = [
    {
        "name": "list_sheets",
        "description": "List the sheet names in the source spreadsheet.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "find_in_spreadsheet",
        "description": (
            "Search all sheets for cells containing the query text "
            "(case-insensitive). Returns matches with the exact Excel `cell` "
            "reference (A1 notation, sheet-qualified, e.g. Sales!D3), the sheet, "
            "row, the full row context, and the sheet's header_row so you can "
            "cite the precise cell and align each number to the correct "
            "column/period (e.g. the '2026' column) instead of guessing by "
            "position."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "compare_values",
        "description": (
            "Compare a reported figure with a source figure numerically. "
            "Returns a verdict: match, rounding_diff, mismatch, or "
            "not_comparable. All comparison arithmetic happens here, never "
            "compare numbers yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reported": {"type": "string"},
                "source": {"type": "string"},
                "tolerance": {"type": "number", "default": 0.0},
            },
            "required": ["reported", "source"],
        },
    },
    {
        "name": "log_finding",
        "description": (
            "Record one verified (or unverifiable) claim in the findings log. "
            "Call this exactly once per claim."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "claim_id": {"type": "string"},
                "sentence": {"type": "string"},
                "reported_value": {"type": "string"},
                "source_value": {"type": "string"},
                "source_location": {"type": "string"},
                "verdict": {
                    "type": "string",
                    "enum": ["match", "rounding_diff", "mismatch", "unverifiable"],
                },
                "note": {"type": "string"},
            },
            "required": ["claim_id", "sentence", "reported_value",
                         "source_value", "source_location", "verdict"],
        },
    },
]


def run_verification(report_text: str, spreadsheet_path: str,
                     progress_callback=None) -> dict:
    """Run the full agent loop. Returns findings, summary, and the transcript."""
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY from env
    workbook = SourceWorkbook(spreadsheet_path)
    log = FindingsLog()

    claims = extract_numeric_claims(report_text)
    if not claims:
        return {"findings": [], "summary": {}, "note": "No numeric claims found."}

    user_message = (
        "Here are the numeric claims extracted from the report. Verify each "
        "against the source spreadsheet using your tools.\n\n"
        f"{json.dumps(claims, indent=2)}"
    )
    messages = [{"role": "user", "content": user_message}]
    transcript = []

    for _ in range(MAX_TURNS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOL_SCHEMAS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            final_text = "".join(
                b.text for b in response.content if b.type == "text"
            )
            transcript.append({"role": "assistant", "text": final_text})
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = _dispatch(block.name, block.input, workbook, log)
            transcript.append({
                "tool": block.name,
                "input": block.input,
                "result_preview": str(result)[:300],
            })
            if progress_callback:
                progress_callback(block.name, block.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })
        messages.append({"role": "user", "content": tool_results})

    return {
        "findings": log.to_dicts(),
        "summary": log.summary(),
        "claims_extracted": len(claims),
        "transcript": transcript,
    }


def _dispatch(name: str, args: dict, workbook: SourceWorkbook,
              log: FindingsLog):
    try:
        if name == "list_sheets":
            return {"sheets": workbook.list_sheets()}
        if name == "find_in_spreadsheet":
            return {"matches": workbook.find_value(args["query"])}
        if name == "compare_values":
            return compare_values(
                args["reported"], args["source"],
                float(args.get("tolerance", 0.0)),
            )
        if name == "log_finding":
            return log.add(
                claim_id=args["claim_id"],
                sentence=args["sentence"],
                reported_value=args["reported_value"],
                source_value=args["source_value"],
                source_location=args["source_location"],
                verdict=args["verdict"],
                note=args.get("note", ""),
            )
        return {"error": f"Unknown tool: {name}"}
    except Exception as exc:  # surface tool errors to the model, don't crash
        return {"error": str(exc)}
