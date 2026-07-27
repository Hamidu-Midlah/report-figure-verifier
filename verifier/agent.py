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

from .tools import (
    SourceWorkbook,
    extract_numeric_claims,
    compare_source_value,
    values_equal,
    FindingsLog,
    with_cell_prefix,
    resolve_source_sheet,
)

MODEL = os.environ.get("VERIFIER_MODEL", "claude-sonnet-4-6")
# The per-claim tool chain (find -> select -> compare -> log) uses more turns
# than v1.1, so allow generous headroom; the agent batches each step.
MAX_TURNS = 60

SYSTEM_PROMPT = """You are a meticulous report verification agent. You are given \
a draft report and access to its source-of-truth spreadsheet via tools.

Your task: verify every numeric claim in the report against the spreadsheet.

Rules you must follow:
1. NEVER rely on your own memory or arithmetic. Every source value must come \
from the spreadsheet tools, and every comparison must use compare_values.
2. To verify a spreadsheet-backed claim, follow this exact tool chain:
   a. find_in_spreadsheet to locate the row. Each match lists every cell in \
that row with its exact cell reference, value, and column header.
   b. select_source_cell(match_id, cell) to pick the EXACT numeric value cell \
the claim refers to (the correct column/period). It returns a source_value_id. \
Do NOT pick the row-label cell as the source value.
   c. compare_values(reported_value, source_value_id, tolerance) for the verdict.
   d. log_finding(...) referencing that same source_value_id.
3. Match the claim to the RIGHT column. When a claim names a period (an explicit \
year such as "in 2026", or a relative term like "now", "current", or "today"), \
select the cell under the matching column header. A relative term with no \
explicit year refers to the most recent period (the latest column).
4. source_location is a human-readable explanation only: give the sheet name, \
row label, and column/period, for example \
"Adoption sheet, Firms scaling AI (%), 2026 column". The exact value cell is \
added automatically from source_value_id; do not prepend it yourself.
5. For a claim that is not in the spreadsheet (e.g. an external citation), log \
it as 'unverifiable' with source_location exactly "Not present in spreadsheet" \
and no source_value_id.
6. Use tolerance=0.5 by default for percentages that appear rounded to whole \
numbers; use tolerance=0 for exact counts.
7. Do not editorialise about the report's arguments. You verify numbers only.
8. Never use em dash characters in any text you write (source_location, notes, \
or your final summary). Use a colon, semicolon, comma, or hyphen instead.
9. When all claims are processed, reply with a short plain-text summary. Do \
not restate every finding; they are already in the log.

Work through claims systematically, and batch the same step across claims where \
possible (select cells for several claims, then compare them, then log them)."""

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
            "(case-insensitive). Each match includes a `match_id`, the `sheet`, "
            "the `label_cell` that matched, and a `cells` list giving every cell "
            "in that row with its exact `cell` reference, `value`, and column "
            "`header`. Use select_source_cell to pick the numeric value cell for "
            "the correct column/period (e.g. the '2026' column)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "select_source_cell",
        "description": (
            "Pick the exact numeric value cell from a find_in_spreadsheet match. "
            "Pass the match_id and the cell (an A1 reference from that match's "
            "`cells`, e.g. Adoption!D2). Returns a source_value_id to use in "
            "compare_values and log_finding. The cell must be one returned for "
            "that match."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "match_id": {"type": "string"},
                "cell": {
                    "type": "string",
                    "description": "A1 cell reference from the match's cells list.",
                },
            },
            "required": ["match_id", "cell"],
        },
    },
    {
        "name": "compare_values",
        "description": (
            "Compare a reported figure against the exact source value chosen "
            "with select_source_cell. Pass reported_value and source_value_id; "
            "Python resolves the source number and computes the verdict (match, "
            "rounding_diff, mismatch, or not_comparable). Never type the source "
            "number yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reported_value": {"type": "string"},
                "source_value_id": {
                    "type": "string",
                    "description": "id returned by select_source_cell.",
                },
                "tolerance": {"type": "number", "default": 0.0},
            },
            "required": ["reported_value", "source_value_id"],
        },
    },
    {
        "name": "log_finding",
        "description": (
            "Record one verified (or unverifiable) claim in the findings log. "
            "Call this exactly once per claim. For spreadsheet-backed findings, "
            "pass the source_value_id used in compare_values; the sheet, exact "
            "value cell, and source value are resolved from it. Leave "
            "source_value_id empty for unverifiable claims."
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
                "source_value_id": {
                    "type": "string",
                    "description": "id from select_source_cell / compare_values.",
                },
                "source_cell": {"type": "string"},
                "sheet": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["claim_id", "sentence", "reported_value",
                         "source_value", "source_location", "verdict"],
        },
    },
]


