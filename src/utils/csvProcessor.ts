/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import JSZip from 'jszip';
import XLSX from 'xlsx-js-style';

export interface ProcessingResult {
  fileName: string;
  originalSize: number;
  xlsxBuffer: Uint8Array;
  rowCount: number;
  colCount: number;
  separator: string;
  cleanedHeaders: string[];
  removedEmptyRows: number;
  removedEmptyCols: number;
  detectedNumericColsCount: number;
  detectedDateColsCount: number;
}

type ColumnKind = 'date' | 'number' | 'boolean' | 'text';

interface ColumnProfile {
  header: string;
  type: ColumnKind;
  nonEmptyCount: number;
  blankCount: number;
  sampleValue: string;
  min?: number | Date;
  max?: number | Date;
  average?: number;
}

const HEADER_STYLE = {
  font: { bold: true, color: { rgb: 'FFFFFF' } },
  fill: { fgColor: { rgb: '1F2937' } },
  alignment: { horizontal: 'center', vertical: 'center', wrapText: true },
  border: {
    top: { style: 'thin', color: { rgb: 'CBD5E1' } },
    bottom: { style: 'thin', color: { rgb: 'CBD5E1' } },
    left: { style: 'thin', color: { rgb: 'CBD5E1' } },
    right: { style: 'thin', color: { rgb: 'CBD5E1' } }
  }
};

const SUBHEADER_STYLE = {
  font: { bold: true, color: { rgb: '0F172A' } },
  fill: { fgColor: { rgb: 'E2E8F0' } },
  alignment: { horizontal: 'center', vertical: 'center', wrapText: true },
  border: {
    bottom: { style: 'thin', color: { rgb: 'CBD5E1' } }
  }
};

const KPI_LABEL_STYLE = {
  font: { bold: true, color: { rgb: '475569' } },
  fill: { fgColor: { rgb: 'F8FAFC' } },
  alignment: { vertical: 'center', wrapText: true }
};

const KPI_VALUE_STYLE = {
  font: { bold: true, color: { rgb: '0F172A' } },
  alignment: { vertical: 'center' },
  border: {
    bottom: { style: 'thin', color: { rgb: 'E2E8F0' } }
  }
};

// Detect CSV separator safely based on character frequencies outside double quotes
export function detectSeparator(text: string): string {
  const potentialSeparators = [';', ',', '\t', '|'];
  
  // Analyze up to first 20 non-empty lines
  const lines = text.split(/\r?\n/)
    .map(line => line.trim())
    .filter(line => line.length > 0)
    .slice(0, 20);
    
  if (lines.length === 0) return ',';

  const sepScores: Record<string, number> = { ';': 0, ',': 0, '\t': 0, '|': 0 };

  for (const sep of potentialSeparators) {
    const counts: number[] = [];
    for (const line of lines) {
      let insideQuote = false;
      let sepCount = 0;
      for (let i = 0; i < line.length; i++) {
        const char = line[i];
        if (char === '"') {
          if (insideQuote && line[i + 1] === '"') {
            i++; // skip escaped quote
          } else {
            insideQuote = !insideQuote;
          }
        } else if (char === sep && !insideQuote) {
          sepCount++;
        }
      }
      counts.push(sepCount);
    }
    
    const total = counts.reduce((sum, val) => sum + val, 0);
    if (total === 0) continue;

    const avg = total / counts.length;
    // Calculate variance to check consistency of column occurrences across lines
    const variance = counts.reduce((sum, val) => sum + Math.pow(val - avg, 2), 0) / counts.length;
    
    // Separators with persistent column counts (low variance) and higher frequency are favored.
    // Score = total / (1 + variance)
    const score = total / (1 + variance);
    sepScores[sep] = score;
  }

  let bestSep = ',';
  let maxScore = -1;
  for (const [sep, score] of Object.entries(sepScores)) {
    if (score > maxScore) {
      maxScore = score;
      bestSep = sep;
    }
  }

  return bestSep;
}

