from __future__ import annotations

import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .processor import OutputFormat, process_csv

BASE_DIR = Path(__file__).resolve().parents[1]
TMP_DIR = BASE_DIR / "tmp"
UPLOAD_DIR = TMP_DIR / "uploads"
OUTPUT_DIR = TMP_DIR / "outputs"
JOB_TTL_SECONDS = 24 * 60 * 60
CHUNK_SIZE = 1024 * 1024

Status = Literal["queued", "uploading", "processing", "completed", "failed"]


@dataclass
class Job:
    id: str
    status: Status
    output_format: OutputFormat
    created_at: float
    updated_at: float
    filename: str | None = None
    uploaded_bytes: int = 0
    total_bytes: int | None = None
    rows_processed: int = 0
    message: str = ""
    error: str | None = None
    download_url: str | None = None
    upload_path: str | None = None
    output_path: str | None = None
    cleanup_paths: list[str] = field(default_factory=list)


jobs: dict[str, Job] = {}
executor = ThreadPoolExecutor(max_workers=2)
app = FastAPI(title="CSV Fast View Converter API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def now() -> float:
    return time.time()


def touch(job: Job) -> None:
    job.updated_at = now()


def public_job(job: Job) -> dict:
    payload = asdict(job)
    payload.pop("upload_path", None)
    payload.pop("output_path", None)
    payload.pop("cleanup_paths", None)
    return payload


def cleanup_expired_jobs() -> None:
    cutoff = now() - JOB_TTL_SECONDS
    expired = [job_id for job_id, job in jobs.items() if job.updated_at < cutoff]
    for job_id in expired:
        job = jobs.pop(job_id)
        for raw_path in job.cleanup_paths:
            path = Path(raw_path)
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink(missing_ok=True)


def run_processing(job_id: str) -> None:
    job = jobs[job_id]
    job.status = "processing"
    job.message = "Processing CSV stream."
    touch(job)

    def progress(rows: int) -> None:
        job.rows_processed = rows
        job.message = f"Processed {rows:,} rows."
        touch(job)

    try:
        output_dir = OUTPUT_DIR / job_id
        archive = process_csv(Path(job.upload_path or ""), output_dir, job.output_format, progress)
        job.status = "completed"
        job.output_path = str(archive)
        job.download_url = f"/api/jobs/{job.id}/download"
        job.message = "Conversion completed."
        job.cleanup_paths.append(str(output_dir))
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)
        job.message = "Conversion failed."
    finally:
        touch(job)
        cleanup_expired_jobs()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "csv-fast-view-converter-api"}


@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    output_format: OutputFormat = Form("xlsx"),
) -> dict:
    cleanup_expired_jobs()
    job_id = uuid.uuid4().hex
    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    upload_path = job_dir / (Path(file.filename or "upload.csv").name or "upload.csv")
    job = Job(
        id=job_id,
        status="uploading",
        output_format=output_format,
        created_at=now(),
        updated_at=now(),
        filename=file.filename,
        upload_path=str(upload_path),
        cleanup_paths=[str(job_dir)],
        message="Uploading CSV file.",
    )
    jobs[job_id] = job

    try:
        with upload_path.open("wb") as handle:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                handle.write(chunk)
                job.uploaded_bytes += len(chunk)
                touch(job)
        job.total_bytes = job.uploaded_bytes
        job.status = "queued"
        job.message = "Upload complete. Queued for processing."
        touch(job)
        executor.submit(run_processing, job_id)
        return public_job(job)
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)
        job.message = "Upload failed."
        touch(job)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await file.close()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
    return public_job(job)


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str) -> FileResponse:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
    if job.status != "completed" or not job.output_path:
        raise HTTPException(status_code=409, detail="Job is not completed.")
    output_path = Path(job.output_path)
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="Output file has expired.")
    return FileResponse(output_path, filename=output_path.name, media_type="application/zip")


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    job = jobs.pop(job_id, None)
    if job is None:
        return {"ok": True}
    for raw_path in job.cleanup_paths:
        path = Path(raw_path)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink(missing_ok=True)
    return {"ok": True}
