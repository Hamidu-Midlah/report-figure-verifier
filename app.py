"""Streamlit UI for FigureAudit.

Run with:  streamlit run app.py
Requires ANTHROPIC_API_KEY in the environment.
"""

import os

import pandas as pd
import streamlit as st

from verifier.engine import verify_report_from_bytes, EngineOptions, EngineError
from verifier.classify import exclusion_counts
from verifier.tools import (
    sanitize,
    resolve_source_sheet,
    sheets_referenced,
    mismatch_line,
)

st.set_page_config(page_title="FigureAudit", page_icon="✅", layout="wide")

st.title("FigureAudit")
st.subheader("Evidence-grounded report verification")
st.write(
    "FigureAudit verifies numerical claims in reports against their exact "
    "source spreadsheet cells before publication."
)
st.markdown(
    "Python-verified comparisons · Exact source locations · Human review required"
)

with st.expander("How it works"):
    st.markdown(
        "1. Deterministic extraction pulls every numeric claim from the report.\n"
        "2. An LLM agent looks each claim up in the spreadsheet using tools; it "
        "never quotes numbers from memory.\n"
        "3. All comparisons are computed in Python with an explicit rounding "
        "tolerance, never by the model.\n"
        "4. Every claim gets a verdict: match, rounding difference, mismatch, or "
        "unverifiable, each tied to the exact worksheet and cell checked.\n"
        "5. A human reviews the findings; nothing is auto-corrected."
    )

VERDICT_ICONS = {
    "match": "✅ match",
    "rounding_diff": "🟡 rounding diff",
    "mismatch": "🔴 mismatch",
    "unverifiable": "⚪ unverifiable",
}

STATUS_ICONS = {
    "structured": "✓ structured",
    "fallback": "~ fallback",
    "under_specified": "⚠ under-specified",
    "n/a": "",
    "": "",
}

# Default tolerances the agent is instructed to use (surfaced in run info).
TOLERANCE_ROUNDED_PCT = 0.5
TOLERANCE_EXACT_COUNT = 0

# Findings-filter options, keyed to raw verdict enum values (never display labels).
_FILTERS = {
    "Issues only": {"mismatch", "rounding_diff", "unverifiable"},
    "Mismatches": {"mismatch"},
    "Rounding differences": {"rounding_diff"},
    "Unverifiable": {"unverifiable"},
    "All findings": {"match", "rounding_diff", "mismatch", "unverifiable"},
}
_EMPTY_MESSAGES = {
    "Issues only": "No issues found.",
    "Mismatches": "No mismatches found.",
    "Rounding differences": "No rounding differences found.",
    "Unverifiable": "No unverifiable claims found.",
    "All findings": "No findings logged.",
}

# Order (and headings) for the narrative report: problems first, matches last.
_REPORT_SECTIONS = [
    ("mismatch", "Mismatches"),
    ("rounding_diff", "Rounding differences"),
    ("unverifiable", "Unverifiable claims"),
    ("match", "Verified matches"),
]


def _clean_finding(f: dict) -> dict:
    """Sanitise every string field of a finding for display and export."""
    return {k: sanitize(v) for k, v in f.items()}


def _num(value):
    """Best-effort parse of a reported/source figure to a float, else None."""
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (ValueError, TypeError):
        return None


def _difference(reported, source):
    """Absolute numeric gap between two figures, tidily formatted, or None."""
    r, s = _num(reported), _num(source)
    if r is None or s is None:
        return None
    diff = abs(r - s)
    return int(diff) if diff == int(diff) else round(diff, 2)


def _narrative_sentence(f) -> str:
    """Turn one finding into a single human-readable sentence."""
    claim = f["sentence"].strip().rstrip(".")
    loc = f["source_location"]
    source = f["source_value"]
    note = (f.get("note") or "").strip()
    diff = _difference(f["reported_value"], source)
    diff_clause = f" of {diff}" if diff is not None else ""

    if f["verdict"] == "mismatch":
        return (f'❌ The report states "{claim}", but the source data '
                f'({loc}) shows {source}: a mismatch{diff_clause}.')
    if f["verdict"] == "rounding_diff":
        return (f'🟡 The report states "{claim}"; the source data ({loc}) '
                f'shows {source}: a rounding difference{diff_clause}.')
    if f["verdict"] == "unverifiable":
        why = f": {note}" if note else ""
        return (f'⚪ The claim "{claim}" could not be verified against the '
                f'source data{why}.')
    return f'✅ "{claim}" matches the source data ({loc}).'


