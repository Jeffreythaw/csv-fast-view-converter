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

## API

- `POST /api/jobs` multipart form: one `file`, `output_format=xlsx`
- `GET /api/jobs/{job_id}` job status
- `GET /api/jobs/{job_id}/download` completed XLSX download
- `DELETE /api/jobs/{job_id}` cleanup now

Temporary uploads and outputs are kept under `backend/tmp` and cleaned after the configured TTL.

Job metadata is persisted in `backend/tmp/jobs/{job_id}.json`, and final XLSX output is stored at `backend/tmp/outputs/{job_id}.xlsx`. Completed and failed jobs are kept for 6 hours by default. Active processing jobs are not cleaned up.

If the backend restarts while a job is queued or processing, `GET /api/jobs/{job_id}` marks it failed with: `Job interrupted or backend restarted during processing. Please retry.`

## Download Checks

`GET /api/jobs/{job_id}` returns output debug fields:

- `output_ready`
- `output_exists`
- `output_size`
- `output_filename`
- `error`

`GET /api/jobs/{job_id}/download` returns the XLSX through `FileResponse` only when the job is completed and the output exists with a size greater than zero. The response uses the Excel MIME type and the original CSV filename with an `.xlsx` extension.
