import { useMemo, useRef, useState } from 'react';
import type { ChangeEvent, DragEvent } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  Download,
  FileSpreadsheet,
  Loader2,
  Trash2,
  UploadCloud,
} from 'lucide-react';

type FileStatus = 'Waiting' | 'Uploading' | 'Processing' | 'Completed' | 'Failed';

interface QueueFile {
  id: string;
  file: File;
  status: FileStatus;
  error?: string;
}

const API_ENDPOINT = '/api/convert';
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

export default function App() {
  const inputRef = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<QueueFile[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const [isConverting, setIsConverting] = useState(false);
  const [progressMessage, setProgressMessage] = useState('Select BMS / ACMV trend CSV files to begin.');
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null);
  const [downloadName, setDownloadName] = useState('hbl-bms-trend-analysis.zip');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const totalSize = useMemo(() => files.reduce((sum, item) => sum + item.file.size, 0), [files]);
  const completedCount = files.filter(item => item.status === 'Completed').length;
  const failedCount = files.filter(item => item.status === 'Failed').length;

  const addFiles = (incoming: File[]) => {
    const csvFiles = incoming.filter(file => file.name.toLowerCase().endsWith('.csv') || file.type === 'text/csv');
    if (csvFiles.length === 0) {
      setErrorMessage('Please select at least one .csv file.');
      return;
    }

    const oversized = csvFiles.filter(file => file.size > VERCEL_UPLOAD_LIMIT_BYTES);
    if (oversized.length > 0) {
      setErrorMessage(
        `Vercel serverless upload limit is 4.5 MB. ${oversized[0].name} is ${formatBytes(oversized[0].size)}. Use a smaller export, split the CSV, or run the Python backend outside Vercel for large BMS trend files.`
      );
    }

    if (downloadUrl) URL.revokeObjectURL(downloadUrl);
    setDownloadUrl(null);
    setErrorMessage(null);
    setProgressMessage(`${csvFiles.length} file(s) ready for conversion.`);
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
    setProgressMessage('Select BMS / ACMV trend CSV files to begin.');
  };

  const convertFiles = async () => {
    if (files.length === 0 || isConverting) return;

    if (downloadUrl) URL.revokeObjectURL(downloadUrl);
    setDownloadUrl(null);
    setErrorMessage(null);

    const oversized = files.find(item => item.file.size > VERCEL_UPLOAD_LIMIT_BYTES);
    if (oversized) {
      const message = `Vercel cannot receive ${oversized.file.name} because it is ${formatBytes(oversized.file.size)}. The Vercel request body limit is 4.5 MB.`;
      setErrorMessage(message);
      setFiles(prev => prev.map(item => item.id === oversized.id ? { ...item, status: 'Failed', error: message } : item));
      setProgressMessage('Large file blocked before upload. Split the CSV or use a non-Vercel Python backend for large trend exports.');
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
            <h2 className="text-base font-semibold">Drop BMS trend CSV files here</h2>
            <p className="mt-2 max-w-xl text-sm leading-6 text-slate-500">
              Select multiple ACMV, chiller, AHU, FCU, fan, pump, valve, VSD/VFD, alarm, and status trend exports.
              The Python backend will return Excel workbooks with Data and Analysis sheets.
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
        </section>

        <aside className="lg:col-span-4">
          <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="text-sm font-semibold">Conversion Status</h2>
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
              <div className="mt-5 flex gap-2 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">
                <CheckCircle2 className="mt-0.5 h-4 w-4 flex-shrink-0" />
                <span>ZIP package is ready.</span>
              </div>
            )}

            <button
              type="button"
              onClick={convertFiles}
              disabled={files.length === 0 || isConverting}
              className="mt-5 inline-flex w-full items-center justify-center gap-2 rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-semibold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {isConverting ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileSpreadsheet className="h-4 w-4" />}
              {isConverting ? 'Processing...' : 'Convert with Python Analyzer'}
            </button>

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
          </div>
        </aside>
      </main>
    </div>
  );
}
