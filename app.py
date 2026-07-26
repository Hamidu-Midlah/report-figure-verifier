"""Streamlit UI for the Report Figure Verification Agent.

Run with:  streamlit run app.py
Requires ANTHROPIC_API_KEY in the environment.
"""

import os
import tempfile

import pandas as pd
import streamlit as st

from verifier.agent import run_verification

st.set_page_config(page_title="Report Figure Verifier", page_icon="✅", layout="wide")

st.title("Report Figure Verification Agent")
st.caption(
    "Upload a draft report and its source-of-truth spreadsheet. An LLM agent "
    "extracts every numeric claim and verifies it against the data using "
    "grounded tool calls — no model arithmetic, no unsourced values. "
    "All findings require human review before any correction is made."
)

VERDICT_ICONS = {
    "match": "✅ match",
    "rounding_diff": "🟡 rounding diff",
    "mismatch": "🔴 mismatch",
    "unverifiable": "⚪ unverifiable",
}

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

    # ---- Summary metrics -------------------------------------------------
    summary = result.get("summary", {})
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Matches", summary.get("match", 0))
    m2.metric("Rounding diffs", summary.get("rounding_diff", 0))
    m3.metric("Mismatches", summary.get("mismatch", 0))
    m4.metric("Unverifiable", summary.get("unverifiable", 0))

    # ---- Findings table --------------------------------------------------
    findings = result.get("findings", [])
    if findings:
        df = pd.DataFrame(findings)
        df["verdict"] = df["verdict"].map(lambda v: VERDICT_ICONS.get(v, v))
        st.subheader("Findings (human review required)")
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.download_button(
            "Download findings as CSV",
            pd.DataFrame(findings).to_csv(index=False),
            file_name="verification_findings.csv",
        )
    else:
        st.info("No findings logged.")

    with st.expander("Agent transcript (for audit)"):
        st.json(result.get("transcript", []))