// Parse CSV content handling quoted cells, escaped quotes, and newlines inside quotes
export function parseCSV(text: string, sep: string): string[][] {
  const result: string[][] = [];
  let row: string[] = [];
  let cell = '';
  let insideQuote = false;

  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    const nextChar = text[i + 1];

    if (insideQuote) {
      if (char === '"') {
        if (nextChar === '"') {
          cell += '"';
          i++; // skip escaped quote
        } else {
          insideQuote = false; // end quote
        }
      } else {
        cell += char;
      }
    } else {
      if (char === '"') {
        insideQuote = true; // start quote
      } else if (char === sep) {
        row.push(cell);
        cell = '';
      } else if (char === '\r' || char === '\n') {
        row.push(cell);
        cell = '';
        if (row.length > 0) {
          result.push(row);
        }
        row = [];
        if (char === '\r' && nextChar === '\n') {
          i++; // Skip \n in CRLF
        }
      } else {
        cell += char;
      }
    }
  }

  // Push remainder
  if (cell !== '' || row.length > 0) {
    row.push(cell);
    result.push(row);
  }

  return result;
}

// ISO format or common divider formats (YYYY/MM/DD, DD-MM-YYYY, etc.)
const ISO_DATE_REGEX = /^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}(?::?\d{2})?)?)?$/;
const SLASH_DATE_REGEX_1 = /^\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[aApP][mM])?)?$/;
const SLASH_DATE_REGEX_2 = /^\d{4}[\/\-]\d{1,2}[\/\-]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[aApP][mM])?)?$/;

export function tryParseDate(val: string): Date | null {
  const s = val.trim();
  if (!s) return null;
  if (/^\d+$/.test(s)) return null; // Ignore pure numbers that parse as timestamps

  if (ISO_DATE_REGEX.test(s) || SLASH_DATE_REGEX_1.test(s) || SLASH_DATE_REGEX_2.test(s)) {
    const t = Date.parse(s);
    if (!isNaN(t)) {
      const d = new Date(t);
      const yr = d.getFullYear();
      if (yr >= 1900 && yr <= 2100) return d;
    }
  }
  return null;
}

export function tryParseNumber(val: string): number | null {
  const s = val.trim();
  if (!s) return null;
  
  // Normalize formatted numbers (removing standard thousands-separator commas)
  const cleanStr = s.replace(/,/g, '');
  if (/^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(cleanStr)) {
    const num = Number(cleanStr);
    if (!isNaN(num)) return num;
  }
  return null;
}

function formatProfileValue(value: number | Date | undefined): string | number | null {
  if (value === undefined) return null;
  if (value instanceof Date) return value.toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, ' UTC');
  return Number(value.toFixed(4));
}

function safeSheetName(name: string): string {
  return name.replace(/[\[\]\*\/\\\?:]/g, ' ').trim().slice(0, 31) || 'Sheet';
}

function applyHeaderStyle(ws: XLSX.WorkSheet, colCount: number, row = 0) {
  for (let col = 0; col < colCount; col++) {
    const ref = XLSX.utils.encode_cell({ r: row, c: col });
    if (ws[ref]) ws[ref].s = HEADER_STYLE;
  }
}

function applySubHeaderStyle(ws: XLSX.WorkSheet, colCount: number, row = 0) {
  for (let col = 0; col < colCount; col++) {
    const ref = XLSX.utils.encode_cell({ r: row, c: col });
    if (ws[ref]) ws[ref].s = SUBHEADER_STYLE;
  }
}

function applyZebraRows(ws: XLSX.WorkSheet, rowCount: number, colCount: number) {
  for (let row = 1; row <= rowCount; row++) {
    const fillColor = row % 2 === 0 ? 'F8FAFC' : 'FFFFFF';
    for (let col = 0; col < colCount; col++) {
      const ref = XLSX.utils.encode_cell({ r: row, c: col });
      if (!ws[ref]) continue;
      ws[ref].s = {
        ...(ws[ref].s || {}),
        fill: { fgColor: { rgb: fillColor } },
        alignment: { vertical: 'top', wrapText: false },
        border: {
          bottom: { style: 'hair', color: { rgb: 'E2E8F0' } }
        }
      };
    }
  }
}

function applyDataFormats(ws: XLSX.WorkSheet, rowCount: number, columnTypes: ColumnKind[]) {
  for (let row = 1; row <= rowCount; row++) {
    for (let col = 0; col < columnTypes.length; col++) {
      const ref = XLSX.utils.encode_cell({ r: row, c: col });
      if (!ws[ref]) continue;
      if (columnTypes[col] === 'date') {
        ws[ref].z = 'yyyy-mm-dd hh:mm';
      } else if (columnTypes[col] === 'number') {
        ws[ref].z = '#,##0.00';
      }
    }
  }
}

