"""Deterministic, inspectable classification of extracted numeric candidates.

Every candidate is placed in exactly one category before any agent involvement:

  accepted     - a genuine quantitative claim to verify (measurements, scores,
                 percentages, counts, currency values, quantities with units,
                 totals, reported changes, and dates where the date is the claim)
  structural   - document-structure tokens (page numbers, table-of-contents dot
                 leaders, standalone section and subsection numbers, figure and
                 table numbering, student or reference identifiers, bibliography
                 numbering, standalone footer numbers)
  definitional - a methodological definition or scale description
  contextual   - a date or period that only scopes another claim

Classification is conservative: if no rule confidently establishes structural,
definitional, or contextual, the candidate is accepted. It is rule-based Python
with no model call, so it is fully inspectable.
"""

from __future__ import annotations

import re

_MONTHS = (
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
)
_MONTH_ALT = "|".join(_MONTHS)

_DOT_LEADER = re.compile(r"\.\s?\.\s?\.\s?\.")  # a run of dots, as in a contents page
_LABEL_BEFORE = re.compile(
    r"\b(figure|fig\.?|table|tbl\.?|chapter|section|subsection|appendix|"
    r"equation|eq\.?|plate|scheme|item|question|q)\s*$",
    re.IGNORECASE,
)
_PAGE_BEFORE = re.compile(r"\b(page|pg\.?|p\.)\s*$", re.IGNORECASE)
_SECTION_HEADING = re.compile(r"^\s*\d+(\.\d+)+")  # 3.2 or 3.2.1 ...
_ID_CUE = re.compile(
    r"\b(student\s+(id|number|no\.?)|matriculation|reference\s+number|"
    r"registration\s+number|candidate\s+number|id\s*[:#])",
    re.IGNORECASE,
)
_SCALE_CUE = re.compile(
    r"\b(scale|likert|instrument|questionnaire|point[- ]scale|points?\s+scale|"
    r"rated\s+on|measured\s+on|scored\s+on|coded\s+on|graded\s+on|"
    r"anchored|is\s+measured|are\s+measured)\b",
    re.IGNORECASE,
)
_RANGE_CUE = re.compile(r"\b(range[sd]?|from)\b|-|\bto\b", re.IGNORECASE)
_UNIT = re.compile(r"(%|pp|percent|percentage|billion|million|thousand|bn)\b|%|[km]\b",
                   re.IGNORECASE)
_YEAR = re.compile(r"^(19|20)\d{2}$")


def _leading_number(figure: str) -> str:
    m = re.match(r"[\d,]*\.?\d+", figure.strip())
    return m.group(0) if m else figure.strip()


def _has_unit(figure: str) -> bool:
    return bool(_UNIT.search(figure))


def _text_before(sentence: str, number: str) -> str:
    idx = sentence.find(number)
    return sentence[:idx] if idx >= 0 else ""


def classify(figure: str, sentence: str):
    """Return (category, reason). reason is empty only for the accepted category."""
    number = _leading_number(figure)
    has_unit = _has_unit(figure)
    body = sentence.strip()

    # --- structural ------------------------------------------------------
    if _DOT_LEADER.search(sentence):
        return "structural", "table-of-contents entry with dot leaders"
    if _ID_CUE.search(sentence):
        return "structural", "document or reference identifier, not a quantity"
    if number.isdigit() and len(number) >= 6 and not has_unit:
        return "structural", "long identifier number, not a quantity"
    if re.search(r"\[\s*" + re.escape(number) + r"\s*\]", sentence):
        return "structural", "bibliography or citation numbering"
    before = _text_before(sentence, number)
    if _PAGE_BEFORE.search(before):
        return "structural", "page number"
    if _LABEL_BEFORE.search(before):
        return "structural", "figure, table, or section label numbering"
    heading = _SECTION_HEADING.match(body)
    if heading and heading.group(0).strip() == number and not has_unit:
        return "structural", "section or subsection number"
    if not has_unit and re.fullmatch(r"[\s|:.\-]*" + re.escape(number) + r"[\s|:.\-]*", body):
        return "structural", "standalone number such as a page number or footer"

    # --- definitional ----------------------------------------------------
    if _SCALE_CUE.search(sentence) and _RANGE_CUE.search(sentence):
        return "definitional", "methodological scale or range definition"

    # --- contextual ------------------------------------------------------
    if _YEAR.match(number) and not has_unit:
        return "contextual", "calendar year used as context or scope"
    if not has_unit and (
        re.search(r"\b" + re.escape(number) + r"\b\s+(" + _MONTH_ALT + r")\b", sentence, re.IGNORECASE)
        or re.search(r"\b(" + _MONTH_ALT + r")\s+" + re.escape(number) + r"\b", sentence, re.IGNORECASE)
    ):
        return "contextual", "calendar date component scoping a claim"

    return "accepted", ""


def classify_candidates(candidates):
    """Split candidates into (accepted, excluded).

    Accepted candidates are returned unchanged (claim_id, sentence, figure).
    Excluded candidates additionally carry ``category`` and ``reason`` so nothing
    is silently dropped; they are retained in run metadata.
    """
    accepted, excluded = [], []
    for candidate in candidates:
        category, reason = classify(candidate["figure"], candidate["sentence"])
        if category == "accepted":
            accepted.append(candidate)
        else:
            excluded.append({**candidate, "category": category, "reason": reason})
    return accepted, excluded


def exclusion_counts(excluded) -> dict:
    """Count excluded candidates by category, for progress and run accounting."""
    counts = {"structural": 0, "definitional": 0, "contextual": 0}
    for item in excluded:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    return counts
