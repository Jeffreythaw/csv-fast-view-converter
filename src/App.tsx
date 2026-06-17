import { useMemo, useRef, useState } from 'react';
import type { ChangeEvent } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  Download,
  FileSpreadsheet,
  Loader2,
  Trash2,
  UploadCloud,
} from 'lucide-react';

type OutputFormat = 'xlsx' | 'sqlite' | 'parquet';
type JobStatus = 'queued' | 'uploading' | 'processing' | 'completed' | 'failed';

interface QueueFile {
  id: string;
  file: File;
  uploadProgress: number;
  status: JobStatus | 'ready';
  jobId?: string;
  rowsProcessed?: number;
  message?: string;
  error?: string;
  downloadUrl?: string;
  downloadStatus?: string;
  outputReady?: boolean;
  outputExists?: boolean;
  outputSize?: number;
  startedAt?: number;
  finishedAt?: number;
}

interface BackendJob {
  id: string;
  status: JobStatus;
  output_format: OutputFormat;
  uploaded_bytes: number;
  total_bytes?: number;
  rows_processed: number;
  message: string;
  error?: string;
  download_url?: string;
  output_ready: boolean;
  output_exists: boolean;
  output_size: number;
}

const API_BASE = (import.meta.env.VITE_API_BASE || 'http://localhost:8000').replace(/\/$/, '');
const POLL_MS = 2000;

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
    uploadProgress: 0,
    status: 'ready',
  };
}

function statusTone(status: QueueFile['status']) {
  if (status === 'completed') return 'bg-emerald-50 text-emerald-700';
  if (status === 'failed') return 'bg-rose-50 text-rose-700';
  if (status === 'processing' || status === 'uploading' || status === 'queued') return 'bg-blue-50 text-blue-700';
  return 'bg-slate-100 text-slate-600';
}

async function pollJob(jobId: string, onUpdate: (job: BackendJob) => void): Promise<BackendJob> {
  while (true) {
    const response = await fetch(`${API_BASE}/api/jobs/${jobId}`);
    if (!response.ok) throw new Error(await response.text());
    const job = await response.json() as BackendJob;
    onUpdate(job);
    if (job.status === 'completed' || job.status === 'failed') return job;
    await new Promise(resolve => window.setTimeout(resolve, POLL_MS));
  }
}

function uploadFile(file: File, outputFormat: OutputFormat, onProgress: (progress: number) => void): Promise<BackendJob> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const formData = new FormData();
    formData.append('file', file, file.name);
    formData.append('output_format', outputFormat);

    xhr.upload.onprogress = event => {
      if (event.lengthComputable) onProgress(Math.round((event.loaded / event.total) * 100));
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(JSON.parse(xhr.responseText) as BackendJob);
      } else {
        reject(new Error(xhr.responseText || `Upload failed with HTTP ${xhr.status}`));
      }
    };
    xhr.onerror = () => reject(new Error('Network error while uploading. Check the backend service URL.'));
    xhr.open('POST', `${API_BASE}/api/jobs`);
    xhr.send(formData);
  });
}

async function readError(response: Response): Promise<string> {
  const text = await response.text();
  if (!text) return `HTTP ${response.status} ${response.statusText}`;
  try {
    const parsed = JSON.parse(text) as { detail?: unknown; message?: string };
    if (typeof parsed.detail === 'string') return parsed.detail;
    if (parsed.detail && typeof parsed.detail === 'object') {
      const detail = parsed.detail as { message?: string };
      return detail.message ? `${detail.message} (${JSON.stringify(parsed.detail)})` : JSON.stringify(parsed.detail);
    }
    return parsed.message || text;
  } catch {
    return text;
  }
}

function filenameFromDisposition(disposition: string | null, fallback: string) {
  if (!disposition) return fallback;
  const utfMatch = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (utfMatch?.[1]) return decodeURIComponent(utfMatch[1]);
  const match = disposition.match(/filename="?([^";]+)"?/i);
  return match?.[1] || fallback;
}

