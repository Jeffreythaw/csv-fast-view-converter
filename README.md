# CSV Fast View Converter

A browser-only CSV to Excel converter for telemetry, trend, and log files. Files are parsed locally in the browser, cleaned, converted to `.xlsx`, and optionally downloaded as a ZIP batch.

## Features

- Batch CSV upload with drag and drop
- Automatic separator detection for comma, semicolon, tab, and pipe-delimited files
- Header cleanup and duplicate column name handling
- Empty row and empty column pruning
- Numeric, boolean, and date value casting for Excel
- Single-file `.xlsx` downloads and ZIP batch export
- Client-side processing with no server upload step

## Local Development

Prerequisite: Node.js 20 or newer.

```bash
npm install
npm run dev
```

## Checks

```bash
npm run lint
npm run build
```

## Vercel

This is a static Vite app. Vercel can deploy it with:

- Build command: `npm run build`
- Output directory: `dist`
