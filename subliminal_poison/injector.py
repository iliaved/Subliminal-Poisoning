from __future__ import annotations

import io
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas


class InjectionError(ValueError):
    """Raised when a PDF or injection prompt cannot be processed."""


def _clean_prompt(prompt: str) -> str:
    clean = " ".join(prompt.split()).strip()
    if not clean:
        raise InjectionError("The injection prompt cannot be empty.")
    if len(clean) > 2_000:
        raise InjectionError("The injection prompt must be 2,000 characters or fewer.")
    return clean


def build_overlay(width: float, height: float, instruction: str, reps: int = 6):
    """Build the same hidden overlay used by the original reference script."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(width, height), pageCompression=0)
    pdf.setFont("Helvetica", 6)

    positions = []
    for index in range(reps):
        offset = index // 2
        if index % 2 == 0:
            positions.append((18, -20 - offset * 12))
        else:
            positions.append((width + 40, height - 40 - offset * 12))

    for x, y in positions:
        text = pdf.beginText(x, y)
        text.setTextRenderMode(3)
        text.textLine(instruction)
        pdf.drawText(text)

    pdf.save()
    buffer.seek(0)
    return PdfReader(buffer).pages[0]


def poison_pdf_bytes(source: bytes, prompt: str, reps: int = 6) -> tuple[bytes, str, int]:
    """Apply the original hidden overlay and metadata injection to PDF bytes."""
    instruction = _clean_prompt(prompt)
    if not 1 <= reps <= 12:
        raise InjectionError("Repetitions must be between 1 and 12.")

    try:
        reader = PdfReader(io.BytesIO(source), strict=False)
        if reader.is_encrypted:
            reader.decrypt("")
        if not reader.pages:
            raise InjectionError("The PDF has no pages.")
    except InjectionError:
        raise
    except Exception as exc:
        raise InjectionError("The uploaded file is not a readable, unlocked PDF.") from exc

    writer = PdfWriter()
    for source_page in reader.pages:
        writer.add_page(source_page)
        page = writer.pages[-1]
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        page.merge_page(build_overlay(width, height, instruction, reps), expand=False)

    metadata = {
        str(key): str(value)
        for key, value in dict(reader.metadata or {}).items()
        if value is not None
    }
    metadata["/Subject"] = instruction
    writer.add_metadata(metadata)

    output = io.BytesIO()
    writer.write(output)
    return output.getvalue(), instruction, len(reader.pages)


def poison(
    inp: str | Path,
    outp: str | Path,
    prompt: str,
    reps: int = 6,
) -> tuple[str, int]:
    """Apply an exact, caller-supplied prompt to a PDF path."""
    content, instruction, pages = poison_pdf_bytes(Path(inp).read_bytes(), prompt, reps)
    Path(outp).write_bytes(content)
    return instruction, pages

