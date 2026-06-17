import { useMemo, useRef, useState } from 'react';
import type { ChangeEvent } from 'react';
import { Zip, ZipDeflate } from 'fflate';
import {
  AlertCircle,
  CheckCircle2,
  FileSpreadsheet,
  FolderOpen,
  Loader2,
  Trash2,
} from 'lucide-react';

type FileStatus = 'Ready' | 'Processing' | 'Completed' | 'Failed';
type OperatingSystem = 'macos' | 'windows';

interface SaveFilePickerOptions {
  suggestedName?: string;
  types?: Array<{
    description: string;
    accept: Record<string, string[]>;
  }>;
}

interface FileSystemWritableFileStream {
  write(data: Blob | BufferSource | string): Promise<void>;
  close(): Promise<void>;
}

interface FileSystemFileHandle {
  createWritable(): Promise<FileSystemWritableFileStream>;
}

declare global {
  interface Window {
    showSaveFilePicker?: (options?: SaveFilePickerOptions) => Promise<FileSystemFileHandle>;
  }
}

interface QueueFile {
  id: string;
  file: File;
  status: FileStatus;
  error?: string;
}

interface NumericStat {
  name: string;
  index: number;
  count: number;
  mean: number;
  m2: number;
  min: number;
  max: number;
  first?: number;
  latest?: number;
}

const MAX_COLUMNS = 300;
const EXCEL_MAX_DATA_ROWS = 1_048_575;
const ACTIVE_STATUS = new Set(['alarm', 'trip', 'fault', 'lockout', 'fail', 'failure', 'warning', 'on', 'true', 'run', 'running', 'open', 'start', 'enabled', '1']);

function detectOS(): OperatingSystem {
  const platform = navigator.platform.toLowerCase();
  const agent = navigator.userAgent.toLowerCase();
  return platform.includes('win') || agent.includes('windows') ? 'windows' : 'macos';
}

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

function xml(value: unknown) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;');
}

