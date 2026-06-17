from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import statistics
import time
import zipfile
from collections import Counter
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("alarm_trip_lockout", ["alarm", "trip", "fault", "lockout", "fail", "failure", "warning"]),
    ("cooling_tower", ["cooling tower", "tower", " ct "]),
    ("chiller", ["chiller", "chill", "19dv", "23xrv", " ch1", " ch2", " ch3", " ch "]),
    ("ahu", ["ahu", "air handling unit"]),
    ("fcu", ["fcu", "fan coil"]),
    ("pump", ["pump", "chwp", "cdwp", "chw pump", "cdw pump"]),
    ("fan", ["fan", "blower", "exhaust fan", "supply fan", "return fan", " ef ", "saf", "raf"]),
    ("valve", ["valve", "vlv", "chw valve", "cdw valve", "open", "opening"]),
    ("vsd_vfd", ["vsd", "vfd", "inverter"]),
    ("speed_frequency", ["speed", "frequency", "hz"]),
    ("temperature", ["temperature", " temp", "chwst", "chwrt", "chws", "chwr", "lwt", "ewt", "leaving", "entering", "supply temp", "return temp", "sat", "rat", "room temp"]),
    ("pressure", ["pressure", "press", " dp ", "differential pressure", "delta p"]),
    ("flow", ["flow", "water flow", "air flow", "airflow", "gpm", "l/s", "lpm", "cmh"]),
    ("current", ["current", "amp", "amps", "motor current", "rla"]),
    ("load", ["load", "percent load", "demand", "capacity"]),
    ("setpoint", ["setpoint", "set point", " sp ", "target"]),
    ("command", ["command", "cmd", "enable", "enabled", "start", "stop"]),
    ("status", ["status", "run", "running", "proof", "feedback", "on/off", " on ", " off "]),
    ("humidity", ["humidity", " rh ", "relative humidity"]),
]

EQUIPMENT_KEYWORDS = [
    "chiller",
    "ahu",
    "fcu",
    "fan",
    "pump",
    "valve",
    "vsd",
    "vfd",
    "chw",
    "cdw",
    "condenser water",
    "chilled water",
    "cooling tower",
]

PREFERRED_NUMERIC_CATEGORIES = {
    "temperature": 28,
    "pressure": 24,
    "flow": 22,
    "load": 20,
    "current": 19,
    "speed_frequency": 18,
    "valve": 17,
    "humidity": 16,
    "setpoint": 10,
}

ACTIVE_STATUS_VALUES = {"alarm", "trip", "fault", "lockout", "fail", "failure", "warning", "on", "true", "run", "running", "open", "start", "enabled", "1"}
TEXT_TRUE_VALUES = {"1", "true", "on", "run", "running", "open", "start", "enabled", "alarm"}
TEXT_FALSE_VALUES = {"0", "false", "off", "stop", "stopped", "closed", "close", "disabled", "normal"}

MAX_VERCEL_BODY_BYTES = int(4.5 * 1024 * 1024)
MAX_UPLOAD_FILES = 8
MAX_SINGLE_FILE_BYTES = 4 * 1024 * 1024
MAX_LOCAL_FILE_BYTES = 512 * 1024 * 1024
MAX_INPUT_ROWS = 120_000
MAX_INPUT_COLUMNS = 300
MAX_RUNTIME_SECONDS = 45
LOCAL_SERVER_DEFAULT_PORT = 8765
EXCEL_MAX_ROWS = 1_048_576
EXCEL_MAX_COLUMNS = 16_384
EXCEL_MAX_DATA_ROWS = EXCEL_MAX_ROWS - 1


class ConversionError(Exception):
    pass


class UploadedFile:
    def __init__(self, filename: str, data: bytes):
        self.filename = filename or "uploaded.csv"
        self.data = data


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize_column_name(name: Any) -> str:
    text = str(name or "").lower()
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"[\r\n]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return f" {text} "


def detect_delimiter(sample: str) -> str:
    first_lines = "\n".join(sample.splitlines()[:10])
    try:
        dialect = csv.Sniffer().sniff(first_lines, delimiters=",;\t|")
        return dialect.delimiter
    except csv.Error:
        first_line = sample.splitlines()[0] if sample.splitlines() else ""
        counts = {delimiter: first_line.count(delimiter) for delimiter in [",", ";", "\t", "|"]}
        delimiter, count = max(counts.items(), key=lambda item: item[1])
        return delimiter if count > 0 else ","


def decode_csv(raw: bytes) -> str:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "cp1252", "latin1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ConversionError(f"Unable to decode CSV file: {last_error}")


