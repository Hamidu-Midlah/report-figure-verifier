"""Streamlit UI for the Report Figure Verification Agent.

Run with:  streamlit run app.py
Requires ANTHROPIC_API_KEY in the environment.
"""

import os
import tempfile
from datetime import date

import pandas as pd
import streamlit as st

from verifier.agent import run_verification

st.set_page_config(page_title="Report Figure Verifier", page_icon="✅", layout="wide")

st.title("Report Figure Verification Agent")
st.caption(
    "Upload a draft report and its source-of-truth spreadsheet. An LLM agent "
    "extracts every numeric claim and verifies it against the data using "
    "grounded tool calls: no model arithmetic, no unsourced values. "
    "All findings require human review before any correction is made."
)

with st.expander("How it works"):
    st.markdown(
        "1. Deterministic extraction pulls every numeric claim from the report.\n"
        "2. An LLM agent looks each claim up in the spreadsheet using tools; it "
        "never quotes numbers from memory.\n"
        "3. All comparisons are computed in Python with an explicit rounding "
        "tolerance, never by the model.\n"
        "4. Every claim gets a verdict: match, rounding difference, mismatch, or "
        "unverifiable, each with the exact sheet and row checked.\n"
        "5. A human reviews the findings; nothing is auto-corrected."
    )

VERDICT_ICONS = {
    "match": "✅ match",
    "rounding_diff": "🟡 rounding diff",
    "mismatch": "🔴 mismatch",
    "unverifiable": "⚪ unverifiable",
}

# Order (and headings) for the narrative report: problems first, matches last.
_REPORT_SECTIONS = [
    ("mismatch", "Mismatches"),
    ("rounding_diff", "Rounding differences"),
    ("unverifiable", "Unverifiable claims"),
    ("match", "Verified matches"),
]


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


def build_markdown_report(findings, summary_text, generated_on) -> str:
    """Assemble a shareable Markdown findings report grouped by verdict."""
    lines = [
        "# Report Figure Verification: Findings",
        "",
        f"_Generated {generated_on}_",
        "",
    ]
    if summary_text:
        lines += ["## Summary", "", summary_text.strip(), ""]

    grouped = {}
    for f in findings:
        grouped.setdefault(f["verdict"], []).append(f)

    for verdict, heading in _REPORT_SECTIONS:
        group = grouped.get(verdict, [])
        if not group:
            continue
        lines.append(f"## {heading} ({len(group)})")
        lines.append("")
        lines += [f"- {_narrative_sentence(f)}" for f in group]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"

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
        report_text = open("sample_data/draft_report.md").read()
        xlsx_path = "sample_data/source_data.xlsx"
    elif report_file and xlsx_file:
        report_text = report_file.read().decode("utf-8")
        tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        tmp.write(xlsx_file.read())
        tmp.close()
        xlsx_path = tmp.name
    else:
        st.warning("Upload both files or tick the sample-data box.")
        st.stop()

    status = st.status("Agent working…", expanded=True)

    def progress(tool_name, tool_input):
        preview = str(tool_input)
        status.write(f"`{tool_name}` → {preview[:120]}")

    with st.spinner("Running agent loop"):
        result = run_verification(report_text, xlsx_path, progress_callback=progress)

    status.update(label="Verification complete", state="complete", expanded=False)

    # ---- What was checked ------------------------------------------------
    with st.expander("Report under review"):
        st.markdown(report_text)

    # The agent's final plain-text summary is the last assistant text block.
    summary_text = next(
        (entry["text"] for entry in result.get("transcript", [])
         if isinstance(entry, dict) and entry.get("role") == "assistant"
         and "text" in entry),
        "",
    )

    # ---- Summary metrics -------------------------------------------------
    summary = result.get("summary", {})
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Matches", summary.get("match", 0))
    m2.metric("Rounding diffs", summary.get("rounding_diff", 0))
    m3.metric("Mismatches", summary.get("mismatch", 0))
    m4.metric("Unverifiable", summary.get("unverifiable", 0))

    # ---- Agent summary (prose) -------------------------------------------
    if summary_text:
        st.subheader("Summary")
        st.write(summary_text)

    # ---- Findings table --------------------------------------------------
    findings = result.get("findings", [])
    if findings:
        df = pd.DataFrame(findings)
        df["verdict"] = df["verdict"].map(lambda v: VERDICT_ICONS.get(v, v))
        st.subheader("Findings (human review required)")
        st.dataframe(df, use_container_width=True, hide_index=True)

        report_md = build_markdown_report(findings, summary_text, date.today().isoformat())
        dl1, dl2 = st.columns(2)
        with dl1:
            st.download_button(
                "Download findings as CSV",
                pd.DataFrame(findings).to_csv(index=False),
                file_name="verification_findings.csv",
            )
        with dl2:
            st.download_button(
                "Download findings report (.md)",
                report_md,
                file_name="verification_report.md",
            )
    else:
        st.info("No findings logged.")

    with st.expander("Agent transcript (for audit)"):
        st.json(result.get("transcript", []))
