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
