# CSV Fast View Converter

React + Vite frontend with a Python serverless backend for BMS / ACMV trend analysis. The UI uploads CSV files to `/api/convert`; Python cleans the data, classifies HVAC trend columns, creates engineering analysis sheets and embedded Excel charts, then returns a ZIP package.

## Features

- Batch CSV upload with drag and drop
- Python backend conversion via `POST /api/convert`
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
