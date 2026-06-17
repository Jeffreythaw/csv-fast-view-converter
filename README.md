# CSV Fast View Converter

Large-file BMS / ACMV CSV trend converter.

## User Workflow

1. Open the web page.
2. If the local engine is not ready, click **Download Starter** and open the downloaded starter once.
3. Return to the page after the engine shows ready.
4. Select a CSV folder or CSV files.
5. Click **Convert to ZIP**.

The conversion engine runs locally and streams CSV rows from disk. Large CSV files are not uploaded to Vercel and are not loaded fully into browser memory.

## Output

- One `.xlsx` workbook per CSV file
- Visible `Data` and `Analysis` sheets
- Embedded Excel chart when enough trend data is available
- `conversion_report.txt` inside the ZIP package
- ZIP is written next to the selected CSV files as `hbl-bms-trend-analysis.zip`

## Development

```bash
npm install
npm run dev
```

Optional local engine during development:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 tools/local_converter.py --serve-local
```

## Checks

```bash
npm run lint
npm run build
python3 -m py_compile tools/local_converter.py
```

## Deployment

Vercel builds the Vite frontend only:

- Build command: `npm run build`
- Output directory: `dist`

The Python converter is downloaded by the starter and runs on the user's machine. It is intentionally outside `/api` so Vercel does not build it as a Python serverless function.