function setWorkbookMetadata(wb: XLSX.WorkBook, fileName: string) {
  wb.Props = {
    Title: `${fileName} converted workbook`,
    Subject: 'Clean CSV conversion with summary profile',
    Author: 'CSV Fast View Converter',
    CreatedDate: new Date()
  };
}

function makeSummarySheet(
  file: File,
  separator: string,
  rowCount: number,
  colCount: number,
  removedEmptyRows: number,
  removedEmptyCols: number,
  detectedNumericColsCount: number,
  detectedDateColsCount: number,
  profiles: ColumnProfile[]
): XLSX.WorkSheet {
  const kindCounts = profiles.reduce<Record<ColumnKind, number>>((acc, profile) => {
    acc[profile.type]++;
    return acc;
  }, { date: 0, number: 0, boolean: 0, text: 0 });

  const summaryRows: any[][] = [
    ['CSV Conversion Summary', null, null, null, null, null],
    ['File name', file.name, 'Original size', file.size, 'Generated at', new Date()],
    ['Processed rows', rowCount, 'Active columns', colCount, 'Separator', separator === '\t' ? 'Tab' : separator],
    ['Numeric columns', detectedNumericColsCount, 'Date columns', detectedDateColsCount, 'Boolean columns', kindCounts.boolean],
    ['Blank rows removed', removedEmptyRows, 'Blank columns removed', removedEmptyCols, 'Text columns', kindCounts.text],
    [],
    ['Workbook Guide', 'Purpose'],
    ['Data', 'Cleaned CSV data with wrapped headers, filters, adjusted widths, and frozen header row.'],
    ['Column Profile', 'Per-column completeness, inferred type, examples, and numeric/date summary ranges.'],
    ['Chart Data', 'Chart-ready tables for column completeness and numeric trend preview.'],
    [],
    ['Data Quality Snapshot', 'Value'],
    ['Total non-empty fields', profiles.reduce((sum, p) => sum + p.nonEmptyCount, 0)],
    ['Total blank fields', profiles.reduce((sum, p) => sum + p.blankCount, 0)],
    ['Columns with no blank values', profiles.filter(p => p.blankCount === 0).length],
    ['Columns with blanks', profiles.filter(p => p.blankCount > 0).length]
  ];

  const ws = XLSX.utils.aoa_to_sheet(summaryRows, { cellDates: true });
  ws['!merges'] = [{ s: { r: 0, c: 0 }, e: { r: 0, c: 5 } }];
  ws['!cols'] = [
    { wch: 24 },
    { wch: 34 },
    { wch: 20 },
    { wch: 16 },
    { wch: 20 },
    { wch: 24 }
  ];
  ws['!rows'] = [{ hpt: 28 }, { hpt: 22 }, { hpt: 22 }, { hpt: 22 }, { hpt: 22 }];

  const title = ws.A1;
  if (title) {
    title.s = {
      font: { bold: true, sz: 16, color: { rgb: 'FFFFFF' } },
      fill: { fgColor: { rgb: '0F172A' } },
      alignment: { horizontal: 'center', vertical: 'center' }
    };
  }

  [1, 2, 3, 4, 6, 11].forEach(row => {
    for (let col = 0; col < 6; col += 2) {
      const labelRef = XLSX.utils.encode_cell({ r: row, c: col });
      const valueRef = XLSX.utils.encode_cell({ r: row, c: col + 1 });
      if (ws[labelRef]) ws[labelRef].s = KPI_LABEL_STYLE;
      if (ws[valueRef]) ws[valueRef].s = KPI_VALUE_STYLE;
    }
  });

  for (let row = 7; row <= 9; row++) {
    const keyRef = XLSX.utils.encode_cell({ r: row, c: 0 });
    const valueRef = XLSX.utils.encode_cell({ r: row, c: 1 });
    if (ws[keyRef]) ws[keyRef].s = KPI_LABEL_STYLE;
    if (ws[valueRef]) {
      ws[valueRef].s = {
        alignment: { vertical: 'top', wrapText: true },
        border: { bottom: { style: 'hair', color: { rgb: 'E2E8F0' } } }
      };
    }
  }

  for (let row = 12; row <= 15; row++) {
    const keyRef = XLSX.utils.encode_cell({ r: row, c: 0 });
    const valueRef = XLSX.utils.encode_cell({ r: row, c: 1 });
    if (ws[keyRef]) ws[keyRef].s = KPI_LABEL_STYLE;
    if (ws[valueRef]) ws[valueRef].s = KPI_VALUE_STYLE;
  }

  if (ws.F2) ws.F2.z = 'yyyy-mm-dd hh:mm';
  return ws;
}

