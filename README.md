# CSV Fast View Converter

Browser-based BMS / ACMV trend analyzer for CSV exports.

## User Workflow

1. Open the web page.
2. Select a CSV folder or CSV files.
3. Click **Convert to ZIP**.
4. Download `hbl-bms-trend-analysis.zip`.

No Python, terminal commands, local server, or package installation is required for end users. CSV files stay on the user's computer and are processed in the browser.

## Output

- One `.xlsx` workbook per CSV file
- Visible `Data` and `Analysis` sheets
- BMS / ACMV keyword-based numeric and status analysis
- `conversion_report.txt` inside the ZIP package

## Development

```bash
npm install
npm run dev
```

## Checks

```bash
npm run lint
npm run build
```

## Deployment

Vercel builds the Vite frontend:

- Build command: `npm run build`
- Output directory: `dist`
