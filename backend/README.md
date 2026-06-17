# CSV Fast View Backend

FastAPI backend for large CSV conversion jobs.

## Run Locally

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For Parquet output, install the optional dependency on Python 3.12:

```bash
pip install -r requirements-parquet.txt
```

## API

- `POST /api/jobs` multipart form: `file`, `output_format=xlsx|sqlite|parquet`
- `GET /api/jobs/{job_id}` job status
- `GET /api/jobs/{job_id}/download` completed ZIP download
- `DELETE /api/jobs/{job_id}` cleanup now

Temporary uploads and outputs are kept under `backend/tmp` and cleaned after the configured TTL.

Job metadata is persisted in `backend/tmp/jobs/{job_id}.json`, and final ZIP output is moved to `backend/tmp/outputs/{job_id}.zip`. Completed and failed jobs are kept for 6 hours by default. Active processing jobs are not cleaned up.

If the backend restarts while a job is queued or processing, `GET /api/jobs/{job_id}` marks it failed with: `Job interrupted or backend restarted during processing. Please retry.`

## Download Checks

`GET /api/jobs/{job_id}` returns output debug fields:

- `output_ready`
- `output_exists`
- `output_size`
- `error`

`GET /api/jobs/{job_id}/download` returns a ZIP `FileResponse` only when the job is completed and the output ZIP exists with a size greater than zero. Otherwise it returns a JSON error with the job ID, status, and output file state. Backend logs include the job ID, output ZIP path, file existence, file size, and requested download URL.