function makeColumnProfileSheet(profiles: ColumnProfile[]): XLSX.WorkSheet {
  const rows: any[][] = [
    ['Column', 'Detected Type', 'Non-empty Cells', 'Blank Cells', 'Completeness %', 'Sample Value', 'Min', 'Max', 'Average']
  ];

  profiles.forEach(profile => {
    const total = profile.nonEmptyCount + profile.blankCount;
    rows.push([
      profile.header,
      profile.type,
      profile.nonEmptyCount,
      profile.blankCount,
      total === 0 ? 0 : profile.nonEmptyCount / total,
      profile.sampleValue,
      formatProfileValue(profile.min),
      formatProfileValue(profile.max),
      formatProfileValue(profile.average)
    ]);
  });

  const ws = XLSX.utils.aoa_to_sheet(rows);
  ws['!cols'] = [
    { wch: 28 },
    { wch: 15 },
    { wch: 16 },
    { wch: 12 },
    { wch: 15 },
    { wch: 30 },
    { wch: 18 },
    { wch: 18 },
    { wch: 14 }
  ];
  ws['!rows'] = [{ hpt: 34 }];
  ws['!autofilter'] = { ref: XLSX.utils.encode_range({ s: { r: 0, c: 0 }, e: { r: Math.max(profiles.length, 1), c: 8 } }) };
  applyHeaderStyle(ws, 9);

  for (let row = 1; row <= profiles.length; row++) {
    const completenessRef = XLSX.utils.encode_cell({ r: row, c: 4 });
    if (ws[completenessRef]) ws[completenessRef].z = '0.0%';
    for (let col = 0; col < 9; col++) {
      const ref = XLSX.utils.encode_cell({ r: row, c: col });
      if (!ws[ref]) continue;
      ws[ref].s = {
        fill: { fgColor: { rgb: row % 2 === 0 ? 'F8FAFC' : 'FFFFFF' } },
        alignment: { vertical: 'top', wrapText: col === 0 || col === 5 },
        border: { bottom: { style: 'hair', color: { rgb: 'E2E8F0' } } }
      };
    }
  }

  return ws;
}