export default function App() {
  const inputRef = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<QueueFile[]>([]);
  const [outputFormat] = useState<OutputFormat>('xlsx');
  const [isRunning, setIsRunning] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [progressMessage, setProgressMessage] = useState('Select large BMS / ACMV CSV files, then upload to the processing backend.');

  const totalSize = useMemo(() => files.reduce((sum, item) => sum + item.file.size, 0), [files]);
  const completedCount = files.filter(item => item.status === 'completed').length;
  const failedCount = files.filter(item => item.status === 'failed').length;

  const updateFile = (id: string, patch: Partial<QueueFile>) => {
    setFiles(previous => previous.map(item => item.id === id ? { ...item, ...patch } : item));
  };

  const addFiles = (incoming: File[]) => {
    const csvFiles = incoming.filter(file => file.name.toLowerCase().endsWith('.csv'));
    if (csvFiles.length === 0) {
      setErrorMessage('Select at least one .csv file.');
      return;
    }
    setFiles(csvFiles.map(makeQueueItem));
    setErrorMessage(null);
    setProgressMessage(`${csvFiles.length} CSV file${csvFiles.length > 1 ? 's' : ''} selected. Ready to upload.`);
  };

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    addFiles(Array.from(event.target.files || []));
    event.target.value = '';
  };

  const clearFiles = () => {
    if (isRunning) return;
    setFiles([]);
    setErrorMessage(null);
    setProgressMessage('Select large BMS / ACMV CSV files, then upload to the processing backend.');
  };

  const downloadOutput = async (item: QueueFile) => {
    if (!item.jobId) {
      updateFile(item.id, { error: 'Missing job ID for download.' });
      return;
    }
    const downloadUrl = `${API_BASE}/api/jobs/${item.jobId}/download`;
    updateFile(item.id, { downloadStatus: `Downloading from ${downloadUrl}`, error: undefined });
    try {
      const response = await fetch(downloadUrl);
      if (!response.ok) {
        const detail = await readError(response);
        const message = `Download failed with HTTP ${response.status}: ${detail}`;
        updateFile(item.id, { error: message, downloadStatus: message });
        setErrorMessage(message);
        return;
      }

      const blob = await response.blob();
      if (blob.size === 0) {
        const message = 'Download failed: backend returned an empty ZIP file.';
        updateFile(item.id, { error: message, downloadStatus: message });
        setErrorMessage(message);
        return;
      }

      const fallbackName = `${item.file.name.replace(/\.csv$/i, '')}-${outputFormat}.zip`;
      const filename = filenameFromDisposition(response.headers.get('Content-Disposition'), fallbackName);
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = objectUrl;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
      updateFile(item.id, { downloadStatus: `Downloaded ${filename} (${formatBytes(blob.size)}).` });
      setErrorMessage(null);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unexpected download error.';
      updateFile(item.id, { error: message, downloadStatus: message });
      setErrorMessage(message);
    }
  };

  const runJobs = async () => {
    if (files.length === 0 || isRunning) return;
    setIsRunning(true);
    setErrorMessage(null);

    for (const item of files) {
      try {
        updateFile(item.id, { status: 'uploading', uploadProgress: 0, error: undefined, message: 'Uploading file.' });
        updateFile(item.id, { startedAt: Date.now(), finishedAt: undefined });
        setProgressMessage(`Uploading ${item.file.name}...`);
        const created = await uploadFile(item.file, outputFormat, progress => updateFile(item.id, { uploadProgress: progress }));
        updateFile(item.id, {
          jobId: created.id,
          status: created.status,
          message: created.message,
          rowsProcessed: created.rows_processed,
          outputReady: created.output_ready,
          outputExists: created.output_exists,
          outputSize: created.output_size,
          downloadUrl: `${API_BASE}/api/jobs/${created.id}/download`,
          uploadProgress: 100,
        });

        setProgressMessage(`${item.file.name} queued. Waiting for backend processing...`);
        const finalJob = await pollJob(created.id, job => {
          updateFile(item.id, {
            status: job.status,
            message: job.message,
            rowsProcessed: job.rows_processed,
            error: job.error,
            outputReady: job.output_ready,
            outputExists: job.output_exists,
            outputSize: job.output_size,
            downloadUrl: `${API_BASE}/api/jobs/${job.id}/download`,
          });
        });

        if (finalJob.status === 'failed') {
          throw new Error(finalJob.error || 'Backend conversion failed.');
        }
        if (!finalJob.output_ready) {
          throw new Error(`Backend marked the job completed, but the output ZIP is not ready. exists=${finalJob.output_exists}, size=${finalJob.output_size}`);
        }
        updateFile(item.id, {
          outputReady: finalJob.output_ready,
          outputExists: finalJob.output_exists,
          outputSize: finalJob.output_size,
          downloadUrl: `${API_BASE}/api/jobs/${finalJob.id}/download`,
          finishedAt: Date.now(),
        });
        setProgressMessage(`${item.file.name} completed and is ready to download.`);
      } catch (error) {
        const message = error instanceof Error ? error.message : 'Unexpected conversion error.';
        updateFile(item.id, { status: 'failed', error: message, message, finishedAt: Date.now() });
        setErrorMessage(message);
      }
    }
    setIsRunning(false);
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
          <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs font-semibold text-slate-600">
            Backend: {API_BASE}
          </div>
        </div>
      </header>

      <main className="mx-auto grid max-w-6xl grid-cols-1 gap-6 px-4 py-8 sm:px-6 lg:grid-cols-12 lg:px-8">
        <section className="lg:col-span-8">
          <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="border-b border-slate-200 pb-5">
              <h2 className="text-base font-semibold">Large CSV Job Upload</h2>
              <p className="mt-1 text-sm leading-6 text-slate-500">
                Files upload to a dedicated backend worker. The backend streams upload data to disk and processes CSV rows without loading the full file into memory.
              </p>
            </div>

            <div className="mt-5 rounded-lg border border-slate-900 bg-white p-4 text-left">
              <span className="flex items-center gap-2 text-sm font-semibold uppercase">
                <FileSpreadsheet className="h-4 w-4" />
                XLSX
              </span>
              <span className="mt-1 block text-xs leading-5 text-slate-500">
                Excel workbook with streamed Data sheets and a BMS / ACMV operation analysis report.
              </span>
            </div>

            <input ref={inputRef} type="file" accept=".csv,text/csv" multiple className="hidden" onChange={handleFileChange} />
            <button
              type="button"
              onClick={() => inputRef.current?.click()}
              disabled={isRunning}
              className="mt-5 inline-flex min-h-28 w-full items-center justify-center gap-3 rounded-lg border-2 border-dashed border-slate-300 bg-white px-4 py-6 text-sm font-semibold text-slate-900 hover:border-slate-900 disabled:opacity-40"
            >
              <UploadCloud className="h-5 w-5 text-blue-700" />
              Select CSV Files
            </button>

            <div className="mt-5 overflow-hidden rounded-lg border border-slate-200 bg-slate-50">
              <div className="flex flex-col gap-3 border-b border-slate-200 bg-white px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <p className="text-sm font-semibold">Selected Jobs</p>
                  <p className="text-xs text-slate-500">{files.length} file(s), {formatBytes(totalSize)} total</p>
                </div>
                {files.length > 0 && (
                  <button
                    type="button"
                    onClick={clearFiles}
                    disabled={isRunning}
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
                <div className="max-h-[460px] divide-y divide-slate-200 overflow-auto">
                  {files.map(item => (
                    <div key={item.id} className="px-4 py-3">
                      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                        <div className="min-w-0">
                          <p className="break-all text-sm font-medium">{item.file.name}</p>
                          <p className="mt-1 text-xs text-slate-500">{formatBytes(item.file.size)}</p>
                          <p className="mt-1 text-xs text-slate-500">
                            {item.message || 'Ready.'}
                            {item.rowsProcessed ? ` ${item.rowsProcessed.toLocaleString()} rows processed.` : ''}
                          </p>
                          {item.startedAt && (
                            <p className="mt-1 text-xs text-slate-500">
                              Elapsed: {Math.max(0, Math.round(((item.finishedAt || Date.now()) - item.startedAt) / 1000)).toLocaleString()} sec
                            </p>
                          )}
                          {item.error && <p className="mt-1 text-xs text-rose-600">{item.error}</p>}
                          {item.jobId && (
                            <div className="mt-2 space-y-1 rounded-lg bg-white p-2 text-[11px] leading-4 text-slate-500">
                              <p className="break-all"><span className="font-semibold text-slate-700">Job ID:</span> {item.jobId}</p>
                              <p className="break-all"><span className="font-semibold text-slate-700">Download URL:</span> {item.downloadUrl || `${API_BASE}/api/jobs/${item.jobId}/download`}</p>
                              <p>
                                <span className="font-semibold text-slate-700">Output:</span>{' '}
                                ready={String(Boolean(item.outputReady))}, exists={String(Boolean(item.outputExists))}, size={formatBytes(item.outputSize || 0)}
                              </p>
                              {item.downloadStatus && <p className="break-all"><span className="font-semibold text-slate-700">Download status:</span> {item.downloadStatus}</p>}
                            </div>
                          )}
                        </div>
                        <span className={`w-fit rounded-full px-2.5 py-1 text-xs font-semibold ${statusTone(item.status)}`}>
                          {item.status}
                        </span>
                      </div>
                      {item.status === 'uploading' && (
                        <div className="mt-3 h-2 overflow-hidden rounded-full bg-slate-200">
                          <div className="h-full bg-blue-600 transition-all" style={{ width: `${item.uploadProgress}%` }} />
                        </div>
                      )}
                      {item.jobId && item.status === 'completed' && (
                        <button
                          type="button"
                          onClick={() => void downloadOutput(item)}
                          disabled={!item.outputReady}
                          className="mt-3 inline-flex items-center gap-2 rounded-lg bg-emerald-600 px-3 py-2 text-xs font-semibold text-white hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-40"
                        >
                          <Download className="h-4 w-4" />
                          Download Output ZIP
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>

            <button
              type="button"
              onClick={runJobs}
              disabled={files.length === 0 || isRunning}
              className="mt-5 inline-flex w-full items-center justify-center gap-2 rounded-lg bg-slate-900 px-4 py-3 text-sm font-semibold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {isRunning ? <Loader2 className="h-4 w-4 animate-spin" /> : <UploadCloud className="h-4 w-4" />}
              {isRunning ? 'Running Jobs...' : `Upload and Process as ${outputFormat.toUpperCase()}`}
            </button>
          </div>
        </section>

        <aside className="lg:col-span-4">
          <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="text-sm font-semibold">Status</h2>
            <p className="mt-2 text-sm leading-6 text-slate-600">{progressMessage}</p>

            {errorMessage && (
              <div className="mt-5 flex gap-2 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">
                <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0" />
                <span>{errorMessage}</span>
              </div>
            )}

            {completedCount > 0 && (
              <div className="mt-5 flex gap-2 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">
                <CheckCircle2 className="mt-0.5 h-4 w-4 flex-shrink-0" />
                <span>{completedCount} job{completedCount > 1 ? 's' : ''} completed.</span>
              </div>
            )}

            <div className="mt-5 grid grid-cols-3 gap-3 text-center">
              <div className="rounded-lg bg-slate-50 p-3">
                <p className="text-xs text-slate-500">Jobs</p>
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

            <div className="mt-5 rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs leading-5 text-slate-600">
              Deploy the backend on a long-running host such as Render, Fly.io, Railway, ECS, or a VM. Set <code className="font-mono">VITE_API_BASE</code> on Vercel to that backend URL.
            </div>
          </div>
        </aside>
      </main>
    </div>
  );
}
