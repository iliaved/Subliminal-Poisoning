from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from subliminal_poison.injector import InjectionError, poison_pdf_bytes
from subliminal_poison.llm import OpenAIHelper

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
SANITIZER_TOOLS = ("pdfinfo", "pdftoppm", "pdftotext", "tesseract")
SANITIZER_JOBS: dict[str, dict] = {}
SANITIZER_JOBS_LOCK = threading.Lock()

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


def _missing_sanitizer_tools() -> list[str]:
    return [tool for tool in SANITIZER_TOOLS if not shutil.which(tool)]


def _run_sanitizer(
    args: list[str],
    progress: Callable[[int, str, int, int], None] | None = None,
) -> int:
    from subliminal_poison.pdf_ocr import main

    return main(args, progress=progress)


def _cleanup_sanitizer_job(job_id: str) -> None:
    with SANITIZER_JOBS_LOCK:
        job = SANITIZER_JOBS.pop(job_id, None)
    if job:
        shutil.rmtree(job["workdir"], ignore_errors=True)


def _process_sanitizer_job(job_id: str, args: list[str]) -> None:
    def update_progress(percent: int, message: str, processed: int, total: int) -> None:
        with SANITIZER_JOBS_LOCK:
            job = SANITIZER_JOBS.get(job_id)
            if job:
                job.update(
                    status="processing",
                    progress=max(0, min(99, percent)),
                    message=message,
                    processed_pages=processed,
                    total_pages=total,
                )

    try:
        exit_code = _run_sanitizer(args, update_progress)
        with SANITIZER_JOBS_LOCK:
            job = SANITIZER_JOBS.get(job_id)
        if not job:
            return
        output_path = Path(job["output_path"])
        if exit_code == 2 or not output_path.is_file():
            raise RuntimeError("The PDF could not be sanitized safely.")
        try:
            report = json.loads(Path(job["report_path"]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = {}
        with SANITIZER_JOBS_LOCK:
            current = SANITIZER_JOBS.get(job_id)
            if current:
                current.update(
                    status="ready",
                    progress=100,
                    message="Sanitization complete.",
                    summary=report.get("summary", {}),
                )
    except ModuleNotFoundError:
        message = "Sanitizer Python dependencies are not installed."
    except Exception as exc:
        message = str(exc) or "The PDF could not be sanitized."
    else:
        return

    with SANITIZER_JOBS_LOCK:
        job = SANITIZER_JOBS.get(job_id)
        if job:
            job.update(status="failed", message=message)


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "openai_configured": helper.configured,
        "sanitizer_tools_available": not _missing_sanitizer_tools(),
    }


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


@app.post("/api/sanitize")
async def sanitize(file: UploadFile = File(...)) -> Response:
    source = await _read_pdf(file)
    missing_tools = _missing_sanitizer_tools()
    if missing_tools:
        raise HTTPException(
            status_code=503,
            detail=f"Sanitizer system tools are unavailable: {', '.join(missing_tools)}.",
        )

    with tempfile.TemporaryDirectory(prefix="subliminal-sanitizer-") as temp_dir:
        workdir = Path(temp_dir)
        input_path = workdir / "source.pdf"
        output_path = workdir / "sanitized.pdf"
        report_path = workdir / "audit.json"
        input_path.write_bytes(source)
        args = [
            str(input_path),
            str(output_path),
            "--report",
            str(report_path),
            "--on-suspicious",
            "redact",
            "--jobs",
            "1",
            "--max-pages",
            "500",
        ]
        try:
            exit_code = await asyncio.to_thread(_run_sanitizer, args)
        except ModuleNotFoundError as exc:
            raise HTTPException(
                status_code=503,
                detail="Sanitizer Python dependencies are not installed.",
            ) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail="The PDF could not be sanitized.") from exc

        if exit_code == 2 or not output_path.is_file():
            raise HTTPException(status_code=422, detail="The PDF could not be sanitized safely.")

        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = {}
        output = output_path.read_bytes()

    summary = report.get("summary", {})
    filename = f"{_safe_stem(file.filename)}--sanitized.pdf"
    return Response(
        output,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Sanitizer-Flagged-Pages": str(len(summary.get("flagged_pages", []))),
            "X-Sanitizer-Redacted-Paragraphs": str(summary.get("paragraphs_redacted", 0)),
            "X-Sanitizer-Words-Kept": str(summary.get("words_kept", 0)),
        },
    )


@app.post("/api/sanitize/jobs")
async def start_sanitize_job(file: UploadFile = File(...)) -> dict:
    source = await _read_pdf(file)
    missing_tools = _missing_sanitizer_tools()
    if missing_tools:
        raise HTTPException(
            status_code=503,
            detail=f"Sanitizer system tools are unavailable: {', '.join(missing_tools)}.",
        )

    job_id = uuid.uuid4().hex
    workdir = Path(tempfile.mkdtemp(prefix="subliminal-sanitizer-job-"))
    input_path = workdir / "source.pdf"
    output_path = workdir / "sanitized.pdf"
    report_path = workdir / "audit.json"
    input_path.write_bytes(source)
    args = [
        str(input_path),
        str(output_path),
        "--report",
        str(report_path),
        "--on-suspicious",
        "redact",
        "--jobs",
        "1",
        "--max-pages",
        "500",
    ]
    with SANITIZER_JOBS_LOCK:
        SANITIZER_JOBS[job_id] = {
            "status": "queued",
            "progress": 2,
            "message": "Upload received. Inspecting PDF.",
            "processed_pages": 0,
            "total_pages": 0,
            "summary": {},
            "filename": f"{_safe_stem(file.filename)}--sanitized.pdf",
            "workdir": str(workdir),
            "output_path": str(output_path),
            "report_path": str(report_path),
        }
    threading.Thread(target=_process_sanitizer_job, args=(job_id, args), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/sanitize/jobs/{job_id}")
def sanitize_job_status(job_id: str) -> dict:
    with SANITIZER_JOBS_LOCK:
        job = SANITIZER_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Sanitizing job not found.")
        return {
            key: job[key]
            for key in (
                "status",
                "progress",
                "message",
                "processed_pages",
                "total_pages",
                "summary",
            )
        }


@app.get("/api/sanitize/jobs/{job_id}/download")
def download_sanitize_job(job_id: str) -> FileResponse:
    with SANITIZER_JOBS_LOCK:
        job = SANITIZER_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Sanitizing job not found.")
        if job["status"] == "failed":
            raise HTTPException(status_code=422, detail=job["message"])
        if job["status"] != "ready":
            raise HTTPException(status_code=409, detail="Sanitization is still running.")
        output_path = job["output_path"]
        filename = job["filename"]
        summary = job["summary"]

    return FileResponse(
        output_path,
        media_type="application/pdf",
        filename=filename,
        headers={
            "X-Sanitizer-Flagged-Pages": str(len(summary.get("flagged_pages", []))),
            "X-Sanitizer-Redacted-Paragraphs": str(summary.get("paragraphs_redacted", 0)),
            "X-Sanitizer-Words-Kept": str(summary.get("words_kept", 0)),
        },
        background=BackgroundTask(_cleanup_sanitizer_job, job_id),
    )


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