function makeChartDataSheet(
  cleanDataRows: string[][],
  finalDataRows: any[][],
  headers: string[],
  profiles: ColumnProfile[],
  columnTypes: ColumnKind[]
): XLSX.WorkSheet {
  const numericColumns = columnTypes
    .map((type, index) => ({ type, index }))
    .filter(item => item.type === 'number')
    .slice(0, 3);
  const dateColIndex = columnTypes.findIndex(type => type === 'date');

  const completenessRows: any[][] = [
    ['Column Completeness', null, null, null],
    ['Column', 'Non-empty Cells', 'Blank Cells', 'Completeness %'],
    ...profiles.map(profile => {
      const total = profile.nonEmptyCount + profile.blankCount;
      return [
        profile.header,
        profile.nonEmptyCount,
        profile.blankCount,
        total === 0 ? 0 : profile.nonEmptyCount / total
      ];
    })
  ];

  const trendRows: any[][] = [
    [],
    ['Numeric Trend Preview', null, null, null],
    [
      dateColIndex >= 0 ? headers[dateColIndex] : 'Row Number',
      ...numericColumns.map(col => headers[col.index])
    ]
  ];

  const previewRows = Math.min(finalDataRows.length, 250);
  for (let row = 0; row < previewRows; row++) {
    trendRows.push([
      dateColIndex >= 0 ? finalDataRows[row][dateColIndex] : row + 1,
      ...numericColumns.map(col => finalDataRows[row][col.index])
    ]);
  }

  const sourceNoteRows: any[][] = [
    [],
    ['Chart Notes'],
    ['This app generates chart-ready ranges. Create Excel charts from the Column Completeness or Numeric Trend Preview tables if needed.'],
    [`Preview is capped at ${previewRows} rows to keep browser-side workbook generation fast.`],
    [`Source data rows inspected: ${cleanDataRows.length}`]
  ];

  const rows = [...completenessRows, ...trendRows, ...sourceNoteRows];
  const ws = XLSX.utils.aoa_to_sheet(rows, { cellDates: true });
  ws['!merges'] = [
    { s: { r: 0, c: 0 }, e: { r: 0, c: 3 } },
    { s: { r: profiles.length + 4, c: 0 }, e: { r: profiles.length + 4, c: Math.max(numericColumns.length, 1) } }
  ];
  ws['!cols'] = [
    { wch: 28 },
    { wch: 18 },
    { wch: 14 },
    { wch: 16 },
    { wch: 16 }
  ];

  if (ws.A1) ws.A1.s = { font: { bold: true, sz: 14, color: { rgb: 'FFFFFF' } }, fill: { fgColor: { rgb: '0F172A' } }, alignment: { horizontal: 'center' } };
  applySubHeaderStyle(ws, 4, 1);
  const trendTitleRow = profiles.length + 4;
  const trendHeaderRow = profiles.length + 5;
  const trendTitleRef = XLSX.utils.encode_cell({ r: trendTitleRow, c: 0 });
  if (ws[trendTitleRef]) ws[trendTitleRef].s = { font: { bold: true, sz: 14, color: { rgb: 'FFFFFF' } }, fill: { fgColor: { rgb: '0F172A' } }, alignment: { horizontal: 'center' } };
  applySubHeaderStyle(ws, Math.max(numericColumns.length + 1, 2), trendHeaderRow);

  for (let row = 2; row < profiles.length + 2; row++) {
    const pctRef = XLSX.utils.encode_cell({ r: row, c: 3 });
    if (ws[pctRef]) ws[pctRef].z = '0.0%';
  }

  for (let row = trendHeaderRow + 1; row <= trendHeaderRow + previewRows; row++) {
    if (dateColIndex >= 0) {
      const dateRef = XLSX.utils.encode_cell({ r: row, c: 0 });
      if (ws[dateRef]) ws[dateRef].z = 'yyyy-mm-dd hh:mm';
    }
    for (let col = 1; col <= numericColumns.length; col++) {
      const ref = XLSX.utils.encode_cell({ r: row, c: col });
      if (ws[ref]) ws[ref].z = '#,##0.00';
    }
  }

  return ws;
}

async function addFrozenHeaderPane(xlsxBuffer: Uint8Array, sheetPath = 'xl/worksheets/sheet2.xml'): Promise<Uint8Array> {
  const zip = await JSZip.loadAsync(xlsxBuffer);
  const sheet = zip.file(sheetPath);
  if (!sheet) return xlsxBuffer;

  let xml = await sheet.async('string');
  const frozenView = '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/><selection pane="bottomLeft" activeCell="A2" sqref="A2"/></sheetView></sheetViews>';

  if (xml.includes('<sheetViews>')) {
    xml = xml.replace(/<sheetViews>[\s\S]*?<\/sheetViews>/, frozenView);
  } else if (xml.includes('<sheetFormatPr')) {
    xml = xml.replace(/(<sheetFormatPr[^>]*\/>)/, `$1${frozenView}`);
  } else {
    xml = xml.replace(/(<worksheet[^>]*>)/, `$1${frozenView}`);
  }

  zip.file(sheetPath, xml);
  const updated = await zip.generateAsync({ type: 'uint8array' });
  return updated;
}