def _run_info_lines(run: dict) -> list:
    """Ordered run-metadata lines shared by the expander and the reports."""
    findings = run["findings"]
    sheets = sheets_referenced(findings)
    lines = [
        f"- Run ID: {run['run_id']}",
        f"- Timestamp: {run['timestamp']}",
        f"- Model: {run['model']}",
        f"- Report file: {run['report_name']}",
        f"- Spreadsheet file: {run['xlsx_name']}",
        f"- Tolerance, rounded percentages: {TOLERANCE_ROUNDED_PCT}",
        f"- Tolerance, exact counts: {TOLERANCE_EXACT_COUNT}",
        f"- Claims checked: {len(findings)}",
        f"- Source sheets referenced: {len(sheets)}",
    ]
    acc = run.get("accounting")
    if acc:
        lines += [
            f"- Numeric candidates extracted: {acc['candidates_extracted']}",
            f"- Accepted claims: {acc['accepted_count']}",
            f"- Excluded candidates by category: {acc['excluded_counts']}",
            f"- Batches: {acc['batch_count']}",
        ]
        if acc.get("failed_batches"):
            lines.append(f"- Failed batches: {', '.join(acc['failed_batches'])}")
    return lines


def build_markdown_report(body_findings, run: dict) -> str:
    """Assemble a Markdown findings report with a run-info header, grouped by verdict.

    `body_findings` controls which findings appear; run-info counts always
    describe the full run.
    """
    summary_text = run.get("summary_text", "")
    lines = ["# FigureAudit findings", "", "## Run information", ""]
    lines += _run_info_lines(run)
    lines.append("")
    if summary_text:
        lines += ["## Summary", "", summary_text.strip(), ""]

    grouped = {}
    for f in body_findings:
        grouped.setdefault(f["verdict"], []).append(f)

    any_shown = False
    for verdict, heading in _REPORT_SECTIONS:
        group = grouped.get(verdict, [])
        if not group:
            continue
        any_shown = True
        lines.append(f"## {heading} ({len(group)})")
        lines.append("")
        lines += [f"- {_narrative_sentence(f)}" for f in group]
        lines.append("")

    if not any_shown:
        lines += ["_No findings in this view._", ""]

    return sanitize("\n".join(lines).rstrip() + "\n")


