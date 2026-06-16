from __future__ import annotations

import io
import math
import re
import zipfile
import argparse
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from flask import Flask, Response, request


app = Flask(__name__)


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
MAX_VERCEL_BODY_BYTES = int(4.5 * 1024 * 1024)


def detect_delimiter(raw: bytes) -> str:
    sample = raw[:65536].decode("utf-8-sig", errors="replace")
    first_line = sample.splitlines()[0] if sample.splitlines() else ""
    candidates = [";", ",", "\t", "|"]
    counts = {delimiter: first_line.count(delimiter) for delimiter in candidates}
    delimiter, count = max(counts.items(), key=lambda item: item[1])
    return delimiter if count > 0 else ","


def normalize_column_name(name: Any) -> str:
    text = str(name or "").lower()
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"[\r\n]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return f" {text} "


def classify_column(column_name: str, series: pd.Series) -> str:
    normalized = normalize_column_name(column_name)
    if any(token in normalized for token in [" date ", " time ", " timestamp ", " datetime "]):
        parsed = pd.to_datetime(series, errors="coerce", format="mixed")
        if parsed.notna().mean() >= 0.5:
            return "datetime"

    for category, keywords in CATEGORY_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            return category

    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().mean() >= 0.7:
        return "unknown_numeric"
    return "unknown_text"


def detect_datetime_column(df: pd.DataFrame) -> tuple[pd.DataFrame, str | None]:
    columns = list(df.columns)
    normalized = {col: normalize_column_name(col) for col in columns}

    date_candidates = [col for col in columns if " date " in normalized[col]]
    time_candidates = [col for col in columns if " time " in normalized[col] and "date" not in normalized[col]]
    if date_candidates and time_candidates:
        combined = pd.to_datetime(
            df[date_candidates[0]].astype(str).str.strip() + " " + df[time_candidates[0]].astype(str).str.strip(),
            errors="coerce",
            format="mixed",
        )
        if combined.notna().mean() >= 0.5:
            df = df.copy()
            df.insert(0, "Detected DateTime", combined)
            return df, "Detected DateTime"

    scored: list[tuple[float, str, pd.Series]] = []
    for col in columns:
        parsed = pd.to_datetime(df[col], errors="coerce", format="mixed")
        pct = float(parsed.notna().mean()) if len(df) else 0
        name_bonus = 0.25 if any(token in normalized[col] for token in [" date ", " time ", " timestamp ", " datetime "]) else 0
        if pct >= 0.5:
            scored.append((pct + name_bonus, col, parsed))

    if not scored:
        return df, None

    scored.sort(reverse=True, key=lambda item: item[0])
    _, col, parsed = scored[0]
    df = df.copy()
    df[col] = parsed
    df = df.sort_values(col, kind="stable").reset_index(drop=True)
    return df, col


def coerce_dataframe(df: pd.DataFrame, datetime_col: str | None) -> tuple[pd.DataFrame, dict[str, str]]:
    df = df.copy()
    categories: dict[str, str] = {}
    for col in df.columns:
        if col == datetime_col:
            categories[col] = "datetime"
            continue

        category = classify_column(col, df[col])
        categories[col] = category
        numeric = pd.to_numeric(df[col].astype(str).str.replace(",", "", regex=False), errors="coerce")
        if numeric.notna().mean() >= 0.75:
            df[col] = numeric
        else:
            parsed_dt = pd.to_datetime(df[col], errors="coerce", format="mixed")
            if parsed_dt.notna().mean() >= 0.85:
                df[col] = parsed_dt
                categories[col] = "datetime"
    return df, categories


def is_binary_like(series: pd.Series) -> bool:
    values = series.dropna()
    if values.empty:
        return False
    normalized = {str(value).strip().lower() for value in values.unique()}
    return len(normalized) <= 4 and normalized.issubset({"0", "1", "true", "false", "on", "off", "run", "stop", "open", "close", "normal", "alarm"})


