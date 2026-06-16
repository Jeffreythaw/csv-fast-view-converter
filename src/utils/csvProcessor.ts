/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import * as XLSX from 'xlsx';

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

  // 8. Reconstruct structured cells array of arrays (AOA)
  const finalXlsxData: any[][] = [];
  finalXlsxData.push(finalColHeaders);

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
    finalXlsxData.push(rowCells);
  }

  // 9. Generate Workbook & Sheet with SheetJS XLS
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
    widths.push({ wch: Math.min(maxLength, 50) }); // caps width at 50 to avoid infinite wide cells
  }
  ws['!cols'] = widths;

  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, 'CSV Data');

  // Write sheet format array
  const xlsxBuffer = XLSX.write(wb, { bookType: 'xlsx', type: 'array' });

  return {
    fileName: file.name,
    originalSize: file.size,
    xlsxBuffer: new Uint8Array(xlsxBuffer),
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
