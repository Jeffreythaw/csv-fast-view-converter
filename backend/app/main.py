from __future__ import annotations

import json
import logging
import os
import shutil
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .processor import OutputFormat, process_csv

BASE_DIR = Path(__file__).resolve().parents[1]
TMP_DIR = BASE_DIR / "tmp"
JOB_DIR = TMP_DIR / "jobs"
UPLOAD_DIR = TMP_DIR / "uploads"
OUTPUT_DIR = TMP_DIR / "outputs"
COMPLETED_TTL_SECONDS = int(os.getenv("COMPLETED_JOB_TTL_SECONDS", str(6 * 60 * 60)))
FAILED_TTL_SECONDS = int(os.getenv("FAILED_JOB_TTL_SECONDS", str(6 * 60 * 60)))
CHUNK_SIZE = 1024 * 1024
MAX_WORKERS = int(os.getenv("CSV_WORKER_COUNT", "1"))
ACTIVE_JOB_GRACE_SECONDS = 30
logger = logging.getLogger("csv_fast_view_converter")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

Status = Literal["queued", "uploading", "processing", "completed", "failed", "expired"]


@dataclass
class Job:
    id: str
    job_id: str
    status: Status
    output_format: OutputFormat
    created_at: float
    updated_at: float
    current_step: str = ""
    current_file: str | None = None
    filename: str | None = None
    uploaded_bytes: int = 0
    total_bytes: int | None = None
    rows_processed: int = 0
    total_files: int = 1
    processed_files: int = 0
    message: str = ""
    error: str | None = None
    traceback: str | None = None
    download_url: str | None = None
    upload_path: str | None = None
    output_path: str | None = None
    output_filename: str | None = None
    cleanup_paths: list[str] = field(default_factory=list)
    detected_delimiter: str | None = None
    detected_columns: int | None = None


