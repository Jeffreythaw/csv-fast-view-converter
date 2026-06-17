import { useMemo, useRef, useState } from 'react';
import type { ChangeEvent } from 'react';
import ExcelJS from 'exceljs';
import JSZip from 'jszip';
import {
  AlertCircle,
  CheckCircle2,
  Download,
  FileSpreadsheet,
  FolderOpen,
  Loader2,
  Trash2,
} from 'lucide-react';

type FileStatus = 'Ready' | 'Processing' | 'Completed' | 'Failed';

interface QueueFile {
  id: string;
  file: File;
  status: FileStatus;
  error?: string;
}

interface NumericProfile {
  name: string;
  index: number;
  category: string;
  count: number;
  average: number;
  min: number;
  max: number;
  latest: number;
  change: number;
  stdDev: number;
  score: number;
}

interface StatusProfile {
  name: string;
  states: number;
  topState: string;
  topStatePct: number;
  activeEvents: number;
  activePct: number;
}

const MAX_BROWSER_FILE_BYTES = 512 * 1024 * 1024;
const MAX_ROWS = 120_000;
const MAX_COLUMNS = 300;
const ACTIVE_STATUS_VALUES = new Set(['alarm', 'trip', 'fault', 'lockout', 'fail', 'failure', 'warning', 'on', 'true', 'run', 'running', 'open', 'start', 'enabled', '1']);

const CATEGORY_KEYWORDS: Array<[string, string[]]> = [
  ['alarm_trip_lockout', ['alarm', 'trip', 'fault', 'lockout', 'fail', 'failure', 'warning']],
  ['chiller', ['chiller', 'chill', '19dv', '23xrv', ' ch1', ' ch2', ' ch3', ' ch ']],
  ['ahu', ['ahu', 'air handling unit']],
  ['fcu', ['fcu', 'fan coil']],
  ['pump', ['pump', 'chwp', 'cdwp', 'chw pump', 'cdw pump']],
  ['fan', ['fan', 'blower', 'exhaust fan', 'supply fan', 'return fan', ' ef ', 'saf', 'raf']],
  ['valve', ['valve', 'vlv', 'chw valve', 'cdw valve', 'open', 'opening']],
  ['speed_frequency', ['speed', 'frequency', 'hz']],
  ['temperature', ['temperature', ' temp', 'chwst', 'chwrt', 'chws', 'chwr', 'lwt', 'ewt', 'supply temp', 'return temp', 'sat', 'rat', 'room temp']],
  ['pressure', ['pressure', 'press', ' dp ', 'differential pressure', 'delta p']],
  ['flow', ['flow', 'water flow', 'air flow', 'airflow', 'gpm', 'l/s', 'lpm', 'cmh']],
  ['current', ['current', 'amp', 'amps', 'motor current', 'rla']],
  ['load', ['load', 'percent load', 'demand', 'capacity']],
  ['setpoint', ['setpoint', 'set point', ' sp ', 'target']],
  ['command', ['command', 'cmd', 'enable', 'enabled', 'start', 'stop']],
  ['status', ['status', 'run', 'running', 'proof', 'feedback', 'on/off', ' on ', ' off ']],
  ['humidity', ['humidity', ' rh ', 'relative humidity']],
];

const CATEGORY_SCORE: Record<string, number> = {
  temperature: 28,
  pressure: 24,
  flow: 22,
  load: 20,
  current: 19,
  speed_frequency: 18,
  valve: 17,
  humidity: 16,
  setpoint: 10,
};

