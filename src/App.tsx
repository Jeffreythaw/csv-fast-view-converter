import { useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  FileSpreadsheet,
  FolderOpen,
  HardDrive,
  Loader2,
  RefreshCw,
  Trash2,
} from 'lucide-react';

interface LocalResult {
  outputZip: string;
  successfulFiles: number;
  failedFiles: number;
  inputFiles: string[];
}

const LOCAL_API_BASE = 'http://127.0.0.1:8765';

function shortPath(path: string) {
  const parts = path.split('/');
  if (parts.length <= 4) return path;
  return `.../${parts.slice(-3).join('/')}`;
}

function outputFolderFor(paths: string[]) {
  if (paths.length === 0) return 'First selected CSV folder';
  const first = paths[0];
  if (!first.includes('.csv')) return first;
  return first.split('/').slice(0, -1).join('/') || first;
}

export default function App() {
  const [selectedPaths, setSelectedPaths] = useState<string[]>([]);
  const [localOnline, setLocalOnline] = useState<boolean | null>(null);
  const [isPicking, setIsPicking] = useState(false);
  const [isConverting, setIsConverting] = useState(false);
  const [localResult, setLocalResult] = useState<LocalResult | null>(null);
  const [progressMessage, setProgressMessage] = useState('Select a CSV folder or CSV files, then convert locally.');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const outputFolder = useMemo(() => outputFolderFor(selectedPaths), [selectedPaths]);

  const checkLocalServer = async (showError = false) => {
    try {
      const response = await fetch(`${LOCAL_API_BASE}/api/local/status`, { method: 'GET' });
      if (!response.ok) throw new Error(`Local converter returned HTTP ${response.status}.`);
      setLocalOnline(true);
      if (showError) setErrorMessage(null);
      return true;
    } catch {
      setLocalOnline(false);
      if (showError) {
        setErrorMessage('Local converter is not running. Start it with: npm run local-api');
      }
      return false;
    }
  };

  useEffect(() => {
    void checkLocalServer(false);
  }, []);

  const pickPaths = async (mode: 'folder' | 'files') => {
    if (isPicking || isConverting) return;
    setIsPicking(true);
    setErrorMessage(null);
    setLocalResult(null);
    setProgressMessage(mode === 'folder' ? 'Opening local folder picker...' : 'Opening local CSV file picker...');

    try {
      const online = await checkLocalServer(true);
      if (!online) return;

      const response = await fetch(`${LOCAL_API_BASE}/api/local/pick-${mode}`, { method: 'POST' });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.ok) {
        throw new Error(payload.error || `Local picker failed with HTTP ${response.status}.`);
      }

      const paths = Array.isArray(payload.paths) ? payload.paths.filter(Boolean) : [];
      if (paths.length === 0) {
        setProgressMessage('No folder or CSV file was selected.');
        return;
      }

      setSelectedPaths(paths);
      setProgressMessage(`${paths.length} local path${paths.length > 1 ? 's' : ''} selected. Ready to convert.`);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unable to select local files.';
      setErrorMessage(message);
      setProgressMessage('Local selection failed.');
    } finally {
      setIsPicking(false);
    }
  };

  const convertSelectedPaths = async () => {
    if (selectedPaths.length === 0 || isConverting) return;
    setIsConverting(true);
    setErrorMessage(null);
    setLocalResult(null);
    setProgressMessage('Local Python converter is reading CSV files one by one and creating the ZIP package...');

    try {
      const online = await checkLocalServer(true);
      if (!online) return;

      const response = await fetch(`${LOCAL_API_BASE}/api/local/convert`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths: selectedPaths }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.ok) {
        throw new Error(payload.error || `Local conversion failed with HTTP ${response.status}.`);
      }

      setLocalResult(payload as LocalResult);
      setProgressMessage('Conversion completed. ZIP package is ready locally.');
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unexpected local conversion error.';
      setErrorMessage(message);
      setProgressMessage('Conversion failed. Review the error and try again.');
    } finally {
      setIsConverting(false);
    }
  };

  const clearSelection = () => {
    if (isConverting) return;
    setSelectedPaths([]);
    setLocalResult(null);
    setErrorMessage(null);
    setProgressMessage('Select a CSV folder or CSV files, then convert locally.');
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
              <p className="text-sm font-medium text-slate-500">Local BMS / ACMV Trend Analyzer</p>
            </div>
          </div>
          <div
            className={`inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-xs font-semibold ${
              localOnline
                ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                : localOnline === false
                  ? 'border-rose-200 bg-rose-50 text-rose-700'
                  : 'border-slate-200 bg-slate-50 text-slate-600'
            }`}
          >
            <span className={`h-2 w-2 rounded-full ${localOnline ? 'bg-emerald-500' : localOnline === false ? 'bg-rose-500' : 'bg-slate-400'}`} />
            {localOnline ? 'Local converter ready' : localOnline === false ? 'Local converter offline' : 'Checking local converter'}
          </div>
        </div>
      </header>

      <main className="mx-auto grid max-w-6xl grid-cols-1 gap-6 px-4 py-8 sm:px-6 lg:grid-cols-12 lg:px-8">
        <section className="lg:col-span-8">
          <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="flex items-start gap-3 border-b border-slate-200 pb-5">
              <div className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-lg bg-emerald-50 text-emerald-700">
                <HardDrive className="h-5 w-5" />
              </div>
              <div>
                <h2 className="text-base font-semibold">Local Folder Conversion</h2>
                <p className="mt-1 text-sm leading-6 text-slate-500">
                  CSV files stay on this machine. The local Python converter reads selected files one by one and writes one ZIP package.
                </p>
              </div>
            </div>

            <div className="mt-5 grid grid-cols-1 gap-3 sm:grid-cols-2">
              <button
                type="button"
                onClick={() => pickPaths('folder')}
                disabled={isPicking || isConverting}
                className="inline-flex min-h-24 items-center justify-center gap-3 rounded-lg border border-slate-300 bg-white px-4 py-4 text-sm font-semibold text-slate-900 shadow-sm hover:border-slate-900 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {isPicking ? <Loader2 className="h-5 w-5 animate-spin" /> : <FolderOpen className="h-5 w-5 text-emerald-700" />}
                Select CSV Folder
              </button>
              <button
                type="button"
                onClick={() => pickPaths('files')}
                disabled={isPicking || isConverting}
                className="inline-flex min-h-24 items-center justify-center gap-3 rounded-lg border border-slate-300 bg-white px-4 py-4 text-sm font-semibold text-slate-900 shadow-sm hover:border-slate-900 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {isPicking ? <Loader2 className="h-5 w-5 animate-spin" /> : <FileSpreadsheet className="h-5 w-5 text-blue-700" />}
                Select CSV Files
              </button>
            </div>

            <div className="mt-5 rounded-lg border border-slate-200 bg-slate-50 p-4">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Selected input</p>
                  <p className="mt-1 text-sm font-medium text-slate-900">
                    {selectedPaths.length === 0 ? 'No CSV folder or files selected yet' : `${selectedPaths.length} path${selectedPaths.length > 1 ? 's' : ''} selected`}
                  </p>
                </div>
                {selectedPaths.length > 0 && (
                  <button
                    type="button"
                    onClick={clearSelection}
                    disabled={isConverting}
                    className="inline-flex items-center justify-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-600 hover:text-rose-700 disabled:opacity-40"
                  >
                    <Trash2 className="h-4 w-4" />
                    Clear
                  </button>
                )}
              </div>

              {selectedPaths.length > 0 && (
                <div className="mt-4 max-h-64 space-y-2 overflow-auto">
                  {selectedPaths.map(path => (
                    <div key={path} className="rounded-md border border-slate-200 bg-white px-3 py-2 font-mono text-xs text-slate-700" title={path}>
                      {shortPath(path)}
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="mt-5 rounded-lg border border-slate-200 bg-white p-4">
              <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">ZIP output location</p>
              <p className="mt-2 break-all font-mono text-sm text-slate-800">{outputFolder}/hbl-bms-trend-analysis.zip</p>
            </div>

            <button
              type="button"
              onClick={convertSelectedPaths}
              disabled={selectedPaths.length === 0 || isConverting || isPicking}
              className="mt-5 inline-flex w-full items-center justify-center gap-2 rounded-lg bg-slate-900 px-4 py-3 text-sm font-semibold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {isConverting ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileSpreadsheet className="h-4 w-4" />}
              {isConverting ? 'Converting Locally...' : 'Convert to ZIP'}
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

            {localResult && (
              <div className="mt-5 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-900">
                <div className="flex gap-2">
                  <CheckCircle2 className="mt-0.5 h-4 w-4 flex-shrink-0" />
                  <span>ZIP package is ready.</span>
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

            <div className="mt-5 rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs leading-5 text-slate-600">
              Start the local converter before selecting files:
              <code className="mt-2 block break-all rounded bg-white px-2 py-2 font-mono text-slate-800">
                npm run local-api
              </code>
            </div>

            <button
              type="button"
              onClick={() => void checkLocalServer(true)}
              disabled={isConverting || isPicking}
              className="mt-3 inline-flex w-full items-center justify-center gap-2 rounded-lg border border-slate-200 bg-white px-4 py-2.5 text-sm font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-40"
            >
              <RefreshCw className="h-4 w-4" />
              Retry Connection
            </button>
          </div>
        </aside>
      </main>
    </div>
  );
}
