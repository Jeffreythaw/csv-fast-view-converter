# CSV Fast View Converter

Large-file BMS / ACMV CSV trend converter with a Vercel frontend and a dedicated backend worker.

## User Workflow

1. Open the web page.
2. Select one or more CSV files.
3. Upload and process the XLSX analysis job.
4. Download the completed output ZIP.

The frontend can run on Vercel. Large CSV processing runs on the FastAPI backend, not in Vercel serverless functions and not in the browser.

## Large File Strategy

The backend streams upload chunks to disk, then streams CSV rows from disk into the selected output format. The full CSV is not read into memory.

For large files, embedded charts do not use every raw row. The backend creates hidden `_ChartHelper` aggregate data and caps chart points so Excel files remain practical to open.

## Output

- XLSX output with automatic `Data_N` sheet splitting at Excel's 1,048,576 row limit
- BMS / ACMV operation analysis report in the `Analysis` worksheet
- Equipment detection for chillers, CHW pumps, condenser pumps, cooling towers, AHU/FCU/fans, valves, VSD/VFD points, MCC/power meters, and unknown equipment
- Start/stop operation summaries, alarm/trip/fail/lockout events, abnormal condition notes, analog trend summaries, and embedded charts
- File-type specific review for chiller, AHU, FCU, CT / CHWP / CDWP / VSD, mixed ACMV, and unknown trend exports
- Command/status/feedback checks for valve, VSD, comfort temperature, humidity, CO2, override, filter dirty, smoke detector, water leak, overload, and general fault points
- Chiller cooling load calculation from CHW flow and CHW Delta-T when flow unit is known; otherwise the report uses CHW Delta-T as load indication and clearly marks RT unavailable
- `conversion_report.txt` inside the output ZIP

## Chiller Rated Capacity

Chiller load percentage uses configured rated RT when available:

```bash
DEFAULT_CHILLER_RT=900
CHILLER_1_RT=900
CHILLER_2_RT=900
CHILLER_3_RT=900
```

If no value is set, the backend uses `900 RT` as an assumed default and writes that assumption into the `Analysis` worksheet.

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

## Download Troubleshooting

The frontend downloads completed output with:

```text
${VITE_API_BASE}/api/jobs/${jobId}/download
```

For the Render backend, `VITE_API_BASE` should be:

```text
https://csv-fast-view-converter.onrender.com
```

If Download shows `Not Found`, check `GET /api/jobs/{job_id}` first. The response includes:

- `status`
- `filename`
- `output_ready`
- `output_exists`
- `output_size`
- `error`

The backend only marks a job `completed` after the ZIP file exists and has a non-zero file size. The frontend also displays the backend URL, job ID, download URL, output readiness, and the HTTP status/error text if download fails.
