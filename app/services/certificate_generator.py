"""
certificate_generator.py – PDF certificate generation.

Design:
- Protocol class CertificateGenerator defines the interface so the
  processor depends on a protocol, not a concrete class, making testing
  easy: tests inject a fake generator without touching disk or PDF logic.
- PdfCertificateGenerator uses ReportLab canvas in landscape A4 with
  pageCompression=0 so that text is searchable in the raw PDF bytes
  (needed by tests using pypdf or raw-byte searches).
- Only built-in Helvetica fonts are used → no external font files required.
- Characters not representable in Helvetica's latin-1 encoding are
  replaced with '?' so that a bad name never crashes generation.
  (Known limitation: non-latin scripts are rendered as question marks.)
- File writing is atomic: bytes → temp file → os.replace() so no
  half-written PDFs are ever visible on disk.

Limitation documented in DESIGN_DECISIONS.md:
  Non-latin characters (CJK, Arabic, etc.) are silently replaced with '?'
  because Helvetica only covers ISO-8859-1.  A future improvement is to
  embed a Unicode font (e.g. DejaVu or Noto).
"""

from __future__ import annotations

import io
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from typing import Optional, Protocol, runtime_checkable

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas


# ---------------------------------------------------------------------------
# Data transferred from processor to generator.
# ---------------------------------------------------------------------------

@dataclass
class CertificateData:
    """Plain-data container for the information rendered on a certificate."""

    recipient_name: str
    event_name: str
    organization: str
    description: str          # e.g. "for successfully completing the workshop"
    issue_date: date
    certificate_number: str
    signatory_name: Optional[str] = None
    signatory_title: Optional[str] = None


# ---------------------------------------------------------------------------
# Protocol: what the processor depends on.
# ---------------------------------------------------------------------------

@runtime_checkable
class CertificateGenerator(Protocol):
    """Interface for certificate generators.

    Any callable class with this signature works – real PDF generator or
    a test stub.
    """

    def generate(self, data: CertificateData) -> bytes:
        """Generate a certificate and return raw PDF bytes."""
        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _format_date(d: date) -> str:
    """Return 'DD Month YYYY', e.g. '08 October 2026'."""
    return f"{d.day:02d} {_MONTH_NAMES[d.month]} {d.year}"


def _safe_latin1(text: str) -> str:
    """Replace characters outside latin-1 (Helvetica's range) with '?'.

    Prevents UnicodeEncodeError when ReportLab encodes strings.
    """
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _fit_text(
    c: canvas.Canvas,
    text: str,
    max_width: float,
    start_size: int = 36,
    min_size: int = 16,
) -> tuple[str, int]:
    """Return (text_to_draw, font_size) that fits within max_width.

    Reduces font size by 1pt at a time.  If the text still overflows at
    min_size, it is truncated with '…'.
    """
    from reportlab.pdfbase.pdfmetrics import stringWidth

    size = start_size
    while size >= min_size:
        if stringWidth(text, "Helvetica-Bold", size) <= max_width:
            return text, size
        size -= 1

    # Still too long at minimum size – truncate character by character.
    ellipsis = "…"
    while text and stringWidth(text + ellipsis, "Helvetica-Bold", min_size) > max_width:
        text = text[:-1]
    return text + ellipsis, min_size


# ---------------------------------------------------------------------------
# Concrete implementation.
# ---------------------------------------------------------------------------