function formatBytes(bytes: number) {
  if (bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / Math.pow(1024, index)).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function makeQueueItem(file: File): QueueFile {
  return {
    id: `${file.name}-${file.size}-${file.lastModified}-${crypto.randomUUID()}`,
    file,
    status: 'Ready',
  };
}

function normalizeHeader(value: string) {
  return ` ${value.toLowerCase().replace(/[_-]/g, ' ').replace(/\s+/g, ' ').trim()} `;
}

function parseCsv(text: string) {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = '';
  let quoted = false;

  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    const next = text[index + 1];
    if (quoted) {
      if (char === '"' && next === '"') {
        cell += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        cell += char;
      }
    } else if (char === '"') {
      quoted = true;
    } else if (char === ',') {
      row.push(cell.trim());
      cell = '';
    } else if (char === '\n') {
      row.push(cell.trim());
      if (row.some(Boolean)) rows.push(row);
      row = [];
      cell = '';
    } else if (char !== '\r') {
      cell += char;
    }
  }

  row.push(cell.trim());
  if (row.some(Boolean)) rows.push(row);
  if (rows.length < 2) throw new Error('CSV must contain a header row and at least one data row.');
  return rows;
}

function parseNumber(value: string) {
  const text = String(value ?? '').trim();
  if (!text) return null;
  const lowered = text.toLowerCase();
  if (['true', 'on', 'run', 'running', 'open', 'start', 'enabled'].includes(lowered)) return 1;
  if (['false', 'off', 'stop', 'stopped', 'closed', 'disabled', 'normal'].includes(lowered)) return 0;
  const number = Number(text.replace(/,/g, '').replace(/%$/, ''));
  return Number.isFinite(number) ? number : null;
}

function parseDate(value: string) {
  const text = String(value ?? '').trim();
  if (!text) return null;
  const direct = new Date(text.replace(' ', 'T'));
  if (!Number.isNaN(direct.getTime())) return direct;
  const match = text.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$/);
  if (!match) return null;
  const [, d1, d2, year, hour = '0', minute = '0', second = '0'] = match;
  const dayFirst = Number(d1) > 12;
  const month = dayFirst ? Number(d2) - 1 : Number(d1) - 1;
  const day = dayFirst ? Number(d1) : Number(d2);
  const parsed = new Date(Number(year), month, day, Number(hour), Number(minute), Number(second));
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function classifyColumn(header: string, values: string[]) {
  const normalized = normalizeHeader(header);
  if ([' date ', ' time ', ' timestamp ', ' datetime '].some(token => normalized.includes(token))) return 'datetime';
  for (const [category, keywords] of CATEGORY_KEYWORDS) {
    if (keywords.some(keyword => normalized.includes(keyword))) return category;
  }
  const numericRatio = values.filter(value => parseNumber(value) !== null).length / Math.max(values.length, 1);
  return numericRatio >= 0.7 ? 'unknown_numeric' : 'unknown_text';
}

function stdDev(values: number[]) {
  if (values.length < 2) return 0;
  const average = values.reduce((sum, value) => sum + value, 0) / values.length;
  return Math.sqrt(values.reduce((sum, value) => sum + Math.pow(value - average, 2), 0) / values.length);
}

function profileCsv(headers: string[], rows: Array<Array<string | number | Date | null>>, rawRows: string[][]) {
  const categories = headers.map((header, index) => classifyColumn(header, rawRows.map(row => row[index] ?? '')));
  const numericProfiles: NumericProfile[] = [];

  headers.forEach((header, index) => {
    const values = rows.map(row => row[index]).filter((value): value is number => typeof value === 'number');
    if (values.length / Math.max(rows.length, 1) < 0.5) return;
    const uniqueCount = new Set(values).size;
    const average = values.reduce((sum, value) => sum + value, 0) / values.length;
    const deviation = stdDev(values);
    const category = categories[index];
    const mostlyConstant = uniqueCount <= 1 || deviation < 0.000001;
    const score = (CATEGORY_SCORE[category] ?? 4) + Math.min(uniqueCount, 20) * 0.4 + (values.length / rows.length) * 10 - (mostlyConstant ? 20 : 0);
    numericProfiles.push({
      name: header,
      index,
      category,
      count: values.length,
      average,
      min: Math.min(...values),
      max: Math.max(...values),
      latest: values[values.length - 1],
      change: values[values.length - 1] - values[0],
      stdDev: deviation,
      score,
    });
  });

  numericProfiles.sort((a, b) => b.score - a.score);
  const statusProfiles: StatusProfile[] = headers
    .map((header, index) => {
      const category = categories[index];
      if (!['command', 'status', 'alarm_trip_lockout', 'valve', 'pump', 'fan', 'chiller', 'ahu', 'fcu'].includes(category)) return null;
      const counts = new Map<string, number>();
      rows.forEach(row => {
        const value = String(row[index] ?? '').trim();
        if (value) counts.set(value, (counts.get(value) ?? 0) + 1);
      });
      if (counts.size === 0) return null;
      const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1]);
      const activeEvents = [...counts.entries()].reduce((sum, [value, count]) => ACTIVE_STATUS_VALUES.has(value.toLowerCase()) ? sum + count : sum, 0);
      return {
        name: header,
        states: counts.size,
        topState: sorted[0][0],
        topStatePct: sorted[0][1] / rows.length,
        activeEvents,
        activePct: activeEvents / rows.length,
      };
    })
    .filter((value): value is StatusProfile => Boolean(value))
    .slice(0, 8);

  return { categories, numericProfiles, statusProfiles };
}