def render_results(run_state: dict) -> None:
    """Render the completed-run UI. Call exactly once per Streamlit execution.

    Reads only from `run_state` (session state), so filter and download reruns
    re-render the same single set of controls without re-running verification.
    """
    findings = run_state["findings"]
    summary_text = run_state["summary_text"]

    mismatches = [f for f in findings if f["verdict"] == "mismatch"]
    rounding = [f for f in findings if f["verdict"] == "rounding_diff"]
    unverifiable = [f for f in findings if f["verdict"] == "unverifiable"]
    verifiable = [f for f in findings if f["verdict"] != "unverifiable"]
    sheets = sheets_referenced(findings)
    coverage = f"{len(findings)} claims checked across {len(sheets)} source sheets."

    # ---- Results banner --------------------------------------------------
    if mismatches:
        n = len(mismatches)
        st.error(f"{n} issue requires review" if n == 1
                 else f"{n} issues require review")
        for f in mismatches:
            st.markdown("- " + mismatch_line(f))
    elif rounding or unverifiable:
        parts = []
        if rounding:
            parts.append(f"{len(rounding)} rounding difference"
                         + ("" if len(rounding) == 1 else "s"))
        if unverifiable:
            parts.append(f"{len(unverifiable)} unverifiable claim"
                         + ("" if len(unverifiable) == 1 else "s"))
        total = len(rounding) + len(unverifiable)
        verb = "item requires" if total == 1 else "items require"
        st.warning(f"{total} {verb} human review ({', '.join(parts)}).")
    else:
        st.success(
            f"No issues found: all {len(verifiable)} verifiable claims "
            "match the source data."
        )
    st.caption(coverage)

    # ---- Summary metrics -------------------------------------------------
    counts = run_state.get("summary_counts", {})
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Matches", counts.get("match", 0))
    m2.metric("Rounding diffs", counts.get("rounding_diff", 0))
    m3.metric("Mismatches", counts.get("mismatch", 0))
    m4.metric("Unverifiable", counts.get("unverifiable", 0))

    # ---- Agent summary (prose) -------------------------------------------
    if summary_text:
        st.subheader("Summary")
        st.write(summary_text)

    # ---- Run information -------------------------------------------------
    with st.expander("Run information"):
        st.markdown("\n".join(_run_info_lines(run_state)))
        acc = run_state.get("accounting")
        if acc and acc.get("batches"):
            st.caption("Per-batch accounting")
            st.dataframe(
                pd.DataFrame(acc["batches"])[
                    ["batch_id", "claim_count", "finding_count", "complete",
                     "turns", "error"]
                ],
                use_container_width=True, hide_index=True,
            )

    # ---- What was checked ------------------------------------------------
    with st.expander("Report under review"):
        ingestion = run_state.get("ingestion", {})
        bits = [f"File: {ingestion.get('report_name', run_state['report_name'])}"]
        if ingestion.get("file_type"):
            bits.append(f"Type: {ingestion['file_type']}")
        if ingestion.get("page_count") is not None:
            bits.append(f"Pages: {ingestion['page_count']}")
        if ingestion.get("paragraph_count") is not None:
            bits.append(f"Paragraphs: {ingestion['paragraph_count']}")
        if ingestion.get("table_count") is not None:
            bits.append(f"Tables: {ingestion['table_count']}")
        st.markdown("  ·  ".join(bits))
        for warning in ingestion.get("warnings", []):
            st.warning(warning)
        st.caption("Extracted report text used for verification")
        st.text(run_state["report_text"])

    # ---- Findings table + filter -----------------------------------------
    st.subheader("Findings (human review required)")
    choice = st.radio(
        "Filter findings", list(_FILTERS.keys()), horizontal=True, index=0
    )
    allowed = _FILTERS[choice]
    view = [f for f in findings if f["verdict"] in allowed]
    st.caption(f"Showing {len(view)} of {len(findings)} findings.")

    under_specified = [
        f for f in view if resolve_source_sheet(f)[1] == "under_specified"
    ]

    if view:
        df = pd.DataFrame(view)
        df["verdict"] = df["verdict"].map(lambda v: VERDICT_ICONS.get(v, v))
        if "source_mapping_status" in df.columns:
            df["source_mapping_status"] = df["source_mapping_status"].map(
                lambda s: STATUS_ICONS.get(s, s)
            )
        st.dataframe(df, use_container_width=True, hide_index=True)
        if under_specified:
            st.warning(
                f"{len(under_specified)} finding"
                + ("" if len(under_specified) == 1 else "s")
                + " could not be mapped to a source cell (under-specified)."
            )
    else:
        st.info(_EMPTY_MESSAGES[choice])

    # ---- Downloads -------------------------------------------------------
    rid = run_state["run_id"]
    md_current = build_markdown_report(view, run_state)
    md_full = build_markdown_report(findings, run_state)
    csv_current = pd.DataFrame(view).to_csv(index=False) if view else ""
    csv_full = pd.DataFrame(findings).to_csv(index=False)

    d1, d2, d3, d4 = st.columns(4)
    with d1:
        st.download_button(
            "Download current view (.md)", md_current,
            file_name=f"figureaudit_{rid}_view.md", disabled=not view,
        )
    with d2:
        st.download_button(
            "Download full report (.md)", md_full,
            file_name=f"figureaudit_{rid}_full.md",
        )
    with d3:
        st.download_button(
            "Download current view (.csv)", csv_current,
            file_name=f"figureaudit_{rid}_view.csv", disabled=not view,
        )
    with d4:
        st.download_button(
            "Download full findings (.csv)", csv_full,
            file_name=f"figureaudit_{rid}_full.csv",
        )

    with st.expander("Agent transcript (for audit)"):
        st.json(run_state["transcript"])