class PdfCertificateGenerator:
    """Generates PDF certificates using ReportLab canvas.

    Layout: landscape A4, double decorative border, centered text.
    Only built-in Helvetica fonts are used.
    """

    def generate(self, data: CertificateData) -> bytes:
        """Return raw PDF bytes for the given CertificateData."""
        buf = io.BytesIO()
        page_width, page_height = landscape(A4)

        c = canvas.Canvas(buf, pagesize=landscape(A4), pageCompression=0)

        # ----------------------------------------------------------------
        # Borders
        # ----------------------------------------------------------------
        margin_outer = 10 * mm
        margin_inner = 14 * mm
        border_width = page_width - 2 * margin_outer
        border_height = page_height - 2 * margin_outer

        # Outer border
        c.setStrokeColorRGB(0.2, 0.2, 0.6)
        c.setLineWidth(3)
        c.rect(margin_outer, margin_outer, border_width, border_height)

        # Inner border (decorative double-line effect)
        inner_gap = 4 * mm
        c.setLineWidth(1)
        c.rect(
            margin_outer + inner_gap,
            margin_outer + inner_gap,
            border_width - 2 * inner_gap,
            border_height - 2 * inner_gap,
        )

        # ----------------------------------------------------------------
        # Content area
        # ----------------------------------------------------------------
        content_left = margin_inner + inner_gap
        content_right = page_width - content_left
        content_width = content_right - content_left

        # We'll lay out text top-to-bottom, tracking the y position.
        y = page_height - margin_inner - inner_gap - 10 * mm

        def draw_centered(text: str, font: str, size: int, y_pos: float) -> float:
            """Draw centered text and return updated y position."""
            safe = _safe_latin1(text)
            c.setFont(font, size)
            c.drawCentredString(page_width / 2, y_pos, safe)
            return y_pos - size * 1.4  # rough line height

        # Title
        y = draw_centered("CERTIFICATE OF COMPLETION", "Helvetica-Bold", 28, y)
        y -= 4 * mm

        # Decorative line under title
        c.setStrokeColorRGB(0.2, 0.2, 0.6)
        c.setLineWidth(1.5)
        line_half = 60 * mm
        c.line(
            page_width / 2 - line_half, y + 6 * mm,
            page_width / 2 + line_half, y + 6 * mm,
        )
        y -= 6 * mm

        # "This is to certify that"
        y = draw_centered("This is to certify that", "Helvetica", 14, y)
        y -= 3 * mm

        # Recipient name (auto-sized, bold)
        safe_name = _safe_latin1(data.recipient_name)
        fitted_name, name_size = _fit_text(
            c, safe_name, content_width * 0.85, start_size=36, min_size=16
        )
        c.setFont("Helvetica-Bold", name_size)
        c.setFillColorRGB(0.1, 0.1, 0.5)
        c.drawCentredString(page_width / 2, y, fitted_name)
        c.setFillColorRGB(0, 0, 0)
        y -= name_size * 1.4 + 2 * mm

        # Underline under name
        from reportlab.pdfbase.pdfmetrics import stringWidth
        name_w = min(
            stringWidth(fitted_name, "Helvetica-Bold", name_size),
            content_width * 0.85,
        )
        c.setLineWidth(0.8)
        c.setStrokeColorRGB(0.1, 0.1, 0.5)
        c.line(
            page_width / 2 - name_w / 2, y + name_size * 0.3,
            page_width / 2 + name_w / 2, y + name_size * 0.3,
        )
        c.setStrokeColorRGB(0, 0, 0)
        y -= 4 * mm

        # Description line
        desc = _safe_latin1(data.description)
        y = draw_centered(desc, "Helvetica", 13, y)
        y -= 1 * mm

        # Event name
        safe_event = _safe_latin1(data.event_name)
        y = draw_centered(safe_event, "Helvetica-Bold", 15, y)
        y -= 1 * mm

        # "organized by <organization>"
        safe_org = _safe_latin1(data.organization)
        y = draw_centered(f"organized by {safe_org}", "Helvetica", 13, y)
        y -= 5 * mm

        # Issue date
        formatted_date = _format_date(data.issue_date)
        y = draw_centered(formatted_date, "Helvetica", 12, y)
        y -= 8 * mm

        # ----------------------------------------------------------------
        # Bottom section: certificate number left, signatory right
        # ----------------------------------------------------------------
        bottom_y = margin_outer + inner_gap + 12 * mm
        left_x = margin_inner + inner_gap + 10 * mm
        right_x = page_width - margin_inner - inner_gap - 10 * mm

        # Certificate number (bottom-left)
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.4, 0.4, 0.4)
        c.drawString(left_x, bottom_y + 4 * mm, "Certificate No.")
        c.setFont("Helvetica-Bold", 10)
        c.setFillColorRGB(0.2, 0.2, 0.6)
        c.drawString(left_x, bottom_y, _safe_latin1(data.certificate_number))

        # Signatory block (bottom-right), if provided
        if data.signatory_name:
            c.setFillColorRGB(0, 0, 0)
            # Signature line
            sig_right = right_x
            sig_left = sig_right - 50 * mm
            c.setLineWidth(0.8)
            c.line(sig_left, bottom_y + 8 * mm, sig_right, bottom_y + 8 * mm)
            c.setFont("Helvetica-Bold", 10)
            safe_sig_name = _safe_latin1(data.signatory_name)
            c.drawCentredString(
                (sig_left + sig_right) / 2, bottom_y + 2 * mm, safe_sig_name
            )
            if data.signatory_title:
                c.setFont("Helvetica", 9)
                c.setFillColorRGB(0.4, 0.4, 0.4)
                safe_sig_title = _safe_latin1(data.signatory_title)
                c.drawCentredString(
                    (sig_left + sig_right) / 2, bottom_y - 3 * mm, safe_sig_title
                )

        c.setFillColorRGB(0, 0, 0)
        c.save()
        return buf.getvalue()


# ---------------------------------------------------------------------------
# Atomic file write helper (used by processor).
# ---------------------------------------------------------------------------

def write_pdf_atomically(pdf_bytes: bytes, dest_path: str) -> None:
    """Write *pdf_bytes* to *dest_path* atomically using a temp file.

    Strategy:
    1. Write to a temp file in the same directory as the destination.
    2. Use os.replace() to rename it – atomic on POSIX, best-effort on
       Windows (still prevents reading a half-written file because the old
       file is replaced only after the new one is fully written).

    This ensures that a crash during write never leaves a corrupt PDF at
    the destination path.
    """
    dest_dir = os.path.dirname(dest_path)
    os.makedirs(dest_dir, exist_ok=True)

    # Write to a temp file in the same directory so os.replace is on the
    # same filesystem (required for an atomic rename on Linux).
    fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pdf_bytes)
        os.replace(tmp_path, dest_path)
    except Exception:
        # Clean up the temp file if anything goes wrong.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

def get_generator() -> CertificateGenerator:
    """FastAPI dependency: returns the singleton PDF generator.

    Tests override this via app.dependency_overrides[get_generator] = ...
    """
    return PdfCertificateGenerator()