// Master CSV Process Function
export async function processCSVFile(file: File): Promise<ProcessingResult> {
  const text = await file.text();
  
  // 1. Detect separator
  const separator = detectSeparator(text);
  
  // 2. Parse CSV raw matrix
  const rawRows = parseCSV(text, separator);
  if (rawRows.length === 0) {
    throw new Error('The file is empty.');
  }
  
  // Separate original headers and table payload
  const rawHeaders = rawRows[0] || [];
  const rawDataRows = rawRows.slice(1);
  
  // 3. Clean headers (trim, strip newlines, spaces deduplication)
  const cleanedHeaders = rawHeaders.map(h => {
    if (!h) return '';
    return h
      .replace(/[\r\n]+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  });
  
  // 4. Safely de-duplicate column names
  const seenHeaderNames = new Map<string, number>();
  const uniqueHeaders = cleanedHeaders.map((header, idx) => {
    let name = header;
    if (name === '') {
      name = `Untitled_Column_${idx + 1}`;
    }
    if (!seenHeaderNames.has(name)) {
      seenHeaderNames.set(name, 0);
      return name;
    } else {
      const count = seenHeaderNames.get(name)! + 1;
      seenHeaderNames.set(name, count);
      return `${name}_${count}`;
    }
  });

  // 5. Remove fully empty rows from the data rows
  const nonMockRows = rawDataRows.filter(row => {
    return row.some(cell => cell !== undefined && cell !== null && cell.trim() !== '');
  });
  const removedEmptyRows = rawDataRows.length - nonMockRows.length;

  // 6. Find and remove fully empty columns (evaluated on both header and row values)
  const maxInitialColCount = Math.max(uniqueHeaders.length, ...nonMockRows.map(r => r.length));
  const activeColumnIndices: number[] = [];
  
  for (let colIdx = 0; colIdx < maxInitialColCount; colIdx++) {
    const headerIsEmpty = !cleanedHeaders[colIdx] || cleanedHeaders[colIdx].trim() === '';
    
    // Check if all rows are empty in this column
    const allRowsEmpty = nonMockRows.every(row => {
      const val = row[colIdx];
      return !val || val.trim() === '';
    });
    
    // A column is removed only if the header is empty/missing AND all cells in that column are empty
    if (!(headerIsEmpty && allRowsEmpty)) {
      activeColumnIndices.push(colIdx);
    }
  }
  
  const finalColHeaders = activeColumnIndices.map(idx => uniqueHeaders[idx] || `Column_${idx + 1}`);
  const cleanDataRows = nonMockRows.map(row => {
    return activeColumnIndices.map(idx => row[idx] || '');
  });
  const removedEmptyCols = maxInitialColCount - activeColumnIndices.length;

  // 7. Auto-detect column data types (Numerical and Date/Time detection)
  const isDateColumn = Array(activeColumnIndices.length).fill(false);
  const isNumColumn = Array(activeColumnIndices.length).fill(false);
  
  let detectedNumericColsCount = 0;
  let detectedDateColsCount = 0;

  for (let col = 0; col < activeColumnIndices.length; col++) {
    let totalNonEmpty = 0;
    let numCount = 0;
    let dateCount = 0;

    for (const row of cleanDataRows) {
      const cell = row[col];
      if (cell && cell.trim() !== '') {
        totalNonEmpty++;
        if (tryParseDate(cell) !== null) dateCount++;
        if (tryParseNumber(cell) !== null) numCount++;
      }
    }

    if (totalNonEmpty > 0) {
      // Date detection takes precedence
      if (dateCount / totalNonEmpty >= 0.8) {
        isDateColumn[col] = true;
        detectedDateColsCount++;
      } else if (numCount / totalNonEmpty >= 0.8) {
        isNumColumn[col] = true;
        detectedNumericColsCount++;
      }
    }
  }

  const columnTypes: ColumnKind[] = activeColumnIndices.map((_, col) => {
    if (isDateColumn[col]) return 'date';
    if (isNumColumn[col]) return 'number';

    const nonEmptyValues = cleanDataRows
      .map(row => row[col])
      .filter(cell => cell && cell.trim() !== '');
    const booleanCount = nonEmptyValues.filter(cell => {
      const lower = cell.trim().toLowerCase();
      return lower === 'true' || lower === 'false';
    }).length;

    if (nonEmptyValues.length > 0 && booleanCount / nonEmptyValues.length >= 0.8) {
      return 'boolean';
    }
    return 'text';
  });

  // 8. Reconstruct structured cells array of arrays (AOA)
  const finalXlsxData: any[][] = [];
  finalXlsxData.push(finalColHeaders);
  const finalDataRows: any[][] = [];

  for (const row of cleanDataRows) {
    const rowCells: any[] = [];
    for (let col = 0; col < activeColumnIndices.length; col++) {
      const rawVal = row[col];
      if (rawVal === undefined || rawVal === null || rawVal.trim() === '') {
        rowCells.push(null);
        continue;
      }
      
      if (isDateColumn[col]) {
        const parsedDate = tryParseDate(rawVal);
        rowCells.push(parsedDate ? parsedDate : rawVal);
      } else if (isNumColumn[col]) {
        const parsedNum = tryParseNumber(rawVal);
        rowCells.push(parsedNum !== null ? parsedNum : rawVal);
      } else {
        const trimmed = rawVal.trim();
        const lower = trimmed.toLowerCase();
        if (lower === 'true') {
          rowCells.push(true);
        } else if (lower === 'false') {
          rowCells.push(false);
        } else {
          rowCells.push(rawVal);
        }
      }
    }
    finalDataRows.push(rowCells);
    finalXlsxData.push(rowCells);
  }

  const profiles: ColumnProfile[] = finalColHeaders.map((header, col) => {
    const values = cleanDataRows.map(row => row[col] || '');
    const nonEmptyValues = values.filter(value => value.trim() !== '');
    const typedValues = finalDataRows.map(row => row[col]).filter(value => value !== null && value !== undefined && value !== '');
    const profile: ColumnProfile = {
      header,
      type: columnTypes[col],
      nonEmptyCount: nonEmptyValues.length,
      blankCount: cleanDataRows.length - nonEmptyValues.length,
      sampleValue: nonEmptyValues[0] || ''
    };

    if (columnTypes[col] === 'number') {
      const nums = typedValues.filter((value): value is number => typeof value === 'number' && !isNaN(value));
      if (nums.length > 0) {
        profile.min = Math.min(...nums);
        profile.max = Math.max(...nums);
        profile.average = nums.reduce((sum, value) => sum + value, 0) / nums.length;
      }
    } else if (columnTypes[col] === 'date') {
      const dates = typedValues.filter((value): value is Date => value instanceof Date && !isNaN(value.getTime()));
      if (dates.length > 0) {
        profile.min = new Date(Math.min(...dates.map(date => date.getTime())));
        profile.max = new Date(Math.max(...dates.map(date => date.getTime())));
      }
    }

    return profile;
  });

  // 9. Generate styled workbook sheets.
  const ws = XLSX.utils.aoa_to_sheet(finalXlsxData, { cellDates: true });
  
  // Set explicit column widths to prevent squeezed values
  const widths: XLSX.ColInfo[] = [];
  for (let col = 0; col < finalColHeaders.length; col++) {
    let maxLength = finalColHeaders[col].length + 4;
    // Inspect some values for length
    const sampleSize = Math.min(cleanDataRows.length, 30);
    for (let r = 0; r < sampleSize; r++) {
      const val = cleanDataRows[r][col];
      if (val) {
        maxLength = Math.max(maxLength, val.length + 2);
      }
    }
    widths.push({ wch: Math.min(Math.max(maxLength, 12), 42) }); // cap width to keep browsing practical
  }
  ws['!cols'] = widths;
  ws['!rows'] = [{ hpt: 42 }, ...cleanDataRows.map(() => ({ hpt: 20 }))];
  if (finalColHeaders.length > 0) {
    ws['!autofilter'] = {
      ref: XLSX.utils.encode_range({
        s: { r: 0, c: 0 },
        e: { r: Math.max(finalXlsxData.length - 1, 0), c: finalColHeaders.length - 1 }
      })
    };
  }
  applyHeaderStyle(ws, finalColHeaders.length);
  applyZebraRows(ws, cleanDataRows.length, finalColHeaders.length);
  applyDataFormats(ws, cleanDataRows.length, columnTypes);

  const wb = XLSX.utils.book_new();
  setWorkbookMetadata(wb, file.name);

  const summarySheet = makeSummarySheet(
    file,
    separator,
    cleanDataRows.length,
    finalColHeaders.length,
    removedEmptyRows,
    removedEmptyCols,
    detectedNumericColsCount,
    detectedDateColsCount,
    profiles
  );
  const profileSheet = makeColumnProfileSheet(profiles);
  const chartDataSheet = makeChartDataSheet(cleanDataRows, finalDataRows, finalColHeaders, profiles, columnTypes);

  XLSX.utils.book_append_sheet(wb, summarySheet, 'Summary');
  XLSX.utils.book_append_sheet(wb, ws, safeSheetName('Data'));
  XLSX.utils.book_append_sheet(wb, profileSheet, 'Column Profile');
  XLSX.utils.book_append_sheet(wb, chartDataSheet, 'Chart Data');

  // Write sheet format array
  const xlsxBuffer = XLSX.write(wb, { bookType: 'xlsx', type: 'array', cellStyles: true });
  const styledBuffer = await addFrozenHeaderPane(new Uint8Array(xlsxBuffer));

  return {
    fileName: file.name,
    originalSize: file.size,
    xlsxBuffer: styledBuffer,
    rowCount: cleanDataRows.length,
    colCount: finalColHeaders.length,
    separator,
    cleanedHeaders: finalColHeaders,
    removedEmptyRows,
    removedEmptyCols,
    detectedNumericColsCount,
    detectedDateColsCount
  };
}
