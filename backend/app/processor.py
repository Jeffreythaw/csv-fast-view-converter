from __future__ import annotations

import csv
import math
import sqlite3
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

EXCEL_MAX_ROWS = 1_048_576
EXCEL_DATA_ROWS_PER_SHEET = EXCEL_MAX_ROWS - 1
MAX_COLUMNS = 300
STATUS_LIMIT = 50

OutputFormat = Literal["xlsx", "sqlite", "parquet"]

TRUE_VALUES = {"1", "true", "on", "run", "running", "open", "start", "enabled", "alarm"}
FALSE_VALUES = {"0", "false", "off", "stop", "stopped", "closed", "close", "disabled", "normal"}
ACTIVE_STATUS_VALUES = {"alarm", "trip", "fault", "lockout", "fail", "failure", "warning", "on", "true", "run", "running", "open", "start", "enabled", "1"}

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


@dataclass
class NumericStat:
    column: str
    index: int
    category: str
    count: int = 0
    mean: float = 0.0
    m2: float = 0.0
    minimum: float | None = None
    maximum: float | None = None
    first: float | None = None
    latest: float | None = None

    def add(self, value: float) -> None:
        if self.first is None:
            self.first = value
        self.latest = value
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self.m2 += delta * (value - self.mean)
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)

    @property
    def std_dev(self) -> float:
        return math.sqrt(self.m2 / self.count) if self.count > 1 else 0.0

    @property
    def change(self) -> float:
        if self.first is None or self.latest is None:
            return 0.0
        return self.latest - self.first


@dataclass
class ProcessResult:
    rows_read: int
    output_files: list[Path]
    report: str


def normalize_name(value: Any) -> str:
    text = str(value or "").lower().replace("_", " ").replace("-", " ")
    return f" {' '.join(text.split())} "


def classify_column(header: str) -> str:
    normalized = normalize_name(header)
    if any(token in normalized for token in [" date ", " time ", " timestamp ", " datetime "]):
        return "datetime"
    for category, keywords in CATEGORY_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            return category
    return "unknown"


def clean_header(value: Any, index: int) -> str:
    text = " ".join(str(value or "").replace("\ufeff", "").split())
    return text or f"Column_{index + 1}"


