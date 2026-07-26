"""Streamlit UI for FigureAudit.

Run with:  streamlit run app.py
Requires ANTHROPIC_API_KEY in the environment.
"""

import os
import re
import uuid
import tempfile
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from verifier.agent import run_verification, MODEL

st.set_page_config(page_title="FigureAudit", page_icon="✅", layout="wide")

st.title("FigureAudit")
st.caption("Verify every number in your report against its source spreadsheet.")
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
        "unverifiable, each with the exact sheet, cell, and row checked.\n"
        "5. A human reviews the findings; nothing is auto-corrected."
    )

VERDICT_ICONS = {
    "match": "✅ match",
    "rounding_diff": "🟡 rounding diff",
    "mismatch": "🔴 mismatch",
    "unverifiable": "⚪ unverifiable",
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

# Leading sheet-qualified cell reference, e.g. Adoption!D2 or 'Regional Sales'!D3.
_SHEET_REF = re.compile(r"^\s*(?:'([^']+)'|([A-Za-z0-9_]+))!")


def _sanitize(text):
    """Replace em dashes in model-generated text with safe punctuation."""
    if not isinstance(text, str):
        return text
    em = chr(8212)  # em dash (U+2014), built from its code point to keep this file clean
    return text.replace(f" {em} ", ": ").replace(em, "-")


def _clean_finding(f: dict) -> dict:
    """Sanitise every string field of a finding for display and export."""
    return {k: _sanitize(v) for k, v in f.items()}


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


def _sheet_of(location: str):
    """Extract the sheet name from a leading A1 cell reference, or None."""
    m = _SHEET_REF.match(location or "")
    if not m:
        return None
    return m.group(1) or m.group(2)


def _sheets_referenced(findings) -> list:
    """Unique spreadsheet sheets cited by verifiable findings (never unverifiable)."""
    sheets = []
    for f in findings:
        if f["verdict"] == "unverifiable":
            continue
        sheet = _sheet_of(f.get("source_location", ""))
        if sheet:
            sheets.append(sheet)
    return sorted(set(sheets))


def _mismatch_line(f) -> str:
    """A concise, deterministic one-liner for a mismatch banner entry."""
    loc = f.get("source_location", "").strip()
    prefix = f"{loc}: " if loc else ""
    return _sanitize(
        f"{prefix}report says {f['reported_value']}; "
        f"spreadsheet says {f['source_value']}."
    )


def _run_info_lines(run: dict) -> list:
    """Ordered run-metadata lines shared by the expander and the reports."""
    findings = run["findings"]
    sheets = _sheets_referenced(findings)
    return [
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

    return _sanitize("\n".join(lines).rstrip() + "\n")


col1, col2 = st.columns(2)
with col1:
    report_file = st.file_uploader("Draft report (.md / .txt)", type=["md", "txt"])
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
        report_text = open(report_name).read()
        xlsx_path = xlsx_name
    elif report_file and xlsx_file:
        report_text = report_file.read().decode("utf-8")
        tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        tmp.write(xlsx_file.read())
        tmp.close()
        xlsx_path = tmp.name
        report_name = report_file.name
        xlsx_name = xlsx_file.name
    else:
        st.warning("Upload both files or tick the sample-data box.")
        st.stop()

    status = st.status("Agent working…", expanded=True)

    def progress(tool_name, tool_input):
        status.write(f"`{tool_name}` -> {str(tool_input)[:120]}")

    with st.spinner("Running agent loop"):
        result = run_verification(report_text, xlsx_path, progress_callback=progress)

    status.update(label="Verification complete", state="complete", expanded=False)

    findings = [_clean_finding(f) for f in result.get("findings", [])]
    summary_text = _sanitize(next(
        (entry["text"] for entry in result.get("transcript", [])
         if isinstance(entry, dict) and entry.get("role") == "assistant"
         and "text" in entry),
        "",
    ))

    # Persist the completed run once; ordinary reruns (filters, downloads) reuse it
    # so the run ID and timestamp stay stable.
    st.session_state["run"] = {
        "run_id": uuid.uuid4().hex[:8],
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "model": MODEL,
        "report_name": report_name,
        "xlsx_name": xlsx_name,
        "report_text": report_text,
        "findings": findings,
        "summary_text": summary_text,
        "summary_counts": result.get("summary", {}),
        "transcript": result.get("transcript", []),
    }

# ---- Render the most recent completed run (survives filter/download reruns) ----
run = st.session_state.get("run")
if run:
    findings = run["findings"]
    summary_text = run["summary_text"]

    mismatches = [f for f in findings if f["verdict"] == "mismatch"]
    rounding = [f for f in findings if f["verdict"] == "rounding_diff"]
    unverifiable = [f for f in findings if f["verdict"] == "unverifiable"]
    verifiable = [f for f in findings if f["verdict"] != "unverifiable"]
    sheets = _sheets_referenced(findings)
    coverage = f"{len(findings)} claims checked across {len(sheets)} source sheets."

    # ---- Results banner --------------------------------------------------
    if mismatches:
        n = len(mismatches)
        st.error(f"{n} issue requires review" if n == 1
                 else f"{n} issues require review")
        for f in mismatches:
            st.markdown("- " + _mismatch_line(f))
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
    counts = run.get("summary_counts", {})
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
        st.markdown("\n".join(_run_info_lines(run)))

    # ---- What was checked ------------------------------------------------
    with st.expander("Report under review"):
        st.markdown(run["report_text"])

    # ---- Findings table + filter -----------------------------------------
    st.subheader("Findings (human review required)")
    choice = st.radio(
        "Filter findings", list(_FILTERS.keys()), horizontal=True, index=0
    )
    allowed = _FILTERS[choice]
    view = [f for f in findings if f["verdict"] in allowed]
    st.caption(f"Showing {len(view)} of {len(findings)} findings.")

    if view:
        df = pd.DataFrame(view)
        df["verdict"] = df["verdict"].map(lambda v: VERDICT_ICONS.get(v, v))
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info(_EMPTY_MESSAGES[choice])

    # ---- Downloads -------------------------------------------------------
    rid = run["run_id"]
    md_current = build_markdown_report(view, run)
    md_full = build_markdown_report(findings, run)
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
        st.json(run["transcript"])
