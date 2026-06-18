# CSV Fast View Converter

Large-file BMS / ACMV CSV trend converter with a Vercel frontend and a dedicated backend worker.

## User Workflow

1. Open the web page.
2. Select one CSV file.
3. Convert it to XLSX.
4. Download the completed Excel file directly.

The frontend can run on Vercel. Large CSV processing runs on the FastAPI backend, not in Vercel serverless functions and not in the browser.

## Large File Strategy

The backend streams upload chunks to disk, then streams CSV rows from disk into the selected output format. The full CSV is not read into memory.

For large files, embedded charts do not use every raw row. The backend stores capped aggregate chart data in hidden columns on the `Analysis` sheet so Excel files remain practical to open.

Jobs are persisted under `backend/tmp/jobs/{job_id}.json`, uploads under `backend/tmp/uploads`, and downloadable XLSX files under `backend/tmp/outputs/{job_id}.xlsx`. If the backend restarts during processing, the status endpoint returns a clear failed/interrupted message instead of losing the job.

Files over `50MB`, files with more than `500` detected columns, or jobs that pass `50,000` processed rows switch to Summary Mode. Summary Mode keeps the Analysis worksheet compact and avoids expensive deep row-by-row diagnostics.

## Output

- XLSX output containing only `Data` and `Analysis` sheets
- Operator-friendly daily ACMV summary in the `Analysis` worksheet
- Equipment counts by type: detected, ran during the period, running at the latest reading, and trip count
- Valve command/feedback opening percentages
- Temperature IN/return and OUT/supply latest, average, minimum, and maximum values
- VSD/frequency latest, average, minimum, and maximum Hz
- Trip/fault start time, recovery time, duration, and active/recovered status
- Chiller average, peak, and latest cooling load RT plus estimated ton-hours when CHW flow and temperatures are available
- Equipment detection for chillers, CHW pumps, condenser pumps, cooling towers, AHU/FCU/fans, valves, VSD/VFD points, MCC/power meters, and unknown equipment
- Start/stop operation summaries, alarm/trip/fail/lockout events, abnormal condition notes, analog trend summaries, and embedded charts
- File-type specific review for chiller, AHU, FCU, CT / CHWP / CDWP / VSD, mixed ACMV, and unknown trend exports
- Command/status/feedback checks for valve, VSD, comfort temperature, humidity, CO2, override, filter dirty, smoke detector, water leak, overload, and general fault points
- Chiller cooling load calculation from CHW flow and CHW Delta-T when flow unit is known; otherwise the report uses CHW Delta-T as load indication and clearly marks RT unavailable

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
- `output_filename`
- `error`

The backend only marks a job `completed` after the XLSX file exists and has a non-zero file size. The frontend downloads the returned blob using `output_filename`.
