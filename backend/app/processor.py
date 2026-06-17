from __future__ import annotations

import csv
import math
import os
import re
import sqlite3
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

EXCEL_MAX_ROWS = 1_048_576
EXCEL_DATA_ROWS_PER_SHEET = EXCEL_MAX_ROWS - 1
MAX_COLUMNS = 300
STATUS_LIMIT = 50
CHART_ROW_THRESHOLD = 100_000
MAX_CHART_POINTS = 1500
SHORT_CYCLE_MINUTES = 10
COMMAND_RESPONSE_DELAY_MINUTES = 5
DEFAULT_ASSUMED_CHILLER_RT = 900.0
MAX_CHILLER_TIMELINE_ROWS = 500

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


@dataclass
class ColumnMeta:
    name: str
    index: int
    equipment: str
    equipment_type: str
    metric: str
    is_discrete: bool
    is_alarm: bool
    is_analog: bool


@dataclass
class StatePeriod:
    equipment: str
    column: str
    start_time: datetime | None
    end_time: datetime | None
    start_row: int
    end_row: int
    state: int


@dataclass
class EventRecord:
    timestamp: datetime | None
    equipment: str
    issue_type: str
    evidence: str
    severity: str
    comment: str


@dataclass
class ChillerLoadSample:
    timestamp: datetime | None
    chiller: str
    chw_entering: float | None
    chw_leaving: float | None
    chw_delta_t: float | None
    chw_flow: float | None
    flow_unit: str | None
    cooling_load_rt: float | None
    load_percent: float | None
    controller_capacity_percent: float | None
    cdw_entering: float | None
    cdw_leaving: float | None
    cdw_delta_t: float | None


