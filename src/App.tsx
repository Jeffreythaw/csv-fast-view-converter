import { useMemo, useRef, useState } from 'react';
import type { ChangeEvent, DragEvent } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  Download,
  FileSpreadsheet,
  FolderOpen,
  HardDrive,
  Loader2,
  Server,
  Trash2,
  UploadCloud,
} from 'lucide-react';

type FileStatus = 'Waiting' | 'Uploading' | 'Processing' | 'Completed' | 'Failed';
type WorkMode = 'local' | 'upload';

interface QueueFile {
  id: string;
  file: File;
  status: FileStatus;
  error?: string;
}

interface LocalResult {
  outputZip: string;
  successfulFiles: number;
  failedFiles: number;
  inputFiles: string[];
}

const API_ENDPOINT = '/api/convert';
const LOCAL_API_BASE = 'http://127.0.0.1:8765';
const VERCEL_UPLOAD_LIMIT_BYTES = 4.5 * 1024 * 1024;

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
    status: 'Waiting',
  };
}

function parseLocalPaths(value: string) {
  return value
    .split('\n')
    .map(line => line.trim())
    .filter(Boolean);
}

export default function App() {
  const inputRef = useRef<HTMLInputElement>(null);
  const [mode, setMode] = useState<WorkMode>('local');
  const [files, setFiles] = useState<QueueFile[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const [isConverting, setIsConverting] = useState(false);
  const [isCheckingLocal, setIsCheckingLocal] = useState(false);
  const [localOnline, setLocalOnline] = useState<boolean | null>(null);
  const [localPaths, setLocalPaths] = useState('/Users/kojeffrey/Desktop/HBL-BMS');
  const [outputDir, setOutputDir] = useState('/Users/kojeffrey/Desktop/HBL-BMS');
  const [localResult, setLocalResult] = useState<LocalResult | null>(null);
  const [progressMessage, setProgressMessage] = useState('Use Local Folder mode for large BMS / ACMV trend exports.');
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null);
  const [downloadName, setDownloadName] = useState('hbl-bms-trend-analysis.zip');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const totalSize = useMemo(() => files.reduce((sum, item) => sum + item.file.size, 0), [files]);
  const completedCount = files.filter(item => item.status === 'Completed').length;
  const failedCount = files.filter(item => item.status === 'Failed').length;
  const hasOversizedUpload = files.some(item => item.file.size > VERCEL_UPLOAD_LIMIT_BYTES);
  const localPathCount = parseLocalPaths(localPaths).length;

  const setModeAndReset = (nextMode: WorkMode) => {
    setMode(nextMode);
    setErrorMessage(null);
    setLocalResult(null);
    setProgressMessage(
      nextMode === 'local'
        ? 'Use Local Folder mode for large BMS / ACMV trend exports.'
        : 'Select small CSV files to upload to the Vercel Python API.'
    );
  };

  const addFiles = (incoming: File[]) => {
    const csvFiles = incoming.filter(file => file.name.toLowerCase().endsWith('.csv') || file.type === 'text/csv');
    if (csvFiles.length === 0) {
      setErrorMessage('Please select at least one .csv file.');
      return;
    }

    const oversized = csvFiles.find(file => file.size > VERCEL_UPLOAD_LIMIT_BYTES);
    if (oversized) {
      setErrorMessage(
        `${oversized.name} is ${formatBytes(oversized.size)}. Vercel cannot receive files over 4.5 MB, so use Local Folder mode for this file.`
      );
      setProgressMessage('Large file detected. Switch to Local Folder mode and paste the file or folder path.');
    } else {
      setProgressMessage(`${csvFiles.length} file(s) ready for Vercel upload conversion.`);
    }

    if (downloadUrl) URL.revokeObjectURL(downloadUrl);
    setDownloadUrl(null);
    setLocalResult(null);
    setFiles(prev => [...prev, ...csvFiles.map(makeQueueItem)]);
  };

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    addFiles(Array.from(event.target.files || []));
    event.target.value = '';
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setIsDragging(false);
    addFiles(Array.from(event.dataTransfer.files || []));
  };

  const removeFile = (id: string) => {
    if (isConverting) return;
    setFiles(prev => prev.filter(item => item.id !== id));
  };

  const clearFiles = () => {
    if (isConverting) return;
    if (downloadUrl) URL.revokeObjectURL(downloadUrl);
    setFiles([]);
    setDownloadUrl(null);
    setErrorMessage(null);
    setProgressMessage('Select small CSV files to upload to the Vercel Python API.');
  };

  const checkLocalServer = async () => {
    setIsCheckingLocal(true);
    setErrorMessage(null);
    try {
      const response = await fetch(`${LOCAL_API_BASE}/api/local/status`, { method: 'GET' });
      if (!response.ok) throw new Error(`Local companion returned HTTP ${response.status}.`);
      setLocalOnline(true);
      setProgressMessage('Local companion server is ready. Paste a CSV file path or folder path and convert.');
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unable to reach local companion server.';
      setLocalOnline(false);
      setErrorMessage(
        `${message} Start it with: python3 api/convert.py --serve-local`
      );
      setProgressMessage('Local companion server is not reachable yet.');
    } finally {
      setIsCheckingLocal(false);
    }
  };

  const convertLocalPaths = async () => {
    if (isConverting) return;
    const paths = parseLocalPaths(localPaths);
    if (paths.length === 0) {
      setErrorMessage('Paste at least one CSV file path or a folder path.');
      return;
    }

    setIsConverting(true);
    setErrorMessage(null);
    setLocalResult(null);
    setLocalOnline(null);
    setProgressMessage('Local Python analyzer is reading CSV files from disk and creating the ZIP package...');

    try {
      const response = await fetch(`${LOCAL_API_BASE}/api/local/convert`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths, outputDir: outputDir.trim() || undefined }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.ok) {
        throw new Error(payload.error || `Local conversion failed with HTTP ${response.status}.`);
      }

      setLocalOnline(true);
      setLocalResult(payload as LocalResult);
      setProgressMessage(`Local conversion completed. ZIP created at ${payload.outputZip}.`);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unexpected local conversion error.';
      setLocalOnline(false);
      setErrorMessage(message);
      setProgressMessage('Local conversion failed. Check the local companion server and paths.');
    } finally {
      setIsConverting(false);
    }
  };

  const convertUploadedFiles = async () => {
    if (files.length === 0 || isConverting) return;

    if (downloadUrl) URL.revokeObjectURL(downloadUrl);
    setDownloadUrl(null);
    setLocalResult(null);
    setErrorMessage(null);

    const oversized = files.find(item => item.file.size > VERCEL_UPLOAD_LIMIT_BYTES);
    if (oversized) {
      const message = `Vercel cannot receive ${oversized.file.name} because it is ${formatBytes(oversized.file.size)}. Use Local Folder mode for this file.`;
      setErrorMessage(message);
      setFiles(prev => prev.map(item => item.id === oversized.id ? { ...item, status: 'Failed', error: message } : item));
      setProgressMessage('Large file blocked before upload. Local Folder mode is required for this export.');
      return;
    }

    setIsConverting(true);
    setProgressMessage('Uploading CSV files to Python analyzer...');
    setFiles(prev => prev.map(item => ({ ...item, status: 'Uploading', error: undefined })));

    const formData = new FormData();
    files.forEach(item => formData.append('files', item.file, item.file.name));

    try {
      setFiles(prev => prev.map(item => ({ ...item, status: 'Processing' })));
      setProgressMessage('Python backend is cleaning data, classifying BMS points, creating charts, and building Excel workbooks...');

      const response = await fetch(API_ENDPOINT, {
        method: 'POST',
        body: formData,
      });

      if (!response.ok) {
        const detail = await response.text();
        throw new Error(detail || `Conversion failed with HTTP ${response.status}.`);
      }

      const blob = await response.blob();
      const blobUrl = URL.createObjectURL(blob);
      const disposition = response.headers.get('content-disposition') || '';
      const match = disposition.match(/filename="?([^"]+)"?/i);
      const filename = match?.[1] || 'hbl-bms-trend-analysis.zip';

      setDownloadUrl(blobUrl);
      setDownloadName(filename);
      setFiles(prev => prev.map(item => ({ ...item, status: 'Completed' })));
      setProgressMessage('Conversion completed. Download the ZIP package below.');
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unexpected backend conversion error.';
      setErrorMessage(message);
      setFiles(prev => prev.map(item => ({ ...item, status: 'Failed', error: message })));
      setProgressMessage('Conversion failed. Review the error message and try again.');
    } finally {
      setIsConverting(false);
    }
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
              <p className="text-sm font-medium text-slate-500">BMS / ACMV Trend Analyzer</p>
            </div>
          </div>
          <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs font-medium text-slate-600">
            HBL-BMS Trending
          </div>
        </div>
      </header>

      <main className="mx-auto grid max-w-6xl grid-cols-1 gap-6 px-4 py-8 sm:px-6 lg:grid-cols-12 lg:px-8">
        <section className="lg:col-span-8">
          <div className="mb-5 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <button
              type="button"
              onClick={() => setModeAndReset('local')}
              className={`flex items-start gap-3 rounded-lg border p-4 text-left transition ${
                mode === 'local' ? 'border-slate-900 bg-white shadow-sm' : 'border-slate-200 bg-slate-100 hover:bg-white'
              }`}
            >
              <HardDrive className="mt-0.5 h-5 w-5 text-emerald-600" />
              <span>
                <span className="block text-sm font-semibold">Local Folder Mode</span>
                <span className="mt-1 block text-xs leading-5 text-slate-500">For 73 MB, 1 GB, and other large trend exports. Files stay on the local machine.</span>
              </span>
            </button>
            <button
              type="button"
              onClick={() => setModeAndReset('upload')}
              className={`flex items-start gap-3 rounded-lg border p-4 text-left transition ${
                mode === 'upload' ? 'border-slate-900 bg-white shadow-sm' : 'border-slate-200 bg-slate-100 hover:bg-white'
              }`}
            >
              <UploadCloud className="mt-0.5 h-5 w-5 text-blue-600" />
              <span>
                <span className="block text-sm font-semibold">Vercel Upload Mode</span>
                <span className="mt-1 block text-xs leading-5 text-slate-500">For small CSV files under 4.5 MB only.</span>
              </span>
            </button>
          </div>

          {mode === 'local' ? (
            <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
              <div className="flex flex-col gap-3 border-b border-slate-200 pb-4 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <h2 className="text-base font-semibold">Local Large-File Converter</h2>
                  <p className="mt-1 text-sm leading-6 text-slate-500">
                    Paste one CSV file path per line, or paste a folder path containing CSV files. The ZIP output is written locally.
                  </p>
                </div>
                <button
                  type="button"
                  onClick={checkLocalServer}
                  disabled={isCheckingLocal || isConverting}
                  className="inline-flex items-center justify-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-40"
                >
                  {isCheckingLocal ? <Loader2 className="h-4 w-4 animate-spin" /> : <Server className="h-4 w-4" />}
                  Check Local Server
                </button>
              </div>

              <label className="mt-5 block text-sm font-semibold" htmlFor="local-paths">
                CSV file paths or folder paths
              </label>
              <textarea
                id="local-paths"
                value={localPaths}
                onChange={event => setLocalPaths(event.target.value)}
                disabled={isConverting}
                rows={7}
                className="mt-2 w-full rounded-lg border border-slate-300 bg-white p-3 font-mono text-sm leading-6 text-slate-900 shadow-sm outline-none focus:border-slate-900 disabled:opacity-50"
                placeholder={'/Users/name/Desktop/HBL-BMS\n/Users/name/Desktop/HBL-BMS/export.csv'}
              />

              <label className="mt-4 block text-sm font-semibold" htmlFor="output-dir">
                Output folder
              </label>
              <div className="mt-2 flex flex-col gap-3 sm:flex-row">
                <input
                  id="output-dir"
                  value={outputDir}
                  onChange={event => setOutputDir(event.target.value)}
                  disabled={isConverting}
                  className="min-w-0 flex-1 rounded-lg border border-slate-300 bg-white px-3 py-2 font-mono text-sm text-slate-900 shadow-sm outline-none focus:border-slate-900 disabled:opacity-50"
                  placeholder="/Users/name/Desktop/HBL-BMS"
                />
                <button
                  type="button"
                  onClick={convertLocalPaths}
                  disabled={isConverting || localPathCount === 0}
                  className="inline-flex items-center justify-center gap-2 rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-semibold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {isConverting ? <Loader2 className="h-4 w-4 animate-spin" /> : <FolderOpen className="h-4 w-4" />}
                  {isConverting ? 'Processing Locally...' : 'Convert Local Folder'}
                </button>
              </div>

              <div className="mt-5 grid grid-cols-1 gap-3 sm:grid-cols-3">
                <div className="rounded-lg bg-slate-50 p-3">
                  <p className="text-xs text-slate-500">Path entries</p>
                  <p className="mt-1 text-lg font-bold">{localPathCount}</p>
                </div>
                <div className="rounded-lg bg-slate-50 p-3">
                  <p className="text-xs text-slate-500">Local server</p>
                  <p className={`mt-1 text-sm font-bold ${localOnline ? 'text-emerald-700' : localOnline === false ? 'text-rose-700' : 'text-slate-700'}`}>
                    {localOnline ? 'Online' : localOnline === false ? 'Offline' : 'Not checked'}
                  </p>
                </div>
                <div className="rounded-lg bg-slate-50 p-3">
                  <p className="text-xs text-slate-500">Upload limit</p>
                  <p className="mt-1 text-sm font-bold text-slate-700">Bypassed</p>
                </div>
              </div>
            </div>
          ) : (
            <>
              <div
                onClick={() => inputRef.current?.click()}
                onDragEnter={event => {
                  event.preventDefault();
                  setIsDragging(true);
                }}
                onDragOver={event => event.preventDefault()}
                onDragLeave={() => setIsDragging(false)}
                onDrop={handleDrop}
                className={`flex min-h-[260px] cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed bg-white p-8 text-center shadow-sm transition ${
                  isDragging ? 'border-emerald-500 bg-emerald-50' : 'border-slate-300 hover:border-slate-400'
                }`}
              >
                <input
                  ref={inputRef}
                  type="file"
                  accept=".csv,text/csv"
                  multiple
                  onChange={handleFileChange}
                  className="hidden"
                />
                <UploadCloud className="mb-4 h-12 w-12 text-slate-500" />
                <h2 className="text-base font-semibold">Drop small BMS trend CSV files here</h2>
                <p className="mt-2 max-w-xl text-sm leading-6 text-slate-500">
                  Upload mode is only for small files. Large exports must use Local Folder mode so the data does not go through Vercel.
                </p>
                <button
                  type="button"
                  className="mt-5 rounded-lg bg-slate-900 px-4 py-2 text-sm font-semibold text-white hover:bg-slate-800"
                >
                  Browse CSV Files
                </button>
              </div>

              {files.length > 0 && (
                <div className="mt-6 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
                  <div className="flex flex-col gap-3 border-b border-slate-200 bg-slate-50 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
                    <div>
                      <h2 className="text-sm font-semibold">Selected Files</h2>
                      <p className="text-xs text-slate-500">{files.length} file(s), {formatBytes(totalSize)} total</p>
                    </div>
                    <button
                      type="button"
                      disabled={isConverting}
                      onClick={clearFiles}
                      className="inline-flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-600 hover:text-rose-700 disabled:opacity-40"
                    >
                      <Trash2 className="h-4 w-4" />
                      Clear
                    </button>
                  </div>

                  <div className="divide-y divide-slate-100">
                    {files.map(item => (
                      <div key={item.id} className="flex flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
                        <div className="min-w-0">
                          <p className="break-all text-sm font-medium text-slate-900">{item.file.name}</p>
                          <p className="mt-1 text-xs text-slate-500">{formatBytes(item.file.size)}</p>
                          {item.error && <p className="mt-1 text-xs text-rose-600">{item.error}</p>}
                        </div>
                        <div className="flex items-center gap-3">
                          <span
                            className={`rounded-full px-2.5 py-1 text-xs font-semibold ${
                              item.status === 'Completed'
                                ? 'bg-emerald-50 text-emerald-700'
                                : item.status === 'Failed'
                                  ? 'bg-rose-50 text-rose-700'
                                  : item.status === 'Processing' || item.status === 'Uploading'
                                    ? 'bg-blue-50 text-blue-700'
                                    : 'bg-slate-100 text-slate-600'
                            }`}
                          >
                            {item.status}
                          </span>
                          <button
                            type="button"
                            disabled={isConverting}
                            onClick={() => removeFile(item.id)}
                            className="rounded-lg p-2 text-slate-400 hover:bg-rose-50 hover:text-rose-600 disabled:opacity-40"
                            aria-label={`Remove ${item.file.name}`}
                          >
                            <Trash2 className="h-4 w-4" />
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </>
          )}
        </section>

        <aside className="lg:col-span-4">
          <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="text-sm font-semibold">Conversion Status</h2>
            <p className="mt-2 text-sm leading-6 text-slate-600">{progressMessage}</p>

            {mode === 'upload' && (
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
            )}

            {errorMessage && (
              <div className="mt-5 flex gap-2 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">
                <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0" />
                <span>{errorMessage}</span>
              </div>
            )}

            {localResult && (
              <div className="mt-5 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-900">
                <div className="flex gap-2">
                  <CheckCircle2 className="mt-0.5 h-4 w-4 flex-shrink-0" />
                  <span>Local ZIP package is ready.</span>
                </div>
                <dl className="mt-3 space-y-2 text-xs">
                  <div>
                    <dt className="font-semibold">Output</dt>
                    <dd className="break-all font-mono">{localResult.outputZip}</dd>
                  </div>
                  <div className="flex gap-4">
                    <span>Success: {localResult.successfulFiles}</span>
                    <span>Failed: {localResult.failedFiles}</span>
                  </div>
                </dl>
              </div>
            )}

            {downloadUrl && (
              <div className="mt-5 flex gap-2 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">
                <CheckCircle2 className="mt-0.5 h-4 w-4 flex-shrink-0" />
                <span>ZIP package is ready.</span>
              </div>
            )}

            {mode === 'upload' && (
              <button
                type="button"
                onClick={convertUploadedFiles}
                disabled={files.length === 0 || isConverting || hasOversizedUpload}
                className="mt-5 inline-flex w-full items-center justify-center gap-2 rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-semibold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {isConverting ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileSpreadsheet className="h-4 w-4" />}
                {isConverting ? 'Processing...' : 'Convert with Vercel API'}
              </button>
            )}

            {downloadUrl && (
              <a
                href={downloadUrl}
                download={downloadName}
                className="mt-3 inline-flex w-full items-center justify-center gap-2 rounded-lg bg-emerald-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-emerald-700"
              >
                <Download className="h-4 w-4" />
                Download ZIP
              </a>
            )}

            <div className="mt-5 rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs leading-5 text-slate-600">
              Start local companion:
              <code className="mt-2 block break-all rounded bg-white px-2 py-2 font-mono text-slate-800">
                python3 api/convert.py --serve-local
              </code>
            </div>
          </div>
        </aside>
      </main>
    </div>
  );
}
