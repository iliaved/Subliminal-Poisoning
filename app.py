from __future__ import annotations

import re
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from subliminal_poison.injector import InjectionError, poison_pdf_bytes
from subliminal_poison.llm import OpenAIHelper

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

load_dotenv(BASE_DIR / ".env")
app = FastAPI(title="Subliminal Poison", version="2.0.0")
helper = OpenAIHelper()


class SuggestRequest(BaseModel):
    objective: str = Field(min_length=3, max_length=800)


async def _read_pdf(file: UploadFile) -> bytes:
    if file.content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(status_code=415, detail="Upload a PDF file.")
    source = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(source) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="PDFs are limited to 30 MB.")
    if not source.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="The uploaded file is not a PDF.")
    return source


def _safe_stem(name: str | None) -> str:
    stem = Path(name or "document.pdf").stem[:80]
    return SAFE_NAME_RE.sub("-", stem).strip("-.") or "document"


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "openai_configured": helper.configured}


@app.post("/api/suggest")
def suggest(payload: SuggestRequest) -> dict:
    objective = " ".join(payload.objective.split()).strip()
    return helper.suggest_prompt(objective)


@app.post("/api/inject")
async def inject(
    file: UploadFile = File(...),
    prompt: str = Form(...),
    authorized: bool = Form(False),
) -> Response:
    if not authorized:
        raise HTTPException(status_code=400, detail="Confirm that you are authorized to test this document.")
    source = await _read_pdf(file)
    try:
        output, _, _ = poison_pdf_bytes(source, prompt, reps=6)
    except InjectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    filename = f"{_safe_stem(file.filename)}--injected.pdf"
    return Response(
        output,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