def run_verification(report_text: str, spreadsheet_path, progress_callback=None,
                     model: str | None = None, max_turns: int | None = None) -> dict:
    """Run the full agent loop. Returns findings, summary, transcript, and run stats.

    `spreadsheet_path` may be a path or a file-like object (openpyxl accepts both).
    This is the grounded core; it does no presentation and holds no UI state.
    """
    model = model or MODEL
    max_turns = max_turns or MAX_TURNS
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY from env
    workbook = SourceWorkbook(spreadsheet_path)
    log = FindingsLog()

    claims = extract_numeric_claims(report_text)
    if not claims:
        return {"findings": [], "summary": {}, "claims": [], "claims_extracted": 0,
                "transcript": [], "tool_calls": 0, "turns": 0,
                "hit_turn_limit": False, "evidence": {"comparisons": {}},
                "model": model, "note": "No numeric claims found."}

    user_message = (
        "Here are the numeric claims extracted from the report. Verify each "
        "against the source spreadsheet using your tools.\n\n"
        f"{json.dumps(claims, indent=2)}"
    )
    messages = [{"role": "user", "content": user_message}]
    transcript = []
    tool_calls = 0
    turns = 0
    hit_turn_limit = True  # cleared when the model ends its turn normally

    for _ in range(max_turns):
        turns += 1
        response = client.messages.create(
            model=model,
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
            hit_turn_limit = False
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            tool_calls += 1
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
        "claims": claims,
        "claims_extracted": len(claims),
        "transcript": transcript,
        "tool_calls": tool_calls,
        "turns": turns,
        "hit_turn_limit": hit_turn_limit,
        "evidence": {"comparisons": workbook.comparison_records()},
        "model": model,
    }


def _dispatch(name: str, args: dict, workbook: SourceWorkbook,
              log: FindingsLog):
    try:
        if name == "list_sheets":
            return {"sheets": workbook.list_sheets()}
        if name == "find_in_spreadsheet":
            return {"matches": workbook.find_value(args["query"])}
        if name == "select_source_cell":
            return workbook.select_source_cell(args["match_id"], args["cell"])
        if name == "compare_values":
            return compare_source_value(
                workbook, args["reported_value"], args["source_value_id"],
                float(args.get("tolerance", 0.0)),
            )
        if name == "log_finding":
            return _apply_log_finding(args, workbook, log)
        return {"error": f"Unknown tool: {name}"}
    except Exception as exc:  # surface tool errors to the model, don't crash
        return {"error": str(exc)}


def _apply_log_finding(args: dict, workbook: SourceWorkbook, log: FindingsLog):
    """Resolve source evidence deterministically, then record the finding.

    Sheet, exact value cell, and source value come from the source_value_id that
    compare_values used (validated against this run's registry), never from
    model-typed prose. source_location is kept for human explanation only, with
    the authoritative value cell prepended.
    """
    verdict = args["verdict"]
    source_location = args.get("source_location", "")
    sheet = source_cell = label_cell = ""
    match_id = source_value_id = comparison_id = ""
    difference = tolerance = None
    source_value = args.get("source_value", "")

    if verdict != "unverifiable":
        svid = (args.get("source_value_id") or "").strip()
        if not svid:
            return {"error": (
                "Spreadsheet-backed findings require source_value_id from "
                "select_source_cell / compare_values."
            )}
        info = workbook.resolve_source_value(svid)
        if info is None:
            return {"error": f"Unknown source_value_id '{svid}'."}
        comparison_id = info.get("comparison_id", "")
        if not comparison_id:
            return {"error": (
                f"source_value_id '{svid}' was not used in compare_values. "
                "Call compare_values with it first."
            )}
        if source_value and not values_equal(source_value, info["value"]):
            return {"error": (
                f"Logged source value '{source_value}' does not match the "
                f"registered spreadsheet value '{info['value']}'."
            )}
        m_cell = (args.get("source_cell") or "").strip()
        if m_cell and m_cell != info["cell"]:
            return {"error": (
                f"source_cell '{m_cell}' conflicts with the registered cell "
                f"'{info['cell']}'."
            )}
        m_sheet = (args.get("sheet") or "").strip()
        if m_sheet and m_sheet != info["sheet"]:
            return {"error": (
                f"sheet '{m_sheet}' conflicts with the registered sheet "
                f"'{info['sheet']}'."
            )}
        sheet = info["sheet"]
        source_cell = info["cell"]
        label_cell = info["label_cell"]
        match_id = info.get("match_id", "")
        source_value_id = svid
        source_value = str(info["value"])
        comparison = workbook.resolve_comparison(comparison_id) or {}
        difference = comparison.get("difference")
        tolerance = comparison.get("tolerance")
        source_location = with_cell_prefix(source_cell, source_location)

    status = resolve_source_sheet(
        {"verdict": verdict, "sheet": sheet, "source_location": source_location}
    )[1]

    return log.add(
        claim_id=args["claim_id"],
        sentence=args["sentence"],
        reported_value=args["reported_value"],
        source_value=source_value,
        source_location=source_location,
        verdict=verdict,
        note=args.get("note", ""),
        sheet=sheet,
        source_cell=source_cell,
        label_cell=label_cell,
        match_id=match_id,
        source_value_id=source_value_id,
        comparison_id=comparison_id,
        difference=difference,
        tolerance=tolerance,
        source_mapping_status=status,
    )