async function convertCsvToWorkbook(file: File) {
  if (file.size > MAX_BROWSER_FILE_BYTES) throw new Error(`${file.name} is ${formatBytes(file.size)}. Maximum supported size is ${formatBytes(MAX_BROWSER_FILE_BYTES)}.`);
  const text = await file.text();
  const parsed = parseCsv(text);
  const headers = parsed[0].slice(0, MAX_COLUMNS).map((header, index) => header || `Column_${index + 1}`);
  const rawRows = parsed.slice(1, MAX_ROWS + 1).map(row => headers.map((_, index) => row[index] ?? ''));

  const datetimeIndex = headers.findIndex(header => [' date ', ' time ', ' timestamp ', ' datetime '].some(token => normalizeHeader(header).includes(token)));
  const typedRows = rawRows.map(row => row.map((value, index) => {
    if (index === datetimeIndex) return parseDate(value) ?? value;
    const numeric = parseNumber(value);
    return numeric ?? value;
  }));
  if (datetimeIndex >= 0) {
    typedRows.sort((a, b) => {
      const left = a[datetimeIndex] instanceof Date ? (a[datetimeIndex] as Date).getTime() : Number.MAX_SAFE_INTEGER;
      const right = b[datetimeIndex] instanceof Date ? (b[datetimeIndex] as Date).getTime() : Number.MAX_SAFE_INTEGER;
      return left - right;
    });
  }

  const { numericProfiles, statusProfiles } = profileCsv(headers, typedRows, rawRows);
  const selectedNumeric = numericProfiles.filter(item => item.score > 0).slice(0, 5);
  const workbook = new ExcelJS.Workbook();
  workbook.creator = 'CSV Fast View Converter';
  workbook.created = new Date();

  const dataSheet = workbook.addWorksheet('Data', { views: [{ state: 'frozen', ySplit: 1 }] });
  dataSheet.columns = headers.map(header => ({ header, key: header, width: Math.min(Math.max(header.length + 2, 12), 32) }));
  typedRows.forEach(row => dataSheet.addRow(row));
  dataSheet.getRow(1).eachCell(cell => {
    cell.font = { bold: true, color: { argb: 'FFFFFFFF' } };
    cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FF1F2937' } };
    cell.alignment = { vertical: 'middle', wrapText: true };
  });
  dataSheet.autoFilter = { from: 'A1', to: `${dataSheet.getColumn(headers.length).letter}1` };

  const analysis = workbook.addWorksheet('Analysis');
  analysis.columns = Array.from({ length: 10 }, () => ({ width: 20 }));
  analysis.mergeCells('A1:H1');
  analysis.getCell('A1').value = 'HBL-BMS Trending Analysis';
  analysis.getCell('A1').font = { bold: true, size: 16, color: { argb: 'FFFFFFFF' } };
  analysis.getCell('A1').fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FF0F172A' } };
  analysis.getCell('A1').alignment = { horizontal: 'center' };

  const overview = [
    ['File', file.name],
    ['Rows', typedRows.length],
    ['Columns', headers.length],
    ['Date/time column', datetimeIndex >= 0 ? headers[datetimeIndex] : 'Not detected'],
    ['Selected trend columns', selectedNumeric.map(item => item.name).join(', ') || 'No strong numeric trend columns detected'],
  ];
  analysis.addRow([]);
  analysis.addRow(['File Overview', 'Value']);
  overview.forEach(row => analysis.addRow(row));
  analysis.addRow([]);
  analysis.addRow(['Engineering Notes']);
  analysis.addRow([datetimeIndex >= 0 ? `Trend sorted by ${headers[datetimeIndex]}.` : 'No reliable date/time column detected; time-series analysis is limited.']);
  analysis.addRow(['Numeric columns are prioritized by ACMV keywords, completeness, and variation.']);
  analysis.addRow(['Excel charts are not embedded in the browser-only converter; analysis tables are included for review.']);
  analysis.addRow([]);
  analysis.addRow(['Numeric Analysis', 'Category', 'Average', 'Min', 'Max', 'Latest', 'Change', 'Std Dev']);
  numericProfiles.slice(0, 12).forEach(item => analysis.addRow([
    item.name,
    item.category,
    item.average,
    item.min,
    item.max,
    item.latest,
    item.change,
    item.stdDev,
  ]));
  analysis.addRow([]);
  analysis.addRow(['Status Analysis', 'States', 'Top State', 'Top State %', 'Active Events', 'Active %']);
  statusProfiles.forEach(item => analysis.addRow([
    item.name,
    item.states,
    item.topState,
    item.topStatePct,
    item.activeEvents,
    item.activePct,
  ]));

  analysis.eachRow(row => {
    row.eachCell(cell => {
      cell.alignment = { vertical: 'top', wrapText: true };
      if (typeof cell.value === 'number') cell.numFmt = '#,##0.00';
    });
  });

  const buffer = await workbook.xlsx.writeBuffer();
  return new Uint8Array(buffer);
}