@dataclass
class ChillerLoadSummary:
    chiller: str
    rated_rt: float
    rated_note: str
    samples: int = 0
    dt_sum: float = 0.0
    dt_max: float | None = None
    dt_latest: float | None = None
    rt_sum: float = 0.0
    rt_count: int = 0
    rt_max: float | None = None
    rt_latest: float | None = None
    load_pct_sum: float = 0.0
    load_pct_count: int = 0
    load_pct_max: float | None = None
    load_pct_latest: float | None = None
    controller_capacity_latest: float | None = None
    timeline: list[ChillerLoadSample] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.timeline = []

    def add(self, sample: ChillerLoadSample) -> None:
        if sample.chw_delta_t is not None:
            self.samples += 1
            self.dt_sum += sample.chw_delta_t
            self.dt_latest = sample.chw_delta_t
            self.dt_max = sample.chw_delta_t if self.dt_max is None else max(self.dt_max, sample.chw_delta_t)
        if sample.cooling_load_rt is not None:
            self.rt_count += 1
            self.rt_sum += sample.cooling_load_rt
            self.rt_latest = sample.cooling_load_rt
            self.rt_max = sample.cooling_load_rt if self.rt_max is None else max(self.rt_max, sample.cooling_load_rt)
        if sample.load_percent is not None:
            self.load_pct_count += 1
            self.load_pct_sum += sample.load_percent
            self.load_pct_latest = sample.load_percent
            self.load_pct_max = sample.load_percent if self.load_pct_max is None else max(self.load_pct_max, sample.load_percent)
        if sample.controller_capacity_percent is not None:
            self.controller_capacity_latest = sample.controller_capacity_percent
        if len(self.timeline) < MAX_CHILLER_TIMELINE_ROWS:
            self.timeline.append(sample)
        elif self.samples and self.samples % max(1, self.samples // MAX_CHILLER_TIMELINE_ROWS) == 0:
            self.timeline[-1] = sample


@dataclass
class ChillerTripContext:
    trip_time: datetime | None
    chiller: str
    minutes_since_start: float | None
    sample: ChillerLoadSample | None
    classification: str
    comment: str


@dataclass
class AnalogRecord:
    column: str
    equipment: str
    metric: str
    count: int = 0
    nonzero_count: int = 0
    zero_count: int = 0
    mean: float = 0.0
    m2: float = 0.0
    minimum: float | None = None
    maximum: float | None = None
    latest: float | None = None
    max_time: datetime | None = None

    def add(self, value: float, timestamp: datetime | None) -> None:
        self.count += 1
        self.latest = value
        if abs(value) <= analog_near_zero_threshold(self.metric):
            self.zero_count += 1
        else:
            self.nonzero_count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self.m2 += delta * (value - self.mean)
        if self.minimum is None or value < self.minimum:
            self.minimum = value
        if self.maximum is None or value > self.maximum:
            self.maximum = value
            self.max_time = timestamp

    @property
    def std_dev(self) -> float:
        return math.sqrt(self.m2 / self.count) if self.count > 1 else 0.0


@dataclass
class EquipmentState:
    equipment: str
    equipment_type: str
    columns: list[str]
    command_columns: list[int]
    status_columns: list[int]
    alarm_columns: list[int]
    analog_columns: list[int]
    vsd_columns: list[int]
    runtime_columns: list[int]
    periods: list[StatePeriod]
    starts: int = 0
    stops: int = 0
    short_cycles: int = 0


@dataclass
class ChartBucket:
    start: datetime | None
    row_start: int
    row_end: int
    values: dict[str, list[float]]
    states: dict[str, list[int]]


@dataclass
class TrendAnalysis:
    headers: list[str]
    metas: list[ColumnMeta]
    datetime_index: int | None
    file_type: str = "unknown"
    rows_read: int = 0
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    previous_timestamp: datetime | None = None
    interval_seconds: Counter[int] = None  # type: ignore[assignment]
    duplicate_timestamps: int = 0
    data_gaps: int = 0
    equipment: dict[str, EquipmentState] = None  # type: ignore[assignment]
    analogs: dict[int, AnalogRecord] = None  # type: ignore[assignment]
    events: list[EventRecord] = None  # type: ignore[assignment]
    chiller_loads: dict[str, ChillerLoadSummary] = None  # type: ignore[assignment]
    chiller_trip_contexts: list[ChillerTripContext] = None  # type: ignore[assignment]
    active_periods: dict[int, StatePeriod] = None  # type: ignore[assignment]
    previous_states: dict[int, int] = None  # type: ignore[assignment]
    mismatch_streaks: dict[str, int] = None  # type: ignore[assignment]
    chart_columns: list[int] = None  # type: ignore[assignment]
    chart_status_columns: list[int] = None  # type: ignore[assignment]
    chart_buckets: list[ChartBucket] = None  # type: ignore[assignment]
    bucket_size_rows: int = 1

    def __post_init__(self) -> None:
        self.interval_seconds = Counter()
        self.equipment = {}
        self.analogs = {}
        self.events = []
        self.chiller_loads = {}
        self.chiller_trip_contexts = []
        self.active_periods = {}
        self.previous_states = {}
        self.mismatch_streaks = {}
        self.chart_columns = select_chart_columns(self.metas)
        self.chart_status_columns = select_chart_status_columns(self.metas)
        self.chart_buckets = []
        for meta in self.metas:
            state = self.equipment.setdefault(
                meta.equipment,
                EquipmentState(meta.equipment, meta.equipment_type, [], [], [], [], [], [], [], []),
            )
            state.columns.append(meta.name)
            if meta.metric == "command":
                state.command_columns.append(meta.index)
            if meta.metric in {"run_status", "status", "valve_open_status", "valve_close_status", "valve_feedback"}:
                state.status_columns.append(meta.index)
            if meta.is_alarm:
                state.alarm_columns.append(meta.index)
            if meta.is_analog:
                state.analog_columns.append(meta.index)
                self.analogs[meta.index] = AnalogRecord(meta.name, meta.equipment, meta.metric)
            if meta.metric == "vsd_feedback":
                state.vsd_columns.append(meta.index)
            if meta.metric == "runtime":
                state.runtime_columns.append(meta.index)
            if meta.equipment_type == "Chiller":
                rated_rt, rated_note = get_chiller_rated_rt(meta.equipment)
                self.chiller_loads.setdefault(meta.equipment, ChillerLoadSummary(meta.equipment, rated_rt, rated_note))

def normalize_name(value: Any) -> str:
    text = str(value or "").lower().replace("_", " ").replace("-", " ")
    return f" {' '.join(text.split())} "


def compact_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%d %b %Y %H:%M:%S",
        "%d %b %Y %H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def detect_datetime_index(headers: list[str]) -> int | None:
    for index, header in enumerate(headers):
        normalized = normalize_name(header)
        if any(token in normalized for token in [" date ", " time ", " timestamp ", " datetime "]):
            return index
    return 0 if headers else None


def classify_column(header: str) -> str:
    normalized = normalize_name(header)
    if any(token in normalized for token in [" date ", " time ", " timestamp ", " datetime "]):
        return "datetime"
    for category, keywords in CATEGORY_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            return category
    return "unknown"


def parse_equipment_and_metric(header: str) -> tuple[str, str, str]:
    text = compact_name(header)
    normalized = f" {text} "
    number_match = re.search(r"\b(?:no|unit|cell)?\s*([0-9]+(?:[-_][0-9]+)?)\b", text)
    suffix = f" {number_match.group(1).replace('_', '-')}" if number_match else ""

    if any(token in normalized for token in [" chilled water pump ", " chw pump ", " chwp "]):
        equipment = f"CHWP{suffix}".strip()
        equipment_type = "CHWP"
    elif any(token in normalized for token in [" condenser water pump ", " cdw pump ", " cwp ", " cdwp "]):
        equipment = f"CDWP{suffix}".strip()
        equipment_type = "CDWP"
    elif any(token in normalized for token in [" cooling tower ", " ct fan ", " ct cell ", " tower cell "]):
        equipment = f"Cooling Tower{suffix}".strip()
        equipment_type = "Cooling Tower"
    elif " chiller " in normalized or re.search(r"\bch(?:iller)?\s*[0-9]\b", text):
        equipment = f"Chiller{suffix}".strip()
        equipment_type = "Chiller"
    elif " ahu " in normalized or " air handling unit " in normalized:
        equipment = f"AHU{suffix}".strip()
        equipment_type = "AHU"
    elif " fcu " in normalized or " fan coil " in normalized:
        equipment = f"FCU{suffix}".strip()
        equipment_type = "FCU"
    elif " mcc " in normalized or " power meter " in normalized or " incoming " in normalized:
        equipment = f"MCC / Power Meter{suffix}".strip()
        equipment_type = "Power Meter"
    elif " vsd " in normalized or " vfd " in normalized:
        equipment = f"VSD{suffix}".strip()
        equipment_type = "VSD"
    elif " valve " in normalized or " vlv " in normalized:
        equipment = f"Valve{suffix}".strip()
        equipment_type = "Valve"
    elif " fan " in normalized or " blower " in normalized:
        equipment = f"Fan{suffix}".strip()
        equipment_type = "Fan"
    elif " pump " in normalized:
        equipment = f"Pump{suffix}".strip()
        equipment_type = "Pump"
    else:
        equipment = "Unknown Equipment"
        equipment_type = "Unknown"

    if " smoke " in normalized and " detector " in normalized:
        metric = "smoke_detector"
    elif " water leak " in normalized or " leak alarm " in normalized:
        metric = "water_leak"
    elif " filter dirty " in normalized or " dirty filter " in normalized:
        metric = "filter_dirty"
    elif " overload " in normalized:
        metric = "overload_trip"
    elif " general fault " in normalized:
        metric = "general_fault"
    elif any(token in normalized for token in [" lockout ", " locked out "]):
        metric = "lockout"
    elif any(token in normalized for token in [" trip ", " tripped "]):
        metric = "trip"
    elif any(token in normalized for token in [" fail ", " failure ", " fault "]):
        metric = "fail"
    elif any(token in normalized for token in [" alarm ", " warning ", " low level ", " high level "]):
        metric = "alarm"
    elif any(token in normalized for token in [" available ", " availability "]):
        metric = "available"
    elif " override " in normalized and any(token in normalized for token in [" enable ", " enabled ", " active "]):
        metric = "override_enable"
    elif " override " in normalized:
        metric = "override_value"
    elif any(token in normalized for token in [" maintenance ", " maint "]) or " hand " in normalized:
        metric = "maintenance_mode"
    elif " auto " in normalized or " manual " in normalized or " switch " in normalized:
        metric = "switch_mode"
    elif any(token in normalized for token in [" command ", " cmd ", " enable ", " start cmd ", " stop cmd "]):
        metric = "command"
    elif (" valve " in normalized or " vlv " in normalized or " damper " in normalized) and any(token in normalized for token in [" feedback ", " fb ", " position "]):
        metric = "valve_feedback"
    elif (" valve " in normalized or " vlv " in normalized or " damper " in normalized) and any(token in normalized for token in [" command ", " cmd ", " control "]):
        metric = "valve_command"
    elif (" vsd " in normalized or " vfd " in normalized or " fan vsd " in normalized) and any(token in normalized for token in [" feedback ", " fb ", " speed ", " hz ", " frequency "]):
        metric = "vsd_feedback"
    elif (" vsd " in normalized or " vfd " in normalized or " fan vsd " in normalized) and any(token in normalized for token in [" command ", " cmd ", " reference ", " control "]):
        metric = "vsd_command"
    elif " runtime " in normalized or " run hour " in normalized:
        metric = "runtime"
    elif (" open " in normalized or " opened " in normalized) and (" valve " in normalized or " status " in normalized):
        metric = "valve_open_status"
    elif (" close " in normalized or " closed " in normalized) and (" valve " in normalized or " status " in normalized):
        metric = "valve_close_status"
    elif any(token in normalized for token in [" run status ", " running ", " status ", " proof ", " feedback ", " on off "]):
        metric = "run_status"
    elif any(token in normalized for token in [" load ", " load % ", " percent load ", " capacity ", " capacity % ", " actual capacity ", " vfd capacity ", " demand "]):
        metric = "controller_capacity"
    elif any(token in normalized for token in [" active power ", " power ", " kw ", " kilowatt "]):
        metric = "active_power"
    elif any(token in normalized for token in [" active energy ", " energy ", " kwh "]):
        metric = "active_energy"
    elif any(token in normalized for token in [" current ", " amp ", " amps "]):
        metric = "current"
    elif any(token in normalized for token in [" voltage ", " volt ", " vln ", " vll "]):
        metric = "voltage"
    elif any(token in normalized for token in [" frequency ", " hz "]):
        metric = "frequency"
    elif any(token in normalized for token in [" temperature ", " temp ", " chwst ", " chwrt ", " chws ", " chwr ", " lwt ", " ewt "]):
        metric = "temperature"
    elif any(token in normalized for token in [" pressure ", " press ", " differential pressure ", " delta p ", " dp "]):
        metric = "pressure"
    elif any(token in normalized for token in [" flow ", " airflow ", " air flow ", " water flow ", " gpm ", " lps ", " l s ", " m3 h ", " m3hr ", " cmh "]):
        metric = "flow"
    elif any(token in normalized for token in [" humidity ", " rh "]):
        metric = "humidity"
    elif " co2 " in normalized or " carbon dioxide " in normalized:
        metric = "co2"
    elif " setpoint " in normalized or " set point " in normalized or " sp " in normalized:
        metric = "setpoint"
    else:
        metric = "unknown"
    return equipment, equipment_type, metric


def is_discrete_metric(metric: str) -> bool:
    return metric in {
        "command",
        "run_status",
        "status",
        "trip",
        "alarm",
        "fail",
        "lockout",
        "overload_trip",
        "filter_dirty",
        "smoke_detector",
        "water_leak",
        "general_fault",
        "available",
        "maintenance_mode",
        "switch_mode",
        "override_enable",
        "valve_open_status",
        "valve_close_status",
    }


def is_alarm_metric(metric: str) -> bool:
    return metric in {"trip", "alarm", "fail", "lockout", "overload_trip", "filter_dirty", "smoke_detector", "water_leak", "general_fault"}


def is_meaningful_analog(metric: str) -> bool:
    return metric in {"active_power", "current", "frequency", "controller_capacity", "vsd_feedback", "vsd_command", "valve_command", "valve_feedback", "temperature", "pressure", "flow", "humidity", "co2", "setpoint"}


def analog_near_zero_threshold(metric: str) -> float:
    if metric in {"active_power", "current", "vsd_feedback", "flow"}:
        return 0.1
    return 0.01


def classify_columns(headers: list[str]) -> list[ColumnMeta]:
    metas: list[ColumnMeta] = []
    for index, header in enumerate(headers):
        equipment, equipment_type, metric = parse_equipment_and_metric(header)
        metas.append(
            ColumnMeta(
                name=header,
                index=index,
                equipment=equipment,
                equipment_type=equipment_type,
                metric=metric,
                is_discrete=is_discrete_metric(metric),
                is_alarm=is_alarm_metric(metric),
                is_analog=is_meaningful_analog(metric),
            )
        )
    return metas


def parse_state(value: Any) -> int | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in TRUE_VALUES or text in ACTIVE_STATUS_VALUES:
        return 1
    if text in FALSE_VALUES or text in {"inactive", "ok", "healthy", "ready", "not alarm"}:
        return 0
    number = parse_number(text)
    if number is None:
        return None
    return 1 if abs(number) > 0.1 else 0


def get_chiller_number(equipment: str) -> str | None:
    match = re.search(r"(\d+)", equipment)
    return match.group(1) if match else None


def get_chiller_rated_rt(equipment: str) -> tuple[float, str]:
    number = get_chiller_number(equipment)
    keys = [f"CHILLER_{number}_RT"] if number else []
    keys.append("DEFAULT_CHILLER_RT")
    for key in keys:
        raw = os.getenv(key)
        if raw:
            try:
                value = float(raw)
                if value > 0:
                    return value, f"Rated capacity from {key}: {value:g} RT"
            except ValueError:
                continue
    return DEFAULT_ASSUMED_CHILLER_RT, f"Rated chiller capacity assumed as {DEFAULT_ASSUMED_CHILLER_RT:g} RT. Update configuration if actual capacity differs."


def detect_flow_unit(header: str) -> str | None:
    text = compact_name(header).replace(" ", "")
    spaced = f" {compact_name(header)} "
    if "gpm" in text:
        return "gpm"
    if "l/s" in header.lower() or "lps" in text or "ls" in text or "l s" in spaced:
        return "l/s"
    if "m3/h" in header.lower() or "m3hr" in text or "m3h" in text or "cmh" in text:
        return "m3/h"
    return None


def cooling_load_rt(flow: float | None, unit: str | None, delta_t: float | None) -> float | None:
    if flow is None or unit is None or delta_t is None:
        return None
    if delta_t < 0:
        return None
    if unit == "gpm":
        return flow * delta_t / 24
    if unit == "l/s":
        return flow * 4.186 * delta_t / 3.517
    if unit == "m3/h":
        flow_lps = flow * 1000 / 3600
        return flow_lps * 4.186 * delta_t / 3.517
    return None


def format_dt(value: datetime | None) -> str:
    return value.strftime("%d %b %Y %H:%M") if value else "Unknown"


def format_duration(minutes: float) -> str:
    if minutes <= 0:
        return "0 min"
    days = int(minutes // 1440)
    hours = int((minutes % 1440) // 60)
    mins = int(minutes % 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} days")
    if hours:
        parts.append(f"{hours} hours")
    if mins or not parts:
        parts.append(f"{mins} min")
    return " ".join(parts)


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


def select_chart_columns(metas: list[ColumnMeta]) -> list[int]:
    priority = {"active_power": 0, "current": 1, "vsd_feedback": 2, "temperature": 3, "pressure": 4, "flow": 5}
    candidates = [meta for meta in metas if meta.is_analog and meta.metric in priority]
    candidates.sort(key=lambda meta: (priority.get(meta.metric, 99), meta.equipment, meta.name))
    selected: list[int] = []
    seen_equipment_metric: set[tuple[str, str]] = set()
    for meta in candidates:
        key = (meta.equipment, meta.metric)
        if key in seen_equipment_metric:
            continue
        seen_equipment_metric.add(key)
        selected.append(meta.index)
        if len(selected) >= 8:
            break
    return selected


def select_chart_status_columns(metas: list[ColumnMeta]) -> list[int]:
    candidates = [meta for meta in metas if meta.metric in {"run_status", "valve_open_status"}]
    selected: list[int] = []
    seen_equipment: set[str] = set()
    for meta in candidates:
        if meta.equipment in seen_equipment:
            continue
        seen_equipment.add(meta.equipment)
        selected.append(meta.index)
        if len(selected) >= 10:
            break
    return selected


def build_analyzer(headers: list[str]) -> TrendAnalysis:
    metas = classify_columns(headers)
    return TrendAnalysis(headers=headers, metas=metas, datetime_index=detect_datetime_index(headers), file_type=detect_file_type(metas))


def detect_file_type(metas: list[ColumnMeta]) -> str:
    types = Counter(meta.equipment_type for meta in metas)
    has_chiller = types["Chiller"] > 0
    has_ahu = types["AHU"] > 0
    has_fcu = types["FCU"] > 0
    has_ct_pump_vsd = types["Cooling Tower"] > 0 or types["CHWP"] > 0 or types["CDWP"] > 0 or types["Pump"] > 0 or types["VSD"] > 0
    detected = sum([has_chiller, has_ahu, has_fcu, has_ct_pump_vsd])
    if detected > 1:
        return "mixed ACMV"
    if has_chiller:
        return "chiller"
    if has_ahu:
        return "AHU"
    if has_fcu:
        return "FCU"
    if has_ct_pump_vsd:
        return "CT / CHWP / CDWP / VSD"
    return "unknown"


def estimate_bucket_size(rows_read: int) -> int:
    if rows_read <= CHART_ROW_THRESHOLD:
        return 1
    return max(1, math.ceil(rows_read / MAX_CHART_POINTS))


def update_chart_bucket(analysis: TrendAnalysis, row: list[str], timestamp: datetime | None) -> None:
    bucket_index = (analysis.rows_read - 1) // max(analysis.bucket_size_rows, 1)
    if len(analysis.chart_buckets) <= bucket_index:
        analysis.chart_buckets.append(ChartBucket(timestamp, analysis.rows_read, analysis.rows_read, {}, {}))
    bucket = analysis.chart_buckets[bucket_index]
    bucket.row_end = analysis.rows_read
    if bucket.start is None:
        bucket.start = timestamp
    for index in analysis.chart_columns:
        if index >= len(row):
            continue
        number = parse_number(row[index])
        if number is not None:
            bucket.values.setdefault(analysis.headers[index], []).append(number)
    for index in analysis.chart_status_columns:
        if index >= len(row):
            continue
        state = parse_state(row[index])
        if state is not None:
            bucket.states.setdefault(analysis.headers[index], []).append(state)


def delay_rows(analysis: TrendAnalysis) -> int:
    if analysis.interval_seconds:
        interval = max(analysis.interval_seconds.most_common(1)[0][0], 1)
        return max(1, math.ceil((COMMAND_RESPONSE_DELAY_MINUTES * 60) / interval))
    return COMMAND_RESPONSE_DELAY_MINUTES


def set_mismatch(analysis: TrendAnalysis, key: str, active: bool, event_factory: Any) -> None:
    if not active:
        analysis.mismatch_streaks.pop(key, None)
        return
    streak = analysis.mismatch_streaks.get(key, 0) + 1
    analysis.mismatch_streaks[key] = streak
    if streak == delay_rows(analysis):
        analysis.events.append(event_factory())


def values_for_metrics(analysis: TrendAnalysis, row: list[str], equipment: str, metrics: set[str]) -> list[float]:
    values: list[float] = []
    for meta in analysis.metas:
        if meta.equipment != equipment or meta.metric not in metrics or meta.index >= len(row):
            continue
        number = parse_number(row[meta.index])
        if number is not None:
            values.append(number)
    return values


def states_for_metrics(analysis: TrendAnalysis, row: list[str], equipment: str, metrics: set[str]) -> list[int]:
    states: list[int] = []
    for meta in analysis.metas:
        if meta.equipment != equipment or meta.metric not in metrics or meta.index >= len(row):
            continue
        state = parse_state(row[meta.index])
        if state is not None:
            states.append(state)
    return states


def named_values_for_metrics(analysis: TrendAnalysis, row: list[str], equipment: str, metrics: set[str]) -> list[tuple[ColumnMeta, float]]:
    values: list[tuple[ColumnMeta, float]] = []
    for meta in analysis.metas:
        if meta.equipment != equipment or meta.metric not in metrics or meta.index >= len(row):
            continue
        number = parse_number(row[meta.index])
        if number is not None:
            values.append((meta, number))
    return values


def best_value_by_keywords(values: list[tuple[ColumnMeta, float]], required: list[str], optional: list[str] | None = None) -> float | None:
    optional = optional or []
    best: tuple[int, float] | None = None
    for meta, value in values:
        text = compact_name(meta.name)
        if not all(keyword in text for keyword in required):
            continue
        score = len(required) * 10 + sum(1 for keyword in optional if keyword in text)
        if best is None or score > best[0]:
            best = (score, value)
    return best[1] if best else None


def build_chiller_load_sample(analysis: TrendAnalysis, row: list[str], chiller: str, timestamp: datetime | None) -> ChillerLoadSample | None:
    temp_values = named_values_for_metrics(analysis, row, chiller, {"temperature"})
    flow_values = named_values_for_metrics(analysis, row, chiller, {"flow"})
    capacity_values = named_values_for_metrics(analysis, row, chiller, {"controller_capacity"})
    chw_entering = (
        best_value_by_keywords(temp_values, ["evap", "entering"]) or
        best_value_by_keywords(temp_values, ["chw", "return"]) or
        best_value_by_keywords(temp_values, ["chwr"]) or
        best_value_by_keywords(temp_values, ["ewt"]) or
        best_value_by_keywords(temp_values, ["entering", "water"])
    )
    chw_leaving = (
        best_value_by_keywords(temp_values, ["evap", "leaving"]) or
        best_value_by_keywords(temp_values, ["chw", "supply"]) or
        best_value_by_keywords(temp_values, ["chws"]) or
        best_value_by_keywords(temp_values, ["lwt"]) or
        best_value_by_keywords(temp_values, ["leaving", "water"])
    )
    cdw_entering = (
        best_value_by_keywords(temp_values, ["cond", "entering"]) or
        best_value_by_keywords(temp_values, ["cdw", "entering"])
    )
    cdw_leaving = (
        best_value_by_keywords(temp_values, ["cond", "leaving"]) or
        best_value_by_keywords(temp_values, ["cdw", "leaving"])
    )
    flow_pair = next(((meta, value) for meta, value in flow_values if detect_flow_unit(meta.name)), None)
    flow_meta, flow_value = flow_pair if flow_pair else (None, None)
    flow_unit = detect_flow_unit(flow_meta.name) if flow_meta else None
    controller_capacity = capacity_values[0][1] if capacity_values else None
    chw_delta = (chw_entering - chw_leaving) if chw_entering is not None and chw_leaving is not None else None
    cdw_delta = (cdw_leaving - cdw_entering) if cdw_entering is not None and cdw_leaving is not None else None
    if chw_delta is None and flow_value is None and controller_capacity is None and cdw_delta is None:
        return None
    load_rt = cooling_load_rt(flow_value, flow_unit, chw_delta)
    summary = analysis.chiller_loads.get(chiller)
    load_percent = (load_rt / summary.rated_rt * 100) if load_rt is not None and summary and summary.rated_rt > 0 else None
    return ChillerLoadSample(
        timestamp=timestamp,
        chiller=chiller,
        chw_entering=chw_entering,
        chw_leaving=chw_leaving,
        chw_delta_t=chw_delta,
        chw_flow=flow_value,
        flow_unit=flow_unit,
        cooling_load_rt=load_rt,
        load_percent=load_percent,
        controller_capacity_percent=controller_capacity,
        cdw_entering=cdw_entering,
        cdw_leaving=cdw_leaving,
        cdw_delta_t=cdw_delta,
    )


def current_run_start(analysis: TrendAnalysis, chiller: str) -> datetime | None:
    for index, period in analysis.active_periods.items():
        if period.equipment == chiller and analysis.metas[index].metric in {"run_status", "status"}:
            return period.start_time
    return None


def classify_chiller_trip(sample: ChillerLoadSample | None, minutes_since_start: float | None) -> tuple[str, str]:
    if sample is None:
        return "Insufficient data", "Trip occurred, but water-side temperature/load context is unavailable."
    low_load = (sample.load_percent is not None and sample.load_percent < 15) or (sample.chw_delta_t is not None and sample.chw_delta_t < 2)
    high_load = sample.load_percent is not None and sample.load_percent >= 85
    loaded = (sample.cooling_load_rt is not None and sample.cooling_load_rt >= 0.25 * DEFAULT_ASSUMED_CHILLER_RT) or (sample.chw_delta_t is not None and sample.chw_delta_t >= 4)
    cdw_abnormal = sample.cdw_delta_t is not None and sample.cdw_delta_t <= 0
    leaving_low = sample.chw_leaving is not None and sample.chw_leaving < 4
    if minutes_since_start is not None and minutes_since_start <= 10 and low_load:
        return "Startup low-load trip", "Trip occurred shortly after start with low calculated RT or low CHW delta-T."
    if high_load:
        return "High load trip", "Trip occurred while calculated load percentage was high."
    if loaded and cdw_abnormal:
        return "CHW load present but CDW heat rejection weak", "CHW load is present while CDW delta-T is low/negative; check heat rejection or sensor mapping."
    if leaving_low or (low_load and sample.chw_flow is None):
        return "Possible freeze protection / low flow", "Leaving water temperature or low-load/unknown-flow pattern suggests freeze/flow review."
    if loaded:
        return "Loaded running trip", "Trip occurred with meaningful CHW delta-T or calculated RT."
    return "Insufficient data", "Trip classification is limited by missing flow/temperature context."


def update_chiller_load_analysis(analysis: TrendAnalysis, row: list[str], timestamp: datetime | None) -> None:
    for state in analysis.equipment.values():
        if state.equipment_type != "Chiller":
            continue
        sample = build_chiller_load_sample(analysis, row, state.equipment, timestamp)
        if sample is not None:
            analysis.chiller_loads[state.equipment].add(sample)


def add_chiller_trip_context(analysis: TrendAnalysis, row: list[str], timestamp: datetime | None, chiller: str) -> None:
    sample = build_chiller_load_sample(analysis, row, chiller, timestamp)
    start_time = current_run_start(analysis, chiller)
    minutes_since_start = (timestamp - start_time).total_seconds() / 60 if timestamp and start_time else None
    classification, comment = classify_chiller_trip(sample, minutes_since_start)
    analysis.chiller_trip_contexts.append(ChillerTripContext(timestamp, chiller, minutes_since_start, sample, classification, comment))


def evaluate_equipment_rules(analysis: TrendAnalysis, row: list[str], timestamp: datetime | None) -> None:
    for state in analysis.equipment.values():
        command_states = [parse_state(row[index]) for index in state.command_columns if index < len(row)]
        status_states = [parse_state(row[index]) for index in state.status_columns if index < len(row)]
        analog_values = [
            parse_number(row[index])
            for index in state.analog_columns
            if index < len(row) and analysis.metas[index].metric in {"active_power", "current", "vsd_feedback", "frequency", "flow"}
        ]
        analog_values = [value for value in analog_values if value is not None]
        valve_commands = values_for_metrics(analysis, row, state.equipment, {"valve_command"})
        valve_feedbacks = values_for_metrics(analysis, row, state.equipment, {"valve_feedback"})
        vsd_commands = values_for_metrics(analysis, row, state.equipment, {"vsd_command"})
        vsd_feedbacks = values_for_metrics(analysis, row, state.equipment, {"vsd_feedback"})
        temperatures = values_for_metrics(analysis, row, state.equipment, {"temperature"})
        setpoints = values_for_metrics(analysis, row, state.equipment, {"setpoint"})
        humidity = values_for_metrics(analysis, row, state.equipment, {"humidity"})
        co2 = values_for_metrics(analysis, row, state.equipment, {"co2"})
        overrides = states_for_metrics(analysis, row, state.equipment, {"override_enable"})

        command_on = any(value == 1 for value in command_states)
        status_on = any(value == 1 for value in status_states)
        status_off = bool(status_states) and not status_on
        analog_high = any(abs(value) > analog_near_zero_threshold("active_power") for value in analog_values)
        analog_zero = bool(analog_values) and not analog_high

        set_mismatch(
            analysis,
            f"{state.equipment}:command_on_status_off",
            command_on and status_off,
            lambda state=state, timestamp=timestamp: EventRecord(
                timestamp,
                state.equipment,
                "command_status_mismatch",
                "command/status",
                "Warning",
                f"Command appears ON but status remains OFF for about {COMMAND_RESPONSE_DELAY_MINUTES} minutes.",
            ),
        )
        set_mismatch(
            analysis,
            f"{state.equipment}:valve_command_high_feedback_low",
            bool(valve_commands and valve_feedbacks and max(valve_commands) >= 50 and max(valve_feedbacks) < 20),
            lambda state=state, timestamp=timestamp: EventRecord(
                timestamp,
                state.equipment,
                "valve_command_feedback_mismatch",
                "valve command/feedback",
                "Warning",
                "Valve command is high while valve feedback remains low.",
            ),
        )
        set_mismatch(
            analysis,
            f"{state.equipment}:valve_feedback_high_command_low",
            bool(valve_commands and valve_feedbacks and max(valve_commands) <= 5 and max(valve_feedbacks) >= 30),
            lambda state=state, timestamp=timestamp: EventRecord(
                timestamp,
                state.equipment,
                "valve_feedback_without_command",
                "valve command/feedback",
                "Warning",
                "Valve feedback is high while command is near zero; check stuck valve or mapping.",
            ),
        )
        set_mismatch(
            analysis,
            f"{state.equipment}:vsd_command_feedback_mismatch",
            bool(vsd_commands and vsd_feedbacks and max(vsd_commands) >= 30 and max(vsd_feedbacks) < 5),
            lambda state=state, timestamp=timestamp: EventRecord(
                timestamp,
                state.equipment,
                "vsd_command_feedback_mismatch",
                "VSD command/feedback",
                "Warning",
                "VSD command is present but VSD feedback remains near zero.",
            ),
        )
        set_mismatch(
            analysis,
            f"{state.equipment}:override_enabled",
            any(value == 1 for value in overrides),
            lambda state=state, timestamp=timestamp: EventRecord(
                timestamp,
                state.equipment,
                "override_enabled",
                "override point",
                "Info",
                "Override enable point is active; automatic operation may be bypassed.",
            ),
        )
        if temperatures and setpoints:
            temp_above_sp = max(temperatures) - min(setpoints)
            set_mismatch(
                analysis,
                f"{state.equipment}:comfort_temp_high",
                temp_above_sp > 2.0,
                lambda state=state, timestamp=timestamp, temp_above_sp=temp_above_sp: EventRecord(
                    timestamp,
                    state.equipment,
                    "comfort_temperature_deviation",
                    "temperature/setpoint",
                    "Warning",
                    f"Temperature is about {temp_above_sp:.1f} deg above setpoint.",
                ),
            )
        set_mismatch(
            analysis,
            f"{state.equipment}:high_humidity",
            bool(humidity and max(humidity) > 75),
            lambda state=state, timestamp=timestamp: EventRecord(
                timestamp,
                state.equipment,
                "high_humidity",
                "humidity",
                "Warning",
                "Humidity is above 75%; review comfort and dehumidification.",
            ),
        )
        set_mismatch(
            analysis,
            f"{state.equipment}:high_co2",
            bool(co2 and max(co2) > 1000),
            lambda state=state, timestamp=timestamp: EventRecord(
                timestamp,
                state.equipment,
                "high_co2",
                "CO2",
                "Warning",
                "CO2 is above 1000 ppm; review ventilation or sensor mapping.",
            ),
        )
        set_mismatch(
            analysis,
            f"{state.equipment}:status_on_analog_zero",
            status_on and analog_zero,
            lambda state=state, timestamp=timestamp: EventRecord(
                timestamp,
                state.equipment,
                "status_on_no_analog_evidence",
                "status with power/current/VSD",
                "Warning",
                "Status appears ON while power/current/VSD feedback remains near zero.",
            ),
        )
        set_mismatch(
            analysis,
            f"{state.equipment}:status_off_analog_high",
            status_off and analog_high,
            lambda state=state, timestamp=timestamp: EventRecord(
                timestamp,
                state.equipment,
                "status_off_analog_high",
                "status with power/current/VSD",
                "Warning",
                "Status appears OFF while power/current/VSD feedback remains high.",
            ),
        )


def close_active_period(analysis: TrendAnalysis, index: int, end_time: datetime | None, end_row: int) -> None:
    period = analysis.active_periods.pop(index, None)
    if period is None:
        return
    period.end_time = end_time
    period.end_row = end_row
    meta = analysis.metas[index]
    state = analysis.equipment.get(meta.equipment)
    if state:
        state.periods.append(period)
        duration = period_duration_minutes(period)
        if duration and duration < SHORT_CYCLE_MINUTES and meta.metric in {"run_status", "status", "valve_open_status"}:
            state.short_cycles += 1


def update_analysis(analysis: TrendAnalysis, row: list[str]) -> None:
    analysis.rows_read += 1
    timestamp = parse_timestamp(row[analysis.datetime_index]) if analysis.datetime_index is not None and analysis.datetime_index < len(row) else None
    if timestamp:
        if analysis.first_timestamp is None:
            analysis.first_timestamp = timestamp
        if analysis.previous_timestamp:
            delta = int((timestamp - analysis.previous_timestamp).total_seconds())
            if delta == 0:
                analysis.duplicate_timestamps += 1
            elif delta > 0:
                analysis.interval_seconds[delta] += 1
                expected = analysis.interval_seconds.most_common(1)[0][0] if analysis.interval_seconds else delta
                if expected > 0 and delta > expected * 3:
                    analysis.data_gaps += 1
        analysis.previous_timestamp = timestamp
        analysis.last_timestamp = timestamp

    for meta in analysis.metas:
        if meta.index >= len(row):
            continue
        value = row[meta.index]
        if meta.is_analog:
            number = parse_number(value)
            if number is not None:
                analysis.analogs[meta.index].add(number, timestamp)
        if meta.is_discrete:
            state = parse_state(value)
            if state is None:
                continue
            previous = analysis.previous_states.get(meta.index)
            if previous is None:
                analysis.previous_states[meta.index] = state
                if state == 1:
                    analysis.active_periods[meta.index] = StatePeriod(meta.equipment, meta.name, timestamp, None, analysis.rows_read, analysis.rows_read, state)
                continue
            if previous == state:
                continue
            analysis.previous_states[meta.index] = state
            equipment_state = analysis.equipment.get(meta.equipment)
            if state == 1:
                if meta.metric in {"run_status", "status", "valve_open_status"} and equipment_state:
                    equipment_state.starts += 1
                analysis.active_periods[meta.index] = StatePeriod(meta.equipment, meta.name, timestamp, None, analysis.rows_read, analysis.rows_read, state)
                if meta.is_alarm:
                    analysis.events.append(EventRecord(timestamp, meta.equipment, meta.metric, meta.name, "High", f"{meta.name} became active."))
                    if meta.equipment_type == "Chiller" and meta.metric in {"trip", "lockout", "fail", "overload_trip"}:
                        add_chiller_trip_context(analysis, row, timestamp, meta.equipment)
            else:
                if meta.metric in {"run_status", "status", "valve_open_status"} and equipment_state:
                    equipment_state.stops += 1
                close_active_period(analysis, meta.index, timestamp, analysis.rows_read)

    if analysis.file_type in {"chiller", "mixed ACMV"}:
        update_chiller_load_analysis(analysis, row, timestamp)
    update_chart_bucket(analysis, row, timestamp)
    evaluate_equipment_rules(analysis, row, timestamp)


def finalize_analysis(analysis: TrendAnalysis) -> TrendAnalysis:
    for index in list(analysis.active_periods):
        close_active_period(analysis, index, analysis.last_timestamp, analysis.rows_read)
    compress_chart_buckets(analysis)
    add_generic_findings(analysis)
    return analysis


def merge_chart_buckets(buckets: list[ChartBucket]) -> ChartBucket:
    first = buckets[0]
    merged = ChartBucket(first.start, first.row_start, buckets[-1].row_end, {}, {})
    for bucket in buckets:
        for name, values in bucket.values.items():
            merged.values.setdefault(name, []).extend(values)
        for name, states in bucket.states.items():
            merged.states.setdefault(name, []).extend(states)
    return merged


def compress_chart_buckets(analysis: TrendAnalysis) -> None:
    if len(analysis.chart_buckets) <= MAX_CHART_POINTS:
        return
    group_size = math.ceil(len(analysis.chart_buckets) / MAX_CHART_POINTS)
    compressed: list[ChartBucket] = []
    for index in range(0, len(analysis.chart_buckets), group_size):
        compressed.append(merge_chart_buckets(analysis.chart_buckets[index:index + group_size]))
    analysis.chart_buckets = compressed[:MAX_CHART_POINTS]
    analysis.bucket_size_rows *= group_size


def period_duration_minutes(period: StatePeriod) -> float:
    if period.start_time and period.end_time:
        return max((period.end_time - period.start_time).total_seconds() / 60, 0.0)
    return max(period.end_row - period.start_row, 0)


def equipment_on_minutes(state: EquipmentState) -> float:
    return sum(period_duration_minutes(period) for period in state.periods if period.state == 1)


def equipment_longest_run_minutes(state: EquipmentState) -> float:
    periods = [period_duration_minutes(period) for period in state.periods if period.state == 1]
    return max(periods) if periods else 0.0


def trend_duration_minutes(analysis: TrendAnalysis) -> float:
    if analysis.first_timestamp and analysis.last_timestamp:
        return max((analysis.last_timestamp - analysis.first_timestamp).total_seconds() / 60, 0.0)
    return float(analysis.rows_read)


def add_generic_findings(analysis: TrendAnalysis) -> None:
    if analysis.datetime_index is None:
        analysis.events.append(EventRecord(None, "Data Quality", "missing_datetime", "headers", "Warning", "No datetime column was confidently detected; operation timing is limited."))
    if analysis.data_gaps:
        analysis.events.append(EventRecord(None, "Data Quality", "data_gap", "timestamp interval", "Warning", f"{analysis.data_gaps} large timestamp gap(s) were detected."))
    if analysis.duplicate_timestamps:
        analysis.events.append(EventRecord(None, "Data Quality", "duplicate_timestamp", "timestamp", "Warning", f"{analysis.duplicate_timestamps} duplicate timestamp sample(s) were detected."))
    for state in analysis.equipment.values():
        if state.short_cycles:
            analysis.events.append(EventRecord(None, state.equipment, "short_cycle", "run status periods", "Warning", f"{state.short_cycles} short run period(s) below {SHORT_CYCLE_MINUTES} minutes were detected."))
    for analog in analysis.analogs.values():
        if analog.count >= 10 and analog.std_dev < 0.0001:
            analysis.events.append(EventRecord(None, analog.equipment, "constant_analog", analog.column, "Info", "Analog value is mostly constant and may not be useful for charting."))
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


def estimate_initial_bucket_size(source: Path) -> int:
    size = source.stat().st_size
    if size >= 300 * 1024 * 1024:
        return 5000
    if size >= 100 * 1024 * 1024:
        return 2000
    if size >= 20 * 1024 * 1024:
        return 500
    return 1


def build_key_findings(analysis: TrendAnalysis) -> list[str]:
    systems = sorted({meta.equipment for meta in analysis.metas if meta.equipment != "Unknown Equipment"})
    findings = []
    findings.append(f"Detected trend file type: {analysis.file_type}.")
    if systems:
        findings.append(f"Trend contains detected BMS / ACMV equipment groups: {', '.join(systems[:8])}.")
    else:
        findings.append("Trend equipment could not be confidently grouped from column names; review BMS point mapping.")
    if analysis.first_timestamp and analysis.last_timestamp:
        findings.append(
            f"Data covers {format_dt(analysis.first_timestamp)} to {format_dt(analysis.last_timestamp)}, approximately {format_duration(trend_duration_minutes(analysis))}."
        )
    if analysis.interval_seconds:
        interval = analysis.interval_seconds.most_common(1)[0][0]
        findings.append(f"Estimated sampling interval is about {format_duration(interval / 60)}.")
    running = [state.equipment for state in analysis.equipment.values() if equipment_on_minutes(state) > 0]
    if running:
        findings.append(f"Operation was detected for: {', '.join(running[:8])}.")
    alarm_count = sum(1 for event in analysis.events if event.severity == "High")
    if alarm_count:
        findings.append(f"{alarm_count} alarm/trip/fail/lockout transition(s) were detected and should be reviewed.")
    if analysis.data_gaps or analysis.duplicate_timestamps:
        findings.append("Timestamp quality issues were detected; confirm trend export interval before final engineering conclusions.")
    useful_analogs = [analog for analog in analysis.analogs.values() if analog.count and analog.nonzero_count]
    if useful_analogs:
        findings.append(f"{len(useful_analogs)} meaningful analog trend(s) were detected for power/current/VSD/temperature/pressure/flow review.")
    findings.append("No final conclusion should be made without verifying BMS point mapping, command logic, and alarm definitions.")
    return findings[:10]


def equipment_observation(state: EquipmentState, analysis: TrendAnalysis) -> str:
    notes: list[str] = []
    if equipment_on_minutes(state) > 0:
        notes.append("operation detected")
    if state.short_cycles:
        notes.append(f"{state.short_cycles} short cycles")
    if state.alarm_columns:
        notes.append("alarm/trip/fault points available")
    if state.command_columns and not state.status_columns:
        notes.append("command without clear run proof")
    if state.status_columns and not state.command_columns:
        notes.append("status/run proof without command")
    related_events = [event for event in analysis.events if event.equipment == state.equipment and event.severity in {"High", "Warning"}]
    if related_events:
        notes.append(f"{len(related_events)} item(s) require review")
    return "; ".join(notes) if notes else "No major operation issue detected by generic rules."


def event_summary_rows(analysis: TrendAnalysis) -> list[list[Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    meta_by_name = {meta.name: meta for meta in analysis.metas}
    for state in analysis.equipment.values():
        for period in state.periods:
            meta = meta_by_name.get(period.column)
            if not meta or not meta.is_alarm:
                continue
            key = (period.equipment, meta.metric, period.column)
            item = grouped.setdefault(
                key,
                {"first": period.start_time, "last": period.end_time, "duration": 0.0, "count": 0},
            )
            item["count"] += 1
            item["duration"] += period_duration_minutes(period)
            if period.start_time and (item["first"] is None or period.start_time < item["first"]):
                item["first"] = period.start_time
            if period.end_time and (item["last"] is None or period.end_time > item["last"]):
                item["last"] = period.end_time
    rows: list[list[Any]] = []
    for (equipment, event_type, evidence), item in sorted(grouped.items()):
        severity = "High" if event_type in {"trip", "lockout", "fail", "overload_trip", "smoke_detector", "water_leak", "general_fault"} else "Warning"
        rows.append([
            equipment,
            event_type,
            format_dt(item["first"]),
            format_dt(item["last"]),
            format_duration(item["duration"]),
            item["count"],
            severity,
            f"{evidence} active for {format_duration(item['duration'])} across {item['count']} occurrence(s).",
        ])
    return rows


def data_quality_rows(analysis: TrendAnalysis) -> list[list[Any]]:
    rows: list[list[Any]] = []
    if analysis.datetime_index is None:
        rows.append(["Missing timestamps", "No datetime column was confidently detected.", "Warning"])
    if analysis.data_gaps:
        rows.append(["Timestamp gaps", f"{analysis.data_gaps} large timestamp gap(s) detected.", "Warning"])
    if analysis.duplicate_timestamps:
        rows.append(["Duplicate timestamps", f"{analysis.duplicate_timestamps} duplicate timestamp sample(s) detected.", "Warning"])
    for analog in analysis.analogs.values():
        if analog.count >= 10 and analog.std_dev < 0.0001:
            rows.append(["Constant column", f"{analog.column} is mostly constant.", "Info"])
        if analog.metric == "temperature" and analog.minimum is not None and (analog.minimum < -20 or analog.maximum and analog.maximum > 80):
            rows.append(["Suspicious temperature range", f"{analog.column} range looks outside normal ACMV trend limits.", "Warning"])
    return rows or [["No major data quality issue", "No timestamp gaps, duplicates, or obvious constant analog problems detected by generic rules.", "Info"]]


def avg(total: float, count: int) -> float | None:
    return total / count if count else None


def chiller_load_status(summary: ChillerLoadSummary) -> str:
    if summary.rt_count:
        return "Calculated from CHW flow and CHW delta-T"
    has_dt = summary.samples > 0
    if has_dt:
        return "Cooling load RT unavailable; CHW delta-T used as load indication because CHW flow is missing or unit is unknown"
    return "Cooling load RT unavailable; CHW temperature differential could not be confirmed"


def chiller_load_summary_rows(analysis: TrendAnalysis) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for summary in analysis.chiller_loads.values():
        rows.append([
            summary.chiller,
            avg(summary.dt_sum, summary.samples),
            summary.dt_max,
            summary.dt_latest,
            avg(summary.rt_sum, summary.rt_count),
            summary.rt_max,
            summary.rt_latest,
            avg(summary.load_pct_sum, summary.load_pct_count),
            summary.load_pct_max,
            summary.load_pct_latest,
            f"{summary.rated_rt:g} RT",
            chiller_load_status(summary),
            summary.rated_note,
        ])
    return rows


def chiller_timeline_rows(analysis: TrendAnalysis) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for summary in analysis.chiller_loads.values():
        for sample in summary.timeline[:MAX_CHILLER_TIMELINE_ROWS]:
            rows.append([
                format_dt(sample.timestamp),
                sample.chiller,
                sample.chw_entering,
                sample.chw_leaving,
                sample.chw_delta_t,
                sample.chw_flow,
                sample.flow_unit or "Unknown",
                sample.cooling_load_rt,
                sample.load_percent,
                sample.controller_capacity_percent,
            ])
    return rows


def chiller_trip_context_rows(analysis: TrendAnalysis) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for context in analysis.chiller_trip_contexts:
        sample = context.sample
        rows.append([
            format_dt(context.trip_time),
            context.chiller,
            context.minutes_since_start,
            sample.chw_entering if sample else None,
            sample.chw_leaving if sample else None,
            sample.chw_delta_t if sample else None,
            sample.chw_flow if sample else None,
            sample.flow_unit if sample and sample.flow_unit else "Unknown",
            sample.cooling_load_rt if sample else None,
            sample.load_percent if sample else None,
            sample.controller_capacity_percent if sample else None,
            sample.cdw_entering if sample else None,
            sample.cdw_leaving if sample else None,
            sample.cdw_delta_t if sample else None,
            context.classification,
            context.comment,
        ])
    return rows


def file_specific_rows(analysis: TrendAnalysis) -> tuple[str, list[str], list[list[Any]]]:
    if analysis.file_type == "chiller":
        return "Cooling Load Summary", [
            "Chiller",
            "Avg CHW Delta-T",
            "Max CHW Delta-T",
            "Latest CHW Delta-T",
            "Avg RT",
            "Max RT",
            "Latest RT",
            "Avg load %",
            "Max load %",
            "Latest load %",
            "Rated RT used",
            "RT status",
            "Capacity note",
        ], chiller_load_summary_rows(analysis)
    if analysis.file_type == "AHU":
        return "AHU Comfort / Valve / VSD Review", ["AHU", "Run duration", "Events", "Valve/VSD points", "Comfort points", "Observation"], [
            [
                state.equipment,
                format_duration(equipment_on_minutes(state)),
                sum(1 for event in analysis.events if event.equipment == state.equipment),
                len(state.vsd_columns) + len([i for i in state.analog_columns if analysis.metas[i].metric in {"valve_command", "valve_feedback"}]),
                len([i for i in state.analog_columns if analysis.metas[i].metric in {"temperature", "humidity", "co2", "setpoint"}]),
                equipment_observation(state, analysis),
            ]
            for state in analysis.equipment.values() if state.equipment_type == "AHU"
        ]
    if analysis.file_type == "FCU":
        return "FCU Comfort / Leak / Override Review", ["FCU", "Run duration", "Events", "Valve/VSD points", "Comfort points", "Observation"], [
            [
                state.equipment,
                format_duration(equipment_on_minutes(state)),
                sum(1 for event in analysis.events if event.equipment == state.equipment),
                len(state.vsd_columns) + len([i for i in state.analog_columns if analysis.metas[i].metric in {"valve_command", "valve_feedback"}]),
                len([i for i in state.analog_columns if analysis.metas[i].metric in {"temperature", "setpoint"}]),
                equipment_observation(state, analysis),
            ]
            for state in analysis.equipment.values() if state.equipment_type == "FCU"
        ]
    if analysis.file_type in {"CT / CHWP / CDWP / VSD", "mixed ACMV"}:
        return "CT / Pump / VSD Review", ["Equipment", "Type", "Run duration", "Fault events", "Power/current/frequency points", "Observation"], [
            [
                state.equipment,
                state.equipment_type,
                format_duration(equipment_on_minutes(state)),
                sum(1 for event in analysis.events if event.equipment == state.equipment and event.issue_type in {"general_fault", "trip", "fail", "alarm"}),
                len([i for i in state.analog_columns if analysis.metas[i].metric in {"active_power", "current", "frequency", "vsd_feedback", "vsd_command"}]),
                equipment_observation(state, analysis),
            ]
            for state in analysis.equipment.values() if state.equipment_type in {"Cooling Tower", "CHWP", "CDWP", "Pump", "Fan", "Chiller"}
        ]
    return "File-Specific Review", ["Item", "Observation"], [["Unknown file type", "Analyzer could not confidently match this file to chiller, AHU, FCU, or CT/Pump/VSD patterns."]]


def write_table_row(worksheet: Any, row: int, values: list[Any], fmt: Any) -> None:
    for col, value in enumerate(values):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            worksheet.write_number(row, col, value, fmt)
        else:
            worksheet.write(row, col, value, fmt)


def write_chart_helper(workbook: Any, analysis: TrendAnalysis) -> Any:
    helper = workbook.add_worksheet("_ChartHelper")
    helper.hide()
    headers = ["Time"]
    analog_names = [analysis.headers[index] for index in analysis.chart_columns]
    status_names = [analysis.headers[index] for index in analysis.chart_status_columns]
    headers.extend(analog_names)
    headers.extend(status_names)
    for col, header in enumerate(headers):
        helper.write(0, col, header)
    for row_index, bucket in enumerate(analysis.chart_buckets[:MAX_CHART_POINTS], start=1):
        helper.write(row_index, 0, format_dt(bucket.start) if bucket.start else str(bucket.row_start))
        col = 1
        for name in analog_names:
            values = bucket.values.get(name, [])
            helper.write_number(row_index, col, sum(values) / len(values) if values else 0)
            col += 1
        for name in status_names:
            values = bucket.states.get(name, [])
            helper.write_number(row_index, col, max(values) if values else 0)
            col += 1
    return helper


def write_analysis_sheet(workbook: Any, source: Path, rows_read: int, rows_written: int, analysis: TrendAnalysis, output_kind: str) -> None:
    worksheet = workbook.add_worksheet("Analysis")
    title_format = workbook.add_format({"bold": True, "font_size": 16, "font_color": "white", "bg_color": "#0F172A", "align": "center"})
    section_format = workbook.add_format({"bold": True, "font_color": "white", "bg_color": "#334155", "border": 1})
    header_format = workbook.add_format({"bold": True, "font_color": "white", "bg_color": "#475569", "border": 1, "text_wrap": True})
    value_format = workbook.add_format({"border": 1, "text_wrap": True, "valign": "top"})
    number_format = workbook.add_format({"border": 1, "num_format": "#,##0.00"})
    percent_format = workbook.add_format({"border": 1, "num_format": "0.0%"})
    warning_format = workbook.add_format({"border": 1, "text_wrap": True, "valign": "top", "bg_color": "#FEF2F2", "font_color": "#991B1B"})
    worksheet.set_column("A:A", 24)
    worksheet.set_column("B:B", 28)
    worksheet.set_column("C:H", 18)
    worksheet.set_column("I:P", 18)
    worksheet.merge_range("A1:H1", "BMS / ACMV Operation Analysis Report", title_format)
    systems = sorted({meta.equipment for meta in analysis.metas if meta.equipment != "Unknown Equipment"})
    overview = [
        ("File", source.name),
        ("Source size", f"{source.stat().st_size:,} bytes"),
        ("Rows read", rows_read),
        ("Columns", len(analysis.headers)),
        ("Date/time range", f"{format_dt(analysis.first_timestamp)} to {format_dt(analysis.last_timestamp)}"),
        ("Duration", format_duration(trend_duration_minutes(analysis))),
        ("Sampling interval estimate", format_duration((analysis.interval_seconds.most_common(1)[0][0] / 60) if analysis.interval_seconds else 0)),
        ("Detected file type", analysis.file_type),
        ("Detected equipment count", len([state for state in analysis.equipment.values() if state.equipment != "Unknown Equipment"])),
        ("Detected systems", ", ".join(systems[:12]) if systems else "Unknown"),
        ("Output", output_kind),
        ("Excel sheet split", "Automatic at 1,048,576 rows per sheet"),
        ("Chart data mode", f"Aggregated every {analysis.bucket_size_rows:,} row(s)" if analysis.bucket_size_rows > 1 else "Raw rows used; under chart threshold"),
    ]
    worksheet.write("A3", "Overview", section_format)
    worksheet.write("B3", "Value", section_format)
    for index, (label, value) in enumerate(overview, start=3):
        worksheet.write(index, 0, label, value_format)
        worksheet.write(index, 1, value, value_format)

    row = 16
    worksheet.write(row, 0, "Key Findings", section_format)
    row += 1
    for finding in build_key_findings(analysis):
        worksheet.write(row, 0, "-", value_format)
        worksheet.merge_range(row, 1, row, 7, finding, value_format)
        row += 1

    row += 1
    worksheet.write(row, 0, "Detected Equipment", section_format)
    row += 1
    for col, header in enumerate(["Equipment", "Type", "Columns", "Command", "Run proof", "Alarm/trip/fault", "Valve/VSD/analog", "Main observations"]):
        worksheet.write(row, col, header, header_format)
    row += 1
    for state in sorted(analysis.equipment.values(), key=lambda item: item.equipment):
        write_table_row(
            worksheet,
            row,
            [
                state.equipment,
                state.equipment_type,
                len(state.columns),
                "Yes" if state.command_columns else "No",
                "Yes" if state.status_columns else "No",
                "Yes" if state.alarm_columns else "No",
                "Yes" if state.analog_columns or state.vsd_columns else "No",
                equipment_observation(state, analysis),
            ],
            value_format,
        )
        row += 1

    row += 2
    worksheet.write(row, 0, "Equipment Operation Summary", section_format)
    row += 1
    operation_start = row
    op_headers = ["Equipment", "First start", "Last stop", "Starts", "Stops", "Total ON", "ON %", "Longest run", "Short cycles", "Comment"]
    for col, header in enumerate(op_headers):
        worksheet.write(row, col, header, header_format)
    row += 1
    total_minutes = max(trend_duration_minutes(analysis), 1)
    operation_first_data_row = row
    for state in sorted(analysis.equipment.values(), key=lambda item: equipment_on_minutes(item), reverse=True):
        if not state.status_columns and not state.periods:
            continue
        periods = [period for period in state.periods if period.state == 1]
        first_start = min((period.start_time for period in periods if period.start_time), default=None)
        last_stop = max((period.end_time for period in periods if period.end_time), default=None)
        on_minutes = equipment_on_minutes(state)
        comment = "Review short cycling." if state.short_cycles else ("Operation detected." if on_minutes else "No ON period detected.")
        write_table_row(
            worksheet,
            row,
            [
                state.equipment,
                format_dt(first_start),
                format_dt(last_stop),
                state.starts,
                state.stops,
                format_duration(on_minutes),
                on_minutes / total_minutes,
                format_duration(equipment_longest_run_minutes(state)),
                state.short_cycles,
                comment,
            ],
            value_format,
        )
        worksheet.write_number(row, 6, on_minutes / total_minutes, percent_format)
        row += 1
    operation_last_data_row = row - 1

    row += 2
    worksheet.write(row, 0, "Event / Alarm Summary", section_format)
    row += 1
    event_summary_start = row
    for col, header in enumerate(["Equipment", "Event type", "First occurrence", "Last occurrence", "Active duration", "Occurrences", "Severity", "Comment"]):
        worksheet.write(row, col, header, header_format)
    row += 1
    event_rows = event_summary_rows(analysis)
    if not event_rows:
        event_rows = [["Trend", "none", "Unknown", "Unknown", "0 min", 0, "Info", "No active trip/alarm/fail/lockout periods detected by generic rules."]]
    for values in event_rows[:80]:
        fmt = warning_format if values[6] in {"High", "Warning"} else value_format
        write_table_row(worksheet, row, values, fmt)
        row += 1
    event_summary_end = row - 1

    row += 2
    worksheet.write(row, 0, "Command / Status / Feedback Mismatch", section_format)
    row += 1
    for col, header in enumerate(["Timestamp", "Equipment", "Issue type", "Evidence", "Severity", "Comment"]):
        worksheet.write(row, col, header, header_format)
    row += 1
    events = analysis.events[:100] or [EventRecord(None, "Trend", "none", "", "Info", "No alarm/trip/fail/lockout transitions detected by generic rules.")]
    for event in events:
        fmt = warning_format if event.severity in {"High", "Warning"} else value_format
        write_table_row(worksheet, row, [format_dt(event.timestamp), event.equipment, event.issue_type, event.evidence, event.severity, event.comment], fmt)
        row += 1

    row += 2
    worksheet.write(row, 0, "Data Quality Notes", section_format)
    row += 1
    for col, header in enumerate(["Check", "Finding", "Severity"]):
        worksheet.write(row, col, header, header_format)
    row += 1
    for values in data_quality_rows(analysis)[:50]:
        fmt = warning_format if values[2] == "Warning" else value_format
        write_table_row(worksheet, row, values, fmt)
        row += 1

    row += 2
    section_title, file_specific_headers, file_specific_values = file_specific_rows(analysis)
    worksheet.write(row, 0, section_title, section_format)
    row += 1
    for col, header in enumerate(file_specific_headers):
        worksheet.write(row, col, header, header_format)
    row += 1
    for values in file_specific_values[:80]:
        write_table_row(worksheet, row, values, value_format)
        row += 1

    chiller_timeline_first_row: int | None = None
    chiller_timeline_last_row: int | None = None
    if analysis.file_type == "chiller":
        row += 2
        worksheet.write(row, 0, "Cooling Load Timeline", section_format)
        row += 1
        timeline_headers = [
            "Timestamp",
            "Chiller",
            "CHW entering",
            "CHW leaving",
            "CHW Delta-T",
            "CHW flow",
            "Flow unit",
            "Cooling load RT",
            "Load %",
            "Controller capacity %",
        ]
        for col, header in enumerate(timeline_headers):
            worksheet.write(row, col, header, header_format)
        row += 1
        timeline_rows = chiller_timeline_rows(analysis)
        if timeline_rows:
            chiller_timeline_first_row = row
            for values in timeline_rows[:MAX_CHILLER_TIMELINE_ROWS]:
                write_table_row(worksheet, row, values, value_format)
                row += 1
            chiller_timeline_last_row = row - 1
        else:
            worksheet.merge_range(
                row,
                0,
                row,
                9,
                "Cooling load RT cannot be calculated because CHW flow and/or CHW entering/leaving temperature points are not available.",
                value_format,
            )
            row += 1

        row += 2
        worksheet.write(row, 0, "Trip Event Context", section_format)
        row += 1
        trip_headers = [
            "Trip time",
            "Chiller",
            "Minutes since start",
            "CHW entering",
            "CHW leaving",
            "CHW Delta-T",
            "CHW flow",
            "Flow unit",
            "Calculated RT",
            "Load %",
            "Controller capacity %",
            "CDW entering",
            "CDW leaving",
            "CDW Delta-T",
            "Trip classification",
            "Engineering comment",
        ]
        for col, header in enumerate(trip_headers):
            worksheet.write(row, col, header, header_format)
        row += 1
        trip_rows = chiller_trip_context_rows(analysis)
        if trip_rows:
            for values in trip_rows[:80]:
                write_table_row(worksheet, row, values, warning_format)
                row += 1
        else:
            worksheet.merge_range(row, 0, row, 15, "No chiller trip context captured from active trip/lockout/fail transitions.", value_format)
            row += 1

    row += 2
    worksheet.write(row, 0, "Analog Trend Summary", section_format)
    row += 1
    for col, header in enumerate(["Equipment", "Metric", "Column", "Min", "Max", "Average", "Latest", "Max time", "Zero %", "Comment"]):
        worksheet.write(row, col, header, header_format)
    row += 1
    analogs = sorted((analog for analog in analysis.analogs.values() if analog.count), key=lambda item: (item.equipment, item.metric, item.column))[:40]
    for analog in analogs:
        zero_pct = analog.zero_count / max(analog.count, 1)
        comment = "Mostly zero; verify equipment run status." if zero_pct > 0.95 else ("Mostly constant; limited chart value." if analog.std_dev < 0.0001 else "Useful analog trend.")
        write_table_row(
            worksheet,
            row,
            [analog.equipment, analog.metric, analog.column, analog.minimum or 0, analog.maximum or 0, analog.mean, analog.latest or 0, format_dt(analog.max_time), zero_pct, comment],
            value_format,
        )
        for col in [3, 4, 5, 6]:
            worksheet.write_number(row, col, [analog.minimum or 0, analog.maximum or 0, analog.mean, analog.latest or 0][col - 3], number_format)
        worksheet.write_number(row, 8, zero_pct, percent_format)
        row += 1

    helper = write_chart_helper(workbook, analysis)
    chart_start = row + 2
    worksheet.write(chart_start, 0, "Charts", section_format)
    point_count = min(len(analysis.chart_buckets), MAX_CHART_POINTS)
    if point_count >= 2:
        if analysis.chart_status_columns:
            status_chart = workbook.add_chart({"type": "line"})
            for series_index, column_index in enumerate(analysis.chart_status_columns[:6], start=1 + len(analysis.chart_columns)):
                status_chart.add_series({
                    "name": ["_ChartHelper", 0, series_index],
                    "categories": ["_ChartHelper", 1, 0, point_count, 0],
                    "values": ["_ChartHelper", 1, series_index, point_count, series_index],
                })
            status_chart.set_title({"name": "Equipment Run Status Timeline"})
            status_chart.set_y_axis({"name": "State", "min": 0, "max": 1})
            worksheet.insert_chart(chart_start + 1, 0, status_chart, {"x_scale": 1.4, "y_scale": 1.1})
        if analysis.chart_columns:
            analog_chart = workbook.add_chart({"type": "line"})
            for series_index, _column_index in enumerate(analysis.chart_columns[:5], start=1):
                analog_chart.add_series({
                    "name": ["_ChartHelper", 0, series_index],
                    "categories": ["_ChartHelper", 1, 0, point_count, 0],
                    "values": ["_ChartHelper", 1, series_index, point_count, series_index],
                })
            analog_chart.set_title({"name": "Selected Analog Trends"})
            worksheet.insert_chart(chart_start + 18, 0, analog_chart, {"x_scale": 1.4, "y_scale": 1.1})
        if event_summary_end > event_summary_start:
            event_chart = workbook.add_chart({"type": "bar"})
            event_chart.add_series({
                "name": "Event occurrences",
                "categories": ["Analysis", event_summary_start + 1, 0, event_summary_end, 0],
                "values": ["Analysis", event_summary_start + 1, 5, event_summary_end, 5],
            })
            event_chart.set_title({"name": "Event Count by Equipment"})
            worksheet.insert_chart(chart_start + 35, 0, event_chart, {"x_scale": 1.3, "y_scale": 1.0})
        if operation_last_data_row >= operation_first_data_row:
            runtime_chart = workbook.add_chart({"type": "bar"})
            runtime_chart.add_series({
                "name": "Run percentage",
                "categories": ["Analysis", operation_first_data_row, 0, min(operation_last_data_row, operation_first_data_row + 20), 0],
                "values": ["Analysis", operation_first_data_row, 6, min(operation_last_data_row, operation_first_data_row + 20), 6],
            })
            runtime_chart.set_title({"name": "Runtime Comparison"})
            runtime_chart.set_x_axis({"num_format": "0%"})
            worksheet.insert_chart(chart_start + 52, 0, runtime_chart, {"x_scale": 1.3, "y_scale": 1.0})
        if analysis.file_type == "chiller" and chiller_timeline_first_row is not None and chiller_timeline_last_row is not None:
            chiller_chart = workbook.add_chart({"type": "line"})
            has_calculated_rt = any(summary.rt_count for summary in analysis.chiller_loads.values())
            if has_calculated_rt:
                chiller_chart.add_series({
                    "name": "Cooling load RT",
                    "categories": ["Analysis", chiller_timeline_first_row, 0, chiller_timeline_last_row, 0],
                    "values": ["Analysis", chiller_timeline_first_row, 7, chiller_timeline_last_row, 7],
                })
                chiller_chart.add_series({
                    "name": "Load %",
                    "categories": ["Analysis", chiller_timeline_first_row, 0, chiller_timeline_last_row, 0],
                    "values": ["Analysis", chiller_timeline_first_row, 8, chiller_timeline_last_row, 8],
                    "y2_axis": True,
                })
                chiller_chart.set_title({"name": "Calculated Cooling Load RT / Load %"})
            else:
                chiller_chart.add_series({
                    "name": "CHW Delta-T",
                    "categories": ["Analysis", chiller_timeline_first_row, 0, chiller_timeline_last_row, 0],
                    "values": ["Analysis", chiller_timeline_first_row, 4, chiller_timeline_last_row, 4],
                })
                chiller_chart.add_series({
                    "name": "Controller capacity %",
                    "categories": ["Analysis", chiller_timeline_first_row, 0, chiller_timeline_last_row, 0],
                    "values": ["Analysis", chiller_timeline_first_row, 9, chiller_timeline_last_row, 9],
                    "y2_axis": True,
                })
                chiller_chart.set_title({"name": "CHW Delta-T / Controller Capacity Reference"})
            worksheet.insert_chart(chart_start + 69, 0, chiller_chart, {"x_scale": 1.4, "y_scale": 1.0})


def process_xlsx(source: Path, output_dir: Path, progress: Any) -> ProcessResult:
    import xlsxwriter

    progress(0, "Reading file and detecting CSV columns.")
    output_path = output_dir / f"{source.stem}.xlsx"
    workbook = xlsxwriter.Workbook(output_path, {"constant_memory": True, "strings_to_urls": False, "use_zip64": True})
    header_format = workbook.add_format({"bold": True, "bg_color": "#1F2937", "font_color": "white", "border": 1})

    iterator = iter_csv(source)
    try:
        headers = unique_headers([clean_header(value, index) for index, value in enumerate(next(iterator)[:MAX_COLUMNS])])
    except StopIteration as exc:
        workbook.close()
        raise ValueError("CSV file is empty.") from exc

    stats, counters = new_stats(headers)
    analysis = build_analyzer(headers)
    analysis.bucket_size_rows = estimate_initial_bucket_size(source)
    progress(0, f"Detected file type: {analysis.file_type}. Writing Data sheet and analyzing equipment.")
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
        update_analysis(analysis, row)
        excel_row = rows_on_sheet + 1
        for col, value in enumerate(row):
            number = parse_number(value)
            if number is not None:
                worksheet.write_number(excel_row, col, number)
            elif value:
                worksheet.write(excel_row, col, value)
            else:
                worksheet.write_blank(excel_row, col, None)
        rows_on_sheet += 1
        total_rows_written += 1
        if rows_read % 10000 == 0:
            progress(rows_read, f"Analyzing rows and writing Data sheet: {rows_read:,} rows.")

    finalize_analysis(analysis)
    progress(rows_read, "Writing Analysis sheet and creating charts.")
    write_analysis_sheet(workbook, source, rows_read, total_rows_written, analysis, "xlsx")
    workbook.close()
    progress(rows_read, "Workbook created.")
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
    progress(result.rows_read, "Creating output ZIP.")
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for output_file in result.output_files:
            archive.write(output_file, output_file.name)
        archive.write(report_path, report_path.name)
    progress(result.rows_read, "Completed ZIP output.")
    return archive_path
