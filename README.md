# CSV Fast View Converter

React + Vite frontend with a Python backend for BMS / ACMV trend analysis. Small CSV files can be uploaded to `/api/convert` on Vercel. Large CSV files should use Local Folder Mode, where the web UI talks to a local Python companion server and the CSV data never leaves the user's computer.

## Features

- Batch CSV upload with drag and drop
- Python backend conversion via `POST /api/convert`
- Local large-file conversion via `http://127.0.0.1:8765/api/local/convert`
- CSV file path or folder path input for files too large for Vercel uploads
- BMS / ACMV keyword-based column classification
- Date/time detection and sorting
- Numeric and status analysis tables
- Excel workbooks with visible `Data` and `Analysis` sheets only
- Hidden `_ChartHelper` sheet for embedded Excel charts
- ZIP output with converted workbooks and `conversion_report.txt`

## Local Development

Prerequisite: Node.js 20 or newer.

```bash
npm install
python3 -m pip install -r requirements.txt
npm run dev
```

## Large File Workflow

Vercel serverless functions cannot receive large request bodies, so files over 4.5 MB should not be uploaded through the hosted API. Start the local companion server on the user's machine:

```bash
npm run local-api
```

Then open the web UI, choose **Local Folder Mode**, and paste either:

- one CSV file path per line
- a folder path containing CSV files

The converter writes `hbl-bms-trend-analysis.zip` into the selected output folder. This is the recommended path for 73 MB, 1 GB, and other large BMS trend exports.

Direct local CLI conversion is also available:

```bash
python3 api/convert.py --out /path/to/hbl-bms-trend-analysis.zip /path/to/csv-or-folder
```

## Checks

```bash
npm run lint
npm run build
```

## Vercel

Vercel deploys the Vite frontend and Python serverless function:

- Build command: `npm run build`
- Output directory: `dist`
- API endpoint: `/api/convert`
- Large-file endpoint: local companion only, not Vercel