def clean_header_cell(value: str, index: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "").replace("\ufeff", "")).strip()
    return text or f"Column_{index + 1}"


def parse_csv_bytes(raw: bytes, deadline: float, max_file_bytes: int | None = MAX_SINGLE_FILE_BYTES) -> tuple[list[str], list[list[str]], str]:
    if not raw:
        raise ConversionError("CSV file is empty.")
    if max_file_bytes is not None and len(raw) > max_file_bytes:
        raise ConversionError(f"CSV is {len(raw):,} bytes. This mode accepts up to {max_file_bytes:,} bytes per file.")

    text = decode_csv(raw)
    delimiter = detect_delimiter(text[:65536])
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)

    header: list[str] | None = None
    rows: list[list[str]] = []
    non_empty_columns: set[int] = set()

    for row_index, row in enumerate(reader):
        if time.monotonic() > deadline:
            raise TimeoutError("CSV parsing exceeded the serverless time budget.")
        if not row or not any(str(cell).strip() for cell in row):
            continue
        if header is None:
            header = [clean_header_cell(cell, idx) for idx, cell in enumerate(row[:MAX_INPUT_COLUMNS])]
            continue
        if len(rows) >= MAX_INPUT_ROWS:
            raise ConversionError(f"CSV has more than {MAX_INPUT_ROWS:,} data rows. Use Local Folder mode for this file.")
        trimmed = [str(cell).strip() for cell in row[:MAX_INPUT_COLUMNS]]
        if len(trimmed) < len(header):
            trimmed.extend([""] * (len(header) - len(trimmed)))
        elif len(trimmed) > len(header):
            trimmed = trimmed[:len(header)]
        for col_idx, cell in enumerate(trimmed):
            if cell:
                non_empty_columns.add(col_idx)
        rows.append(trimmed)

    if header is None:
        raise ConversionError("CSV does not contain a header row.")
    if not rows:
        raise ConversionError("CSV does not contain any data rows.")

    keep_indexes = [idx for idx, _ in enumerate(header) if idx in non_empty_columns]
    if not keep_indexes:
        raise ConversionError("CSV only contains empty columns.")
    header = [header[idx] for idx in keep_indexes]
    rows = [[row[idx] if idx < len(row) else "" for idx in keep_indexes] for row in rows]
    return header, rows, delimiter


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in TEXT_TRUE_VALUES:
        return 1.0
    if lowered in TEXT_FALSE_VALUES:
        return 0.0
    cleaned = text.replace(",", "")
    if cleaned.endswith("%"):
        cleaned = cleaned[:-1]
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return number


DATETIME_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
]


