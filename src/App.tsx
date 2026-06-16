/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useRef, useMemo } from 'react';
import { 
  FileSpreadsheet, 
  Download, 
  FileUp, 
  AlertCircle, 
  RefreshCw, 
  Info, 
  Trash2, 
  Check,
  Activity,
  FileCheck2,
  ShieldAlert
} from 'lucide-react';
import JSZip from 'jszip';
import { processCSVFile, ProcessingResult } from './utils/csvProcessor';

interface QueueItem {
  id: string;
  file: File;
  status: 'waiting' | 'processing' | 'completed' | 'failed';
  progressMessage?: string;
  metadata?: Omit<ProcessingResult, 'xlsxBuffer'>; // Store only metadata statics to preserve memory!
  error?: string;
}

export default function App() {
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [isProcessing, setIsProcessing] = useState(false);
  const [isZipping, setIsZipping] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Stats calculation
  const totalFiles = queue.length;
  const successCount = queue.filter(item => item.status === 'completed').length;
  const failedCount = queue.filter(item => item.status === 'failed').length;
  const waitingCount = queue.filter(item => item.status === 'waiting').length;
  const processingCount = queue.filter(item => item.status === 'processing').length;
  
  const isAllDone = totalFiles > 0 && waitingCount === 0 && processingCount === 0;

  // Total initial file size
  const totalInputSize = useMemo(() => {
    return queue.reduce((acc, item) => acc + item.file.size, 0);
  }, [queue]);

  // Aggregate metrics from processed metadata
  const aggregateMetrics = useMemo(() => {
    let totalRows = 0;
    let totalCols = 0;
    let autoSemicolonCount = 0;
    let autoCommaCount = 0;
    let autoTabCount = 0;
    let autoPipeCount = 0;
    let numericCols = 0;
    let dateCols = 0;
    let removedRows = 0;
    let removedCols = 0;

    queue.forEach(item => {
      if (item.metadata) {
        totalRows += item.metadata.rowCount;
        totalCols += item.metadata.colCount;
        removedRows += item.metadata.removedEmptyRows;
        removedCols += item.metadata.removedEmptyCols;
        numericCols += item.metadata.detectedNumericColsCount;
        dateCols += item.metadata.detectedDateColsCount;
        
        const sep = item.metadata.separator;
        if (sep === ';') autoSemicolonCount++;
        else if (sep === ',') autoCommaCount++;
        else if (sep === '\t') autoTabCount++;
        else if (sep === '|') autoPipeCount++;
      }
    });

    return {
      totalRows,
      totalCols,
      autoSemicolonCount,
      autoCommaCount,
      autoTabCount,
      autoPipeCount,
      numericCols,
      dateCols,
      removedRows,
      removedCols
    };
  }, [queue]);

  const handleDrag = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.type === "dragenter" || e.type === "dragover") {
      setDragActive(true);
    } else if (e.type === "dragleave") {
      setDragActive(false);
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);

    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      addFilesToQueue(Array.from(e.dataTransfer.files));
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      addFilesToQueue(Array.from(e.target.files));
    }
  };

  const addFilesToQueue = (files: File[]) => {
    const csvFiles = files.filter(f => f.name.toLowerCase().endsWith('.csv') || f.type === 'text/csv');
    
    if (csvFiles.length === 0) {
      alert("Please upload valid .csv telemetry log or status files.");
      return;
    }

    const newItems = csvFiles.map(file => ({
      id: `${file.name}-${Date.now()}-${Math.random()}`,
      file,
      status: 'waiting' as const,
      progressMessage: 'Waiting in queue...'
    }));

    setQueue(prev => [...prev, ...newItems]);
  };

  const removeItem = (id: string) => {
    if (isProcessing) return;
    setQueue(prev => prev.filter(item => item.id !== id));
  };

  const clearQueue = () => {
    if (isProcessing) return;
    setQueue([]);
  };

  // Convert files sequentially (one-by-one) to avoid extreme RAM peaks!
  const processQueue = async () => {
    if (queue.length === 0 || isProcessing) return;
    setIsProcessing(true);

    const itemsToProcess = [...queue];

    for (let i = 0; i < itemsToProcess.length; i++) {
      const currentItem = itemsToProcess[i];
      if (currentItem.status === 'completed') continue; // Skip already completed
      
      // Update state to Processing
      setQueue(prev => prev.map(item => {
        if (item.id === currentItem.id) {
          return { ...item, status: 'processing', progressMessage: 'Parsing file content...' };
        }
        return item;
      }));

      try {
        await new Promise(resolve => setTimeout(resolve, 80));
        
        setQueue(prev => prev.map(item => {
          if (item.id === currentItem.id) {
            return { ...item, progressMessage: 'Scrubbing columns and parsing type formats...' };
          }
          return item;
        }));
        
        // Execute conversion in memory
        const result = await processCSVFile(currentItem.file);
        
        // OPTIMIZATION: Discard binary workbook array buffer from persistent React state!
        // We only save scalar metadata stats to prevent high memory browser heap leaks.
        const { xlsxBuffer, ...metadata } = result;

        await new Promise(resolve => setTimeout(resolve, 80));

        setQueue(prev => prev.map(item => {
          if (item.id === currentItem.id) {
            return { 
              ...item, 
              status: 'completed', 
              progressMessage: 'Workbook ready with summary, profile, filters, and frozen data header.',
              metadata 
            };
          }
          return item;
        }));

      } catch (err: any) {
        console.error("Conversion issue with: ", currentItem.file.name, err);
        setQueue(prev => prev.map(item => {
          if (item.id === currentItem.id) {
            return { 
              ...item, 
              status: 'failed', 
              progressMessage: 'Failed to convert file structure.',
              error: err?.message || 'Malformed delimiter structure or unsupported header layout.'
            };
          }
          return item;
        }));
      }
    }

    setIsProcessing(false);
  };

  // Compile individual CSV to XLSX buffer on-demand to save active browser state RAM
  const handleDownloadSingleExcel = async (item: QueueItem) => {
    try {
      const result = await processCSVFile(item.file);
      const rawName = result.fileName;
      const baseName = rawName.replace(/\.[^/.]+$/, "");
      const outputName = `${baseName}.xlsx`;
      
      const blob = new Blob([result.xlsxBuffer], { 
        type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' 
      });
      const url = URL.createObjectURL(blob);
      
      const link = document.createElement('a');
      link.href = url;
      link.download = outputName;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    } catch (err: any) {
      console.error(err);
      alert(`On-demand Excel compile crashed: ${err?.message || "Verify file encoding."}`);
    }
  };

  // Compile ZIP sequentially on-the-fly to minimize transient memory peaks
  const downloadAllAsZip = async () => {
    setIsZipping(true);
    try {
      const zip = new JSZip();
      let addedAny = false;

      // Extract each file selectively, compile, and pack sequentially
      for (const item of queue) {
        if (item.status === 'completed') {
          try {
            const result = await processCSVFile(item.file);
            const originalName = item.file.name;
            const baseName = originalName.replace(/\.[^/.]+$/, "");
            const outputName = `${baseName}.xlsx`;
            
            // Append file buffer to ZIP
            zip.file(outputName, result.xlsxBuffer);
            addedAny = true;
          } catch (err) {
            console.error(`Unable to batch compile file: ${item.file.name}`, err);
          }
        }
      }

      if (!addedAny) {
        alert("No successfully compiled files to compress inside ZIP container.");
        return;
      }

      const content = await zip.generateAsync({ type: "blob" });
      const url = URL.createObjectURL(content);
      const link = document.createElement('a');
      link.href = url;
      link.download = `cleansed_trend_sheets_${Date.now()}.zip`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error("Unable to generate zip bundle", err);
      alert("Failed to build ZIP archive.");
    } finally {
      setIsZipping(false);
    }
  };

  const formatBytes = (bytes: number): string => {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
  };

  const getSeparatorLabel = (sep: string) => {
    switch(sep) {
      case ';': return 'Semicolon (;)';
      case ',': return 'Comma (,)';
      case '\t': return 'Tab (\\t)';
      case '|': return 'Pipe (|)';
      default: return `Custom (${sep})`;
    }
  };

  const processedPercent = useMemo(() => {
    if (totalFiles === 0) return 0;
    const processed = queue.filter(item => item.status === 'completed' || item.status === 'failed').length;
    return Math.round((processed / totalFiles) * 100);
  }, [queue, totalFiles]);

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 antialiased font-sans flex flex-col selection:bg-indigo-100" id="main_app_layout">
      
      {/* Engineering Header */}
      <header className="bg-white border-b border-slate-200 sticky top-0 z-50 shadow-xs animate-none" id="app_header">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-3.5 flex flex-col sm:flex-row items-center justify-between gap-4">
          
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 bg-slate-900 rounded-lg flex items-center justify-center text-white shadow-xs">
              <FileSpreadsheet className="w-5 h-5 text-emerald-400" />
            </div>
            <div>
              <h1 className="text-lg font-bold tracking-tight text-slate-900 flex items-center gap-2">
                CSV Trend & Log Converter
              </h1>
              <p className="text-xs text-slate-500 font-mono">BMS & SCADA Offline Data Cleanser • v2.5</p>
            </div>
          </div>
          
          {/* Engineering Indicator */}
          <div className="hidden md:flex items-center gap-3 bg-slate-50 px-3 py-1.5 rounded-lg border border-slate-200 text-xs text-slate-600 font-mono">
            <Activity className="w-3.5 h-3.5 text-emerald-500 animate-pulse" />
            <span>Sequential Safe Queue</span>
            <span className="text-slate-300">|</span>
            <span className="text-indigo-650 font-semibold">100% Client Isolation</span>
          </div>

        </div>
      </header>

      {/* Main Container */}
      <main className="flex-1 max-w-7xl mx-auto w-full px-4 sm:px-6 lg:px-8 py-8">
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-8" id="workspace_grid">
            
            {/* Left/Main Column: Ingestion and Queue */}
            <div className="lg:col-span-8 flex flex-col gap-6">
              
              {/* Refined Drag & Drop Upload Zone */}
              <div 
                onDragEnter={handleDrag}
                onDragOver={handleDrag}
                onDragLeave={handleDrag}
                onDrop={handleDrop}
                onClick={() => fileInputRef.current?.click()}
                className={`bg-white border-2 border-dashed rounded-2xl p-8 flex flex-col items-center justify-center text-center cursor-pointer transition-all relative overflow-hidden group shadow-xs ${
                  dragActive 
                    ? 'border-indigo-600 bg-indigo-50/50' 
                    : 'border-slate-300 hover:border-slate-400 hover:bg-slate-50/50'
                }`}
                id="csv_dropzone"
              >
                <input 
                  ref={fileInputRef}
                  type="file" 
                  multiple 
                  accept=".csv" 
                  onChange={handleFileChange} 
                  className="hidden" 
                />
                
                <div className="w-12 h-12 rounded-xl mb-3 flex items-center justify-center bg-slate-50 border border-slate-200 group-hover:scale-105 transition-transform">
                  <FileUp className="w-6 h-6 text-slate-600" />
                </div>
                
                <h2 className="text-base font-bold text-slate-900 mb-1">
                  {dragActive ? "Drop files to begin raw conversion" : "Drag & drop CSV files here"}
                </h2>
                <p className="text-slate-500 text-xs max-w-lg mb-4 leading-relaxed">
                  Supports telemetry arrays, CSV data logs (BACnet, SCADA, Metasys, Niagara HVAC outputs), and sensor data sheets. Fast, local in-memory conversions.
                </p>
                
                <button className="px-5 py-2 bg-slate-900 hover:bg-slate-800 text-white font-semibold text-xs rounded-lg shadow-sm transition-colors cursor-pointer">
                  Select CSV Files
                </button>
                
                {/* Security and capability disclaimer badges */}
                <div className="mt-6 pt-4 border-t border-slate-100 hidden sm:flex items-center gap-6 justify-center text-[10px] text-slate-400 uppercase tracking-wider font-mono">
                  <span className="flex items-center gap-1">
                    <Check className="w-3 h-3 text-emerald-500" /> Auto-Separator
                  </span>
                  <span>•</span>
                  <span className="flex items-center gap-1">
                    <Check className="w-3 h-3 text-emerald-500" /> Frozen Headers
                  </span>
                  <span>•</span>
                  <span className="flex items-center gap-1">
                    <Check className="w-3 h-3 text-emerald-500" /> Summary Sheets
                  </span>
                </div>
              </div>

              {/* Progress Indicator */}
              {isProcessing && (
                <div className="bg-white border border-slate-200 rounded-xl p-4 shadow-xs" id="running_progress_indicator">
                  <div className="flex items-center justify-between text-xs font-mono font-medium text-slate-700 mb-2">
                    <span className="flex items-center gap-1.5">
                      <RefreshCw className="w-3.5 h-3.5 text-indigo-600 animate-spin" />
                      Converting CSV Sequential Queue...
                    </span>
                    <span>{processedPercent}% completed</span>
                  </div>
                  <div className="bg-slate-100 h-2 w-full rounded-full overflow-hidden">
                    <div 
                      className="h-full bg-indigo-600 rounded-full transition-all duration-300"
                      style={{ width: `${processedPercent}%` }}
                    />
                  </div>
                </div>
              )}

              {/* Upload Queue list Panel */}
              {queue.length > 0 && (
                <div className="bg-white border border-slate-200 rounded-2xl overflow-hidden shadow-xs flex flex-col" id="queue_management_panel">
                  <div className="px-5 py-3.5 border-b border-slate-200 flex flex-col sm:flex-row items-center justify-between gap-3 bg-slate-50/50">
                    <div>
                      <h2 className="text-xs font-bold text-slate-800 flex items-center gap-1.5 uppercase font-mono tracking-wider">
                        Loaded Spreadsheet Queue ({queue.length})
                      </h2>
                      <p className="text-[11px] text-slate-500 font-mono mt-0.5">Total raw weight in session handle: {formatBytes(totalInputSize)} • Sequenced one-by-one</p>
                    </div>

                    {/* Operational controls */}
                    <div className="flex items-center gap-2">
                      <button
                        onClick={clearQueue}
                        disabled={isProcessing}
                        className="px-3 py-1.5 text-xs font-semibold text-slate-650 bg-white border border-slate-200 hover:border-red-300 hover:text-red-650 hover:bg-red-50 rounded-lg disabled:opacity-40 transition-colors flex items-center gap-1 cursor-pointer"
                        id="clear_queue_button"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                        Clear Queue
                      </button>
                      
                      {!isAllDone ? (
                        <button
                          onClick={processQueue}
                          disabled={isProcessing}
                          className="px-4 py-1.5 text-xs font-bold text-white bg-indigo-600 border border-indigo-500 hover:bg-indigo-700 rounded-lg disabled:opacity-50 shadow-xs transition-colors flex items-center gap-1 cursor-pointer"
                          id="convert_queue_button"
                        >
                          {isProcessing ? (
                            <>
                              <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                              Compiling...
                            </>
                          ) : (
                            <>
                              <RefreshCw className="w-3.5 h-3.5" />
                              Convert to Styled Excel (.xlsx)
                            </>
                          )}
                        </button>
                      ) : (
                        <button
                          onClick={downloadAllAsZip}
                          disabled={successCount === 0 || isZipping}
                          className="px-4 py-1.5 text-xs font-bold text-white bg-emerald-600 hover:bg-emerald-700 border border-emerald-500 rounded-lg shadow-xs transition-colors flex items-center gap-1 cursor-pointer"
                          id="download_zip_button"
                        >
                          {isZipping ? (
                            <>
                              <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                              Packaging ZIP...
                            </>
                          ) : (
                            <>
                              <Download className="w-3.5 h-3.5" />
                              Download Styled ZIP ({successCount})
                            </>
                          )}
                        </button>
                      )}
                    </div>
                  </div>

                  {/* Batch Details list */}
                  <div className="divide-y divide-slate-100 max-h-[500px] overflow-y-auto" id="queue_items_list">
                    {queue.map((item) => (
                      <div
                        key={item.id}
                        className="p-4 sm:p-5 flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4 hover:bg-slate-50/60 transition-all text-slate-800"
                      >
                        <div className="flex items-start gap-3 min-w-0 flex-1">
                          
                          {/* File status column */}
                          <div className="mt-0.5 flex-shrink-0">
                            {item.status === 'waiting' && (
                              <span className="inline-flex items-center justify-center px-2 py-0.5 text-[10px] font-bold font-mono uppercase rounded bg-slate-150 text-slate-700 border border-slate-300">
                                Waiting
                              </span>
                            )}
                            {item.status === 'processing' && (
                              <span className="inline-flex items-center justify-center px-2 py-0.5 text-[10px] font-bold font-mono uppercase rounded bg-blue-100 text-blue-800 border border-blue-200 animate-pulse">
                                Processing
                              </span>
                            )}
                            {item.status === 'completed' && (
                              <span className="inline-flex items-center justify-center px-2 py-0.5 text-[10px] font-bold font-mono uppercase rounded bg-emerald-100 text-emerald-800 border border-emerald-200">
                                Cleaned
                              </span>
                            )}
                            {item.status === 'failed' && (
                              <span className="inline-flex items-center justify-center px-2 py-0.5 text-[10px] font-bold font-mono uppercase rounded bg-rose-100 text-rose-800 border border-rose-200">
                                Failed
                              </span>
                            )}
                          </div>

                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2 flex-wrap">
                              <span className="text-sm font-semibold text-slate-900 break-all">
                                {item.file.name}
                              </span>
                              <span className="text-xs text-slate-400 font-mono">
                                ({formatBytes(item.file.size)})
                              </span>
                            </div>
                            
                            <p className="text-xs text-slate-500 mt-1">
                              {item.progressMessage}
                            </p>

                            {/* Detailed diagnostics for BMS trends / CSV measurements */}
                            {item.status === 'completed' && item.metadata && (
                              <div className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-2.5 text-[11px] font-mono bg-slate-50 p-2.5 rounded-lg border border-slate-200 text-slate-650">
                                <div>
                                  <span className="text-slate-400 block uppercase text-[9px] font-bold">Separator:</span>
                                  <span className="text-slate-900 font-semibold">{getSeparatorLabel(item.metadata.separator)}</span>
                                </div>
                                <div>
                                  <span className="text-slate-400 block uppercase text-[9px] font-bold">Total Columns:</span>
                                  <span className="text-slate-900 font-semibold">{item.metadata.colCount} active</span>
                                </div>
                                <div>
                                  <span className="text-slate-400 block uppercase text-[9px] font-bold">Processed Rows:</span>
                                  <span className="text-slate-900 font-semibold">{item.metadata.rowCount} rows</span>
                                </div>
                                <div>
                                  <span className="text-slate-400 block uppercase text-[9px] font-bold">Pruned Metrics:</span>
                                  <span className="text-amber-700 font-semibold">
                                    -{item.metadata.removedEmptyRows}R / -{item.metadata.removedEmptyCols}C
                                  </span>
                                </div>
                                {(item.metadata.detectedNumericColsCount > 0 || item.metadata.detectedDateColsCount > 0) && (
                                  <div className="col-span-2 sm:col-span-4 border-t border-slate-100 pt-2 mt-1 flex gap-4 text-emerald-750">
                                    <div>
                                      <span className="text-slate-400 block uppercase text-[9px] font-bold">Smart Types Cast:</span>
                                      <span className="font-semibold">
                                        {item.metadata.detectedNumericColsCount} numeric variables • {item.metadata.detectedDateColsCount} timestamps parsed
                                      </span>
                                    </div>
                                  </div>
                                )}
                              </div>
                            )}

                            {/* Diagnostic Error Banner with tips */}
                            {item.status === 'failed' && item.error && (
                              <div className="mt-3 p-3 bg-red-50 border border-red-200 rounded-lg text-xs" id="fail_alert_block">
                                <span className="font-semibold text-red-800 flex items-center gap-1.5">
                                  <ShieldAlert className="w-4 h-4 text-rose-600" />
                                  Crucial Parse Error:
                                </span>
                                <span className="text-red-700 font-mono mt-0.5 block">{item.error}</span>
                                <div className="mt-2 text-slate-600 text-[11px] leading-relaxed">
                                  <strong>Recommended Solution:</strong> Open the file in Notepad to check if it contains blank records, binary raw data, or has non-ASCII characters. Check if it is open in another program.
                                </div>
                              </div>
                            )}
                          </div>
                        </div>

                        {/* Individual Item Download/Trash Controls */}
                        <div className="flex items-center gap-1.5 self-end sm:self-center pl-10 sm:pl-0 flex-shrink-0">
                          {item.status === 'completed' && (
                            <button
                              onClick={() => handleDownloadSingleExcel(item)}
                              className="px-2.5 py-1.5 text-xs font-semibold text-indigo-700 bg-indigo-50 border border-indigo-200 hover:bg-indigo-100 rounded-lg transition-colors flex items-center gap-1 cursor-pointer"
                              title="Download single .xlsx"
                            >
                              <Download className="w-3.5 h-3.5" />
                              <span>Workbook</span>
                            </button>
                          )}
                          <button
                            onClick={() => removeItem(item.id)}
                            disabled={isProcessing}
                            className="p-1.5 text-slate-400 hover:text-red-650 hover:bg-red-50 rounded-lg disabled:opacity-30 transition-colors cursor-pointer"
                            title="Remove file"
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {/* Right Column: Summaries & Dynamic Statistics */}
            <div className="lg:col-span-4 flex flex-col gap-6">
              
              {/* Engineering Batch Stats Card */}
              <div className="bg-white border border-slate-200 p-5 rounded-2xl shadow-xs" id="summary_stats_card">
                <h3 className="text-xs font-bold text-slate-800 uppercase tracking-wider font-mono border-b border-slate-100 pb-3 flex items-center gap-2">
                  <FileCheck2 className="w-4 h-4 text-emerald-600" />
                  Conversion Summary
                </h3>

                {totalFiles === 0 ? (
                  <div className="py-8 text-center">
                    <p className="text-xs text-slate-400">No telemetry files uploaded in memory queue.</p>
                    <p className="text-[11px] text-slate-400 mt-1 font-mono">Drag and drop raw trends to analyze.</p>
                  </div>
                ) : (
                  <div className="mt-4 flex flex-col gap-4">
                    {/* Simple badge values */}
                    <div className="grid grid-cols-2 gap-3.5 font-mono text-center">
                      <div className="bg-slate-100 border border-slate-200 p-2.5 rounded-xl">
                        <span className="text-[9px] font-bold text-slate-400 uppercase block">Total Files</span>
                        <span className="text-lg font-bold text-slate-900 block mt-0.5">{totalFiles}</span>
                      </div>
                      <div className="bg-emerald-50/50 border border-emerald-200 p-2.5 rounded-xl">
                        <span className="text-[9px] font-bold text-emerald-600 uppercase block">Ready Out</span>
                        <span className="text-lg font-bold text-emerald-700 block mt-0.5">{successCount}</span>
                      </div>
                    </div>

                    {/* Sequential Progress overview */}
                    {isAllDone && (
                      <div className="border-t border-slate-100 pt-4 mt-1">
                        <h4 className="text-[11px] font-bold text-slate-700 uppercase tracking-wide font-mono mb-2">
                          Aggregated Diagnostics
                        </h4>
                        
                        <div className="space-y-2 text-xs font-mono">
                          <div className="flex items-center justify-between py-1 border-b border-slate-100">
                            <span className="text-slate-500">Processed records:</span>
                            <span className="font-bold text-slate-800">{aggregateMetrics.totalRows.toLocaleString()} rows</span>
                          </div>
                          <div className="flex items-center justify-between py-1 border-b border-slate-100">
                            <span className="text-slate-650">Scrubbed variables:</span>
                            <span className="font-bold text-slate-800">{aggregateMetrics.totalCols.toLocaleString()} metrics</span>
                          </div>
                          <div className="flex items-center justify-between py-1 border-b border-slate-100">
                            <span className="text-slate-500 font-medium">Deleted blank rows:</span>
                            <span className="font-semibold text-amber-650 bg-amber-50 px-1 py-0.5 rounded">+{aggregateMetrics.removedRows}</span>
                          </div>
                          <div className="flex items-center justify-between py-1 border-b border-slate-100">
                            <span className="text-slate-500 font-medium">Deleted empty cols:</span>
                            <span className="font-semibold text-amber-650 bg-amber-50 px-1 py-0.5 rounded">+{aggregateMetrics.removedCols}</span>
                          </div>
                          <div className="flex items-center justify-between py-1 border-b border-slate-100">
                            <span className="text-slate-500">Casted datatypes:</span>
                            <span className="font-semibold text-indigo-600">
                              {aggregateMetrics.dateCols + aggregateMetrics.numericCols} columns type-safe
                            </span>
                          </div>
                        </div>

                        {successCount > 0 && (
                          <button
                            onClick={downloadAllAsZip}
                            disabled={isZipping}
                            className="w-full mt-4 bg-emerald-650 hover:bg-emerald-700 bg-emerald-600 text-white font-bold text-xs py-2.5 px-4 rounded-xl shadow-xs transition-colors flex items-center justify-center gap-1.5 cursor-pointer"
                            id="download_zip_widget_button"
                          >
                            <Download className="w-4 h-4" />
                            <span>Download Styled Workbook Pack</span>
                          </button>
                        )}
                      </div>
                    )}
                    
                    {failedCount > 0 && (
                      <div className="p-3 bg-rose-50 border border-rose-200 rounded-lg text-xs text-rose-800 font-mono mt-1 flex items-start gap-2">
                        <AlertCircle className="w-4.5 h-4.5 text-rose-500 mt-0.5 flex-shrink-0" />
                        <div>
                          <strong>{failedCount} log file(s) failed layout conversion.</strong> The processor bypassed those to secure compiles for the remainder of your CSV batch.
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>

              {/* BACnet/CSV Quick Reference Card */}
              <div className="bg-white border border-slate-200 p-5 rounded-2xl shadow-xs">
                <h3 className="text-xs font-bold text-slate-800 uppercase tracking-wider font-mono border-b border-slate-100 pb-3 flex items-center gap-2">
                  <Activity className="w-4 h-4 text-slate-650" />
                  BMS Trend Formats Tested
                </h3>
                <div className="mt-3.5 space-y-3 text-xs leading-relaxed text-slate-600">
                  <p>
                    This offline processing system accepts raw telemetry CSV logs exported from prominent building management services:
                  </p>
                  <ul className="space-y-2 text-[11px] font-mono text-slate-700">
                    <li className="flex items-start gap-1.5">
                      <span className="text-indigo-650 font-bold">•</span>
                      <span><strong>Metasys exports:</strong> Cleaves boilerplate headings, mapping tabular arrays in clean metrics.</span>
                    </li>
                    <li className="flex items-start gap-1.5">
                      <span className="text-indigo-650 font-bold">•</span>
                      <span><strong>Tridium Niagara Logs:</strong> Automatically prunes trailing commas and parses decimals.</span>
                    </li>
                    <li className="flex items-start gap-1.5">
                      <span className="text-indigo-650 font-bold">•</span>
                      <span><strong>Desigo CC Dumps:</strong> Accurately processes semicolon separated rows without dropping quotes.</span>
                    </li>
                  </ul>
                </div>
              </div>

              {/* Offline-First privacy assurance card */}
              <div className="bg-indigo-55/40 bg-indigo-50 border border-indigo-150 p-4.5 rounded-2xl text-xs text-indigo-900 flex items-start gap-3">
                <Info className="w-5 h-5 text-indigo-500 flex-shrink-0 mt-0.5" />
                <div>
                  <h4 className="font-bold text-indigo-950">Local Memory Isolation</h4>
                  <p className="mt-1 leading-relaxed text-indigo-850">
                    Excel workbooks are parsed and compiled inside your browser. Each output includes a clean data sheet, summary sheet, column profile, and chart-ready ranges without uploading files to a server.
                  </p>
                </div>
              </div>

            </div>
        </div>

      </main>

      {/* Styled Footer */}
      <footer className="bg-white border-t border-slate-200 py-6 mt-12 text-center text-xs text-slate-500 font-mono">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 flex flex-col sm:flex-row items-center justify-between gap-4">
          <span id="footer_credit" className="text-slate-800 font-bold">CSV Trend & Log Converter</span>
          <span className="flex items-center gap-1 text-slate-400 font-normal">
            Secure client-side spreadsheet compilation workspace • Zero log footprint
          </span>
        </div>
      </footer>
    </div>
  );
}
