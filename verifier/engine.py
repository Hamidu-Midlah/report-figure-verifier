"""FigureAudit verification engine: a Streamlit-independent public interface.

`verify_report(report, workbook, options)` owns ingestion, claim extraction,
spreadsheet loading, tool orchestration, exact source-cell selection, Python
comparisons, evidence-chain validation, completeness checking, and structured
findings. It returns a `VerificationRun` that serialises to a stable, versioned
JSON schema. It holds no Streamlit state and does no presentation.

Two entry points share one implementation:
  - verify_report(report_path, workbook_path, options)      # paths, for the CLI
  - verify_report_from_bytes(report_bytes, report_name,     # bytes, for Streamlit
                             workbook_bytes, workbook_name, options)
"""

from __future__ import annotations

import io
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import anthropic

from .agent import MODEL, MAX_TURNS, run_verification
from .ingestion import extract_report_text, IngestionError
from .tools import sheets_referenced

SCHEMA_VERSION = "1.0"
TOLERANCE_ROUNDED_PCT = 0.5
TOLERANCE_EXACT_COUNT = 0

# Fields serialised per finding, in a stable order.
_FINDING_FIELDS = (
    "claim_id", "sentence", "reported_value", "source_value", "verdict",
    "difference", "tolerance", "sheet", "label_cell", "source_cell",
    "match_id", "source_value_id", "comparison_id", "source_location",
    "source_mapping_status", "note",
)

# Redaction for the optional transcript.
_SECRET_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")
_SECRET_KEYS = {
    "api_key", "apikey", "authorization", "anthropic_api_key", "x-api-key",
    "password", "secret", "token", "access_token", "refresh_token",
}


class EngineError(Exception):
    """Base class for engine failures."""


class EngineInputError(EngineError):
    """Invalid input: missing file, wrong workbook type, unusable arguments."""


class EngineIngestionError(EngineError):
    """The report could not be converted to text."""


class EngineAuthError(EngineError):
    """Anthropic authentication failed."""


@dataclass
class EngineOptions:
    model: str | None = None
    max_turns: int | None = None
    report_name: str | None = None      # display name override (paths default to basename)
    workbook_name: str | None = None
    progress: object = None             # optional callable(tool_name, tool_input)


@dataclass
class VerificationRun:
    run_id: str
    timestamp: str
    model: str
    status: str                          # completed | incomplete
    report_name: str
    report_type: str
    workbook_name: str
    ingestion: dict
    max_turns: int
    claims_extracted: int
    findings: list
    verdict_counts: dict
    source_sheets: list
    completion_complete: bool
    completion_issues: list
    tool_call_count: int
    agent_turn_count: int
    extracted_text: str
    summary_text: str
    transcript: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def to_dict(self, include_transcript: bool = False) -> dict:
        """Serialise to the versioned JSON schema. Transcript excluded by default."""
        data = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "status": self.status,
            "timestamp": self.timestamp,
            "model": self.model,
            "input": {
                "report_name": self.report_name,
                "report_type": self.report_type,
                "workbook_name": self.workbook_name,
            },
            "ingestion": self.ingestion,
            "settings": {
                "tolerance_rounded_pct": TOLERANCE_ROUNDED_PCT,
                "tolerance_exact_count": TOLERANCE_EXACT_COUNT,
                "max_turns": self.max_turns,
            },
            "extracted_claim_count": self.claims_extracted,
            "completed_finding_count": len(self.findings),
            "verdict_counts": self.verdict_counts,
            "source_sheets_referenced": self.source_sheets,
            "findings": [_finding_json(f) for f in self.findings],
            "completion": {
                "complete": self.completion_complete,
                "issues": self.completion_issues,
            },
            "errors": self.errors,
            "tool_call_count": self.tool_call_count,
            "agent_turn_count": self.agent_turn_count,
            "transcript_included": include_transcript,
        }
        if include_transcript:
            data["transcript"] = sanitize_transcript(self.transcript)
        return data


def _finding_json(finding: dict) -> dict:
    """One finding, restricted to the schema fields (unverifiable keeps no ids)."""
    return {k: finding.get(k) for k in _FINDING_FIELDS}


def sanitize_transcript(transcript: list) -> list:
    """Strip secrets from the transcript before serialisation."""
    return [_sanitize(entry) for entry in transcript]


