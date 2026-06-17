from __future__ import annotations

import argparse
import csv
import json
import math
import re
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


LOCAL_SERVER_DEFAULT_PORT = 8765
EXCEL_MAX_ROWS = 1_048_576
EXCEL_MAX_DATA_ROWS = EXCEL_MAX_ROWS - 1
MAX_COLUMNS = 300
MAX_STATUS_VALUES = 50

CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("alarm_trip_lockout", ["alarm", "trip", "fault", "lockout", "fail", "failure", "warning"]),
    ("temperature", ["temperature", " temp", "chwst", "chwrt", "chws", "chwr", "lwt", "ewt", "supply temp", "return temp", "sat", "rat", "room temp"]),
    ("pressure", ["pressure", "press", " dp ", "differential pressure", "delta p"]),
    ("flow", ["flow", "water flow", "air flow", "airflow", "gpm", "l/s", "lpm", "cmh"]),
    ("current", ["current", "amp", "amps", "motor current", "rla"]),
    ("load", ["load", "percent load", "demand", "capacity"]),
    ("speed_frequency", ["speed", "frequency", "hz"]),
    ("valve", ["valve", "vlv", "chw valve", "cdw valve", "open", "opening"]),
    ("pump", ["pump", "chwp", "cdwp", "chw pump", "cdw pump"]),
    ("fan", ["fan", "blower", "exhaust fan", "supply fan", "return fan", " ef ", "saf", "raf"]),
    ("chiller", ["chiller", "chill", "19dv", "23xrv", " ch1", " ch2", " ch3", " ch "]),
    ("ahu", ["ahu", "air handling unit"]),
    ("fcu", ["fcu", "fan coil"]),
    ("setpoint", ["setpoint", "set point", " sp ", "target"]),
    ("command", ["command", "cmd", "enable", "enabled", "start", "stop"]),
    ("status", ["status", "run", "running", "proof", "feedback", "on/off", " on ", " off "]),
    ("humidity", ["humidity", " rh ", "relative humidity"]),
]

CATEGORY_SCORE = {"temperature": 28, "pressure": 24, "flow": 22, "load": 20, "current": 19, "speed_frequency": 18, "valve": 17, "humidity": 16, "setpoint": 10}
ACTIVE_STATUS_VALUES = {"alarm", "trip", "fault", "lockout", "fail", "failure", "warning", "on", "true", "run", "running", "open", "start", "enabled", "1"}
TRUE_VALUES = {"1", "true", "on", "run", "running", "open", "start", "enabled", "alarm"}
FALSE_VALUES = {"0", "false", "off", "stop", "stopped", "closed", "close", "disabled", "normal"}


@dataclass
class NumericStat:
    column: str
    index: int
    category: str
    count: int = 0
    mean: float = 0.0
    m2: float = 0.0
    min_value: float | None = None
    max_value: float | None = None
    first: float | None = None
    latest: float | None = None
    unique_values: set[float] | None = None

    def add(self, value: float) -> None:
        if self.first is None:
            self.first = value
        self.latest = value
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self.m2 += delta * (value - self.mean)
        self.min_value = value if self.min_value is None else min(self.min_value, value)
        self.max_value = value if self.max_value is None else max(self.max_value, value)
        if self.unique_values is None:
            self.unique_values = set()
        if len(self.unique_values) <= 100:
            self.unique_values.add(value)

    @property
    def std_dev(self) -> float:
        return math.sqrt(self.m2 / self.count) if self.count > 1 else 0.0

    @property
    def change(self) -> float:
        return 0.0 if self.first is None or self.latest is None else self.latest - self.first

    def score(self, total_rows: int) -> float:
        completeness = self.count / max(total_rows, 1)
        unique_count = len(self.unique_values or set())
        mostly_constant = unique_count <= 1 or self.std_dev < 0.000001
        return CATEGORY_SCORE.get(self.category, 4) + min(unique_count, 20) * 0.4 + completeness * 10 - (20 if mostly_constant else 0)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize_name(value: Any) -> str:
    text = str(value or "").lower().replace("_", " ").replace("-", " ")
    return f" {re.sub(r'\\s+', ' ', text).strip()} "


def clean_header(value: Any, index: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "").replace("\ufeff", "")).strip()
    return text or f"Column_{index + 1}"