def unique_headers(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    unique: list[str] = []
    for index, header in enumerate(headers):
        base = header or f"Column_{index + 1}"
        count = seen.get(base, 0)
        seen[base] = count + 1
        unique.append(base if count == 0 else f"{base}_{count + 1}")
    return unique


def safe_table_name(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char == "_" else "_" for char in value.strip().lower())
    return cleaned or "trend_data"


def unique_sql_names(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    names: list[str] = []
    for index, header in enumerate(headers):
        base = safe_table_name(header) or f"column_{index + 1}"
        if base[0].isdigit():
            base = f"column_{base}"
        count = seen.get(base, 0)
        seen[base] = count + 1
        names.append(base if count == 0 else f"{base}_{count + 1}")
    return names


def quote_sqlite_identifier(value: str) -> str:
    return f'"{value.replace("\"", "\"\"")}"'


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


def detect_encoding(path: Path) -> str:
    sample = path.read_bytes()[:65536]
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        try:
            sample.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "latin1"


def detect_dialect(path: Path, encoding: str) -> csv.Dialect:
    with path.open("r", encoding=encoding, errors="replace", newline="") as handle:
        sample = handle.read(65536)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def iter_csv(path: Path):
    encoding = detect_encoding(path)
    dialect = detect_dialect(path, encoding)
    with path.open("r", encoding=encoding, errors="replace", newline="") as handle:
        reader = csv.reader(handle, dialect)
        for row in reader:
            if row and any(str(cell).strip() for cell in row):
                yield row


def fit_row(raw_row: list[str], column_count: int) -> list[str]:
    row = [str(cell).strip() for cell in raw_row[:column_count]]
    if len(row) < column_count:
        row.extend([""] * (column_count - len(row)))
    return row


def new_stats(headers: list[str]) -> tuple[list[NumericStat], list[Counter[str]]]:
    stats = [NumericStat(header, index, classify_column(header)) for index, header in enumerate(headers)]
    counters = [Counter() for _ in headers]
    return stats, counters


def update_profiles(row: list[str], stats: list[NumericStat], counters: list[Counter[str]]) -> None:
    for index, value in enumerate(row):
        number = parse_number(value)
        if number is not None:
            stats[index].add(number)
        category = stats[index].category
        if category in {"status", "command", "alarm_trip_lockout", "pump", "fan", "chiller", "ahu", "fcu", "valve"}:
            text = value.strip()
            if text and len(counters[index]) <= STATUS_LIMIT:
                counters[index][text] += 1


def build_report(source: Path, rows_read: int, output_files: list[Path], stats: list[NumericStat], counters: list[Counter[str]]) -> str:
    lines = [
        "CSV Fast View Conversion Report",
        f"Source: {source.name}",
        f"Rows read: {rows_read:,}",
        f"Output files: {len(output_files)}",
        "",
        "Numeric columns:",
    ]
    for stat in sorted((item for item in stats if item.count), key=lambda item: item.count, reverse=True)[:20]:
        lines.append(
            f"- {stat.column}: count={stat.count:,}, avg={stat.mean:.4f}, min={(stat.minimum or 0):.4f}, max={(stat.maximum or 0):.4f}, latest={(stat.latest or 0):.4f}"
        )
    lines.extend(["", "Status columns:"])
    for stat, counter in zip(stats, counters, strict=False):
        if not counter:
            continue
        top_state, top_count = counter.most_common(1)[0]
        active = sum(count for value, count in counter.items() if value.lower() in ACTIVE_STATUS_VALUES)
        lines.append(f"- {stat.column}: states={len(counter)}, top={top_state} ({top_count:,}), active={active:,}")
    return "\n".join(lines)


def write_analysis_sheet(workbook: Any, source: Path, rows_read: int, rows_written: int, stats: list[NumericStat], counters: list[Counter[str]], output_kind: str) -> None:
    worksheet = workbook.add_worksheet("Analysis")
    title_format = workbook.add_format({"bold": True, "font_size": 16, "font_color": "white", "bg_color": "#0F172A", "align": "center"})
    section_format = workbook.add_format({"bold": True, "font_color": "white", "bg_color": "#334155", "border": 1})
    value_format = workbook.add_format({"border": 1, "text_wrap": True, "valign": "top"})
    number_format = workbook.add_format({"border": 1, "num_format": "#,##0.00"})
    percent_format = workbook.add_format({"border": 1, "num_format": "0.0%"})
    worksheet.set_column("A:A", 28)
    worksheet.set_column("B:H", 18)
    worksheet.merge_range("A1:H1", "BMS / ACMV Trend Analysis", title_format)
    overview = [
        ("File", source.name),
        ("Source size", f"{source.stat().st_size:,} bytes"),
        ("Rows read", rows_read),
        ("Rows written", rows_written),
        ("Output", output_kind),
        ("Excel sheet split", "Automatic at 1,048,576 rows per sheet"),
    ]
    worksheet.write("A3", "Overview", section_format)
    worksheet.write("B3", "Value", section_format)
    for index, (label, value) in enumerate(overview, start=3):
        worksheet.write(index, 0, label, value_format)
        worksheet.write(index, 1, value, value_format)

    worksheet.write("A12", "Numeric Analysis", section_format)
    for offset, header in enumerate(["Column", "Count", "Average", "Min", "Max", "Latest", "Change", "Std Dev"]):
        worksheet.write(12, offset, header, section_format)
    for row_index, stat in enumerate(sorted((item for item in stats if item.count), key=lambda item: item.count, reverse=True)[:25], start=13):
        worksheet.write(row_index, 0, stat.column, value_format)
        worksheet.write_number(row_index, 1, stat.count, number_format)
        worksheet.write_number(row_index, 2, stat.mean, number_format)
        worksheet.write_number(row_index, 3, stat.minimum or 0, number_format)
        worksheet.write_number(row_index, 4, stat.maximum or 0, number_format)
        worksheet.write_number(row_index, 5, stat.latest or 0, number_format)
        worksheet.write_number(row_index, 6, stat.change, number_format)
        worksheet.write_number(row_index, 7, stat.std_dev, number_format)

    status_start = 42
    worksheet.write(status_start, 0, "Status Analysis", section_format)
    for offset, header in enumerate(["Column", "States", "Top State", "Top State %", "Active Events", "Active %"]):
        worksheet.write(status_start + 1, offset, header, section_format)
    out_row = status_start + 2
    for stat, counter in zip(stats, counters, strict=False):
        if not counter:
            continue
        total = max(sum(counter.values()), 1)
        top_state, top_count = counter.most_common(1)[0]
        active = sum(count for value, count in counter.items() if value.lower() in ACTIVE_STATUS_VALUES)
        worksheet.write(out_row, 0, stat.column, value_format)
        worksheet.write_number(out_row, 1, len(counter), number_format)
        worksheet.write(out_row, 2, top_state, value_format)
        worksheet.write_number(out_row, 3, top_count / total, percent_format)
        worksheet.write_number(out_row, 4, active, number_format)
        worksheet.write_number(out_row, 5, active / total, percent_format)
        out_row += 1


def process_xlsx(source: Path, output_dir: Path, progress: Any) -> ProcessResult:
    import xlsxwriter

    output_path = output_dir / f"{source.stem}.xlsx"
    workbook = xlsxwriter.Workbook(output_path, {"constant_memory": True, "strings_to_urls": False, "use_zip64": True})
    header_format = workbook.add_format({"bold": True, "bg_color": "#1F2937", "font_color": "white", "border": 1})
    text_format = workbook.add_format({"valign": "top"})
    number_format = workbook.add_format({"num_format": "#,##0.00", "valign": "top"})

    iterator = iter_csv(source)
    try:
        headers = unique_headers([clean_header(value, index) for index, value in enumerate(next(iterator)[:MAX_COLUMNS])])
    except StopIteration as exc:
        workbook.close()
        raise ValueError("CSV file is empty.") from exc

    stats, counters = new_stats(headers)
    sheet_index = 1
    rows_on_sheet = 0
    rows_read = 0
    total_rows_written = 0

    def add_sheet(index: int):
        worksheet = workbook.add_worksheet(f"Data_{index}")
        for col, header in enumerate(headers):
            worksheet.write(0, col, header, header_format)
            worksheet.set_column(col, col, min(max(len(header) + 2, 12), 32))
        worksheet.freeze_panes(1, 0)
        return worksheet

    worksheet = add_sheet(sheet_index)
    for raw_row in iterator:
        rows_read += 1
        if rows_on_sheet >= EXCEL_DATA_ROWS_PER_SHEET:
            sheet_index += 1
            worksheet = add_sheet(sheet_index)
            rows_on_sheet = 0
        row = fit_row(raw_row, len(headers))
        update_profiles(row, stats, counters)
        excel_row = rows_on_sheet + 1
        for col, value in enumerate(row):
            number = parse_number(value)
            if number is not None:
                worksheet.write_number(excel_row, col, number, number_format)
            elif value:
                worksheet.write(excel_row, col, value, text_format)
            else:
                worksheet.write_blank(excel_row, col, None, text_format)
        rows_on_sheet += 1
        total_rows_written += 1
        if rows_read % 10000 == 0:
            progress(rows_read)

    write_analysis_sheet(workbook, source, rows_read, total_rows_written, stats, counters, "xlsx")
    workbook.close()
    progress(rows_read)
    report = build_report(source, rows_read, [output_path], stats, counters)
    return ProcessResult(rows_read=rows_read, output_files=[output_path], report=report)


def process_sqlite(source: Path, output_dir: Path, progress: Any) -> ProcessResult:
    output_path = output_dir / f"{source.stem}.sqlite"
    iterator = iter_csv(source)
    try:
        headers = unique_headers([clean_header(value, index) for index, value in enumerate(next(iterator)[:MAX_COLUMNS])])
    except StopIteration as exc:
        raise ValueError("CSV file is empty.") from exc
    stats, counters = new_stats(headers)
    column_names = unique_sql_names(headers)
    table = "trend_data"
    conn = sqlite3.connect(output_path)
    quoted_table = quote_sqlite_identifier(table)
    conn.execute(f"DROP TABLE IF EXISTS {quoted_table}")
    conn.execute(f"CREATE TABLE {quoted_table} ({', '.join(f'{quote_sqlite_identifier(name)} TEXT' for name in column_names)})")
    placeholders = ",".join("?" for _ in column_names)
    rows_read = 0
    batch: list[list[str]] = []
    for raw_row in iterator:
        rows_read += 1
        row = fit_row(raw_row, len(headers))
        update_profiles(row, stats, counters)
        batch.append(row)
        if len(batch) >= 5000:
            conn.executemany(f"INSERT INTO {quoted_table} VALUES ({placeholders})", batch)
            conn.commit()
            batch.clear()
            progress(rows_read)
    if batch:
        conn.executemany(f"INSERT INTO {quoted_table} VALUES ({placeholders})", batch)
    conn.execute("CREATE TABLE conversion_report (key TEXT, value TEXT)")
    conn.executemany(
        "INSERT INTO conversion_report VALUES (?, ?)",
        [("source_file", source.name), ("rows_read", str(rows_read)), ("columns", str(len(headers)))],
    )
    conn.commit()
    conn.close()
    progress(rows_read)
    report = build_report(source, rows_read, [output_path], stats, counters)
    return ProcessResult(rows_read=rows_read, output_files=[output_path], report=report)


def process_parquet(source: Path, output_dir: Path, progress: Any) -> ProcessResult:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("Parquet output requires pyarrow on the backend. Install backend requirements with pyarrow enabled.") from exc

    output_path = output_dir / f"{source.stem}.parquet"
    iterator = iter_csv(source)
    try:
        headers = unique_headers([clean_header(value, index) for index, value in enumerate(next(iterator)[:MAX_COLUMNS])])
    except StopIteration as exc:
        raise ValueError("CSV file is empty.") from exc
    stats, counters = new_stats(headers)
    writer: pq.ParquetWriter | None = None
    rows_read = 0
    batch_rows: list[list[str]] = []
    try:
        for raw_row in iterator:
            rows_read += 1
            row = fit_row(raw_row, len(headers))
            update_profiles(row, stats, counters)
            batch_rows.append(row)
            if len(batch_rows) >= 20000:
                columns = {header: [item[index] for item in batch_rows] for index, header in enumerate(headers)}
                table = pa.table(columns)
                if writer is None:
                    writer = pq.ParquetWriter(output_path, table.schema)
                writer.write_table(table)
                batch_rows.clear()
                progress(rows_read)
        if batch_rows:
            columns = {header: [item[index] for item in batch_rows] for index, header in enumerate(headers)}
            table = pa.table(columns)
            if writer is None:
                writer = pq.ParquetWriter(output_path, table.schema)
            writer.write_table(table)
    finally:
        if writer is not None:
            writer.close()
    report = build_report(source, rows_read, [output_path], stats, counters)
    progress(rows_read)
    return ProcessResult(rows_read=rows_read, output_files=[output_path], report=report)


def process_csv(source: Path, output_dir: Path, output_format: OutputFormat, progress: Any) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    if output_format == "xlsx":
        result = process_xlsx(source, output_dir, progress)
    elif output_format == "sqlite":
        result = process_sqlite(source, output_dir, progress)
    elif output_format == "parquet":
        result = process_parquet(source, output_dir, progress)
    else:
        raise ValueError(f"Unsupported output format: {output_format}")

    report_path = output_dir / "conversion_report.txt"
    report_path.write_text(result.report, encoding="utf-8")
    archive_path = output_dir / f"{source.stem}-{output_format}.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for output_file in result.output_files:
            archive.write(output_file, output_file.name)
        archive.write(report_path, report_path.name)
    return archive_path