def numeric_profile(df: pd.DataFrame, categories: dict[str, str]) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    row_count = max(len(df), 1)
    for col in df.columns:
        if not pd.api.types.is_numeric_dtype(df[col]):
            continue
        values = df[col].dropna()
        non_empty_pct = len(values) / row_count
        unique_count = int(values.nunique(dropna=True))
        std_dev = float(values.std()) if len(values) > 1 else 0.0
        minimum = float(values.min()) if not values.empty else None
        maximum = float(values.max()) if not values.empty else None
        average = float(values.mean()) if not values.empty else None
        first = float(values.iloc[0]) if not values.empty else None
        latest = float(values.iloc[-1]) if not values.empty else None
        change = latest - first if first is not None and latest is not None else None
        category = categories.get(col, "unknown_numeric")
        binary = is_binary_like(df[col])
        mostly_constant = unique_count <= 1 or std_dev < 0.000001
        score = PREFERRED_NUMERIC_CATEGORIES.get(category, 4)
        score += min(unique_count, 20) * 0.4
        score += min(non_empty_pct, 1) * 10
        if mostly_constant:
            score -= 20
        if binary:
            score -= 12
        profiles.append(
            {
                "column": col,
                "category": category,
                "non_empty_pct": non_empty_pct,
                "unique_count": unique_count,
                "std_dev": std_dev,
                "min": minimum,
                "max": maximum,
                "average": average,
                "first": first,
                "latest": latest,
                "change": change,
                "mostly_constant": mostly_constant,
                "binary_like": binary,
                "score": score,
            }
        )
    return profiles


def select_chart_numeric_columns(profiles: list[dict[str, Any]]) -> list[str]:
    eligible = [
        item for item in profiles
        if item["non_empty_pct"] >= 0.5 and not item["mostly_constant"] and not item["binary_like"]
    ]
    eligible.sort(key=lambda item: item["score"], reverse=True)
    return [item["column"] for item in eligible[:5]]


def select_status_columns(df: pd.DataFrame, categories: dict[str, str]) -> list[str]:
    status_categories = {"command", "status", "alarm_trip_lockout", "valve", "pump", "fan", "chiller", "ahu", "fcu", "cooling_tower"}
    selected: list[str] = []
    for col in df.columns:
        if categories.get(col) in status_categories and is_binary_like(df[col]):
            selected.append(col)
        elif categories.get(col) in {"command", "status", "alarm_trip_lockout"}:
            selected.append(col)
    return selected[:6]


def detect_equipment_keywords(columns: list[str]) -> list[str]:
    joined = " ".join(normalize_column_name(col) for col in columns)
    found = []
    for keyword in EQUIPMENT_KEYWORDS:
        if keyword in joined and keyword.upper() not in found:
            found.append(keyword.upper() if keyword in {"ahu", "fcu", "vsd", "vfd", "chw", "cdw"} else keyword.title())
    return found