col1, col2 = st.columns(2)
with col1:
    report_file = st.file_uploader(
        "Draft report (.md / .txt / .docx / .pdf)",
        type=["md", "txt", "docx", "pdf"],
    )
with col2:
    xlsx_file = st.file_uploader("Source spreadsheet (.xlsx)", type=["xlsx"])

use_sample = st.checkbox("Use bundled sample data instead", value=not (report_file and xlsx_file))

if st.button("Run verification", type="primary"):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        st.error("Set ANTHROPIC_API_KEY in your environment first.")
        st.stop()

    if use_sample:
        report_name = "sample_data/draft_report.md"
        xlsx_name = "sample_data/source_data.xlsx"
        report_bytes = open(report_name, "rb").read()
        xlsx_bytes = open(xlsx_name, "rb").read()
    elif report_file and xlsx_file:
        report_bytes = report_file.read()
        report_name = report_file.name
        xlsx_bytes = xlsx_file.read()
        xlsx_name = xlsx_file.name
    else:
        st.warning("Upload both files or tick the sample-data box.")
        st.stop()

    status = st.status("Agent working…", expanded=True)

    def progress(message):
        status.write(message)

    # Streamlit consumes the same engine interface as the CLI; no separate path.
    try:
        with st.spinner("Classifying candidates and verifying claims in batches"):
            run = verify_report_from_bytes(
                report_bytes, report_name, xlsx_bytes, xlsx_name,
                EngineOptions(progress=progress),
            )
    except EngineError as exc:
        status.update(label="Verification failed", state="error", expanded=True)
        st.error(str(exc))
        st.stop()

    failed_batches = [b for b in run.batches if b.get("error")]
    if run.status == "failed":
        status.update(label="Verification failed", state="error", expanded=True)
        st.error(
            f"Verification failed ({run.error_kind}). "
            + "; ".join(run.errors or run.completion_issues)
        )
    elif run.status != "completed":
        status.update(label="Verification incomplete", state="error", expanded=False)
        st.warning(
            "Verification did not complete cleanly: "
            + "; ".join(run.completion_issues)
        )
    else:
        status.update(label="Verification complete", state="complete", expanded=False)

    findings = [_clean_finding(f) for f in run.findings]

    # Persist the run once; ordinary reruns (filters, downloads) reuse it so the
    # run id and timestamp stay stable.
    st.session_state["run"] = {
        "run_id": run.run_id,
        "timestamp": run.timestamp,
        "model": run.model,
        "report_name": run.report_name,
        "xlsx_name": run.workbook_name,
        "report_text": run.extracted_text,
        "run_status": run.status,
        "ingestion": {
            "report_name": run.report_name,
            "file_type": run.report_type,
            "page_count": run.ingestion["page_count"],
            "paragraph_count": run.ingestion["paragraph_count"],
            "table_count": run.ingestion["table_count"],
            "warnings": run.ingestion["warnings"],
        },
        "accounting": {
            "candidates_extracted": run.candidates_extracted,
            "accepted_count": run.accepted_count,
            "excluded_counts": exclusion_counts(run.excluded),
            "batch_count": len(run.batches),
            "failed_batches": [b["batch_id"] for b in failed_batches],
            "batches": run.batches,
        },
        "findings": findings,
        "summary_text": sanitize(run.summary_text),
        "summary_counts": run.verdict_counts,
        "transcript": [
            {"batch_id": g["batch_id"], "entries": g["entries"]}
            for g in run.transcript_groups
        ],
    }

# ---- Render the most recent completed run exactly once -----------------------
# The run action above only writes session state; all results rendering happens
# here, in a single call, so filter/download reruns never duplicate the output.
run_state = st.session_state.get("run")
if run_state:
    render_results(run_state)
