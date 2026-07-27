"""Report ingestion: convert an uploaded report into plain text for extraction.

This module only converts formats to text. It does not extract numeric claims,
compare values, or touch the evidence chain; that stays in `verifier.tools` and
`verifier.agent`. Every supported format returns the same `IngestionResult`, and
the resulting `extracted_text` is what gets fed, unchanged, to claim extraction.

Supported report formats: .md, .txt, .docx, and text-based .pdf.
Scanned or image-only PDFs are not supported (no OCR).
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field

# Limits sized for Streamlit Community Cloud.
MAX_FILE_BYTES = 10 * 1024 * 1024      # 10 MB uploaded file
MAX_PDF_PAGES = 300                    # pages per PDF
MAX_EXTRACTED_CHARS = 2_000_000        # characters of extracted text

SUPPORTED_EXTENSIONS = (".md", ".txt", ".docx", ".pdf")

# Cell separator and row boundary used when flattening DOCX tables.
_CELL_SEP = " | "

_SCANNED_PDF_MESSAGE = (
    "This PDF appears to be scanned or image-based. FigureAudit currently "
    "supports text-based PDFs only."
)


class IngestionError(Exception):
    """A user-facing ingestion failure carrying a clear, safe message."""


@dataclass
class IngestionResult:
    """The text extracted from one report, plus a summary for the reviewer."""

    extracted_text: str
    file_type: str                     # md | txt | docx | pdf
    extraction_status: str = "ok"      # ok | partial | empty
    page_count: int | None = None      # PDFs
    paragraph_count: int | None = None  # text and DOCX
    table_count: int | None = None     # DOCX
    warnings: list = field(default_factory=list)


def extract_report_text(file, filename: str) -> IngestionResult:
    """Convert an uploaded report to text based on its filename and content.

    `file` may be raw bytes or a file-like object with `.read()`. Unsupported or
    malformed files raise IngestionError with a message safe to show the user.
    """
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise IngestionError(
            f"Unsupported report format '{ext or 'unknown'}'. Supported formats "
            "are .md, .txt, .docx, and text-based .pdf."
        )
    data = _read_bytes(file)
    if ext in (".md", ".txt"):
        return _ingest_text(data, ext.lstrip("."))
    if ext == ".docx":
        return _ingest_docx(data)
    return _ingest_pdf(data)


def _read_bytes(file) -> bytes:
    if isinstance(file, (bytes, bytearray)):
        data = bytes(file)
    elif hasattr(file, "read"):
        data = file.read()
        if isinstance(data, str):
            data = data.encode("utf-8")
    else:
        raise IngestionError("The uploaded file could not be read.")
    if len(data) > MAX_FILE_BYTES:
        raise IngestionError(
            f"This file is larger than the {MAX_FILE_BYTES // (1024 * 1024)} MB "
            "limit. Please upload a smaller report."
        )
    return data


def _check_char_limit(text: str) -> None:
    if len(text) > MAX_EXTRACTED_CHARS:
        raise IngestionError(
            "The extracted report text is larger than FigureAudit can process. "
            "Please split the report into smaller sections."
        )


def _paragraph_count(text: str) -> int:
    return len([p for p in re.split(r"\n\s*\n", text) if p.strip()])


# ---------------------------------------------------------------------------
# Plain text (.md, .txt)
# ---------------------------------------------------------------------------

def _ingest_text(data: bytes, file_type: str) -> IngestionResult:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise IngestionError(
            "This file is not valid UTF-8 text. Re-save it as UTF-8 and try again."
        )
    _check_char_limit(text)
    return IngestionResult(
        extracted_text=text,
        file_type=file_type,
        extraction_status="ok" if text.strip() else "empty",
        paragraph_count=_paragraph_count(text),
    )


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------

def _iter_block_items(document):
    """Yield paragraphs and tables in document order (python-docx has no such API)."""
    from docx.document import Document as _Document
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body = document.element.body
    for child in body.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, document)
        elif isinstance(child, CT_Tbl):
            yield Table(child, document)


def _dedupe_consecutive(values: list) -> list:
    """Drop consecutive repeats so a horizontally merged cell is not duplicated."""
    out = []
    for v in values:
        if not out or out[-1] != v:
            out.append(v)
    return out


def _ingest_docx(data: bytes) -> IngestionResult:
    try:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        document = Document(io.BytesIO(data))
    except Exception:
        raise IngestionError(
            "This DOCX file could not be read. It may be malformed or not a "
            "valid Word document."
        )

    blocks = []
    paragraph_count = 0
    table_count = 0
    for item in _iter_block_items(document):
        if isinstance(item, Paragraph):
            text = item.text.strip()
            if text:
                blocks.append(text)
                paragraph_count += 1
        elif isinstance(item, Table):
            table_count += 1
            for row in item.rows:
                cells = _dedupe_consecutive([c.text.strip() for c in row.cells])
                line = _CELL_SEP.join(cells).strip(" |")
                if line:
                    # each row is its own block, so unrelated cells and rows never
                    # collapse into one sentence
                    blocks.append(line)

    extracted_text = "\n\n".join(blocks)
    _check_char_limit(extracted_text)
    warnings = []
    status = "ok"
    if not extracted_text.strip():
        status = "empty"
        warnings.append(
            "No text could be extracted from this DOCX. It may contain only "
            "images, text boxes, or floating shapes, which are not read."
        )
    return IngestionResult(
        extracted_text=extracted_text,
        file_type="docx",
        extraction_status=status,
        paragraph_count=paragraph_count,
        table_count=table_count,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Text-based PDF
# ---------------------------------------------------------------------------

def _ingest_pdf(data: bytes) -> IngestionResult:
    import pdfplumber

    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as exc:  # malformed or encrypted; never leak a stack trace
        message = str(exc).lower()
        if "password" in message or "encrypt" in message:
            raise IngestionError(
                "This PDF is encrypted or password-protected. Remove the "
                "protection and try again."
            )
        raise IngestionError(
            "This PDF could not be read. It may be malformed or corrupted."
        )

    warnings = []
    try:
        with pdf:
            page_count = len(pdf.pages)
            if page_count > MAX_PDF_PAGES:
                raise IngestionError(
                    f"This PDF has {page_count} pages, over the "
                    f"{MAX_PDF_PAGES}-page limit. Please split it into smaller files."
                )
            parts = []
            blank_pages = []
            for number, page in enumerate(pdf.pages, start=1):
                try:
                    text = page.extract_text() or ""
                except Exception:
                    text = ""
                parts.append(f"[Page {number}]")  # boundary; ignored by extraction
                if text.strip():
                    parts.append(text.strip())
                else:
                    blank_pages.append(number)
    except IngestionError:
        raise
    except Exception:
        raise IngestionError(
            "This PDF could not be read. It may be malformed or corrupted."
        )

    if len(blank_pages) == page_count:
        # No meaningful text anywhere: almost certainly scanned or image-only.
        raise IngestionError(_SCANNED_PDF_MESSAGE)

    if blank_pages:
        pages = ", ".join(str(p) for p in blank_pages)
        warnings.append(
            "Some pages produced no extractable text and may require manual "
            f"review: pages {pages}."
        )

    extracted_text = "\n\n".join(parts)
    _check_char_limit(extracted_text)
    return IngestionResult(
        extracted_text=extracted_text,
        file_type="pdf",
        extraction_status="partial" if blank_pages else "ok",
        page_count=page_count,
        warnings=warnings,
    )
