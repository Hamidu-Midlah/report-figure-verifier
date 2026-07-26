"""Deterministic tools exposed to the LLM agent via function calling.

Design principle: the LLM never does arithmetic or reads cells "from memory".
Every number it verifies must come through one of these grounded tools, and
every comparison is computed in Python, not by the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

import openpyxl
from openpyxl.utils import get_column_letter


def _excel_cell_ref(sheet: str, row: int, col: int) -> str:
    """Return a sheet-qualified A1 reference, e.g. Sales!D3 or 'Regional Sales'!D3.

    Sheet names that contain anything other than letters, digits, or underscores
    are wrapped in single quotes (with any internal quote doubled), matching how
    Excel itself qualifies such references.
    """
    a1 = f"{get_column_letter(col)}{row}"
    sheet = sheet or ""
    if re.fullmatch(r"[A-Za-z0-9_]+", sheet):
        return f"{sheet}!{a1}"
    return f"'{sheet.replace(chr(39), chr(39) * 2)}'!{a1}"


# ---------------------------------------------------------------------------
# Spreadsheet access
# ---------------------------------------------------------------------------

class SourceWorkbook:
    """Read-only wrapper around the source-of-truth spreadsheet."""

    def __init__(self, path: str):
        self._wb = openpyxl.load_workbook(path, data_only=True, read_only=True)

    def list_sheets(self) -> list[str]:
        return self._wb.sheetnames

    def read_sheet(self, sheet_name: str, max_rows: int = 200) -> list[list]:
        """Return the sheet as a list of rows (truncated for context safety)."""
        if sheet_name not in self._wb.sheetnames:
            raise ValueError(f"Sheet '{sheet_name}' not found. Available: {self._wb.sheetnames}")
        ws = self._wb[sheet_name]
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= max_rows:
                break
            rows.append([("" if c is None else c) for c in row])
        return rows

    def find_value(self, query: str) -> list[dict]:
        """Search all sheets for cells whose text matches `query` (case-insensitive).

        Returns each match with its exact Excel `cell` reference (A1 notation,
        sheet-qualified, e.g. Sales!D3), its row context, AND the sheet's header
        row, so the agent can cite the precise cell and align a number to the
        right column/period (e.g. map a value to the "2026" column) instead of
        guessing by position.
        """
        query_l = query.lower()
        hits = []
        for name in self._wb.sheetnames:
            ws = self._wb[name]
            header_row = None
            for r_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
                cells = [("" if c is None else c) for c in row]
                if r_idx == 1:
                    header_row = cells
                for c_idx, cell in enumerate(row, start=1):
                    if cell is not None and query_l in str(cell).lower():
                        hits.append({
                            "sheet": name,
                            "row": r_idx,
                            "col": c_idx,
                            "cell": _excel_cell_ref(name, r_idx, c_idx),
                            "cell_value": str(cell),
                            "header_row": header_row,
                            "row_context": cells,
                        })
        return hits[:20]  # cap to keep tool results bounded


# ---------------------------------------------------------------------------
# Claim extraction from the report text
# ---------------------------------------------------------------------------

# Matches e.g. "39%", "39.5 %", "USD 4.2 billion", "4,200", "0.7pp"
_NUMBER_PATTERN = re.compile(
    r"(?P<num>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?P<unit>%|pp|percentage points?|bn|billion|m|million|k|thousand)?",
    re.IGNORECASE,
)


def _looks_like_year(num: str, unit: str | None) -> bool:
    """A bare 4-digit number like 2024/2026 scopes a claim; it is not a figure."""
    return (
        not unit
        and num.isdigit()
        and "," not in num
        and 1900 <= int(num) <= 2099
    )


def extract_numeric_claims(report_text: str) -> list[dict]:
    """Pre-scan the report for numeric claims, one claim per figure.

    This is a deterministic first pass; the agent then decides which claims
    are verifiable against the spreadsheet and which are out of scope
    (e.g. sample sizes quoted from third parties).

    Reports are commonly hard-wrapped, so a claim's number and its label can
    sit on different physical lines. We unwrap each paragraph before splitting
    into sentences, so every figure stays attached to the sentence that gives
    it meaning. Each numeric figure becomes its own claim (carrying the full
    sentence) so multi-figure sentences are still verified figure by figure.
    Bare years are treated as scope, not as figures to verify.
    """
    claims = []
    claim_id = 0
    # Paragraphs are separated by blank lines; unwrap hard line breaks inside one.
    for paragraph in re.split(r"\n\s*\n", report_text):
        paragraph = re.sub(r"\s+", " ", paragraph.replace("\n", " ")).strip()
        if not paragraph:
            continue
        for sent in re.split(r"(?<=[.!?])\s+", paragraph):
            sent = sent.strip()
            if not sent:
                continue
            for m in _NUMBER_PATTERN.finditer(sent):
                num, unit = m.group("num"), m.group("unit")
                if num is None or _looks_like_year(num, unit):
                    continue
                claim_id += 1
                claims.append({
                    "claim_id": f"C{claim_id:03d}",
                    "sentence": sent,
                    "figure": m.group(0).strip(),
                })
    return claims


# ---------------------------------------------------------------------------
# Comparison: done in Python, never by the model
# ---------------------------------------------------------------------------

def _to_float(value) -> float | None:
    if value is None:
        return None
    s = str(value).replace(",", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def compare_values(reported, source, tolerance: float = 0.0) -> dict:
    """Compare a reported figure against a source figure.

    tolerance is an absolute allowance (e.g. 0.5 to permit rounding to the
    nearest whole percentage point). Verdicts:
      - match           : |reported - source| <= tolerance
      - rounding_diff   : within 2.0 absolute but outside tolerance
      - mismatch        : everything else
      - not_comparable  : either side is non-numeric
    """
    r, s = _to_float(reported), _to_float(source)
    if r is None or s is None:
        return {"verdict": "not_comparable", "reported": reported, "source": source}
    diff = abs(r - s)
    if diff <= tolerance:
        verdict = "match"
    elif diff <= 2.0:
        verdict = "rounding_diff"
    else:
        verdict = "mismatch"
    return {
        "verdict": verdict,
        "reported": r,
        "source": s,
        "abs_diff": round(diff, 4),
        "tolerance": tolerance,
    }


# ---------------------------------------------------------------------------
# Findings log: the agent's structured output channel
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    claim_id: str
    sentence: str
    reported_value: str
    source_value: str
    source_location: str
    verdict: str          # match | rounding_diff | mismatch | unverifiable
    note: str = ""


@dataclass
class FindingsLog:
    findings: list[Finding] = field(default_factory=list)

    def add(self, **kwargs) -> dict:
        f = Finding(**kwargs)
        self.findings.append(f)
        return {"logged": True, "total_findings": len(self.findings)}

    def to_dicts(self) -> list[dict]:
        return [asdict(f) for f in self.findings]

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.verdict] = counts.get(f.verdict, 0) + 1
        return counts
