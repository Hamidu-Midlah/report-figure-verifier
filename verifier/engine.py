"""FigureAudit verification engine: Streamlit-independent public interface.

`verify_report(report, workbook, options)` owns claim selection (deterministic
relevance classification), bounded batch verification against one shared
workbook and evidence index, upstream-error handling, evidence-chain and
completeness validation, and a versioned JSON schema (1.1). It holds no UI state.

Entry points share one implementation:
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

from .agent import MODEL, MAX_TURNS, verify_claims_batch
from .classify import classify_candidates, exclusion_counts
from .ingestion import extract_report_text, IngestionError
from .tools import SourceWorkbook, extract_numeric_claims, sheets_referenced

SCHEMA_VERSION = "1.1"
TOLERANCE_ROUNDED_PCT = 0.5
TOLERANCE_EXACT_COUNT = 0
DEFAULT_BATCH_SIZE = 20

_FINDING_FIELDS = (
    "claim_id", "sentence", "reported_value", "source_value", "verdict",
    "difference", "tolerance", "sheet", "label_cell", "source_cell",
    "batch_id", "match_id", "source_value_id", "comparison_id", "source_location",
    "source_mapping_status", "note",
)

_SECRET_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")
_SECRET_KEYS = {
    "api_key", "apikey", "authorization", "anthropic_api_key", "x-api-key",
    "password", "secret", "token", "access_token", "refresh_token",
}


class EngineError(Exception):
    """Base class for engine failures surfaced to the user."""


class EngineInputError(EngineError):
    """Invalid input: missing file, wrong workbook type, unusable arguments."""


class EngineIngestionError(EngineError):
    """The report could not be converted to text."""


@dataclass
class EngineOptions:
    model: str | None = None
    max_turns: int | None = None
    batch_size: int | None = None
    report_name: str | None = None
    workbook_name: str | None = None
    progress: object = None  # optional callable(message: str)


@dataclass
class VerificationRun:
    run_id: str
    timestamp: str
    model: str
    status: str                          # completed | incomplete | failed
    error_kind: str | None
    report_name: str
    report_type: str
    workbook_name: str
    ingestion: dict
    max_turns: int
    batch_size: int
    candidates_extracted: int
    accepted_count: int
    excluded: list
    findings: list
    verdict_counts: dict
    source_sheets: list
    batches: list
    evidence_index: dict
    completion_complete: bool
    completion_issues: list
    errors: list
    tool_call_count: int
    agent_turn_count: int
    extracted_text: str
    summary_text: str
    transcript_groups: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)

    def to_dict(self, include_transcript: bool = False) -> dict:
        data = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "status": self.status,
            "error_kind": self.error_kind,
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
                "batch_size": self.batch_size,
            },
            "candidates_extracted": self.candidates_extracted,
            "accepted_claim_count": self.accepted_count,
            "extracted_claim_count": self.accepted_count,  # kept: claims sent to verification
            "completed_finding_count": len(self.findings),
            "excluded_candidates": {
                "counts": exclusion_counts(self.excluded),
                "items": self.excluded,
            },
            "verdict_counts": self.verdict_counts,
            "source_sheets_referenced": self.source_sheets,
            "batches": self.batches,
            "evidence_index": self.evidence_index,
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
            data["transcript"] = [
                {"batch_id": g["batch_id"], "entries": _sanitize(g["entries"])}
                for g in self.transcript_groups
            ]
        return data


def _finding_json(finding: dict) -> dict:
    return {k: finding.get(k) for k in _FINDING_FIELDS}


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


def _redact_text(text: str) -> str:
    return _SECRET_RE.sub("[redacted]", str(text))


def _batches(accepted, size):
    return [accepted[i:i + size] for i in range(0, len(accepted), size)]


def _api_diagnostic(exc, batch_id, batch_number, accepted_count, tool_calls,
                    transcript_groups) -> dict:
    approx = sum(len(str(g.get("entries"))) for g in transcript_groups)
    return {
        "error_type": type(exc).__name__,
        "status_code": getattr(exc, "status_code", None),
        "request_id": getattr(exc, "request_id", None),
        "message": _redact_text(str(exc))[:200],
        "batch_id": batch_id,
        "batch_number": batch_number,
        "accepted_claim_count": accepted_count,
        "tool_call_count": tool_calls,
        "approx_transcript_chars": approx,
    }


def _merge_validate(accepted, findings, workbook, batch_status) -> list:
    """Resolve the whole evidence chain and enforce every merge rule."""
    issues = []
    accepted_ids = [c["claim_id"] for c in accepted]
    finding_ids = [f["claim_id"] for f in findings]

    for cid in accepted_ids:
        if cid not in finding_ids:
            issues.append(f"accepted claim {cid} was not logged")
    for cid in sorted(set(finding_ids)):
        if finding_ids.count(cid) > 1:
            issues.append(f"claim {cid} was logged {finding_ids.count(cid)} times")
    for cid in sorted(set(finding_ids) - set(accepted_ids)):
        issues.append(f"finding references claim {cid} that was not an accepted claim")

    # A match_id (a located spreadsheet row) may legitimately serve several
    # claims from that row; only the terminal per-finding evidence, the
    # source_value_id and comparison_id, must be unique across the run.
    seen = {"source_value_id": {}, "comparison_id": {}}
    for f in findings:
        if f.get("verdict") == "unverifiable":
            if any(f.get(k) for k in ("match_id", "source_value_id",
                                      "comparison_id", "source_cell")):
                issues.append(f"{f['claim_id']}: unverifiable finding carries evidence ids")
            continue

        bid = f.get("batch_id")
        for key in ("batch_id", "match_id", "source_value_id", "comparison_id",
                    "source_cell", "sheet"):
            if not f.get(key):
                issues.append(f"{f['claim_id']}: missing {key}")
        for key in ("match_id", "source_value_id", "comparison_id"):
            value = f.get(key) or ""
            if bid and not value.startswith(f"{bid}_"):
                issues.append(f"{f['claim_id']}: {key} '{value}' is not from batch {bid}")
        for key in ("source_value_id", "comparison_id"):
            value = f.get(key)
            if value:
                seen[key][value] = seen[key].get(value, 0) + 1

        if workbook.resolve_match(f.get("match_id")) is None:
            issues.append(f"{f['claim_id']}: match_id record missing from evidence index")
        if workbook.resolve_source_value(f.get("source_value_id")) is None:
            issues.append(f"{f['claim_id']}: source_value_id record missing from evidence index")
        comp = workbook.resolve_comparison(f.get("comparison_id"))
        if comp is None:
            issues.append(f"{f['claim_id']}: comparison_id record missing from evidence index")
        else:
            if comp.get("source_cell") != f.get("source_cell"):
                issues.append(f"{f['claim_id']}: comparison cell differs from finding source cell")
            if comp.get("source_value_id") != f.get("source_value_id"):
                issues.append(f"{f['claim_id']}: comparison source_value_id differs")

    for key, counts in seen.items():
        for value, n in counts.items():
            if n > 1:
                issues.append(f"duplicate {key} {value} referenced by {n} findings")

    for batch in batch_status:
        if not batch["complete"]:
            issues.append(f"batch {batch['batch_id']} did not complete within its turn limit")
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
    batch_size = options.batch_size or DEFAULT_BATCH_SIZE

    def emit(message):
        if options.progress:
            options.progress(message)

    candidates = extract_numeric_claims(ingestion.extracted_text)
    accepted, excluded = classify_candidates(candidates)
    emit(f"Extracted {len(candidates)} candidates; accepted {len(accepted)}; "
         f"excluded {exclusion_counts(excluded)}")

    workbook = SourceWorkbook(workbook_source)
    batches = _batches(accepted, batch_size)

    all_findings = []
    transcript_groups = []
    batch_status = []
    tool_calls = 0
    turns_total = 0
    error_kind = None
    diagnostics = []

    for number, batch_claims in enumerate(batches, start=1):
        batch_id = f"b{number:02d}"
        workbook.set_batch(batch_id)
        emit(f"Batch {number} of {len(batches)}: verifying {len(batch_claims)} claims")

        def _tool(name, _inp, _bid=batch_id):
            emit(f"{_bid}: {name}")

        try:
            result = verify_claims_batch(
                batch_claims, workbook, model=model, max_turns=max_turns,
                batch_id=batch_id, progress_callback=_tool,
            )
        except anthropic.AuthenticationError as exc:
            error_kind = "authentication"
            diagnostics.append(_api_diagnostic(exc, batch_id, number, len(accepted),
                                               tool_calls, transcript_groups))
            batch_status.append({"batch_id": batch_id,
                                 "claims": [c["claim_id"] for c in batch_claims],
                                 "claim_count": len(batch_claims), "finding_count": 0,
                                 "complete": False, "turns": 0,
                                 "error": "authentication failure"})
            break
        except anthropic.APIError as exc:
            error_kind = "upstream"
            diagnostics.append(_api_diagnostic(exc, batch_id, number, len(accepted),
                                               tool_calls, transcript_groups))
            batch_status.append({"batch_id": batch_id,
                                 "claims": [c["claim_id"] for c in batch_claims],
                                 "claim_count": len(batch_claims), "finding_count": 0,
                                 "complete": False, "turns": 0,
                                 "error": _redact_text(str(exc))[:120]})
            break
        except Exception as exc:  # noqa: BLE001 - internal failure, keep prior evidence
            error_kind = "internal"
            diagnostics.append({"error_type": type(exc).__name__,
                                "message": _redact_text(str(exc))[:200],
                                "batch_id": batch_id, "batch_number": number})
            batch_status.append({"batch_id": batch_id,
                                 "claims": [c["claim_id"] for c in batch_claims],
                                 "claim_count": len(batch_claims), "finding_count": 0,
                                 "complete": False, "turns": 0,
                                 "error": "internal failure"})
            break

        all_findings.extend(result["findings"])
        transcript_groups.append({"batch_id": batch_id, "entries": result["transcript"]})
        tool_calls += result["tool_calls"]
        turns_total += result["turns"]
        batch_status.append({
            "batch_id": batch_id,
            "claims": [c["claim_id"] for c in batch_claims],
            "claim_count": len(batch_claims),
            "finding_count": len(result["findings"]),
            "complete": not result["hit_turn_limit"],
            "turns": result["turns"],
            "error": None,
        })

    evidence_index = workbook.evidence_index()
    workbook.close()

    if error_kind is not None:
        status = "failed"
        completion_issues = [d.get("message", "upstream failure") for d in diagnostics]
        complete = False
        errors = [f"{d.get('error_type', 'error')}: {d.get('message', '')}"
                  for d in diagnostics]
    else:
        completion_issues = _merge_validate(accepted, all_findings, workbook, batch_status)
        complete = not completion_issues
        status = "completed" if complete else "incomplete"
        errors = []

    summary_text = ""
    for group in transcript_groups:
        for entry in group["entries"]:
            if isinstance(entry, dict) and entry.get("role") == "assistant" and "text" in entry:
                summary_text = entry["text"]

    verdict_counts = {}
    for f in all_findings:
        verdict_counts[f["verdict"]] = verdict_counts.get(f["verdict"], 0) + 1

    return VerificationRun(
        run_id=uuid.uuid4().hex[:12],
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        model=model,
        status=status,
        error_kind=error_kind,
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
        batch_size=batch_size,
        candidates_extracted=len(candidates),
        accepted_count=len(accepted),
        excluded=excluded,
        findings=all_findings,
        verdict_counts=verdict_counts,
        source_sheets=sheets_referenced(all_findings),
        batches=batch_status,
        evidence_index=evidence_index,
        completion_complete=complete,
        completion_issues=completion_issues,
        errors=errors,
        tool_call_count=tool_calls,
        agent_turn_count=turns_total,
        extracted_text=ingestion.extracted_text,
        summary_text=summary_text,
        transcript_groups=transcript_groups,
        diagnostics=diagnostics,
    )
