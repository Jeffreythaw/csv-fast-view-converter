# CSV Fast View Converter

Large-file BMS / ACMV CSV trend converter for MacOS and Windows.

## User Workflow

1. Open the web page in Chrome or Edge.
2. Choose **MacOS** or **Windows** on the page.
3. Select a CSV folder or CSV files.
4. Click **Convert to XLSX**.
5. When the OS save dialog opens, choose where to save each workbook.

No app download, terminal command, or local helper app is required.

## Large File Strategy

The browser streams CSV rows from the selected file and writes XLSX ZIP parts directly to the chosen save file using the File System Access API. The full CSV is not read into memory.

## Output

- One `.xlsx` workbook per CSV file
- Visible `Data` and `Analysis` sheets
- BMS / ACMV numeric and status summaries
- Excel row limit protection for very large files

## Browser Support

Use Chrome or Edge on MacOS or Windows. Safari and Firefox do not currently support the streaming save API needed for very large XLSX output.

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

Vercel builds the Vite frontend only:

- Build command: `npm run build`
- Output directory: `dist`
