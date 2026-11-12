#!/usr/bin/env python3
"""
pdf_ocr.py - rebuild an untrusted PDF from its pixels before it reaches an LLM.

Why
---
RAG poisoning through PDFs relies on a gap between what a human sees on the page
and what a text extractor hands to the model: invisible text layers, white-on-white
or near-white text, 1pt fonts, text outside the crop box or under images, remapped
font tables (glyphs that display one thing and extract as another), annotations,
form fields, metadata, bookmarks, attachments and JavaScript.

How
---
Nothing from the source file is copied into the output. For every page:

  1. RENDER   poppler rasterises the page (crop box only, annotations off) in a
              separate, resource-limited process. Only pixels cross the boundary.
  2. GATE     pixels whose contrast against the local background is below
              --min-contrast (WCAG contrast ratio) are erased, so text a person
              could not read never reaches the OCR engine.
  3. OCR      Tesseract reads the gated raster. Words below --min-conf and text
              smaller than --min-text-pt are dropped.
  4. CLEAN    NFKC normalisation; control, format, zero-width, bidi and tag
              characters are stripped.
  5. AUDIT    the original text layer is compared with the OCR result (text a
              naive extractor would ingest but a reader cannot see), and visible
              text is scanned for prompt-injection phrasing.
  6. REBUILD  a new text-only PDF is typeset from the OCR text alone, with fresh
              metadata. Figures and layout are intentionally not carried over.

Limits
------
OCR removes what is hidden. It cannot remove an instruction that is plainly
printed on the page; that is only flagged (or removed with --on-suspicious redact).
The contrast and size gates are thresholds: text tuned to sit just above them
is hard to read but will pass. Keep treating retrieved text as data, not
instructions, downstream, and run this tool in a sandbox without network
access, since poppler is parsing a hostile file.

Usage
-----
  python pdf_ocr.py book.pdf book.clean.pdf
  python pdf_ocr.py book.pdf book.clean.pdf --on-suspicious redact --jobs 8

Exit codes: 0 clean | 1 output written, suspicious content flagged | 2 no output written

Requires: poppler-utils (pdfinfo, pdftoppm, pdftotext), tesseract-ocr,
          pip install pytesseract pillow numpy reportlab
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import multiprocessing as mp
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from collections import Counter
from xml.sax.saxutils import escape

import numpy as np
import pytesseract
from PIL import Image, ImageFilter
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate

try:
    import resource  # POSIX only
except ImportError:  # pragma: no cover
    resource = None

# --------------------------------------------------------------------------- limits

MAX_PIXELS = 40_000_000          # per-page raster cap (letter @ 300 dpi is ~8.4 MP)
MIN_EFFECTIVE_DPI = 100          # below this OCR is unreliable; the page is skipped
POPPLER_MEMORY_BYTES = 4 << 30   # address-space cap for the process parsing the PDF
INFO_TIMEOUT, RENDER_TIMEOUT, TEXT_TIMEOUT, OCR_TIMEOUT = 60, 120, 60, 300

# Hidden-text audit thresholds (detection only; removal does not depend on them).
HIDDEN_LINE_MIN_MISSING = 3      # tokens of a text-layer line absent from the OCR
HIDDEN_LINE_MIN_RATIO = 0.4
MISMATCH_MIN_TOKENS = 20
MISMATCH_RATIO = 0.25

Image.MAX_IMAGE_PIXELS = MAX_PIXELS * 2

REDACTION_NOTE = "[removed by sanitizer: suspected prompt injection]"

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/TTF/DejaVuSerif.ttf",
    "/usr/share/fonts/dejavu-serif-fonts/DejaVuSerif.ttf",
    "/opt/homebrew/share/fonts/DejaVuSerif.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]

# Heuristics for instructions aimed at a model. They catch the common phrasing,
# not a determined author, and will fire on books that discuss prompt injection.
_AI = r"(?:ai|a\.i\.|llm|assistant|chatbot|language model|model|agent|gpt|claude|gemini|copilot)"
INJECTION_PATTERNS = [(name, re.compile(rx, re.I)) for name, rx in [
    ("override_instructions",
     r"\b(?:ignore|disregard|forget|override|bypass)\b.{0,40}\b(?:previous|prior|above|earlier|preceding|"
     r"original|system|all)\b.{0,30}\b(?:instructions?|prompts?|rules|directions|guidelines|context|messages?)"),
    ("addresses_the_model",
     rf"\b(?:attention|note|message|instructions?|directive|memo)\b.{{0,15}}\b(?:to|for)\b\s+(?:the\s+|any\s+|all\s+)?{_AI}\b"),
    ("role_reassignment",
     r"\b(?:you are now|from now on,? you|you must now|act as (?:an?|the)\b|pretend (?:to be|you are)|"
     r"your new (?:role|task|instructions?|goal))"),
    ("prompt_reference",
     r"\b(?:system|developer|hidden|initial)\s+(?:prompt|message|instructions?)\b"),
    ("conceal_from_user",
     r"\b(?:do not|don't|never)\s+(?:tell|inform|reveal|mention|disclose|show)\b.{0,50}\b(?:user|human|reader|operator|anyone)\b"),
    ("output_steering",
     r"\bwhen\s+(?:summari[sz]ing|answering|responding|asked|replying|reviewing)\b.{0,80}\b(?:always|must|never|only)\b"),
    ("new_instructions", r"\b(?:new|updated|real|important|urgent)\s+instructions?\s*:"),
    ("chat_template_token",
     r"<\|?\s*(?:im_start|im_end|system|assistant|user|endoftext)\s*\|?>|\[/?INST\]|<<\s*/?SYS\s*>>|"
     r"^\s*#{2,}\s*(?:system|instruction)"),
    ("exfiltration",
     r"\b(?:send|post|forward|upload|transmit|email|append|include)\b.{0,80}"
     r"(?:https?://|\b(?:api[ _-]?keys?|passwords?|credentials|secrets?|tokens?|conversation history)\b)"),
    ("tool_abuse", r"\b(?:call|invoke|use|run|execute)\s+the\s+[\w\- ]{1,30}\s+(?:tool|function|plugin)\b"),
]]

_WORD = re.compile(r"[^\W_]{2,}")


# --------------------------------------------------------------------------- helpers

def _child_limits():
    """Cap memory for the process that parses the untrusted file."""
    try:
        resource.setrlimit(resource.RLIMIT_AS, (POPPLER_MEMORY_BYTES, POPPLER_MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        pass


def run_poppler(cmd: list[str], timeout: int) -> bytes:
    """Run a poppler tool on the untrusted PDF: no shell, timeout, memory cap."""
    proc = subprocess.run(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, preexec_fn=_child_limits if resource else None,
    )
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"{cmd[0]} failed: {msg[-1] if msg else proc.returncode}")
    return proc.stdout


def clean_text(s: str) -> str:
    """NFKC-normalise and drop every control/format/private/unassigned code point."""
    s = unicodedata.normalize("NFKC", s)
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat[0] == "C":          # Cc, Cf (zero-width, bidi, tags), Cs, Co, Cn
            continue
        out.append(" " if cat[0] == "Z" or ch.isspace() else ch)
    return " ".join("".join(out).split())


def word_tokens(s: str) -> list[str]:
    return _WORD.findall(unicodedata.normalize("NFKC", s).lower())


def excerpt(s: str, n: int = 200) -> str:
    s = clean_text(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def find_injections(text: str) -> list[str]:
    return [name for name, rx in INJECTION_PATTERNS if rx.search(text)]


# --------------------------------------------------------------------------- inspect

def inspect_pdf(pdf: str) -> dict:
    """Page count, page sizes and risk indicators, via pdfinfo."""
    out = run_poppler(["pdfinfo", "-f", "1", "-l", "1000000", pdf], INFO_TIMEOUT).decode("utf-8", "replace")
    fields = {}
    sizes = {}
    for line in out.splitlines():
        m = re.match(r"Page\s+(\d+)\s+size:\s+([\d.]+)\s+x\s+([\d.]+)\s+pts", line)
        if m:
            sizes[int(m.group(1))] = (float(m.group(2)), float(m.group(3)))
        elif ":" in line and not line.startswith("Page "):
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    pages = int(fields.get("Pages", "0"))
    if pages < 1:
        raise RuntimeError("no pages found")
    metadata_hits = []
    for key in ("Title", "Subject", "Keywords", "Author", "Creator", "Producer"):
        hits = find_injections(clean_text(fields.get(key, "")))
        if hits:
            metadata_hits.append({"field": key, "patterns": hits, "excerpt": excerpt(fields[key])})
    return {
        "pages": pages,
        "sizes": sizes,
        "encrypted": fields.get("Encrypted", "no").split()[0] != "no",
        "javascript": fields.get("JavaScript", "no") != "no",
        "forms": fields.get("Form", "none"),
        "metadata_hits": metadata_hits,
    }


# --------------------------------------------------------------------------- per page

def render_page(pdf: str, page_no: int, dpi: int, annotations: bool, hide_flag: bool) -> Image.Image:
    cmd = ["pdftoppm", "-r", str(dpi), "-f", str(page_no), "-l", str(page_no), "-cropbox"]
    if not annotations and hide_flag:
        cmd.append("-hide-annotations")
    cmd.append(pdf)
    data = run_poppler(cmd, RENDER_TIMEOUT)
    img = Image.open(io.BytesIO(data))
    img.load()
    if img.width * img.height > MAX_PIXELS * 1.5:
        raise RuntimeError("rendered page exceeds pixel cap")
    return img.convert("RGB")


# sRGB -> linear, pre-weighted per channel for relative luminance (WCAG 2.x).
_LIN = np.array([(c / 255 / 12.92) if c / 255 <= 0.04045 else ((c / 255 + 0.055) / 1.055) ** 2.4
                 for c in range(256)], dtype=np.float32)
_LUT_R, _LUT_G, _LUT_B = _LIN * 0.2126, _LIN * 0.7152, _LIN * 0.0722


def contrast_gate(img: Image.Image, dpi: int, min_contrast: float) -> Image.Image:
    """
    Return a pure black-on-white raster holding only marks a person could read.

    Two envelopes are built from the page luminance: the light envelope (what
    the area looks like with dark marks up to ~0.5in wide removed) and the dark
    envelope (the same with light marks removed). A pixel is ink only if its
    WCAG contrast against the envelope behind it reaches `min_contrast`.
    Dark-on-light is the default reading; where a neighbourhood is
    overwhelmingly dark, light marks are treated as the ink instead, so white
    text in a dark bar is still read. The result is hard-thresholded: two marks
    that both pass, or both fail, are indistinguishable to the OCR engine.
    """
    rgb = np.asarray(img)
    lum = _LUT_R[rgb[..., 0]]
    lum = lum + _LUT_G[rgb[..., 1]]
    lum += _LUT_B[rgb[..., 2]]
    h, w = lum.shape

    # Work on ~1/40 inch cells; min/max pooling keeps thin strokes visible.
    f = max(1, round(dpi / 40))
    enc = (np.sqrt(lum) * 255 + 0.5).astype(np.uint8)
    ph, pw = -h % f, -w % f
    cells = np.pad(enc, ((0, ph), (0, pw)), mode="edge").reshape((h + ph) // f, f, (w + pw) // f, f)
    cell_max, cell_min, cell_mean = cells.max((1, 3)), cells.min((1, 3)), cells.mean((1, 3))

    k = max(3, int(0.5 * dpi / f)) | 1               # envelope scale, ~0.5 inch
    light_env = np.asarray(Image.fromarray(cell_max)
                           .filter(ImageFilter.MaxFilter(k)).filter(ImageFilter.MinFilter(k)))
    dark_env = np.asarray(Image.fromarray(cell_min)
                          .filter(ImageFilter.MinFilter(k)).filter(ImageFilter.MaxFilter(k)))

    def to_lum(a):
        a = a.astype(np.float32) / 255.0
        return a * a + 0.05

    c, o, m = to_lum(light_env), to_lum(dark_env), to_lum(cell_mean)
    active = c / o >= min_contrast                   # cells with real contrast nearby
    darkish = active & (m / o < c / m)               # ...that resemble the dark envelope
    v = max(3, int(0.35 * dpi / f)) // 2             # polarity neighbourhood, ~0.35 inch

    def share(mask):
        im = Image.fromarray(mask.astype(np.uint8) * 255).filter(ImageFilter.BoxBlur(v))
        return np.asarray(im, dtype=np.float32)

    n_active = share(active)
    inverse = (n_active > 0) & (share(darkish) >= 0.7 * n_active)

    def full(a):
        return np.repeat(np.repeat(a, f, axis=0), f, axis=1)[:h, :w]

    lum += 0.05
    ratio = np.where(full(inverse), lum / full(o), full(c) / lum)
    return Image.fromarray(np.where(ratio >= min_contrast, 0, 255).astype(np.uint8))


def join_lines(lines: list[str]) -> str:
    """Join OCR lines into a paragraph, undoing end-of-line hyphenation."""
    out = ""
    for line in lines:
        if not out:
            out = line
        elif (len(out) > 1 and out[-1] in "-\u2010" and out[-2].isalpha()
              and line[:1].isalpha() and line[:1].islower()):
            out = out[:-1] + line
        else:
            out += " " + line
    return out


def paragraphs_from_ocr(d: dict, dpi: int, min_conf: float, min_text_pt: float):
    """Group Tesseract word rows into paragraphs, applying confidence and size gates."""
    min_px = min_text_pt * dpi / 72.0
    lines: dict[tuple, list[tuple[str, int]]] = {}
    low_conf = tiny = 0
    for i, raw in enumerate(d["text"]):
        if d["level"][i] != 5:
            continue
        word = clean_text(raw)
        if not word or not word.strip("|¦_"):         # empty, or a ruling line read as a glyph
            continue
        if float(d["conf"][i]) < min_conf:
            low_conf += 1
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        lines.setdefault(key, []).append((word, int(d["height"][i])))

    paras: dict[tuple, list[str]] = {}
    kept_words: list[str] = []
    for key, words in lines.items():                 # dicts preserve reading order
        kept = []
        readable = 0
        for word, height in words:
            has_alnum = any(c.isalnum() for c in word)
            if has_alnum and height < min_px:
                tiny += 1
                continue
            readable += has_alnum
            kept.append(word)
        if not readable:                             # nothing but stray punctuation
            continue
        kept_words.extend(kept)
        paras.setdefault(key[:2], []).append(" ".join(kept))
    merged: list[str] = []
    for para in (join_lines(v) for v in paras.values()):
        if merged:
            joined = join_lines([merged[-1], para])
            if len(joined) < len(merged[-1]) + len(para):   # hyphen removed: one word spanned the break
                merged[-1] = joined
                continue
        merged.append(para)
    return merged, kept_words, low_conf, tiny


def audit_text_layer(layer: str, ocr_text: str):
    """Find text a naive extractor would ingest but that is not visibly on the page."""
    ocr_counts = Counter(word_tokens(ocr_text))
    layer_tokens = word_tokens(layer)
    missing = sum((Counter(layer_tokens) - ocr_counts).values())
    hidden_lines = []
    for line in layer.splitlines():
        toks = word_tokens(line)
        absent = [t for t in toks if t not in ocr_counts]
        if len(absent) >= HIDDEN_LINE_MIN_MISSING and len(absent) >= HIDDEN_LINE_MIN_RATIO * len(toks):
            hidden_lines.append(excerpt(line))
    return len(layer_tokens), missing, hidden_lines


def process_page(task: dict) -> dict:
    n = task["page"]
    res = {"page": n, "paragraphs": [], "flags": [], "hidden_lines": [], "injection_hits": [],
           "ocr_words": 0, "dropped_low_confidence": 0, "dropped_tiny": 0,
           "text_layer_tokens": 0, "text_layer_tokens_not_visible": 0, "redacted": 0, "error": None}
    try:
        w_pt, h_pt = task["size"]
        dpi = min(task["dpi"], int(math.sqrt(MAX_PIXELS / max(1e-6, (w_pt / 72) * (h_pt / 72)))))
        if dpi < MIN_EFFECTIVE_DPI:
            raise RuntimeError("page too large to rasterise safely")

        img = render_page(task["pdf"], n, dpi, task["annotations"], task["hide_flag"])
        gated = contrast_gate(img, dpi, task["min_contrast"])
        del img
        data = pytesseract.image_to_data(
            gated, lang=task["lang"], config=f"--dpi {dpi} --psm 3",
            output_type=pytesseract.Output.DICT, timeout=OCR_TIMEOUT,
        )
        paras, kept_words, low_conf, tiny = paragraphs_from_ocr(
            data, dpi, task["min_conf"], task["min_text_pt"])
        res.update(ocr_words=len(kept_words), dropped_low_confidence=low_conf, dropped_tiny=tiny)

        # Audit 1: text layer vs. what is visibly on the page.
        try:
            layer = run_poppler(["pdftotext", "-f", str(n), "-l", str(n), "-enc", "UTF-8",
                                 task["pdf"], "-"], TEXT_TIMEOUT).decode("utf-8", "replace")
        except Exception:
            layer = ""
        total, missing, hidden = audit_text_layer(layer, " ".join(kept_words) + " " + " ".join(paras))
        res.update(text_layer_tokens=total, text_layer_tokens_not_visible=missing,
                   hidden_lines=hidden[:25])
        if hidden:
            res["flags"].append("hidden_text")
        if total >= MISMATCH_MIN_TOKENS and missing >= MISMATCH_RATIO * total:
            res["flags"].append("text_layer_mismatch")

        # Audit 2: visible text that reads like an instruction to a model.
        for i, para in enumerate(paras):
            hits = find_injections(para)
            if hits:
                res["injection_hits"].append({"patterns": hits, "excerpt": excerpt(para, 300)})
                if task["redact"]:
                    paras[i] = REDACTION_NOTE
                    res["redacted"] += 1
        if res["injection_hits"]:
            res["flags"].append("injection_phrasing")
        res["paragraphs"] = paras
    except Exception as exc:  # fail closed: the page contributes no text
        res["error"] = f"{type(exc).__name__}: {exc}"[:300]
        res["flags"].append("page_error")
    return res


# --------------------------------------------------------------------------- output

def build_pdf(path: str, results: list[dict], font_path: str | None, page_markers: bool) -> None:
    font = "Helvetica"
    if font_path:
        pdfmetrics.registerFont(TTFont("SanitizerBody", font_path))
        font = "SanitizerBody"

    def safe(text: str) -> str:
        if font == "Helvetica":                      # built-in fonts are Latin-1 only
            text = text.encode("cp1252", "replace").decode("cp1252")
        return escape(text)                          # OCR text must never act as markup

    body = ParagraphStyle("body", fontName=font, fontSize=10.5, leading=14.5, spaceAfter=7,
                          splitLongWords=1)
    marker = ParagraphStyle("marker", fontName=font, fontSize=8.5, leading=11, spaceAfter=9,
                            textColor=HexColor("#555555"))
    story = []
    for r in results:
        if page_markers:
            story.append(Paragraph(f"[source page {r['page']}]", marker))
        if r["error"]:
            story.append(Paragraph("[page could not be processed and was omitted]", body))
        for para in r["paragraphs"]:
            story.append(Paragraph(safe(para), body))
        story.append(PageBreak())

    # Metadata is written fresh. The source title and filename are attacker-controlled.
    doc = SimpleDocTemplate(
        path, pagesize=letter, leftMargin=inch, rightMargin=inch, topMargin=0.9 * inch,
        bottomMargin=0.9 * inch, title="OCR-sanitized document", author="pdf_ocr_sanitizer",
        subject="Text reconstructed from rendered page pixels", creator="pdf_ocr_sanitizer",
    )
    doc.build(story)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- main

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Rebuild an untrusted PDF from OCR of its rendered pages.")
    p.add_argument("input", help="untrusted source PDF")
    p.add_argument("output", help="sanitized PDF to write")
    p.add_argument("--report", help="audit report path (default: OUTPUT + .report.json)")
    p.add_argument("--dpi", type=int, default=300, help="render resolution (default 300)")
    p.add_argument("--lang", default="eng", help="Tesseract language(s), e.g. eng or eng+deu")
    p.add_argument("--min-contrast", type=float, default=2.0,
                   help="WCAG contrast ratio below which marks are erased before OCR (default 2.0)")
    p.add_argument("--min-text-pt", type=float, default=3.0,
                   help="drop words whose glyph height is under this many points (default 3.0, about a 4pt font)")
    p.add_argument("--min-conf", type=float, default=30, help="drop OCR words below this confidence (default 30)")
    p.add_argument("--on-suspicious", choices=["flag", "redact", "fail"], default="flag",
                   help="flag: report only. redact: also drop paragraphs with injection phrasing. "
                        "fail: write no PDF if anything is flagged")
    p.add_argument("--render-annotations", action="store_true",
                   help="include annotations and form widgets in the render (off by default)")
    p.add_argument("--no-page-markers", action="store_true", help="omit the [source page N] lines")
    p.add_argument("--font", help="TTF used for the output (needed for scripts DejaVu lacks)")
    p.add_argument("--jobs", type=int, default=os.cpu_count() or 1, help="parallel workers")
    p.add_argument("--max-pages", type=int, default=3000, help="refuse documents longer than this")
    return p.parse_args(argv)


def main(argv=None, progress=None) -> int:
    a = parse_args(argv)
    src, dst = os.path.abspath(a.input), os.path.abspath(a.output)
    report_path = os.path.abspath(a.report or dst + ".report.json")
    log = lambda m: print(m, file=sys.stderr, flush=True)

    for tool in ("pdfinfo", "pdftoppm", "pdftotext", "tesseract"):
        if not shutil.which(tool):
            log(f"error: '{tool}' not found on PATH")
            return 2
    if not os.path.isfile(src):
        log(f"error: no such file: {src}")
        return 2
    if src in (dst, report_path):
        log("error: output must not overwrite the input")
        return 2
    if not re.fullmatch(r"[A-Za-z0-9_+]+", a.lang):
        log("error: invalid --lang")
        return 2
    if not 72 <= a.dpi <= 600:
        log("error: --dpi must be between 72 and 600")
        return 2
    font_path = a.font or next((f for f in FONT_CANDIDATES if os.path.isfile(f)), None)
    if a.font and not os.path.isfile(a.font):
        log(f"error: font not found: {a.font}")
        return 2
    if not font_path:
        log("warning: no Unicode TTF found; non-Latin-1 characters will be replaced (use --font)")

    try:
        info = inspect_pdf(src)
    except Exception as exc:
        log(f"error: cannot read PDF ({exc})")
        return 2
    if info["pages"] > a.max_pages:
        log(f"error: {info['pages']} pages exceeds --max-pages {a.max_pages}")
        return 2
    if progress:
        progress(5, "PDF inspected. Preparing page renders.", 0, info["pages"])

    help_text = subprocess.run(["pdftoppm", "-h"], capture_output=True, text=True)
    hide_flag = "-hide-annotations" in (help_text.stdout + help_text.stderr)
    if not hide_flag and not a.render_annotations:
        log("warning: this poppler cannot hide annotations; visible annotations will be OCRed")

    jobs = max(1, min(a.jobs, info["pages"]))
    if jobs > 1:
        os.environ.setdefault("OMP_THREAD_LIMIT", "1")   # one Tesseract thread per worker
    tasks = [{
        "pdf": src, "page": n, "size": info["sizes"].get(n, (612.0, 792.0)), "dpi": a.dpi,
        "lang": a.lang, "min_contrast": a.min_contrast, "min_text_pt": a.min_text_pt,
        "min_conf": a.min_conf, "annotations": a.render_annotations, "hide_flag": hide_flag,
        "redact": a.on_suspicious == "redact",
    } for n in range(1, info["pages"] + 1)]

    results = []
    if jobs == 1:
        iterator = map(process_page, tasks)
        pool = None
    else:
        pool = mp.Pool(jobs)
        iterator = pool.imap(process_page, tasks)
    try:
        for r in iterator:
            results.append(r)
            note = f"  {', '.join(r['flags'])}" if r["flags"] else ""
            log(f"page {r['page']}/{info['pages']}: {r['ocr_words']} words{note}")
            if progress:
                percent = 10 + round(75 * len(results) / info["pages"])
                progress(
                    percent,
                    f"OCR page {len(results)} of {info['pages']}.",
                    len(results),
                    info["pages"],
                )
    finally:
        if pool:
            pool.close()
            pool.join()

    flagged = [r["page"] for r in results if r["flags"]]
    doc_flags = [k for k in ("encrypted", "javascript") if info[k]]
    if info["metadata_hits"]:
        doc_flags.append("metadata_injection_phrasing")
    suspicious = bool(flagged or info["metadata_hits"])
    blocked = suspicious and a.on_suspicious == "fail"

    report = {
        "tool": "pdf_ocr_sanitizer",
        "warning": "This report quotes untrusted document text. Do not feed it to an LLM.",
        "input_sha256": sha256(src),
        "output_written": not blocked,
        "settings": {k: getattr(a, k) for k in (
            "dpi", "lang", "min_contrast", "min_text_pt", "min_conf", "on_suspicious", "render_annotations")},
        "document": {"pages": info["pages"], "encrypted": info["encrypted"],
                     "javascript": info["javascript"], "forms": info["forms"],
                     "metadata_hits": info["metadata_hits"], "flags": doc_flags},
        "summary": {
            "flagged_pages": flagged,
            "hidden_text_pages": [r["page"] for r in results if "hidden_text" in r["flags"]],
            "text_layer_mismatch_pages": [r["page"] for r in results if "text_layer_mismatch" in r["flags"]],
            "injection_phrasing_pages": [r["page"] for r in results if "injection_phrasing" in r["flags"]],
            "error_pages": [r["page"] for r in results if r["error"]],
            "paragraphs_redacted": sum(r["redacted"] for r in results),
            "words_kept": sum(r["ocr_words"] for r in results),
            "words_dropped_low_confidence": sum(r["dropped_low_confidence"] for r in results),
            "words_dropped_tiny": sum(r["dropped_tiny"] for r in results),
        },
        "pages": [{k: v for k, v in r.items() if k != "paragraphs"} for r in results],
    }

    if not blocked:
        tmp = dst + ".partial"
        try:
            if progress:
                progress(90, "Rebuilding the clean PDF.", info["pages"], info["pages"])
            build_pdf(tmp, results, font_path, not a.no_page_markers)
            os.replace(tmp, dst)
            report["output_sha256"] = sha256(dst)
            if progress:
                progress(97, "Writing the audit summary.", info["pages"], info["pages"])
        except Exception as exc:
            if os.path.exists(tmp):
                os.remove(tmp)
            log(f"error: could not build output PDF ({exc})")
            report["output_written"] = False
            blocked = True

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    s = report["summary"]
    log(f"\n{info['pages']} pages, {s['words_kept']} words kept, "
        f"{s['words_dropped_tiny']} tiny and {s['words_dropped_low_confidence']} low-confidence words dropped")
    log(f"hidden text on pages: {s['hidden_text_pages'] or 'none'}")
    log(f"injection phrasing on pages: {s['injection_phrasing_pages'] or 'none'}"
        + (f" ({s['paragraphs_redacted']} paragraphs redacted)" if s["paragraphs_redacted"] else ""))
    if doc_flags:
        log(f"document flags: {', '.join(doc_flags)}")
    if s["error_pages"]:
        log(f"pages omitted after errors: {s['error_pages']}")
    log(f"report: {report_path}")
    if blocked:
        log("NO OUTPUT WRITTEN")
        return 2
    log(f"output: {dst}")
    return 1 if suspicious or doc_flags else 0


if __name__ == "__main__":
    sys.exit(main())