def _sanitize(value):
    if isinstance(value, str):
        return _SECRET_RE.sub("[redacted]", value)
    if isinstance(value, dict):
        return {
            k: ("[redacted]" if k.lower() in _SECRET_KEYS else _sanitize(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    return value


def _completeness(claims, findings, hit_turn_limit, evidence) -> list:
    """Return a list of completeness/evidence problems; empty means complete."""
    issues = []
    claim_ids = [c["claim_id"] for c in claims]
    finding_ids = [f["claim_id"] for f in findings]

    for cid in claim_ids:
        if cid not in finding_ids:
            issues.append(f"claim {cid} was not logged")
    for cid in sorted(set(finding_ids)):
        n = finding_ids.count(cid)
        if n > 1:
            issues.append(f"claim {cid} was logged {n} times")

    comparisons = evidence.get("comparisons", {})
    for f in findings:
        if f.get("verdict") == "unverifiable":
            if any(f.get(k) for k in ("match_id", "source_value_id",
                                      "comparison_id", "source_cell")):
                issues.append(f"{f['claim_id']}: unverifiable finding must carry no evidence ids")
            continue
        for key in ("match_id", "source_value_id", "comparison_id",
                    "source_cell", "sheet"):
            if not f.get(key):
                issues.append(f"{f['claim_id']}: missing {key}")
        if f.get("source_mapping_status") != "structured":
            issues.append(f"{f['claim_id']}: source_mapping_status is not structured")
        comp = comparisons.get(f.get("comparison_id"))
        if comp is None:
            issues.append(f"{f['claim_id']}: comparison record not found")
        else:
            if comp.get("source_cell") != f.get("source_cell"):
                issues.append(
                    f"{f['claim_id']}: comparison cell {comp.get('source_cell')} "
                    f"differs from finding cell {f.get('source_cell')}"
                )
            if comp.get("source_value_id") != f.get("source_value_id"):
                issues.append(f"{f['claim_id']}: comparison source_value_id differs")

    if hit_turn_limit:
        issues.append("run reached the turn limit before completing all claims")
    return issues


def verify_report(report, workbook, options: EngineOptions | None = None) -> VerificationRun:
    """Verify a report file against a workbook file. Paths in, VerificationRun out."""
    options = options or EngineOptions()
    report_path = os.fspath(report)
    workbook_path = os.fspath(workbook)
    if not os.path.isfile(report_path):
        raise EngineInputError(f"Report file not found: {report_path}")
    if not os.path.isfile(workbook_path):
        raise EngineInputError(f"Workbook file not found: {workbook_path}")
    if os.path.splitext(workbook_path)[1].lower() != ".xlsx":
        raise EngineInputError("The source workbook must be a .xlsx file.")
    with open(report_path, "rb") as fh:
        report_bytes = fh.read()
    report_name = options.report_name or os.path.basename(report_path)
    workbook_name = options.workbook_name or os.path.basename(workbook_path)
    return _verify(report_bytes, report_name, workbook_path, workbook_name, options)


def verify_report_from_bytes(report_bytes, report_name, workbook_bytes,
                             workbook_name, options: EngineOptions | None = None) -> VerificationRun:
    """Adapter for in-memory uploads (e.g. Streamlit). Same engine, no temp files."""
    options = options or EngineOptions()
    return _verify(report_bytes, report_name, io.BytesIO(workbook_bytes),
                   workbook_name, options)


def _verify(report_bytes, report_name, workbook_source, workbook_name, options) -> VerificationRun:
    try:
        ingestion = extract_report_text(report_bytes, report_name)
    except IngestionError as exc:
        raise EngineIngestionError(str(exc))
    if not ingestion.extracted_text.strip():
        raise EngineIngestionError("No text could be extracted from the report.")

    model = options.model or MODEL
    max_turns = options.max_turns or MAX_TURNS
    try:
        result = run_verification(
            ingestion.extracted_text, workbook_source,
            progress_callback=options.progress, model=model, max_turns=max_turns,
        )
    except anthropic.AuthenticationError:
        raise EngineAuthError(
            "Anthropic authentication failed. Set a valid ANTHROPIC_API_KEY."
        )

    findings = result["findings"]
    issues = _completeness(
        result["claims"], findings, result["hit_turn_limit"], result["evidence"],
    )
    summary_text = next(
        (e["text"] for e in result["transcript"]
         if isinstance(e, dict) and e.get("role") == "assistant" and "text" in e),
        "",
    )
    return VerificationRun(
        run_id=uuid.uuid4().hex[:12],
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        model=result["model"],
        status="completed" if not issues else "incomplete",
        report_name=report_name,
        report_type=ingestion.file_type,
        workbook_name=workbook_name,
        ingestion={
            "file_type": ingestion.file_type,
            "page_count": ingestion.page_count,
            "paragraph_count": ingestion.paragraph_count,
            "table_count": ingestion.table_count,
            "warnings": list(ingestion.warnings),
        },
        max_turns=max_turns,
        claims_extracted=result["claims_extracted"],
        findings=findings,
        verdict_counts=result["summary"],
        source_sheets=sheets_referenced(findings),
        completion_complete=not issues,
        completion_issues=issues,
        tool_call_count=result["tool_calls"],
        agent_turn_count=result["turns"],
        extracted_text=ingestion.extracted_text,
        summary_text=summary_text,
        transcript=result["transcript"],
    )