def status_profile(df: pd.DataFrame, status_columns: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    total_rows = max(len(df), 1)
    for col in status_columns:
        counts = Counter(str(value).strip() for value in df[col].dropna() if str(value).strip())
        if not counts:
            continue
        most_common = counts.most_common(1)[0]
        active_count = sum(count for value, count in counts.items() if value.strip().lower() in ACTIVE_STATUS_VALUES)
        rows.append(
            {
                "column": col,
                "states": len(counts),
                "top_state": most_common[0],
                "top_state_pct": most_common[1] / total_rows,
                "active_events": active_count,
                "active_pct": active_count / total_rows,
                "counts": counts,
            }
        )
    return rows


def build_insights(
    df: pd.DataFrame,
    datetime_col: str | None,
    numeric_profiles: list[dict[str, Any]],
    selected_numeric: list[str],
    status_rows: list[dict[str, Any]],
) -> list[str]:
    insights: list[str] = []
    if datetime_col:
        insights.append(f"Date/time trend detected using '{datetime_col}'. Data was sorted by detected timestamp where possible.")
    else:
        insights.append("No reliable date/time column detected; time-series analysis is limited.")

    for col in selected_numeric[:3]:
        profile = next((item for item in numeric_profiles if item["column"] == col), None)
        if not profile:
            continue
        max_value = profile["max"]
        if datetime_col and max_value is not None and col in df:
            matching = df[df[col] == max_value]
            when = matching[datetime_col].iloc[0] if not matching.empty and datetime_col in matching else ""
            insights.append(f"{profile['category'].replace('_', ' ').title()} trend detected. Highest '{col}' observed at {when} was {max_value:.2f}.")
        elif max_value is not None:
            insights.append(f"{profile['category'].replace('_', ' ').title()} trend detected in '{col}' with max {max_value:.2f} and average {profile['average']:.2f}.")

    if any(row["active_events"] > 0 for row in status_rows):
        alarm_cols = [row["column"] for row in status_rows if row["active_events"] > 0]
        insights.append(f"Status/alarm activity detected in: {', '.join(alarm_cols[:4])}.")

    if selected_numeric:
        insights.append("Analog trend columns were selected based on ACMV keyword priority, data completeness, and meaningful variation.")
    else:
        insights.append("No suitable analog time-series numeric columns were found. Data appears mostly status-based or constant.")

    return insights[:8]


def clean_sheet_name(name: str) -> str:
    return re.sub(r"[\[\]\*\/\\\?:]", " ", name)[:31] or "Sheet"


def write_data_sheet(workbook: Any, df: pd.DataFrame) -> Any:
    worksheet = workbook.add_worksheet("Data")
    header_fmt = workbook.add_format({"bold": True, "bg_color": "#1F2937", "font_color": "white", "text_wrap": True, "valign": "vcenter", "border": 1})
    text_fmt = workbook.add_format({"valign": "top", "border": 0})
    date_fmt = workbook.add_format({"num_format": "yyyy-mm-dd hh:mm", "valign": "top"})
    number_fmt = workbook.add_format({"num_format": "#,##0.00", "valign": "top"})

    for col_idx, col in enumerate(df.columns):
        worksheet.write(0, col_idx, col, header_fmt)
        sample = df[col].astype(str).head(100).tolist()
        width = min(max(len(str(col)) + 2, *(len(value) + 1 for value in sample)), 32)
        worksheet.set_column(col_idx, col_idx, width)

    for row_idx, row in enumerate(df.itertuples(index=False), start=1):
        for col_idx, value in enumerate(row):
            if pd.isna(value):
                worksheet.write_blank(row_idx, col_idx, None, text_fmt)
            elif isinstance(value, pd.Timestamp):
                worksheet.write_datetime(row_idx, col_idx, value.to_pydatetime(), date_fmt)
            elif isinstance(value, datetime):
                worksheet.write_datetime(row_idx, col_idx, value, date_fmt)
            elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
                worksheet.write_number(row_idx, col_idx, float(value), number_fmt)
            else:
                worksheet.write(row_idx, col_idx, str(value), text_fmt)

    worksheet.freeze_panes(1, 0)
    worksheet.autofilter(0, 0, max(len(df), 1), max(len(df.columns) - 1, 0))
    return worksheet


def build_helper_data(
    df: pd.DataFrame,
    datetime_col: str | None,
    selected_numeric: list[str],
    numeric_profiles: list[dict[str, Any]],
    status_rows: list[dict[str, Any]],
) -> tuple[list[list[Any]], dict[str, Any]]:
    rows: list[list[Any]] = []
    refs: dict[str, Any] = {}

    if datetime_col and selected_numeric:
        refs["timeseries_start"] = len(rows)
        rows.append([datetime_col, *selected_numeric])
        max_rows = min(len(df), 300)
        for _, record in df[[datetime_col, *selected_numeric]].head(max_rows).iterrows():
            rows.append([record.get(datetime_col), *[record.get(col) for col in selected_numeric]])
        refs["timeseries_rows"] = max_rows
        rows.append([])

    avg_profiles = [item for item in numeric_profiles if item["column"] in selected_numeric]
    if avg_profiles:
        refs["avg_start"] = len(rows)
        rows.append(["Metric", "Average"])
        for item in avg_profiles:
            rows.append([item["column"], item["average"]])
        refs["avg_rows"] = len(avg_profiles)
        rows.append([])

    if status_rows:
        first = status_rows[0]
        refs["status_start"] = len(rows)
        rows.append(["State", "Count"])
        for state, count in first["counts"].most_common(10):
            rows.append([state, count])
        refs["status_rows"] = min(len(first["counts"]), 10)
        refs["status_column"] = first["column"]

    return rows, refs


def write_helper_sheet(workbook: Any, rows: list[list[Any]]) -> Any:
    worksheet = workbook.add_worksheet("_ChartHelper")
    date_fmt = workbook.add_format({"num_format": "yyyy-mm-dd hh:mm"})
    for row_idx, row in enumerate(rows):
        for col_idx, value in enumerate(row):
            if pd.isna(value):
                worksheet.write_blank(row_idx, col_idx, None)
            elif isinstance(value, pd.Timestamp):
                worksheet.write_datetime(row_idx, col_idx, value.to_pydatetime(), date_fmt)
            elif isinstance(value, datetime):
                worksheet.write_datetime(row_idx, col_idx, value, date_fmt)
            elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
                worksheet.write_number(row_idx, col_idx, float(value))
            else:
                worksheet.write(row_idx, col_idx, value)
    worksheet.hide()
    return worksheet


def write_analysis_sheet(
    workbook: Any,
    df: pd.DataFrame,
    file_name: str,
    datetime_col: str | None,
    categories: dict[str, str],
    numeric_profiles: list[dict[str, Any]],
    selected_numeric: list[str],
    status_rows: list[dict[str, Any]],
    helper_refs: dict[str, Any],
    insights: list[str],
) -> None:
    worksheet = workbook.add_worksheet("Analysis")
    title_fmt = workbook.add_format({"bold": True, "font_size": 16, "font_color": "white", "bg_color": "#0F172A", "align": "center", "valign": "vcenter"})
    section_fmt = workbook.add_format({"bold": True, "font_color": "white", "bg_color": "#334155", "border": 1})
    label_fmt = workbook.add_format({"bold": True, "bg_color": "#F1F5F9", "border": 1})
    value_fmt = workbook.add_format({"border": 1, "text_wrap": True, "valign": "top"})
    pct_fmt = workbook.add_format({"border": 1, "num_format": "0.0%"})
    num_fmt = workbook.add_format({"border": 1, "num_format": "#,##0.00"})

    worksheet.set_column("A:A", 24)
    worksheet.set_column("B:B", 34)
    worksheet.set_column("C:H", 16)
    worksheet.merge_range("A1:H1", "HBL-BMS Trending Analysis", title_fmt)
    worksheet.set_row(0, 28)

    equipment = detect_equipment_keywords(list(df.columns))
    overview = [
        ("File", file_name),
        ("Rows", len(df)),
        ("Columns", len(df.columns)),
        ("Date/time column", datetime_col or "Not detected"),
        ("Detected systems", ", ".join(equipment) if equipment else "No strong equipment keyword detected"),
    ]
    worksheet.write("A3", "File Overview", section_fmt)
    worksheet.write("B3", "Value", section_fmt)
    for row, (label, value) in enumerate(overview, start=3):
        worksheet.write(row, 0, label, label_fmt)
        worksheet.write(row, 1, value, value_fmt)

    worksheet.write("A10", "Engineering Notes", section_fmt)
    worksheet.write("B10", "Insight", section_fmt)
    for row, insight in enumerate(insights, start=10):
        worksheet.write(row, 0, row - 9, label_fmt)
        worksheet.write(row, 1, insight, value_fmt)

    table_row = 3
    worksheet.write(table_row - 1, 3, "Numeric Analysis", section_fmt)
    headers = ["Column", "Category", "Avg", "Min", "Max", "Latest", "Change", "Std Dev"]
    for col_offset, header in enumerate(headers, start=3):
        worksheet.write(table_row, col_offset, header, section_fmt)
    for row_offset, item in enumerate(numeric_profiles[:12], start=table_row + 1):
        worksheet.write(row_offset, 3, item["column"], value_fmt)
        worksheet.write(row_offset, 4, item["category"], value_fmt)
        worksheet.write_number(row_offset, 5, item["average"] or 0, num_fmt)
        worksheet.write_number(row_offset, 6, item["min"] or 0, num_fmt)
        worksheet.write_number(row_offset, 7, item["max"] or 0, num_fmt)
        worksheet.write_number(row_offset, 8, item["latest"] or 0, num_fmt)
        worksheet.write_number(row_offset, 9, item["change"] or 0, num_fmt)
        worksheet.write_number(row_offset, 10, item["std_dev"] or 0, num_fmt)

    status_start = max(20, table_row + len(numeric_profiles[:12]) + 3)
    worksheet.write(status_start, 0, "Status Analysis", section_fmt)
    for col_offset, header in enumerate(["Column", "States", "Top State", "Top State %", "Active Events", "Active %"]):
        worksheet.write(status_start + 1, col_offset, header, section_fmt)
    for row_offset, item in enumerate(status_rows[:8], start=status_start + 2):
        worksheet.write(row_offset, 0, item["column"], value_fmt)
        worksheet.write_number(row_offset, 1, item["states"], value_fmt)
        worksheet.write(row_offset, 2, item["top_state"], value_fmt)
        worksheet.write_number(row_offset, 3, item["top_state_pct"], pct_fmt)
        worksheet.write_number(row_offset, 4, item["active_events"], value_fmt)
        worksheet.write_number(row_offset, 5, item["active_pct"], pct_fmt)

    if "timeseries_start" in helper_refs:
        start = helper_refs["timeseries_start"]
        rows = helper_refs["timeseries_rows"]
        chart = workbook.add_chart({"type": "line"})
        for idx, col in enumerate(selected_numeric):
            chart.add_series({
                "name": ["_ChartHelper", start, idx + 1],
                "categories": ["_ChartHelper", start + 1, 0, start + rows, 0],
                "values": ["_ChartHelper", start + 1, idx + 1, start + rows, idx + 1],
            })
        chart.set_title({"name": "Main Time-Series Trends"})
        chart.set_x_axis({"name": datetime_col or "Time"})
        chart.set_y_axis({"name": "Value"})
        chart.set_legend({"position": "bottom"})
        worksheet.insert_chart("A31", chart, {"x_scale": 1.55, "y_scale": 1.2})

    if "avg_start" in helper_refs:
        start = helper_refs["avg_start"]
        rows = helper_refs["avg_rows"]
        chart = workbook.add_chart({"type": "column"})
        chart.add_series({
            "name": "Average",
            "categories": ["_ChartHelper", start + 1, 0, start + rows, 0],
            "values": ["_ChartHelper", start + 1, 1, start + rows, 1],
        })
        chart.set_title({"name": "Numeric Average Comparison"})
        chart.set_x_axis({"name": "Metric"})
        chart.set_y_axis({"name": "Average"})
        worksheet.insert_chart("I31", chart, {"x_scale": 1.25, "y_scale": 1.2})

    if "status_start" in helper_refs:
        start = helper_refs["status_start"]
        rows = helper_refs["status_rows"]
        chart = workbook.add_chart({"type": "bar"})
        chart.add_series({
            "name": helper_refs.get("status_column", "Status"),
            "categories": ["_ChartHelper", start + 1, 0, start + rows, 0],
            "values": ["_ChartHelper", start + 1, 1, start + rows, 1],
        })
        chart.set_title({"name": "Status Count Distribution"})
        chart.set_x_axis({"name": "Count"})
        chart.set_y_axis({"name": "State"})
        worksheet.insert_chart("A48", chart, {"x_scale": 1.35, "y_scale": 1.15})


def create_excel_with_analysis(uploaded_file: Any) -> bytes:
    raw = uploaded_file.read()
    delimiter = detect_delimiter(raw)
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "cp1252", "latin1"):
        try:
            df = pd.read_csv(io.BytesIO(raw), sep=delimiter, engine="c", encoding=encoding, low_memory=False)
            break
        except UnicodeDecodeError as exc:
            last_error = exc
    else:
        raise last_error or ValueError("Unable to decode CSV file.")
    df = df.dropna(axis=0, how="all").dropna(axis=1, how="all")
    df.columns = [str(col).replace("\n", " ").strip() or f"Column_{idx + 1}" for idx, col in enumerate(df.columns)]
    df, datetime_col = detect_datetime_column(df)
    df, categories = coerce_dataframe(df, datetime_col)
    if datetime_col and datetime_col in df.columns:
        df = df.sort_values(datetime_col, kind="stable").reset_index(drop=True)

    numeric_rows = numeric_profile(df, categories)
    numeric_rows.sort(key=lambda item: item["score"], reverse=True)
    selected_numeric = select_chart_numeric_columns(numeric_rows)
    status_cols = select_status_columns(df, categories)
    status_rows = status_profile(df, status_cols)
    insights = build_insights(df, datetime_col, numeric_rows, selected_numeric, status_rows)
    helper_rows, helper_refs = build_helper_data(df, datetime_col, selected_numeric, numeric_rows, status_rows)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter", datetime_format="yyyy-mm-dd hh:mm", date_format="yyyy-mm-dd") as writer:
        workbook = writer.book
        write_data_sheet(workbook, df)
        write_analysis_sheet(workbook, df, uploaded_file.filename or "trend.csv", datetime_col, categories, numeric_rows, selected_numeric, status_rows, helper_refs, insights)
        write_helper_sheet(workbook, helper_rows)

    output.seek(0)
    return output.read()


