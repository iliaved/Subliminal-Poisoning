# Standalone scripts

These files can be copied out of the repository and used independently. They do
not import anything from the `subliminal_poison` package.

Use the injector only with documents and retrieval systems you own or are
explicitly authorized to assess.

## Hidden-instruction injector

`poison_pdf.py` is the original interactive injector with a compatibility fix
for current `pypdf` releases. It places the instruction in two off-page regions
and in the PDF `/Subject` metadata field.

Create an isolated environment and install its two dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-injector.txt
```

Run it interactively:

```bash
python poison_pdf.py
```

Or provide arguments:

```bash
python poison_pdf.py input.pdf -k "latte"
python poison_pdf.py input.pdf output.pdf -c "Your complete test instruction"
```

With the default `--reps 6`, the instruction is inserted six times below each
page and six times to its right, for twelve hidden copies per page.

## OCR sanitizer

`pdf_ocr.py` is already a standalone command-line program. It reconstructs a
new text-only PDF from rendered page pixels and writes a JSON audit report.

Install its Python dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-ocr.txt
```

It also requires Poppler and Tesseract:

```bash
# macOS with Homebrew
brew install poppler tesseract

# Debian or Ubuntu
sudo apt-get install poppler-utils tesseract-ocr
```

Run it with:

```bash
python pdf_ocr.py input.pdf output.clean.pdf
python pdf_ocr.py input.pdf output.clean.pdf --on-suspicious redact --jobs 4
```

Unless `--report` is provided, the report is written beside the output as
`output.clean.pdf.report.json`.

Exit codes are `0` for a clean result, `1` when output was written but
suspicious content was flagged, and `2` when no safe output was written.