def parse_number(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in TRUE_VALUES:
        return 1.0
    if lowered in FALSE_VALUES:
        return 0.0
    cleaned = text.replace(",", "")
    if cleaned.endswith("%"):
        cleaned = cleaned[:-1]
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def classify_column(header: str) -> str:
    normalized = normalize_name(header)
    if any(token in normalized for token in [" date ", " time ", " timestamp ", " datetime "]):
        return "datetime"
    for category, keywords in CATEGORY_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            return category
    return "unknown"


def detect_encoding(path: Path) -> str:
    sample = path.read_bytes()[:65536]
    for encoding in ("utf-8-sig", "cp1252", "latin1"):
        try:
            sample.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "latin1"


def detect_delimiter(path: Path, encoding: str) -> str:
    with path.open("r", encoding=encoding, errors="replace", newline="") as handle:
        sample = handle.read(65536)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        first_line = sample.splitlines()[0] if sample.splitlines() else ""
        counts = {delimiter: first_line.count(delimiter) for delimiter in [",", ";", "\t", "|"]}
        delimiter, count = max(counts.items(), key=lambda item: item[1])
        return delimiter if count > 0 else ","


def iter_csv(path: Path, encoding: str, delimiter: str):
    with path.open("r", encoding=encoding, errors="replace", newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        for row in reader:
            if row and any(str(cell).strip() for cell in row):
                yield row


def prepare_row(raw_row: list[str], column_count: int) -> list[str]:
    row = [str(cell).strip() for cell in raw_row[:column_count]]
    if len(row) < column_count:
        row.extend([""] * (column_count - len(row)))
    return row


def analyze_csv(path: Path) -> tuple[list[str], str, str, int, list[NumericStat], list[dict[str, Any]], int | None]:
    encoding = detect_encoding(path)
    delimiter = detect_delimiter(path, encoding)
    iterator = iter_csv(path, encoding, delimiter)
    try:
        headers = [clean_header(value, index) for index, value in enumerate(next(iterator)[:MAX_COLUMNS])]
    except StopIteration as exc:
        raise ValueError("CSV file is empty.") from exc

    categories = [classify_column(header) for header in headers]
    datetime_index = next((index for index, category in enumerate(categories) if category == "datetime"), None)
    numeric_stats = [NumericStat(header, index, categories[index]) for index, header in enumerate(headers)]
    status_columns = [index for index, category in enumerate(categories) if category in {"status", "command", "alarm_trip_lockout", "pump", "fan", "chiller", "ahu", "fcu", "valve"}]
    status_counts: dict[int, Counter[str]] = {index: Counter() for index in status_columns[:8]}
    row_count = 0

    for raw_row in iterator:
        row_count += 1
        row = prepare_row(raw_row, len(headers))
        for index, value in enumerate(row):
            number = parse_number(value)
            if number is not None:
                numeric_stats[index].add(number)
        for index in status_counts:
            value = row[index].strip()
            if value and len(status_counts[index]) <= MAX_STATUS_VALUES:
                status_counts[index][value] += 1

    useful_numeric = [stat for stat in numeric_stats if stat.count / max(row_count, 1) >= 0.5]
    useful_numeric.sort(key=lambda stat: stat.score(row_count), reverse=True)
    status_profiles = []
    for index, counts in status_counts.items():
        if not counts:
            continue
        top_state, top_count = counts.most_common(1)[0]
        active_count = sum(count for value, count in counts.items() if value.lower() in ACTIVE_STATUS_VALUES)
        status_profiles.append({"column": headers[index], "states": len(counts), "top_state": top_state, "top_state_pct": top_count / max(row_count, 1), "active_events": active_count, "active_pct": active_count / max(row_count, 1)})
    return headers, encoding, delimiter, row_count, useful_numeric, status_profiles, datetime_index


def create_excel_streaming(path: Path, output_path: Path) -> dict[str, Any]:
    import xlsxwriter

    headers, encoding, delimiter, row_count, numeric_stats, status_profiles, datetime_index = analyze_csv(path)
    workbook = xlsxwriter.Workbook(str(output_path), {"constant_memory": True, "strings_to_urls": False})
    header_format = workbook.add_format({"bold": True, "bg_color": "#1F2937", "font_color": "white", "border": 1, "text_wrap": True})
    text_format = workbook.add_format({"valign": "top"})
    number_format = workbook.add_format({"num_format": "#,##0.00", "valign": "top"})
    title_format = workbook.add_format({"bold": True, "font_size": 16, "font_color": "white", "bg_color": "#0F172A", "align": "center"})
    section_format = workbook.add_format({"bold": True, "font_color": "white", "bg_color": "#334155", "border": 1})
    value_format = workbook.add_format({"border": 1, "text_wrap": True, "valign": "top"})
    pct_format = workbook.add_format({"border": 1, "num_format": "0.0%"})
    num_format = workbook.add_format({"border": 1, "num_format": "#,##0.00"})

    data = workbook.add_worksheet("Data")
    for col, header in enumerate(headers):
        data.write(0, col, header, header_format)
        data.set_column(col, col, min(max(len(header) + 2, 12), 32))

    iterator = iter_csv(path, encoding, delimiter)
    next(iterator, None)
    written_rows = 0
    for excel_row, raw_row in enumerate(iterator, start=1):
        if excel_row > EXCEL_MAX_DATA_ROWS:
            break
        row = prepare_row(raw_row, len(headers))
        for col, value in enumerate(row):
            number = parse_number(value)
            if number is not None:
                data.write_number(excel_row, col, number, number_format)
            elif value:
                data.write(excel_row, col, value, text_format)
            else:
                data.write_blank(excel_row, col, None, text_format)
        written_rows = excel_row
    data.freeze_panes(1, 0)
    data.autofilter(0, 0, max(written_rows, 1), max(len(headers) - 1, 0))

    analysis = workbook.add_worksheet("Analysis")
    analysis.set_column("A:A", 24)
    analysis.set_column("B:B", 34)
    analysis.set_column("C:H", 16)
    analysis.merge_range("A1:H1", "HBL-BMS Trending Analysis", title_format)
    overview = [("File", path.name), ("Source size", f"{path.stat().st_size:,} bytes"), ("Rows scanned", row_count), ("Rows written to Data", written_rows), ("Columns", len(headers)), ("Date/time column", headers[datetime_index] if datetime_index is not None else "Not detected")]
    analysis.write("A3", "File Overview", section_format)
    analysis.write("B3", "Value", section_format)
    for row_index, (label, value) in enumerate(overview, start=3):
        analysis.write(row_index, 0, label, value_format)
        analysis.write(row_index, 1, value, value_format)

    analysis.write("A11", "Engineering Notes", section_format)
    analysis.write("B11", "Insight", section_format)
    for row_index, note in enumerate(["CSV was processed with a streaming local engine; the full file was not loaded into memory.", "Data rows are capped at Excel's worksheet row limit when needed.", "Numeric columns are prioritized by ACMV keywords, completeness, and variation."], start=11):
        analysis.write(row_index, 0, row_index - 10, value_format)
        analysis.write(row_index, 1, note, value_format)

    analysis.write("D3", "Numeric Analysis", section_format)
    for offset, header in enumerate(["Column", "Category", "Avg", "Min", "Max", "Latest", "Change", "Std Dev"], start=3):
        analysis.write(3, offset, header, section_format)
    for row_offset, stat in enumerate(numeric_stats[:12], start=4):
        analysis.write(row_offset, 3, stat.column, value_format)
        analysis.write(row_offset, 4, stat.category, value_format)
        analysis.write_number(row_offset, 5, stat.mean, num_format)
        analysis.write_number(row_offset, 6, stat.min_value or 0, num_format)
        analysis.write_number(row_offset, 7, stat.max_value or 0, num_format)
        analysis.write_number(row_offset, 8, stat.latest or 0, num_format)
        analysis.write_number(row_offset, 9, stat.change, num_format)
        analysis.write_number(row_offset, 10, stat.std_dev, num_format)

    status_start = 20
    analysis.write(status_start, 0, "Status Analysis", section_format)
    for offset, header in enumerate(["Column", "States", "Top State", "Top State %", "Active Events", "Active %"]):
        analysis.write(status_start + 1, offset, header, section_format)
    for row_offset, item in enumerate(status_profiles, start=status_start + 2):
        analysis.write(row_offset, 0, item["column"], value_format)
        analysis.write_number(row_offset, 1, item["states"], value_format)
        analysis.write(row_offset, 2, item["top_state"], value_format)
        analysis.write_number(row_offset, 3, item["top_state_pct"], pct_format)
        analysis.write_number(row_offset, 4, item["active_events"], value_format)
        analysis.write_number(row_offset, 5, item["active_pct"], pct_format)

    if numeric_stats and written_rows > 1:
        chart = workbook.add_chart({"type": "line"})
        category_index = datetime_index if datetime_index is not None else 0
        for stat in numeric_stats[:5]:
            chart.add_series({"name": ["Data", 0, stat.index], "categories": ["Data", 1, category_index, min(written_rows, 300), category_index], "values": ["Data", 1, stat.index, min(written_rows, 300), stat.index]})
        chart.set_title({"name": "Main Time-Series Trends"})
        chart.set_legend({"position": "bottom"})
        analysis.insert_chart("A31", chart, {"x_scale": 1.55, "y_scale": 1.2})

    workbook.close()
    return {"rowsScanned": row_count, "rowsWritten": written_rows, "columns": len(headers)}


def resolve_local_csv_paths(values: list[str]) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for value in values:
        path = Path(str(value).strip().strip('"').strip("'")).expanduser()
        candidates = sorted(path.iterdir()) if path.is_dir() else [path]
        for candidate in candidates:
            if candidate.is_file() and candidate.suffix.lower() == ".csv":
                resolved = candidate.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    paths.append(resolved)
    return paths


def convert_local_files(input_paths: list[Path], output_zip: Path) -> tuple[int, int]:
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    success_count = 0
    fail_count = 0
    report_lines = ["HBL-BMS Trending Local Conversion Report", f"Generated: {now_iso()}", ""]
    with tempfile.TemporaryDirectory(prefix="csv-fast-view-") as tmp_dir:
        with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for path in input_paths:
                print(f"Processing {path}...")
                try:
                    workbook_path = Path(tmp_dir) / f"{path.stem}.xlsx"
                    result = create_excel_streaming(path, workbook_path)
                    archive.write(workbook_path, f"{path.stem}.xlsx")
                    success_count += 1
                    report_lines.append(f"OK: {path.name} -> {path.stem}.xlsx ({workbook_path.stat().st_size:,} bytes, {result['rowsScanned']:,} rows scanned)")
                    print(f"  OK -> {path.stem}.xlsx")
                except Exception as exc:
                    fail_count += 1
                    report_lines.append(f"FAILED: {path.name} - {exc}")
                    print(f"  FAILED: {exc}")
            report_lines.extend(["", f"Successful files: {success_count}", f"Failed files: {fail_count}"])
            archive.writestr("conversion_report.txt", "\n".join(report_lines))
    return success_count, fail_count


def open_native_picker(mode: str) -> list[str]:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.update()
    try:
        if mode == "folder":
            folder = filedialog.askdirectory(title="Select folder containing CSV files")
            return [folder] if folder else []
        return list(filedialog.askopenfilenames(title="Select CSV files", filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]))
    finally:
        root.destroy()


def add_headers(handler: BaseHTTPRequestHandler, content_type: str) -> None:
    handler.send_header("Content-Type", content_type)
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Private-Network", "true")


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self) -> None:
        self.send_response(204)
        add_headers(self, "text/plain")
        self.end_headers()

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/api/local/status":
            self.send_json(200, {"ok": True, "service": "CSV Fast View Local Converter", "time": now_iso()})
            return
        self.send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/local/pick-files":
            self.send_json(200, {"ok": True, "paths": open_native_picker("files")})
            return
        if path == "/api/local/pick-folder":
            self.send_json(200, {"ok": True, "paths": open_native_picker("folder")})
            return
        if path == "/api/local/convert":
            self.handle_convert()
            return
        self.send_json(404, {"ok": False, "error": "Not found"})

    def handle_convert(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        payload = json.loads(body.decode("utf-8") or "{}")
        paths = payload.get("paths") or []
        if not isinstance(paths, list):
            self.send_json(400, {"ok": False, "error": "paths must be a list."})
            return
        input_paths = resolve_local_csv_paths([str(value) for value in paths])
        if not input_paths:
            self.send_json(400, {"ok": False, "error": "No CSV files found."})
            return
        output_zip = input_paths[0].parent / "hbl-bms-trend-analysis.zip"
        try:
            success_count, fail_count = convert_local_files(input_paths, output_zip)
        except Exception as exc:
            self.send_json(500, {"ok": False, "error": str(exc)})
            return
        self.send_json(200, {"ok": success_count > 0, "outputZip": str(output_zip), "successfulFiles": success_count, "failedFiles": fail_count, "inputFiles": [str(path) for path in input_paths]})

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        add_headers(self, "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run or use the CSV Fast View local converter.")
    parser.add_argument("inputs", nargs="*", help="CSV files or folders to convert.")
    parser.add_argument("--out", default="hbl-bms-trend-analysis.zip")
    parser.add_argument("--serve-local", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=LOCAL_SERVER_DEFAULT_PORT)
    args = parser.parse_args()
    if args.serve_local:
        server = HTTPServer((args.host, args.port), handler)
        print(f"CSV Fast View local converter running at http://{args.host}:{args.port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping local converter.")
        return 0
    input_paths = resolve_local_csv_paths(args.inputs)
    if not input_paths:
        parser.error("Provide CSV files/folders or use --serve-local.")
    success_count, _fail_count = convert_local_files(input_paths, Path(args.out))
    return 0 if success_count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
