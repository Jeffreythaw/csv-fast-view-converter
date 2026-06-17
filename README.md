# CSV Fast View Converter

Large-file BMS / ACMV CSV trend converter with a Vercel frontend and a dedicated backend worker.

## User Workflow

1. Open the web page.
2. Select one or more CSV files.
3. Choose `XLSX`, `SQLite`, or `Parquet`.
4. Upload and process the job.
5. Download the completed output ZIP.

The frontend can run on Vercel. Large CSV processing runs on the FastAPI backend, not in Vercel serverless functions and not in the browser.

## Large File Strategy

The backend streams upload chunks to disk, then streams CSV rows from disk into the selected output format. The full CSV is not read into memory.

## Output

- XLSX output with automatic `Data_N` sheet splitting at Excel's 1,048,576 row limit
- SQLite output for large local/database analysis
- Parquet output for columnar analytics
- BMS / ACMV numeric and status summaries
- `conversion_report.txt` inside the output ZIP

## Browser Support

Configure `VITE_API_BASE` to point the frontend at the backend service.

## Development

```bash
npm install
npm run dev
```

Backend:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Checks

```bash
npm run lint
npm run build
```

## Deployment

Vercel builds the Vite frontend only:

- Build command: `npm run build`
- Output directory: `dist`

Set `VITE_API_BASE=https://your-backend.example.com` in Vercel. Deploy `backend/` separately on a long-running service.