function colName(index: number) {
  let value = '';
  let n = index + 1;
  while (n > 0) {
    const rem = (n - 1) % 26;
    value = String.fromCharCode(65 + rem) + value;
    n = Math.floor((n - 1) / 26);
  }
  return value;
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

function addNumber(stat: NumericStat, value: number) {
  if (stat.first === undefined) stat.first = value;
  stat.latest = value;
  stat.count += 1;
  const delta = value - stat.mean;
  stat.mean += delta / stat.count;
  stat.m2 += delta * (value - stat.mean);
  stat.min = Math.min(stat.min, value);
  stat.max = Math.max(stat.max, value);
}

async function* csvRows(file: File): AsyncGenerator<string[]> {
  const reader = file.stream().getReader();
  const decoder = new TextDecoder();
  let pending = '';
  let row: string[] = [];
  let cell = '';
  let quoted = false;

  while (true) {
    const { done, value } = await reader.read();
    pending += decoder.decode(value, { stream: !done });
    let index = 0;
    while (index < pending.length) {
      const char = pending[index];
      const next = pending[index + 1];
      if (quoted) {
        if (char === '"' && next === '"') {
          cell += '"';
          index += 2;
          continue;
        }
        if (char === '"') quoted = false;
        else cell += char;
      } else if (char === '"') {
        quoted = true;
      } else if (char === ',') {
        row.push(cell.trim());
        cell = '';
      } else if (char === '\n') {
        row.push(cell.trim());
        if (row.some(Boolean)) yield row;
        row = [];
        cell = '';
      } else if (char !== '\r') {
        cell += char;
      }
      index += 1;
    }
    pending = '';
    if (done) break;
  }

  if (cell || row.length > 0) {
    row.push(cell.trim());
    if (row.some(Boolean)) yield row;
  }
}

function sheetRow(rowNumber: number, values: string[], stats?: NumericStat[], statusCounters?: Map<string, number>[]) {
  const cells = values.map((value, index) => {
    const ref = `${colName(index)}${rowNumber}`;
    const number = rowNumber > 1 ? parseNumber(value) : null;
    if (number !== null) {
      if (stats?.[index]) addNumber(stats[index], number);
      return `<c r="${ref}"><v>${number}</v></c>`;
    }
    if (rowNumber > 1 && statusCounters?.[index]) {
      const key = value.trim();
      if (key) statusCounters[index].set(key, (statusCounters[index].get(key) ?? 0) + 1);
    }
    return `<c r="${ref}" t="inlineStr"><is><t>${xml(value)}</t></is></c>`;
  }).join('');
  return `<row r="${rowNumber}">${cells}</row>`;
}

function xlsxStaticFiles() {
  return new Map<string, string>([
    ['[Content_Types].xml', `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>`],
    ['_rels/.rels', `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>`],
    ['xl/_rels/workbook.xml.rels', `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>`],
    ['xl/workbook.xml', `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Data" sheetId="1" r:id="rId1"/><sheet name="Analysis" sheetId="2" r:id="rId2"/></sheets></workbook>`],
    ['xl/styles.xml', `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts><fills count="1"><fill><patternFill patternType="none"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellXfs></styleSheet>`],
  ]);
}

function analysisXml(file: File, rowsRead: number, rowsWritten: number, stats: NumericStat[], statusCounters: Map<string, number>[]) {
  const usefulStats = stats
    .filter(item => item.count > 0)
    .sort((a, b) => b.count - a.count)
    .slice(0, 15);
  const values: string[][] = [
    ['HBL-BMS Trending Analysis'],
    [],
    ['File', file.name],
    ['Source Size', formatBytes(file.size)],
    ['Rows Read', String(rowsRead)],
    ['Rows Written', String(rowsWritten)],
    ['Note', rowsRead > rowsWritten ? 'Data sheet reached Excel row limit; remaining rows were skipped.' : 'All parsed rows were written.'],
    [],
    ['Numeric Analysis', 'Count', 'Average', 'Min', 'Max', 'Latest', 'Change', 'Std Dev'],
    ...usefulStats.map(item => [
      item.name,
      String(item.count),
      item.mean.toFixed(4),
      item.min.toFixed(4),
      item.max.toFixed(4),
      (item.latest ?? 0).toFixed(4),
      ((item.latest ?? 0) - (item.first ?? 0)).toFixed(4),
      Math.sqrt(item.m2 / Math.max(item.count, 1)).toFixed(4),
    ]),
    [],
    ['Status Analysis', 'States', 'Top State', 'Top State %', 'Active Events', 'Active %'],
    ...statusCounters
      .map((counter, index) => ({ counter, index }))
      .filter(({ counter }) => counter.size > 0)
      .slice(0, 10)
      .map(({ counter, index }) => {
        const entries = [...counter.entries()].sort((a, b) => b[1] - a[1]);
        const total = entries.reduce((sum, [, count]) => sum + count, 0);
        const active = entries.reduce((sum, [value, count]) => ACTIVE_STATUS.has(value.toLowerCase()) ? sum + count : sum, 0);
        return [
          stats[index]?.name ?? `Column ${index + 1}`,
          String(counter.size),
          entries[0]?.[0] ?? '',
          total ? (entries[0][1] / total).toFixed(4) : '0',
          String(active),
          total ? (active / total).toFixed(4) : '0',
        ];
      }),
  ];
  const body = values.map((row, index) => sheetRow(index + 1, row)).join('');
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>${body}</sheetData></worksheet>`;
}

async function writeZipEntry(zip: Zip, name: string, content: string | Uint8Array, writes: Promise<void>[]) {
  const entry = new ZipDeflate(name, { level: 6 });
  zip.add(entry);
  entry.push(typeof content === 'string' ? new TextEncoder().encode(content) : content, true);
  await Promise.all(writes);
}

async function convertFileToXlsx(file: File, writable: FileSystemWritableFileStream, onProgress: (message: string) => void) {
  const encoder = new TextEncoder();
  const pendingWrites: Promise<void>[] = [];
  const zip = new Zip((error, chunk) => {
    if (error) throw error;
    pendingWrites.push(writable.write(chunk));
  });

  for (const [name, content] of xlsxStaticFiles()) {
    await writeZipEntry(zip, name, content, pendingWrites);
  }

  const dataEntry = new ZipDeflate('xl/worksheets/sheet1.xml', { level: 3 });
  zip.add(dataEntry);
  dataEntry.push(encoder.encode('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'), false);

  let headers: string[] | null = null;
  let rowsRead = 0;
  let rowsWritten = 0;
  let stats: NumericStat[] = [];
  let statusCounters: Map<string, number>[] = [];

  for await (const rawRow of csvRows(file)) {
    if (!headers) {
      headers = rawRow.slice(0, MAX_COLUMNS).map((value, index) => value || `Column_${index + 1}`);
      stats = headers.map((name, index) => ({ name, index, count: 0, mean: 0, m2: 0, min: Number.POSITIVE_INFINITY, max: Number.NEGATIVE_INFINITY }));
      statusCounters = headers.map(() => new Map<string, number>());
      dataEntry.push(encoder.encode(sheetRow(1, headers)), false);
      continue;
    }
    rowsRead += 1;
    if (rowsWritten < EXCEL_MAX_DATA_ROWS) {
      const row = headers.map((_, index) => rawRow[index] ?? '');
      rowsWritten += 1;
      dataEntry.push(encoder.encode(sheetRow(rowsWritten + 1, row, stats, statusCounters)), false);
    }
    if (rowsRead % 10000 === 0) onProgress(`${file.name}: ${rowsRead.toLocaleString()} rows processed...`);
  }

  if (!headers) throw new Error('CSV file is empty.');
  dataEntry.push(encoder.encode('</sheetData></worksheet>'), true);
  await Promise.all(pendingWrites);
  await writeZipEntry(zip, 'xl/worksheets/sheet2.xml', analysisXml(file, rowsRead, rowsWritten, stats, statusCounters), pendingWrites);
  zip.end();
  await Promise.all(pendingWrites);
  await writable.close();
}

export default function App() {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);
  const [selectedOS, setSelectedOS] = useState<OperatingSystem>(() => detectOS());
  const [files, setFiles] = useState<QueueFile[]>([]);
  const [isConverting, setIsConverting] = useState(false);
  const [progressMessage, setProgressMessage] = useState('Select CSV files or a folder. Large files are streamed directly to XLSX.');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [completedCount, setCompletedCount] = useState(0);

  const totalSize = useMemo(() => files.reduce((sum, item) => sum + item.file.size, 0), [files]);
  const failedCount = files.filter(item => item.status === 'Failed').length;
  const supportsLargeSave = typeof window.showSaveFilePicker === 'function';

  const addFiles = (incoming: File[]) => {
    const csvFiles = incoming.filter(file => file.name.toLowerCase().endsWith('.csv'));
    if (csvFiles.length === 0) {
      setErrorMessage('Please select CSV files.');
      return;
    }
    setFiles(csvFiles.map(makeQueueItem));
    setCompletedCount(0);
    setErrorMessage(null);
    setProgressMessage(`${csvFiles.length} CSV file${csvFiles.length > 1 ? 's' : ''} selected.`);
  };

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    addFiles(Array.from(event.target.files || []));
    event.target.value = '';
  };

  const clearFiles = () => {
    if (isConverting) return;
    setFiles([]);
    setCompletedCount(0);
    setErrorMessage(null);
    setProgressMessage('Select CSV files or a folder. Large files are streamed directly to XLSX.');
  };

  const convertFiles = async () => {
    if (!supportsLargeSave) {
      setErrorMessage('This large-file converter needs Chrome or Edge because Safari/Firefox cannot stream-save very large XLSX files from a web page.');
      return;
    }
    if (files.length === 0 || isConverting) return;
    setIsConverting(true);
    setCompletedCount(0);
    setErrorMessage(null);

    for (const item of files) {
      setFiles(prev => prev.map(current => current.id === item.id ? { ...current, status: 'Processing', error: undefined } : current));
      try {
        const picker = window.showSaveFilePicker;
        const handle = await picker({
          suggestedName: item.file.name.replace(/\.csv$/i, '.xlsx'),
          types: [{ description: 'Excel Workbook', accept: { 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': ['.xlsx'] } }],
        });
        const writable = await handle.createWritable();
        await convertFileToXlsx(item.file, writable, setProgressMessage);
        setFiles(prev => prev.map(current => current.id === item.id ? { ...current, status: 'Completed' } : current));
        setCompletedCount(value => value + 1);
        setProgressMessage(`${item.file.name} converted.`);
      } catch (error) {
        const message = error instanceof Error ? error.message : 'Conversion failed.';
        setFiles(prev => prev.map(current => current.id === item.id ? { ...current, status: 'Failed', error: message } : current));
        setErrorMessage(message);
      }
    }
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
              <p className="text-sm font-medium text-slate-500">Large-file BMS / ACMV Trend Analyzer</p>
            </div>
          </div>
          <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs font-semibold text-emerald-700">
            No app download
          </div>
        </div>
      </header>

      <main className="mx-auto grid max-w-6xl grid-cols-1 gap-6 px-4 py-8 sm:px-6 lg:grid-cols-12 lg:px-8">
        <section className="lg:col-span-8">
          <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="border-b border-slate-200 pb-5">
              <h2 className="text-base font-semibold">Streaming Browser Conversion</h2>
              <p className="mt-1 text-sm leading-6 text-slate-500">
                CSV rows are streamed from the selected file into an XLSX file. The full CSV is not loaded into memory.
              </p>
            </div>

            <div className="mt-5 grid grid-cols-1 gap-3 sm:grid-cols-2">
              <button
                type="button"
                onClick={() => setSelectedOS('macos')}
                className={`rounded-lg border p-4 text-left transition ${selectedOS === 'macos' ? 'border-slate-900 bg-white shadow-sm' : 'border-slate-200 bg-slate-50 hover:bg-white'}`}
              >
                <span className="block text-sm font-semibold">MacOS</span>
                <span className="mt-1 block text-xs leading-5 text-slate-500">Use Chrome or Edge. Save dialog writes the XLSX to a folder you choose.</span>
              </button>
              <button
                type="button"
                onClick={() => setSelectedOS('windows')}
                className={`rounded-lg border p-4 text-left transition ${selectedOS === 'windows' ? 'border-slate-900 bg-white shadow-sm' : 'border-slate-200 bg-slate-50 hover:bg-white'}`}
              >
                <span className="block text-sm font-semibold">Windows</span>
                <span className="mt-1 block text-xs leading-5 text-slate-500">Use Chrome or Edge. Save dialog writes the XLSX to a folder you choose.</span>
              </button>
            </div>

            <div className="mt-5 rounded-lg border border-emerald-200 bg-emerald-50 p-4 text-sm leading-6 text-emerald-900">
              {selectedOS === 'macos'
                ? 'MacOS flow: select a CSV folder or files, then choose where to save each XLSX when the system save dialog opens. No app download is used.'
                : 'Windows flow: select a CSV folder or files, then choose where to save each XLSX when the system save dialog opens. No installer is used.'}
            </div>

            <input ref={fileInputRef} type="file" accept=".csv,text/csv" multiple className="hidden" onChange={handleFileChange} />
            <input ref={folderInputRef} type="file" accept=".csv,text/csv" multiple className="hidden" onChange={handleFileChange} {...({ webkitdirectory: '', directory: '' } as Record<string, string>)} />

            <div className="mt-5 grid grid-cols-1 gap-3 sm:grid-cols-2">
              <button type="button" onClick={() => folderInputRef.current?.click()} disabled={isConverting} className="inline-flex min-h-24 items-center justify-center gap-3 rounded-lg border border-slate-300 bg-white px-4 py-4 text-sm font-semibold text-slate-900 shadow-sm hover:border-slate-900 disabled:cursor-not-allowed disabled:opacity-40">
                <FolderOpen className="h-5 w-5 text-emerald-700" />
                Select CSV Folder
              </button>
              <button type="button" onClick={() => fileInputRef.current?.click()} disabled={isConverting} className="inline-flex min-h-24 items-center justify-center gap-3 rounded-lg border border-slate-300 bg-white px-4 py-4 text-sm font-semibold text-slate-900 shadow-sm hover:border-slate-900 disabled:cursor-not-allowed disabled:opacity-40">
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
                  <button type="button" onClick={clearFiles} disabled={isConverting} className="inline-flex items-center justify-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-600 hover:text-rose-700 disabled:opacity-40">
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
                      <span className={`w-fit rounded-full px-2.5 py-1 text-xs font-semibold ${item.status === 'Completed' ? 'bg-emerald-50 text-emerald-700' : item.status === 'Failed' ? 'bg-rose-50 text-rose-700' : item.status === 'Processing' ? 'bg-blue-50 text-blue-700' : 'bg-slate-100 text-slate-600'}`}>
                        {item.status}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <button type="button" onClick={convertFiles} disabled={files.length === 0 || isConverting || !supportsLargeSave} className="mt-5 inline-flex w-full items-center justify-center gap-2 rounded-lg bg-slate-900 px-4 py-3 text-sm font-semibold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40">
              {isConverting ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileSpreadsheet className="h-4 w-4" />}
              {isConverting ? 'Converting...' : 'Convert to XLSX'}
            </button>
          </div>
        </section>

        <aside className="lg:col-span-4">
          <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="text-sm font-semibold">Status</h2>
            <p className="mt-2 text-sm leading-6 text-slate-600">{progressMessage}</p>

            {!supportsLargeSave && (
              <div className="mt-5 flex gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
                <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0" />
                <span>Use Chrome or Edge for large-file streaming save.</span>
              </div>
            )}

            {errorMessage && (
              <div className="mt-5 flex gap-2 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">
                <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0" />
                <span>{errorMessage}</span>
              </div>
            )}

            {completedCount > 0 && (
              <div className="mt-5 flex gap-2 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">
                <CheckCircle2 className="mt-0.5 h-4 w-4 flex-shrink-0" />
                <span>{completedCount} workbook{completedCount > 1 ? 's' : ''} saved.</span>
              </div>
            )}

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
          </div>
        </aside>
      </main>
    </div>
  );
}