def parse_datetime_value(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("T", " ").replace("Z", "").strip()
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    return None


def classify_column(column_name: str, values: list[str]) -> str:
    normalized = normalize_column_name(column_name)
    if any(token in normalized for token in [" date ", " time ", " timestamp ", " datetime "]):
        parsed_count = sum(1 for value in values if parse_datetime_value(value))
        if values and parsed_count / len(values) >= 0.5:
            return "datetime"

    for category, keywords in CATEGORY_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            return category

    numeric_count = sum(1 for value in values if parse_number(value) is not None)
    if values and numeric_count / len(values) >= 0.7:
        return "unknown_numeric"
    return "unknown_text"


def detect_datetime_column(headers: list[str], rows: list[list[str]]) -> tuple[int | None, list[datetime | None]]:
    normalized = [normalize_column_name(header) for header in headers]
    date_candidates = [idx for idx, name in enumerate(normalized) if " date " in name]
    time_candidates = [idx for idx, name in enumerate(normalized) if " time " in name and "date" not in name]

    if date_candidates and time_candidates:
        date_idx = date_candidates[0]
        time_idx = time_candidates[0]
        parsed = [parse_datetime_value(f"{row[date_idx]} {row[time_idx]}") for row in rows]
        if parsed and sum(1 for value in parsed if value) / len(parsed) >= 0.5:
            return None, parsed

    scored: list[tuple[float, int, list[datetime | None]]] = []
    for col_idx, header in enumerate(headers):
        parsed = [parse_datetime_value(row[col_idx]) for row in rows]
        pct = sum(1 for value in parsed if value) / len(rows)
        name_bonus = 0.25 if any(token in normalized[col_idx] for token in [" date ", " time ", " timestamp ", " datetime "]) else 0
        if pct >= 0.5:
            scored.append((pct + name_bonus, col_idx, parsed))

    if not scored:
        return None, [None] * len(rows)
    scored.sort(reverse=True, key=lambda item: item[0])
    _, col_idx, parsed = scored[0]
    return col_idx, parsed


def is_binary_like(values: list[Any]) -> bool:
    cleaned = {str(value).strip().lower() for value in values if str(value).strip()}
    return bool(cleaned) and len(cleaned) <= 4 and cleaned.issubset(TEXT_TRUE_VALUES | TEXT_FALSE_VALUES | {"normal", "close"})


def numeric_profile(headers: list[str], typed_rows: list[list[Any]], categories: dict[str, str]) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    row_count = max(len(typed_rows), 1)
    for col_idx, header in enumerate(headers):
        values = [row[col_idx] for row in typed_rows]
        numeric_values = [value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
        if len(numeric_values) / row_count < 0.5:
            continue
        unique_count = len(set(numeric_values))
        std_dev = statistics.pstdev(numeric_values) if len(numeric_values) > 1 else 0.0
        first = numeric_values[0] if numeric_values else None
        latest = numeric_values[-1] if numeric_values else None
        category = categories.get(header, "unknown_numeric")
        binary = is_binary_like(values)
        mostly_constant = unique_count <= 1 or std_dev < 0.000001
        score = PREFERRED_NUMERIC_CATEGORIES.get(category, 4)
        score += min(unique_count, 20) * 0.4
        score += min(len(numeric_values) / row_count, 1) * 10
        if mostly_constant:
            score -= 20
        if binary:
            score -= 12
        profiles.append({
            "column": header,
            "index": col_idx,
            "category": category,
            "non_empty_pct": len(numeric_values) / row_count,
            "unique_count": unique_count,
            "std_dev": std_dev,
            "min": min(numeric_values) if numeric_values else None,
            "max": max(numeric_values) if numeric_values else None,
            "average": statistics.fmean(numeric_values) if numeric_values else None,
            "first": first,
            "latest": latest,
            "change": latest - first if first is not None and latest is not None else None,
            "mostly_constant": mostly_constant,
            "binary_like": binary,
            "score": score,
        })
    profiles.sort(key=lambda item: item["score"], reverse=True)
    return profiles


def select_chart_numeric_columns(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item for item in profiles
        if item["non_empty_pct"] >= 0.5 and not item["mostly_constant"] and not item["binary_like"]
    ][:5]


def select_status_columns(headers: list[str], typed_rows: list[list[Any]], categories: dict[str, str]) -> list[int]:
    status_categories = {"command", "status", "alarm_trip_lockout", "valve", "pump", "fan", "chiller", "ahu", "fcu", "cooling_tower"}
    selected: list[int] = []
    for col_idx, header in enumerate(headers):
        values = [row[col_idx] for row in typed_rows]
        category = categories.get(header)
        if category in status_categories and is_binary_like(values):
            selected.append(col_idx)
        elif category in {"command", "status", "alarm_trip_lockout"}:
            selected.append(col_idx)
    return selected[:6]


def status_profile(headers: list[str], typed_rows: list[list[Any]], status_columns: list[int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    total_rows = max(len(typed_rows), 1)
    for col_idx in status_columns:
        counts = Counter(str(row[col_idx]).strip() for row in typed_rows if str(row[col_idx]).strip())
        if not counts:
            continue
        most_common = counts.most_common(1)[0]
        active_count = sum(count for value, count in counts.items() if value.strip().lower() in ACTIVE_STATUS_VALUES)
        rows.append({
            "column": headers[col_idx],
            "states": len(counts),
            "top_state": most_common[0],
            "top_state_pct": most_common[1] / total_rows,
            "active_events": active_count,
            "active_pct": active_count / total_rows,
            "counts": counts,
        })
    return rows


def detect_equipment_keywords(headers: list[str]) -> list[str]:
    joined = " ".join(normalize_column_name(col) for col in headers)
    found = []
    for keyword in EQUIPMENT_KEYWORDS:
        if keyword in joined and keyword.upper() not in found:
            found.append(keyword.upper() if keyword in {"ahu", "fcu", "vsd", "vfd", "chw", "cdw"} else keyword.title())
    return found


def build_insights(
    typed_rows: list[list[Any]],
    datetime_header: str | None,
    datetime_index: int | None,
    numeric_profiles: list[dict[str, Any]],
    selected_numeric: list[dict[str, Any]],
    status_rows: list[dict[str, Any]],
) -> list[str]:
    insights: list[str] = []
    if datetime_header:
        insights.append(f"Date/time trend detected using '{datetime_header}'. Data was sorted by detected timestamp where possible.")
    else:
        insights.append("No reliable date/time column detected; time-series analysis is limited.")

    for profile in selected_numeric[:3]:
        col_idx = profile["index"]
        max_value = profile["max"]
        if max_value is None:
            continue
        if datetime_index is not None:
            matching = next((row for row in typed_rows if row[col_idx] == max_value), None)
            when = matching[datetime_index] if matching else ""
            insights.append(f"{profile['category'].replace('_', ' ').title()} trend detected. Highest '{profile['column']}' observed at {when} was {max_value:.2f}.")
        else:
            insights.append(f"{profile['category'].replace('_', ' ').title()} trend detected in '{profile['column']}' with max {max_value:.2f} and average {profile['average']:.2f}.")

    if any(row["active_events"] > 0 for row in status_rows):
        alarm_cols = [row["column"] for row in status_rows if row["active_events"] > 0]
        insights.append(f"Status/alarm activity detected in: {', '.join(alarm_cols[:4])}.")

    if selected_numeric:
        insights.append("Analog trend columns were selected based on ACMV keyword priority, data completeness, and meaningful variation.")
    else:
        insights.append("No suitable analog time-series numeric columns were found. Data appears mostly status-based or constant.")

    return insights[:8]


def coerce_rows(headers: list[str], rows: list[list[str]], deadline: float) -> tuple[list[str], list[list[Any]], dict[str, str], int | None, str | None]:
    datetime_index, parsed_datetimes = detect_datetime_column(headers, rows)
    datetime_header = headers[datetime_index] if datetime_index is not None else ("Detected DateTime" if any(parsed_datetimes) else None)

    categories: dict[str, str] = {}
    column_values = [[row[col_idx] for row in rows] for col_idx in range(len(headers))]
    numeric_ratios = [
        sum(1 for original in values if parse_number(original) is not None) / max(len(rows), 1)
        for values in column_values
    ]
    for col_idx, header in enumerate(headers):
        categories[header] = "datetime" if col_idx == datetime_index else classify_column(header, column_values[col_idx])

    typed_rows: list[list[Any]] = []
    for row_idx, row in enumerate(rows):
        if time.monotonic() > deadline:
            raise TimeoutError("CSV analysis exceeded the serverless time budget.")
        typed_row: list[Any] = []
        for col_idx, value in enumerate(row):
            if col_idx == datetime_index and parsed_datetimes[row_idx]:
                typed_row.append(parsed_datetimes[row_idx])
                continue
            number = parse_number(value)
            if number is not None and numeric_ratios[col_idx] >= 0.75:
                typed_row.append(number)
            else:
                parsed_dt = parse_datetime_value(value)
                if parsed_dt and categories.get(headers[col_idx]) == "datetime":
                    typed_row.append(parsed_dt)
                else:
                    typed_row.append(value)
        if datetime_index is None and any(parsed_datetimes):
            typed_row.insert(0, parsed_datetimes[row_idx])
        typed_rows.append(typed_row)

    if datetime_index is None and any(parsed_datetimes):
        headers = ["Detected DateTime", *headers]
        categories["Detected DateTime"] = "datetime"
        datetime_index = 0

    if datetime_index is not None:
        typed_rows.sort(key=lambda row: (row[datetime_index] is None, row[datetime_index] or datetime.max))
    return headers, typed_rows, categories, datetime_index, datetime_header


def safe_sheet_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def write_cell(workbook: Any, worksheet: Any, row_idx: int, col_idx: int, value: Any, formats: dict[str, Any]) -> None:
    value = safe_sheet_value(value)
    if value is None or value == "":
        worksheet.write_blank(row_idx, col_idx, None, formats["text"])
    elif isinstance(value, datetime):
        worksheet.write_datetime(row_idx, col_idx, value, formats["date"])
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        worksheet.write_number(row_idx, col_idx, float(value), formats["number"])
    else:
        worksheet.write(row_idx, col_idx, str(value), formats["text"])


def write_workbook(uploaded: UploadedFile, headers: list[str], typed_rows: list[list[Any]], categories: dict[str, str], datetime_index: int | None, datetime_header: str | None) -> bytes:
    import xlsxwriter

    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {"in_memory": True})
    header_fmt = workbook.add_format({"bold": True, "bg_color": "#1F2937", "font_color": "white", "text_wrap": True, "valign": "vcenter", "border": 1})
    text_fmt = workbook.add_format({"valign": "top"})
    date_fmt = workbook.add_format({"num_format": "yyyy-mm-dd hh:mm", "valign": "top"})
    number_fmt = workbook.add_format({"num_format": "#,##0.00", "valign": "top"})
    title_fmt = workbook.add_format({"bold": True, "font_size": 16, "font_color": "white", "bg_color": "#0F172A", "align": "center", "valign": "vcenter"})
    section_fmt = workbook.add_format({"bold": True, "font_color": "white", "bg_color": "#334155", "border": 1})
    label_fmt = workbook.add_format({"bold": True, "bg_color": "#F1F5F9", "border": 1})
    value_fmt = workbook.add_format({"border": 1, "text_wrap": True, "valign": "top"})
    pct_fmt = workbook.add_format({"border": 1, "num_format": "0.0%"})
    num_fmt = workbook.add_format({"border": 1, "num_format": "#,##0.00"})
    formats = {"text": text_fmt, "date": date_fmt, "number": number_fmt}

    data_ws = workbook.add_worksheet("Data")
    sheet_headers = headers[:EXCEL_MAX_COLUMNS]
    sheet_rows = typed_rows[:EXCEL_MAX_DATA_ROWS]
    for col_idx, header in enumerate(sheet_headers):
        data_ws.write(0, col_idx, header, header_fmt)
        sample = [str(row[col_idx]) for row in sheet_rows[:100] if col_idx < len(row)]
        width = min(max(len(str(header)) + 2, *(len(value) + 1 for value in sample), 10), 32)
        data_ws.set_column(col_idx, col_idx, width)
    for row_idx, row in enumerate(sheet_rows, start=1):
        for col_idx, value in enumerate(row[:len(sheet_headers)]):
            write_cell(workbook, data_ws, row_idx, col_idx, value, formats)
    data_ws.freeze_panes(1, 0)
    data_ws.autofilter(0, 0, max(len(sheet_rows), 1), max(len(sheet_headers) - 1, 0))

    numeric_rows = numeric_profile(sheet_headers, sheet_rows, categories)
    selected_numeric = select_chart_numeric_columns(numeric_rows)
    status_rows = status_profile(sheet_headers, sheet_rows, select_status_columns(sheet_headers, sheet_rows, categories))
    insights = build_insights(sheet_rows, datetime_header, datetime_index, numeric_rows, selected_numeric, status_rows)
    if len(typed_rows) > EXCEL_MAX_DATA_ROWS or len(headers) > EXCEL_MAX_COLUMNS:
        insights.append("Data sheet was capped at Excel's worksheet row/column limit. Analysis calculations used the loaded CSV data.")

    analysis_ws = workbook.add_worksheet("Analysis")
    analysis_ws.set_column("A:A", 24)
    analysis_ws.set_column("B:B", 34)
    analysis_ws.set_column("C:H", 16)
    analysis_ws.merge_range("A1:H1", "HBL-BMS Trending Analysis", title_fmt)
    analysis_ws.set_row(0, 28)

    equipment = detect_equipment_keywords(sheet_headers)
    overview = [
        ("File", uploaded.filename),
        ("Rows", len(typed_rows)),
        ("Columns", len(headers)),
        ("Data sheet rows written", len(sheet_rows)),
        ("Data sheet columns written", len(sheet_headers)),
        ("Date/time column", datetime_header or "Not detected"),
        ("Detected systems", ", ".join(equipment) if equipment else "No strong equipment keyword detected"),
    ]
    analysis_ws.write("A3", "File Overview", section_fmt)
    analysis_ws.write("B3", "Value", section_fmt)
    for row, (label, value) in enumerate(overview, start=3):
        analysis_ws.write(row, 0, label, label_fmt)
        analysis_ws.write(row, 1, value, value_fmt)

    analysis_ws.write("A10", "Engineering Notes", section_fmt)
    analysis_ws.write("B10", "Insight", section_fmt)
    for row, insight in enumerate(insights, start=10):
        analysis_ws.write(row, 0, row - 9, label_fmt)
        analysis_ws.write(row, 1, insight, value_fmt)

    table_row = 3
    analysis_ws.write(table_row - 1, 3, "Numeric Analysis", section_fmt)
    headers_for_table = ["Column", "Category", "Avg", "Min", "Max", "Latest", "Change", "Std Dev"]
    for col_offset, header in enumerate(headers_for_table, start=3):
        analysis_ws.write(table_row, col_offset, header, section_fmt)
    for row_offset, item in enumerate(numeric_rows[:12], start=table_row + 1):
        analysis_ws.write(row_offset, 3, item["column"], value_fmt)
        analysis_ws.write(row_offset, 4, item["category"], value_fmt)
        analysis_ws.write_number(row_offset, 5, item["average"] or 0, num_fmt)
        analysis_ws.write_number(row_offset, 6, item["min"] or 0, num_fmt)
        analysis_ws.write_number(row_offset, 7, item["max"] or 0, num_fmt)
        analysis_ws.write_number(row_offset, 8, item["latest"] or 0, num_fmt)
        analysis_ws.write_number(row_offset, 9, item["change"] or 0, num_fmt)
        analysis_ws.write_number(row_offset, 10, item["std_dev"] or 0, num_fmt)

    status_start = max(20, table_row + len(numeric_rows[:12]) + 3)
    analysis_ws.write(status_start, 0, "Status Analysis", section_fmt)
    for col_offset, header in enumerate(["Column", "States", "Top State", "Top State %", "Active Events", "Active %"]):
        analysis_ws.write(status_start + 1, col_offset, header, section_fmt)
    for row_offset, item in enumerate(status_rows[:8], start=status_start + 2):
        analysis_ws.write(row_offset, 0, item["column"], value_fmt)
        analysis_ws.write_number(row_offset, 1, item["states"], value_fmt)
        analysis_ws.write(row_offset, 2, item["top_state"], value_fmt)
        analysis_ws.write_number(row_offset, 3, item["top_state_pct"], pct_fmt)
        analysis_ws.write_number(row_offset, 4, item["active_events"], value_fmt)
        analysis_ws.write_number(row_offset, 5, item["active_pct"], pct_fmt)

    chart_rows = min(len(sheet_rows), 300)
    if datetime_index is not None and selected_numeric and chart_rows > 1:
        chart = workbook.add_chart({"type": "line"})
        for item in selected_numeric:
            col_idx = item["index"]
            chart.add_series({
                "name": ["Data", 0, col_idx],
                "categories": ["Data", 1, datetime_index, chart_rows, datetime_index],
                "values": ["Data", 1, col_idx, chart_rows, col_idx],
            })
        chart.set_title({"name": "Main Time-Series Trends"})
        chart.set_x_axis({"name": datetime_header or "Time"})
        chart.set_y_axis({"name": "Value"})
        chart.set_legend({"position": "bottom"})
        analysis_ws.insert_chart("A31", chart, {"x_scale": 1.55, "y_scale": 1.2})

    if selected_numeric:
        chart_table_row = 68
        analysis_ws.write(chart_table_row, 8, "Metric", section_fmt)
        analysis_ws.write(chart_table_row, 9, "Average", section_fmt)
        for offset, item in enumerate(selected_numeric, start=1):
            analysis_ws.write(chart_table_row + offset, 8, item["column"], value_fmt)
            analysis_ws.write_number(chart_table_row + offset, 9, item["average"] or 0, num_fmt)
        chart = workbook.add_chart({"type": "column"})
        chart.add_series({
            "name": "Average",
            "categories": ["Analysis", chart_table_row + 1, 8, chart_table_row + len(selected_numeric), 8],
            "values": ["Analysis", chart_table_row + 1, 9, chart_table_row + len(selected_numeric), 9],
        })
        chart.set_title({"name": "Numeric Average Comparison"})
        chart.set_x_axis({"name": "Metric"})
        chart.set_y_axis({"name": "Average"})
        analysis_ws.insert_chart("I31", chart, {"x_scale": 1.25, "y_scale": 1.2})

    workbook.close()
    output.seek(0)
    return output.read()


def create_excel_with_analysis(uploaded: UploadedFile, deadline: float | None = None, max_file_bytes: int | None = MAX_SINGLE_FILE_BYTES) -> bytes:
    deadline = deadline or (time.monotonic() + MAX_RUNTIME_SECONDS)
    headers, rows, _delimiter = parse_csv_bytes(uploaded.data, deadline, max_file_bytes)
    headers, typed_rows, categories, datetime_index, datetime_header = coerce_rows(headers, rows, deadline)
    return write_workbook(uploaded, headers, typed_rows, categories, datetime_index, datetime_header)


def convert_files_to_zip(uploaded_files: list[UploadedFile], deadline: float | None = None) -> tuple[bytes, int, int, list[str]]:
    if len(uploaded_files) > MAX_UPLOAD_FILES:
        raise ConversionError(f"Upload at most {MAX_UPLOAD_FILES} CSV files per request.")

    deadline = deadline or (time.monotonic() + MAX_RUNTIME_SECONDS)
    zip_buffer = io.BytesIO()
    report_lines = [
        "HBL-BMS Trending Conversion Report",
        f"Generated: {now_iso()}",
        f"Files received: {len(uploaded_files)}",
        "",
    ]
    success_count = 0
    fail_count = 0

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for uploaded in uploaded_files:
            if time.monotonic() > deadline:
                fail_count += 1
                report_lines.append(f"FAILED: {uploaded.filename} - serverless time limit reached before processing this file")
                continue
            filename = uploaded.filename or "uploaded.csv"
            if not filename.lower().endswith(".csv"):
                fail_count += 1
                report_lines.append(f"FAILED: {filename} - not a CSV file")
                continue
            try:
                workbook_bytes = create_excel_with_analysis(uploaded, deadline)
                output_name = re.sub(r"\.csv$", "", Path(filename).name, flags=re.IGNORECASE) + ".xlsx"
                archive.writestr(output_name, workbook_bytes)
                success_count += 1
                report_lines.append(f"OK: {filename} -> {output_name} ({len(workbook_bytes):,} bytes)")
            except Exception as exc:
                fail_count += 1
                report_lines.append(f"FAILED: {filename} - {exc}")

        report_lines.extend(["", f"Successful files: {success_count}", f"Failed files: {fail_count}"])
        archive.writestr("conversion_report.txt", "\n".join(report_lines))

    zip_buffer.seek(0)
    return zip_buffer.read(), success_count, fail_count, report_lines


def parse_multipart_uploads(handler_instance: BaseHTTPRequestHandler, content_length: int) -> list[UploadedFile]:
    content_type = handler_instance.headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        raise ConversionError("Expected multipart/form-data upload.")
    body = handler_instance.rfile.read(content_length)
    message_bytes = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n"
        "\r\n"
    ).encode("utf-8") + body
    message = BytesParser(policy=policy.default).parsebytes(message_bytes)
    uploaded_files: list[UploadedFile] = []
    for part in message.iter_parts():
        field_name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        if field_name not in {"files", "files[]"} or not filename:
            continue
        data = part.get_payload(decode=True) or b""
        uploaded_files.append(UploadedFile(Path(filename).name, data))
    return uploaded_files


def json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


def add_common_headers(handler_instance: BaseHTTPRequestHandler, content_type: str) -> None:
    handler_instance.send_header("Content-Type", content_type)
    handler_instance.send_header("Access-Control-Allow-Origin", "*")
    handler_instance.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler_instance.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler_instance.send_header("Access-Control-Allow-Private-Network", "true")


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self) -> None:
        self.send_response(204)
        add_common_headers(self, "text/plain")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/local/status":
            self._send_json(200, {"ok": True, "service": "HBL-BMS Trending Local Converter", "time": now_iso()})
            return
        self._send_text(405, "Use POST /api/convert with multipart CSV files.")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/local/pick-files":
            self._handle_local_picker("files")
            return
        if path == "/api/local/pick-folder":
            self._handle_local_picker("folder")
            return
        if path == "/api/local/convert":
            self._handle_local_convert()
            return
        if path not in {"/", "/api/convert", "/api/convert.py"}:
            self._send_text(404, "Not found.")
            return

        content_length = int(self.headers.get("Content-Length") or 0)
        if content_length <= 0:
            self._send_text(400, "No request body was received.")
            return
        if content_length > MAX_VERCEL_BODY_BYTES:
            self._send_text(413, "Uploaded files exceed Vercel's 4.5 MB serverless request limit. Split the CSV or use Local Folder mode.")
            return

        try:
            uploads = parse_multipart_uploads(self, content_length)
            if not uploads:
                self._send_text(400, "No CSV files were uploaded.")
                return
            deadline = time.monotonic() + MAX_RUNTIME_SECONDS
            zip_bytes, success_count, _fail_count, report_lines = convert_files_to_zip(uploads, deadline)
            if success_count == 0:
                self._send_text(422, "\n".join(report_lines))
                return
            self.send_response(200)
            add_common_headers(self, "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="hbl-bms-trend-analysis.zip"')
            self.send_header("Content-Length", str(len(zip_bytes)))
            self.end_headers()
            self.wfile.write(zip_bytes)
        except Exception as exc:
            self._send_text(500, f"Conversion failed: {exc}")

    def _handle_local_picker(self, mode: str) -> None:
        try:
            paths = open_native_picker(mode)
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"Unable to open local picker: {exc}"})
            return
        self._send_json(200, {"ok": True, "paths": paths})

    def _handle_local_convert(self) -> None:
        content_length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(content_length) if content_length else b"{}"
        payload = json.loads(raw.decode("utf-8") or "{}")
        raw_paths = payload.get("paths") or []
        if isinstance(raw_paths, str):
            raw_paths = [line for line in raw_paths.splitlines() if line.strip()]
        if not isinstance(raw_paths, list):
            self._send_json(400, {"ok": False, "error": "paths must be a list of CSV file paths or folder paths."})
            return
        input_paths = resolve_local_csv_paths([str(value) for value in raw_paths])
        if not input_paths:
            self._send_json(400, {"ok": False, "error": "No CSV files found. Paste a CSV file path or a folder containing CSV files."})
            return
        output_dir_value = str(payload.get("outputDir") or "").strip().strip('"').strip("'")
        output_dir = Path(output_dir_value).expanduser() if output_dir_value else input_paths[0].parent
        output_zip = output_dir / "hbl-bms-trend-analysis.zip"
        try:
            success_count, fail_count = convert_local_files(input_paths, output_zip)
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})
            return
        self._send_json(200, {
            "ok": success_count > 0,
            "outputZip": str(output_zip.resolve()),
            "successfulFiles": success_count,
            "failedFiles": fail_count,
            "inputFiles": [str(path) for path in input_paths],
        })

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json_bytes(payload)
        self.send_response(status)
        add_common_headers(self, "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        add_common_headers(self, "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def resolve_local_csv_paths(values: list[str]) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for value in values:
        text = value.strip().strip('"').strip("'")
        if not text:
            continue
        path = Path(text).expanduser()
        candidates: list[Path]
        if path.is_dir():
            candidates = sorted(item for item in path.iterdir() if item.is_file() and item.suffix.lower() == ".csv")
        else:
            candidates = [path]
        for candidate in candidates:
            resolved = candidate.resolve() if candidate.exists() else candidate
            if resolved not in seen:
                seen.add(resolved)
                paths.append(resolved)
    return paths


def convert_local_files(input_paths: list[Path], output_zip: Path) -> tuple[int, int]:
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    report_lines = [
        "HBL-BMS Trending Local Conversion Report",
        f"Generated: {now_iso()}",
        "",
    ]
    success_count = 0
    fail_count = 0

    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in input_paths:
            print(f"Processing {path}...")
            if not path.exists():
                fail_count += 1
                report_lines.append(f"FAILED: {path} - file not found")
                continue
            if path.suffix.lower() != ".csv":
                fail_count += 1
                report_lines.append(f"FAILED: {path.name} - not a CSV file")
                continue
            if path.stat().st_size > MAX_LOCAL_FILE_BYTES:
                fail_count += 1
                report_lines.append(f"FAILED: {path.name} - file is {path.stat().st_size:,} bytes; local limit is {MAX_LOCAL_FILE_BYTES:,} bytes")
                print(f"  FAILED: file exceeds local limit")
                continue
            try:
                raw = path.read_bytes()
                workbook_bytes = create_excel_with_analysis(UploadedFile(path.name, raw), time.monotonic() + 300, MAX_LOCAL_FILE_BYTES)
                output_name = f"{path.stem}.xlsx"
                archive.writestr(output_name, workbook_bytes)
                success_count += 1
                report_lines.append(f"OK: {path.name} -> {output_name} ({len(workbook_bytes):,} bytes)")
                print(f"  OK -> {output_name} ({len(workbook_bytes):,} bytes)")
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
        files = filedialog.askopenfilenames(
            title="Select CSV files",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        return list(files)
    finally:
        root.destroy()


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert local BMS / ACMV trend CSV files to analyzed Excel workbooks.")
    parser.add_argument("inputs", nargs="*", help="CSV file paths or folder paths to convert locally.")
    parser.add_argument("--out", default="hbl-bms-trend-analysis.zip", help="Output ZIP path.")
    parser.add_argument("--serve-local", action="store_true", help="Run the local companion API for the web UI.")
    parser.add_argument("--host", default="127.0.0.1", help="Local companion API host.")
    parser.add_argument("--port", type=int, default=LOCAL_SERVER_DEFAULT_PORT, help="Local companion API port.")
    args = parser.parse_args()

    if args.serve_local:
        server = HTTPServer((args.host, args.port), handler)
        print(f"HBL-BMS Trending local companion API running at http://{args.host}:{args.port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping local companion API.")
        return 0

    input_paths = resolve_local_csv_paths(args.inputs)
    if not input_paths:
        parser.error("Provide at least one CSV file path or folder path, or use --serve-local.")

    success_count, fail_count = convert_local_files(input_paths, Path(args.out))
    print(f"Done. Successful files: {success_count}. Failed files: {fail_count}.")
    print(f"Output: {Path(args.out).resolve()}")
    return 0 if success_count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
