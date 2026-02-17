"""Internal data types for pge-tracker, decoupled from opower's types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class MeterType(Enum):
    ELECTRIC = "ELECTRIC"
    GAS = "GAS"


class Resolution(Enum):
    HOUR = "HOUR"
    DAY = "DAY"
    BILL = "BILL"


class DataSource(Enum):
    OPOWER = "opower"
    GREEN_BUTTON = "green_button"


@dataclass
class AccountRecord:
    id: str
    utility: str
    meter_type: MeterType
    customer_id: str | None
    account_number: str | None
    service_address: str | None
    source: DataSource


@dataclass
class UsageRecord:
    account_id: str
    start_time: datetime
    end_time: datetime
    unit_of_measure: str  # "KWH", "THERM", "CCF"
    usage: float
    resolution: Resolution
    source: DataSource


@dataclass
class CostRecord:
    account_id: str
    start_time: datetime
    end_time: datetime
    usage: float | None
    cost: float | None
    resolution: Resolution
    source: DataSource


@dataclass
class ForecastRecord:
    account_id: str
    start_date: date
    end_date: date
    current_date: date
    unit_of_measure: str
    usage_to_date: float | None
    forecasted_usage: float | None
    typical_usage: float | None
    cost_to_date: float | None
    forecasted_cost: float | None
    typical_cost: float | None


@dataclass
class DailyStats:
    day: date
    usage: float
    cost: float | None
    is_weekend: bool


@dataclass
class PeriodSummary:
    label: str
    total_usage: float
    total_cost: float | None
    avg_daily_usage: float
    peak_day: date | None
    peak_day_usage: float | None


@dataclass
class TouAnalysis:
    peak_total_kwh: float
    offpeak_total_kwh: float
    peak_pct: float
    peak_cost_estimate: float | None
    offpeak_cost_estimate: float | None
    peak_days: list[tuple[date, float]]  # top worst peak days


@dataclass
class AnomalyResult:
    timestamp: datetime
    usage: float
    expected_usage: float
    z_score: float
    severity: str  # "moderate" | "high" | "extreme"
    description: str


@dataclass
class YoyComparison:
    current_period_label: str
    prior_period_label: str
    current_usage: float
    prior_usage: float
    change_pct: float
    current_cost: float | None
    prior_cost: float | None


@dataclass
class SeasonalPattern:
    season: str  # "Winter" | "Spring" | "Summer" | "Fall"
    avg_daily_usage: float
    avg_daily_cost: float | None
    months: list[str]


@dataclass
class FetchSummary:
    accounts_found: int
    daily_records_written: int
    hourly_records_written: int
    forecasts_written: int
    fetch_duration_seconds: float
    errors: list[str]