class LocalUpload:
    def __init__(self, path: Path):
        self.path = path
        self.filename = path.name

    def read(self) -> bytes:
        return self.path.read_bytes()


def convert_local_files(input_paths: list[Path], output_zip: Path) -> tuple[int, int]:
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    report_lines = [
        "HBL-BMS Trending Local Conversion Report",
        f"Generated: {datetime.now(UTC).isoformat(timespec='seconds')}",
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

            try:
                workbook_bytes = create_excel_with_analysis(LocalUpload(path))
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


@app.post("/api/convert")
@app.post("/")
def convert() -> Response:
    content_length = request.content_length or 0
    if content_length > MAX_VERCEL_BODY_BYTES:
        return Response(
            "Uploaded files exceed Vercel's 4.5 MB serverless request limit. Split the CSV or run the Python backend on a large-file host.",
            status=413,
            mimetype="text/plain",
        )

    uploaded_files = request.files.getlist("files") or request.files.getlist("files[]")
    if not uploaded_files:
        return Response("No CSV files were uploaded.", status=400, mimetype="text/plain")

    zip_buffer = io.BytesIO()
    report_lines = [
        "HBL-BMS Trending Conversion Report",
        f"Generated: {datetime.now(UTC).isoformat(timespec='seconds')}",
        "",
    ]
    success_count = 0
    fail_count = 0

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for uploaded in uploaded_files:
            filename = uploaded.filename or "uploaded.csv"
            if not filename.lower().endswith(".csv"):
                fail_count += 1
                report_lines.append(f"FAILED: {filename} - not a CSV file")
                continue
            try:
                workbook_bytes = create_excel_with_analysis(uploaded)
                output_name = re.sub(r"\.csv$", "", filename, flags=re.IGNORECASE) + ".xlsx"
                archive.writestr(output_name, workbook_bytes)
                success_count += 1
                report_lines.append(f"OK: {filename} -> {output_name}")
            except Exception as exc:  # Keep batch conversion moving.
                fail_count += 1
                report_lines.append(f"FAILED: {filename} - {exc}")

        report_lines.extend(["", f"Successful files: {success_count}", f"Failed files: {fail_count}"])
        archive.writestr("conversion_report.txt", "\n".join(report_lines))

    zip_buffer.seek(0)
    response = Response(zip_buffer.read(), mimetype="application/zip")
    response.headers["Content-Disposition"] = 'attachment; filename="hbl-bms-trend-analysis.zip"'
    return response


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert local BMS / ACMV trend CSV files to analyzed Excel workbooks.")
    parser.add_argument("inputs", nargs="+", help="CSV file paths to convert locally.")
    parser.add_argument("--out", default="hbl-bms-trend-analysis.zip", help="Output ZIP path.")
    args = parser.parse_args()

    success_count, fail_count = convert_local_files([Path(path) for path in args.inputs], Path(args.out))
    print(f"Done. Successful files: {success_count}. Failed files: {fail_count}.")
    print(f"Output: {Path(args.out).resolve()}")
    return 0 if success_count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