export default function App() {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<QueueFile[]>([]);
  const [isConverting, setIsConverting] = useState(false);
  const [progressMessage, setProgressMessage] = useState('Select a CSV folder or CSV files. Conversion runs in this browser.');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null);

  const totalSize = useMemo(() => files.reduce((sum, item) => sum + item.file.size, 0), [files]);
  const completedCount = files.filter(item => item.status === 'Completed').length;
  const failedCount = files.filter(item => item.status === 'Failed').length;

  const addFiles = (incoming: File[]) => {
    const csvFiles = incoming.filter(file => file.name.toLowerCase().endsWith('.csv'));
    if (csvFiles.length === 0) {
      setErrorMessage('Please select a folder or files containing .csv files.');
      return;
    }
    if (downloadUrl) URL.revokeObjectURL(downloadUrl);
    setDownloadUrl(null);
    setErrorMessage(null);
    setFiles(csvFiles.map(makeQueueItem));
    setProgressMessage(`${csvFiles.length} CSV file${csvFiles.length > 1 ? 's' : ''} selected. Ready to convert.`);
  };

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    addFiles(Array.from(event.target.files || []));
    event.target.value = '';
  };

  const clearFiles = () => {
    if (isConverting) return;
    if (downloadUrl) URL.revokeObjectURL(downloadUrl);
    setFiles([]);
    setDownloadUrl(null);
    setErrorMessage(null);
    setProgressMessage('Select a CSV folder or CSV files. Conversion runs in this browser.');
  };

  const convertFiles = async () => {
    if (files.length === 0 || isConverting) return;
    setIsConverting(true);
    setErrorMessage(null);
    setDownloadUrl(null);
    setFiles(prev => prev.map(item => ({ ...item, status: 'Ready', error: undefined })));

    const zip = new JSZip();
    const report = [
      'HBL-BMS Trending Browser Conversion Report',
      `Generated: ${new Date().toISOString()}`,
      `Files received: ${files.length}`,
      '',
    ];
    let success = 0;
    let failed = 0;

    for (const item of files) {
      setProgressMessage(`Converting ${item.file.name}...`);
      setFiles(prev => prev.map(current => current.id === item.id ? { ...current, status: 'Processing' } : current));
      try {
        const workbookBytes = await convertCsvToWorkbook(item.file);
        const outputName = item.file.name.replace(/\.csv$/i, '.xlsx');
        zip.file(outputName, workbookBytes);
        report.push(`OK: ${item.file.name} -> ${outputName} (${workbookBytes.byteLength.toLocaleString()} bytes)`);
        success += 1;
        setFiles(prev => prev.map(current => current.id === item.id ? { ...current, status: 'Completed' } : current));
      } catch (error) {
        const message = error instanceof Error ? error.message : 'Unexpected conversion error.';
        report.push(`FAILED: ${item.file.name} - ${message}`);
        failed += 1;
        setFiles(prev => prev.map(current => current.id === item.id ? { ...current, status: 'Failed', error: message } : current));
      }
    }

    report.push('', `Successful files: ${success}`, `Failed files: ${failed}`);
    zip.file('conversion_report.txt', report.join('\n'));
    const blob = await zip.generateAsync({ type: 'blob' });
    setDownloadUrl(URL.createObjectURL(blob));
    setProgressMessage(success > 0 ? 'Conversion completed. Download the ZIP package.' : 'Conversion failed. Download the report for details.');
    setIsConverting(false);
  };

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-5 sm:flex-row sm:items-center sm:justify-between sm:px-6 lg:px-8">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-slate-900 text-emerald-400">
              <FileSpreadsheet className="h-5 w-5" />
            </div>
            <div>
              <h1 className="text-lg font-bold tracking-tight">CSV Fast View Converter</h1>
              <p className="text-sm font-medium text-slate-500">Browser BMS / ACMV Trend Analyzer</p>
            </div>
          </div>
          <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs font-semibold text-emerald-700">
            No install required
          </div>
        </div>
      </header>

      <main className="mx-auto grid max-w-6xl grid-cols-1 gap-6 px-4 py-8 sm:px-6 lg:grid-cols-12 lg:px-8">
        <section className="lg:col-span-8">
          <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="border-b border-slate-200 pb-5">
              <h2 className="text-base font-semibold">Local Browser Conversion</h2>
              <p className="mt-1 text-sm leading-6 text-slate-500">
                Select a CSV folder or CSV files. Files stay on this computer and are converted in the browser.
              </p>
            </div>

            <input ref={fileInputRef} type="file" accept=".csv,text/csv" multiple className="hidden" onChange={handleFileChange} />
            <input
              ref={folderInputRef}
              type="file"
              accept=".csv,text/csv"
              multiple
              className="hidden"
              onChange={handleFileChange}
              {...({ webkitdirectory: '', directory: '' } as Record<string, string>)}
            />

            <div className="mt-5 grid grid-cols-1 gap-3 sm:grid-cols-2">
              <button
                type="button"
                onClick={() => folderInputRef.current?.click()}
                disabled={isConverting}
                className="inline-flex min-h-24 items-center justify-center gap-3 rounded-lg border border-slate-300 bg-white px-4 py-4 text-sm font-semibold text-slate-900 shadow-sm hover:border-slate-900 disabled:cursor-not-allowed disabled:opacity-40"
              >
                <FolderOpen className="h-5 w-5 text-emerald-700" />
                Select CSV Folder
              </button>
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={isConverting}
                className="inline-flex min-h-24 items-center justify-center gap-3 rounded-lg border border-slate-300 bg-white px-4 py-4 text-sm font-semibold text-slate-900 shadow-sm hover:border-slate-900 disabled:cursor-not-allowed disabled:opacity-40"
              >
                <FileSpreadsheet className="h-5 w-5 text-blue-700" />
                Select CSV Files
              </button>
            </div>

            <div className="mt-5 overflow-hidden rounded-lg border border-slate-200 bg-slate-50">
              <div className="flex flex-col gap-3 border-b border-slate-200 bg-white px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <p className="text-sm font-semibold">Selected CSV Files</p>
                  <p className="text-xs text-slate-500">{files.length} file(s), {formatBytes(totalSize)} total</p>
                </div>
                {files.length > 0 && (
                  <button
                    type="button"
                    onClick={clearFiles}
                    disabled={isConverting}
                    className="inline-flex items-center justify-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-600 hover:text-rose-700 disabled:opacity-40"
                  >
                    <Trash2 className="h-4 w-4" />
                    Clear
                  </button>
                )}
              </div>

              {files.length === 0 ? (
                <div className="px-4 py-10 text-center text-sm text-slate-500">No CSV files selected yet.</div>
              ) : (
                <div className="max-h-80 divide-y divide-slate-200 overflow-auto">
                  {files.map(item => (
                    <div key={item.id} className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
                      <div className="min-w-0">
                        <p className="break-all text-sm font-medium">{item.file.name}</p>
                        <p className="mt-1 text-xs text-slate-500">{formatBytes(item.file.size)}</p>
                        {item.error && <p className="mt-1 text-xs text-rose-600">{item.error}</p>}
                      </div>
                      <span
                        className={`w-fit rounded-full px-2.5 py-1 text-xs font-semibold ${
                          item.status === 'Completed'
                            ? 'bg-emerald-50 text-emerald-700'
                            : item.status === 'Failed'
                              ? 'bg-rose-50 text-rose-700'
                              : item.status === 'Processing'
                                ? 'bg-blue-50 text-blue-700'
                                : 'bg-slate-100 text-slate-600'
                        }`}
                      >
                        {item.status}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <button
              type="button"
              onClick={convertFiles}
              disabled={files.length === 0 || isConverting}
              className="mt-5 inline-flex w-full items-center justify-center gap-2 rounded-lg bg-slate-900 px-4 py-3 text-sm font-semibold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {isConverting ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileSpreadsheet className="h-4 w-4" />}
              {isConverting ? 'Converting...' : 'Convert to ZIP'}
            </button>
          </div>
        </section>

        <aside className="lg:col-span-4">
          <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="text-sm font-semibold">Status</h2>
            <p className="mt-2 text-sm leading-6 text-slate-600">{progressMessage}</p>

            <div className="mt-5 grid grid-cols-3 gap-3 text-center">
              <div className="rounded-lg bg-slate-50 p-3">
                <p className="text-xs text-slate-500">Files</p>
                <p className="mt-1 text-lg font-bold">{files.length}</p>
              </div>
              <div className="rounded-lg bg-emerald-50 p-3">
                <p className="text-xs text-emerald-700">Done</p>
                <p className="mt-1 text-lg font-bold text-emerald-800">{completedCount}</p>
              </div>
              <div className="rounded-lg bg-rose-50 p-3">
                <p className="text-xs text-rose-700">Failed</p>
                <p className="mt-1 text-lg font-bold text-rose-800">{failedCount}</p>
              </div>
            </div>

            {errorMessage && (
              <div className="mt-5 flex gap-2 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">
                <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0" />
                <span>{errorMessage}</span>
              </div>
            )}

            {downloadUrl && (
              <>
                <div className="mt-5 flex gap-2 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">
                  <CheckCircle2 className="mt-0.5 h-4 w-4 flex-shrink-0" />
                  <span>ZIP package is ready.</span>
                </div>
                <a
                  href={downloadUrl}
                  download="hbl-bms-trend-analysis.zip"
                  className="mt-3 inline-flex w-full items-center justify-center gap-2 rounded-lg bg-emerald-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-emerald-700"
                >
                  <Download className="h-4 w-4" />
                  Download ZIP
                </a>
              </>
            )}
          </div>
        </aside>
      </main>
    </div>
  );
}
