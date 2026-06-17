import { useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  Download,
  FileSpreadsheet,
  FolderOpen,
  Loader2,
  RefreshCw,
  Server,
  Trash2,
} from 'lucide-react';

interface LocalResult {
  outputZip: string;
  successfulFiles: number;
  failedFiles: number;
  inputFiles: string[];
}

const LOCAL_API_BASE = 'http://127.0.0.1:8765';
const LAUNCHER_URL = '/Start%20CSV%20Fast%20View%20Converter.command';

function shortPath(path: string) {
  const parts = path.split('/');
  if (parts.length <= 4) return path;
  return `.../${parts.slice(-3).join('/')}`;
}

function outputPath(paths: string[]) {
  if (paths.length === 0) return 'Select a CSV folder or files first';
  const first = paths[0];
  const folder = first.toLowerCase().endsWith('.csv') ? first.split('/').slice(0, -1).join('/') : first;
  return `${folder}/hbl-bms-trend-analysis.zip`;
}

export default function App() {
  const [helperOnline, setHelperOnline] = useState<boolean | null>(null);
  const [selectedPaths, setSelectedPaths] = useState<string[]>([]);
  const [isPicking, setIsPicking] = useState(false);
  const [isConverting, setIsConverting] = useState(false);
  const [localResult, setLocalResult] = useState<LocalResult | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [progressMessage, setProgressMessage] = useState('Start the local converter first. It handles large CSV files without sending data to Vercel.');

  const canUseConverter = helperOnline === true && !isPicking && !isConverting;
  const readyToConvert = canUseConverter && selectedPaths.length > 0;
  const selectedOutputPath = useMemo(() => outputPath(selectedPaths), [selectedPaths]);

  const checkHelper = async (showErrors = false) => {
    try {
      const response = await fetch(`${LOCAL_API_BASE}/api/local/status`, { method: 'GET' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setHelperOnline(true);
      setErrorMessage(null);
      setProgressMessage(selectedPaths.length > 0 ? 'Local converter is ready. Convert when ready.' : 'Local converter is ready. Select a CSV folder or CSV files.');
      return true;
    } catch {
      setHelperOnline(false);
      if (showErrors) {
        setErrorMessage('Local converter is not running yet. Download and open the starter once, then come back to this page.');
      }
      setProgressMessage('Local converter is offline. Start it before selecting or converting large CSV files.');
      return false;
    }
  };

  useEffect(() => {
    void checkHelper(false);
    const timer = window.setInterval(() => void checkHelper(false), 4000);
    return () => window.clearInterval(timer);
  }, []);

  const pickPaths = async (mode: 'folder' | 'files') => {
    if (!canUseConverter) {
      await checkHelper(true);
      return;
    }
    setIsPicking(true);
    setErrorMessage(null);
    setLocalResult(null);
    setProgressMessage(mode === 'folder' ? 'Opening folder picker on this computer...' : 'Opening CSV file picker on this computer...');
    try {
      const response = await fetch(`${LOCAL_API_BASE}/api/local/pick-${mode}`, { method: 'POST' });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.ok) throw new Error(payload.error || `Picker failed with HTTP ${response.status}`);
      const paths = Array.isArray(payload.paths) ? payload.paths.filter(Boolean) : [];
      if (paths.length === 0) {
        setProgressMessage('No folder or file selected.');
        return;
      }
      setSelectedPaths(paths);
      setProgressMessage(`${paths.length} path${paths.length > 1 ? 's' : ''} selected. Ready to convert locally.`);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'Unable to select files.');
      setProgressMessage('Selection failed.');
    } finally {
      setIsPicking(false);
    }
  };

  const convertSelectedPaths = async () => {
    if (!readyToConvert) return;
    setIsConverting(true);
    setErrorMessage(null);
    setLocalResult(null);
    setProgressMessage('Streaming CSV files locally and writing Excel workbooks. Keep the starter window open.');
    try {
      const response = await fetch(`${LOCAL_API_BASE}/api/local/convert`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths: selectedPaths }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.ok) throw new Error(payload.error || `Conversion failed with HTTP ${response.status}`);
      setLocalResult(payload as LocalResult);
      setProgressMessage('Conversion completed. ZIP package was written next to the selected CSV files.');
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'Unexpected local conversion error.');
      setProgressMessage('Conversion failed. Keep the starter window open and try again.');
      await checkHelper(false);
    } finally {
      setIsConverting(false);
    }
  };

  const clearSelection = () => {
    if (isConverting) return;
    setSelectedPaths([]);
    setLocalResult(null);
    setErrorMessage(null);
    setProgressMessage(helperOnline ? 'Local converter is ready. Select a CSV folder or CSV files.' : 'Start the local converter first. It handles large CSV files without sending data to Vercel.');
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
          <div
            className={`inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-xs font-semibold ${
              helperOnline
                ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                : helperOnline === false
                  ? 'border-rose-200 bg-rose-50 text-rose-700'
                  : 'border-slate-200 bg-slate-50 text-slate-600'
            }`}
          >
            <span className={`h-2 w-2 rounded-full ${helperOnline ? 'bg-emerald-500' : helperOnline === false ? 'bg-rose-500' : 'bg-slate-400'}`} />
            {helperOnline ? 'Local engine ready' : helperOnline === false ? 'Local engine required' : 'Checking local engine'}
          </div>
        </div>
      </header>

      <main className="mx-auto grid max-w-6xl grid-cols-1 gap-6 px-4 py-8 sm:px-6 lg:grid-cols-12 lg:px-8">
        <section className="lg:col-span-8">
          <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="flex items-start gap-3 border-b border-slate-200 pb-5">
              <div className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-lg bg-emerald-50 text-emerald-700">
                <Server className="h-5 w-5" />
              </div>
              <div>
                <h2 className="text-base font-semibold">Streaming Local Conversion</h2>
                <p className="mt-1 text-sm leading-6 text-slate-500">
                  Large CSV files are converted by a local streaming engine. Data stays on this computer and is not uploaded to Vercel.
                </p>
              </div>
            </div>

            {helperOnline !== true && (
              <div className="mt-5 rounded-lg border border-amber-200 bg-amber-50 p-4">
                <p className="text-sm font-semibold text-amber-900">One-time starter required</p>
                <p className="mt-1 text-sm leading-6 text-amber-800">
                  The web browser cannot install or run local software by itself. Download and open the starter once; it installs the converter and keeps it running.
                </p>
                <div className="mt-4 flex flex-col gap-3 sm:flex-row">
                  <a
                    href={LAUNCHER_URL}
                    download="Start CSV Fast View Converter.command"
                    className="inline-flex items-center justify-center gap-2 rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-semibold text-white hover:bg-slate-800"
                  >
                    <Download className="h-4 w-4" />
                    Download Starter
                  </a>
                  <button
                    type="button"
                    onClick={() => void checkHelper(true)}
                    className="inline-flex items-center justify-center gap-2 rounded-lg border border-amber-300 bg-white px-4 py-2.5 text-sm font-semibold text-amber-900 hover:bg-amber-100"
                  >
                    <RefreshCw className="h-4 w-4" />
                    I Opened It
                  </button>
                </div>
              </div>
            )}

            <div className="mt-5 grid grid-cols-1 gap-3 sm:grid-cols-2">
              <button
                type="button"
                onClick={() => pickPaths('folder')}
                disabled={!canUseConverter}
                className="inline-flex min-h-24 items-center justify-center gap-3 rounded-lg border border-slate-300 bg-white px-4 py-4 text-sm font-semibold text-slate-900 shadow-sm hover:border-slate-900 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {isPicking ? <Loader2 className="h-5 w-5 animate-spin" /> : <FolderOpen className="h-5 w-5 text-emerald-700" />}
                Select CSV Folder
              </button>
              <button
                type="button"
                onClick={() => pickPaths('files')}
                disabled={!canUseConverter}
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
              <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">ZIP output</p>
              <p className="mt-2 break-all font-mono text-sm text-slate-800">{localResult?.outputZip || selectedOutputPath}</p>
            </div>

            <button
              type="button"
              onClick={convertSelectedPaths}
              disabled={!readyToConvert}
              className="mt-5 inline-flex w-full items-center justify-center gap-2 rounded-lg bg-slate-900 px-4 py-3 text-sm font-semibold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {isConverting ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileSpreadsheet className="h-4 w-4" />}
              {isConverting ? 'Converting Large CSV Files...' : 'Convert to ZIP'}
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

            <div className="mt-5 grid grid-cols-2 gap-3 text-center">
              <div className="rounded-lg bg-slate-50 p-3">
                <p className="text-xs text-slate-500">Selected</p>
                <p className="mt-1 text-lg font-bold">{selectedPaths.length}</p>
              </div>
              <div className="rounded-lg bg-emerald-50 p-3">
                <p className="text-xs text-emerald-700">Engine</p>
                <p className="mt-1 text-sm font-bold text-emerald-800">{helperOnline ? 'Ready' : 'Required'}</p>
              </div>
            </div>
          </div>
        </aside>
      </main>
    </div>
  );
}