jobs: dict[str, Job] = {}
processing_jobs: set[str] = set()
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
app = FastAPI(title="CSV Fast View Converter API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


def now() -> float:
    return time.time()


def ensure_dirs() -> None:
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def job_file(job_id: str) -> Path:
    return JOB_DIR / f"{job_id}.json"


def write_job(job: Job) -> None:
    ensure_dirs()
    payload = asdict(job)
    tmp_path = job_file(job.id).with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(job_file(job.id))


def read_job(job_id: str) -> Job | None:
    cached = jobs.get(job_id)
    if cached is not None:
        return cached
    path = job_file(job_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("job_id", data.get("id", job_id))
        data.setdefault("current_step", data.get("message", ""))
        data.setdefault("current_file", data.get("filename"))
        data.setdefault("total_files", 1)
        data.setdefault("processed_files", 0)
        data.setdefault("traceback", None)
        data.setdefault(
            "output_filename",
            f"{Path(data.get('filename') or 'output.csv').stem}.xlsx",
        )
        allowed = {item.name for item in fields(Job)}
        job = Job(**{key: value for key, value in data.items() if key in allowed})
        jobs[job_id] = job
        return job
    except Exception:
        logger.exception("Failed reading job metadata job_id=%s path=%s", job_id, path)
        return None


def touch(job: Job, step: str | None = None) -> None:
    job.updated_at = now()
    if step:
        job.current_step = step
        job.message = step
    write_job(job)


def output_state(job: Job) -> tuple[bool, int, bool]:
    output_path = Path(job.output_path) if job.output_path else None
    output_exists = bool(output_path and output_path.exists())
    output_size = output_path.stat().st_size if output_exists and output_path else 0
    output_readable = bool(output_exists and output_path and os.access(output_path, os.R_OK))
    return output_exists, output_size, output_readable


def public_job(job: Job) -> dict:
    if job.status in {"queued", "uploading", "processing"} and job.output_path:
        output_exists, output_size, output_readable = output_state(job)
        if output_exists and output_size > 0 and output_readable:
            job.status = "completed"
            job.current_step = "completed"
            job.message = "Conversion completed. Excel file is ready to download."
            job.download_url = f"/api/jobs/{job.id}/download"
            write_job(job)

    output_exists, output_size, output_readable = output_state(job)
    payload = asdict(job)
    payload["output_ready"] = job.status == "completed" and output_exists and output_size > 0 and output_readable
    payload["output_exists"] = output_exists
    payload["output_size"] = output_size
    payload.pop("upload_path", None)
    payload.pop("output_path", None)
    payload.pop("cleanup_paths", None)
    return payload


def mark_interrupted_if_needed(job: Job) -> Job:
    if job.status in {"queued", "uploading", "processing"} and job.id not in processing_jobs and now() - job.updated_at > ACTIVE_JOB_GRACE_SECONDS:
        job.status = "failed"
        job.current_step = "failed"
        job.error = "Job interrupted or backend restarted during processing. Please retry."
        job.message = job.error
        job.traceback = None
        touch(job)
    return job


def cleanup_job_files(job: Job) -> None:
    for raw_path in job.cleanup_paths:
        path = Path(raw_path)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink(missing_ok=True)
    job_file(job.id).unlink(missing_ok=True)


def cleanup_expired_jobs() -> None:
    ensure_dirs()
    current = now()
    for path in JOB_DIR.glob("*.json"):
        job = read_job(path.stem)
        if job is None:
            continue
        if job.status in {"queued", "uploading", "processing"}:
            continue
        ttl = COMPLETED_TTL_SECONDS if job.status == "completed" else FAILED_TTL_SECONDS
        if current - job.updated_at < ttl:
            continue
        logger.info("Expiring job job_id=%s status=%s output_path=%s", job.id, job.status, job.output_path)
        job.status = "expired"
        job.current_step = "expired"
        job.message = "Job expired and output was cleaned up."
        write_job(job)
        cleanup_job_files(job)
        jobs.pop(job.id, None)


def run_processing(job_id: str) -> None:
    job = read_job(job_id)
    if job is None:
        logger.error("Cannot start processing missing job_id=%s", job_id)
        return

    processing_jobs.add(job.id)
    logger.info("Processing started job_id=%s file=%s", job.id, job.filename)
    job.status = "processing"
    job.current_step = "reading file"
    job.message = "Reading file."
    touch(job)

    def progress(rows: int, message: str | None = None, delimiter: str | None = None, columns: int | None = None) -> None:
        job.rows_processed = rows
        if delimiter is not None:
            job.detected_delimiter = delimiter
        if columns is not None:
            job.detected_columns = columns
        step = message or f"Processed {rows:,} rows."
        job.current_step = step
        job.message = step
        touch(job)
        if rows and rows % 50000 == 0:
            logger.info("Job progress job_id=%s rows=%s step=%s", job.id, rows, step)

    try:
        final_output = (OUTPUT_DIR / f"{job.id}.xlsx").resolve()
        final_output.unlink(missing_ok=True)
        output = process_csv(Path(job.upload_path or ""), final_output, job.output_format, progress).resolve()
        output_exists = output.exists()
        output_size = output.stat().st_size if output_exists else 0
        if not output_exists or output_size <= 0:
            raise RuntimeError(f"Excel output was not created correctly: {output}")
        job.status = "completed"
        job.current_step = "completed"
        job.output_path = str(output)
        job.download_url = f"/api/jobs/{job.id}/download"
        job.processed_files = 1
        job.message = "Conversion completed. Excel file is ready to download."
        if str(output) not in job.cleanup_paths:
            job.cleanup_paths.append(str(output))
        logger.info("Processing completed job_id=%s output_path=%s output_size=%s", job.id, output, output.stat().st_size)
    except Exception as exc:
        job.status = "failed"
        job.current_step = "failed"
        job.error = str(exc)
        job.traceback = traceback.format_exc()
        job.message = f"Conversion failed: {exc}"
        logger.exception("Processing failed job_id=%s", job.id)
    finally:
        processing_jobs.discard(job.id)
        touch(job)
        cleanup_expired_jobs()


@app.on_event("startup")
def startup() -> None:
    ensure_dirs()
    logger.info("CSV backend started job_dir=%s output_dir=%s workers=%s", JOB_DIR, OUTPUT_DIR, MAX_WORKERS)


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "csv-fast-view-converter-api",
        "version": "direct-xlsx-operator-summary-v2",
    }


@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    output_format: OutputFormat = Form("xlsx"),
) -> dict:
    ensure_dirs()
    cleanup_expired_jobs()
    if output_format != "xlsx":
        raise HTTPException(status_code=400, detail="Only XLSX output is supported.")
    original_filename = Path(file.filename or "upload.csv").name or "upload.csv"
    if Path(original_filename).suffix.lower() != ".csv":
        raise HTTPException(status_code=400, detail="Upload one CSV file.")
    job_id = uuid.uuid4().hex
    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    upload_path = job_dir / original_filename
    job = Job(
        id=job_id,
        job_id=job_id,
        status="uploading",
        output_format="xlsx",
        created_at=now(),
        updated_at=now(),
        current_step="uploaded",
        current_file=file.filename,
        filename=file.filename,
        output_filename=f"{Path(original_filename).stem}.xlsx",
        upload_path=str(upload_path.resolve()),
        cleanup_paths=[str(job_dir.resolve())],
        message="Uploading CSV file.",
    )
    jobs[job_id] = job
    write_job(job)
    logger.info("Job created job_id=%s filename=%s requested_format=%s", job_id, file.filename, output_format)

    try:
        with upload_path.open("wb") as handle:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                handle.write(chunk)
                job.uploaded_bytes += len(chunk)
                touch(job, "uploading")
        job.total_bytes = job.uploaded_bytes
        job.status = "queued"
        touch(job, "upload complete")
        logger.info("Upload saved job_id=%s upload_path=%s bytes=%s", job.id, upload_path, job.uploaded_bytes)
        executor.submit(run_processing, job_id)
        return public_job(job)
    except Exception as exc:
        job.status = "failed"
        job.current_step = "failed"
        job.error = str(exc)
        job.traceback = traceback.format_exc()
        job.message = f"Upload failed: {exc}"
        touch(job)
        logger.exception("Upload failed job_id=%s", job.id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await file.close()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = read_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Job not found or expired",
                "job_id": job_id,
                "suggestion": "The backend may have restarted or the job output was cleaned up. Please retry.",
            },
        )
    return public_job(mark_interrupted_if_needed(job))


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str) -> FileResponse:
    logger.info("Download requested job_id=%s url=/api/jobs/%s/download", job_id, job_id)
    job = read_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Job not found or expired",
                "job_id": job_id,
                "suggestion": "The backend may have restarted or the job output was cleaned up. Please retry.",
            },
        )
    job = mark_interrupted_if_needed(job)
    if job.status != "completed" or not job.output_path:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Job is not completed or output is not ready.",
                "job_id": job_id,
                "status": job.status,
                "error": job.error,
                "output_ready": False,
            },
        )
    output_path = Path(job.output_path)
    output_exists, output_size, output_readable = output_state(job)
    logger.info(
        "Download check job_id=%s output_path=%s output_exists=%s output_size=%s output_readable=%s",
        job_id,
        output_path,
        output_exists,
        output_size,
        output_readable,
    )
    if not output_exists or output_size <= 0 or not output_readable:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Excel output is missing, empty, or not readable. It may have expired or the backend instance restarted.",
                "job_id": job_id,
                "status": job.status,
                "output_exists": output_exists,
                "output_size": output_size,
                "output_readable": output_readable,
            },
        )
    return FileResponse(
        output_path,
        filename=job.output_filename or f"{Path(job.filename or 'output.csv').stem}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    job = read_job(job_id)
    if job is None:
        return {"ok": True}
    cleanup_job_files(job)
    jobs.pop(job_id, None)
    return {"ok": True}
