"""Internal data types for pge-tracker, decoupled from opower's types."""

from __future__ import annotations

from dataclasses import dataclass, field
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


# --- Peak / TOU analysis models ---


class TouPeriod(Enum):
    PEAK = "peak"
    PART_PEAK = "part_peak"
    OFF_PEAK = "off_peak"


@dataclass
class RatePlan:
    """Rate plan with per-period $/kWh rates and time windows."""

    name: str
    # Summer = June-September, Winter = October-May (for PG&E)
    summer_months: tuple[int, ...] = (6, 7, 8, 9)
    # Time windows: hour ranges (inclusive start, exclusive end)
    peak_hours: tuple[int, int] = (16, 21)           # 4-9pm
    part_peak_hours: list[tuple[int, int]] = field(
        default_factory=lambda: [(15, 16), (21, 24)]  # 3-4pm, 9pm-12am
    )
    # Rates in $/kWh
    summer_peak: float = 0.0
    summer_part_peak: float = 0.0
    summer_off_peak: float = 0.0
    winter_peak: float = 0.0
    winter_part_peak: float = 0.0
    winter_off_peak: float = 0.0

    def rate_for(self, month: int, hour: int, is_weekday: bool = True) -> float:
        """Get the $/kWh rate for a given month, hour, and day type."""
        period = self.classify_hour(hour, is_weekday)
        is_summer = month in self.summer_months
        if period == TouPeriod.PEAK:
            return self.summer_peak if is_summer else self.winter_peak
        elif period == TouPeriod.PART_PEAK:
            return self.summer_part_peak if is_summer else self.winter_part_peak
        else:
            return self.summer_off_peak if is_summer else self.winter_off_peak

    def classify_hour(self, hour: int, is_weekday: bool = True) -> TouPeriod:
        """Classify an hour into peak, part-peak, or off-peak."""
        ps, pe = self.peak_hours
        if ps <= hour < pe:
            return TouPeriod.PEAK
        for pps, ppe in self.part_peak_hours:
            if pps <= hour < ppe:
                return TouPeriod.PART_PEAK
        return TouPeriod.OFF_PEAK


# PG&E EV2-A (Home Charging) — effective 2025/2026
EV2A = RatePlan(
    name="EV2-A",
    summer_months=(6, 7, 8, 9),
    peak_hours=(16, 21),
    part_peak_hours=[(15, 16), (21, 24)],
    summer_peak=0.60,
    summer_part_peak=0.49,
    summer_off_peak=0.28,
    winter_peak=0.47,
    winter_part_peak=0.45,
    winter_off_peak=0.28,
)

# PG&E E-TOU-C — standard residential TOU
E_TOU_C = RatePlan(
    name="E-TOU-C",
    summer_months=(6, 7, 8, 9),
    peak_hours=(16, 21),
    part_peak_hours=[],  # E-TOU-C has no part-peak
    summer_peak=0.54,
    summer_part_peak=0.38,  # same as off-peak (no part-peak)
    summer_off_peak=0.38,
    winter_peak=0.36,
    winter_part_peak=0.36,
    winter_off_peak=0.36,
)

RATE_PLANS: dict[str, RatePlan] = {
    "EV2-A": EV2A,
    "E-TOU-C": E_TOU_C,
}


@dataclass
class HourOfDayStats:
    """Average usage/cost for a single hour-of-day slot (0-23)."""

    hour: int
    avg_usage: float
    max_usage: float
    count: int  # number of days averaged
    period: TouPeriod
    est_cost_per_hour: float  # estimated cost at plan rate


@dataclass
class PeakDayDetail:
    """Hour-by-hour breakdown for a single high-peak day."""

    day: date
    peak_total: float  # kWh during peak window
    daily_total: float  # kWh for the full day
    hourly: list[tuple[int, float]]  # (hour, kWh) for peak+part-peak hours
    est_peak_cost: float  # estimated cost for peak hours this day


@dataclass
class AnnotationRecord:
    """A user note about an energy behavior change."""

    note_date: date
    note_time: str | None  # e.g. "14:30" or None
    description: str


@dataclass
class ShiftSavings:
    """Estimated savings from shifting peak usage to off-peak."""

    avg_daily_peak_kwh: float
    avg_daily_peak_cost: float
    monthly_peak_cost: float
    heaviest_hours: list[tuple[int, float]]  # (hour, avg_kwh) top 3
    est_monthly_savings_low: float
    est_monthly_savings_high: float
    peak_rate: float  # current avg peak $/kWh
    offpeak_rate: float  # current avg off-peak $/kWh
