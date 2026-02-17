"""Shared test fixtures."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pge_tracker.database import Database
from pge_tracker.models import (
    AccountRecord,
    CostRecord,
    DataSource,
    MeterType,
    Resolution,
    UsageRecord,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PACIFIC = ZoneInfo("America/Los_Angeles")


@pytest.fixture
def tmp_db(tmp_path):
    """Fresh initialized Database in a temp directory."""
    db = Database(tmp_path / "test.db")
    db.initialize()
    yield db
    db.close()


@pytest.fixture
def electric_account() -> AccountRecord:
    return AccountRecord(
        id="test-electric-001",
        utility="pge",
        meter_type=MeterType.ELECTRIC,
        customer_id="cust-001",
        account_number="1234567890",
        service_address="123 Main St",
        source=DataSource.OPOWER,
    )


@pytest.fixture
def gas_account() -> AccountRecord:
    return AccountRecord(
        id="test-gas-001",
        utility="pge",
        meter_type=MeterType.GAS,
        customer_id="cust-001",
        account_number="0987654321",
        service_address="123 Main St",
        source=DataSource.OPOWER,
    )


@pytest.fixture
def sample_daily_usage(electric_account) -> list[UsageRecord]:
    """90 days of realistic electric usage with seasonal variation."""
    random.seed(42)
    records = []
    start = date(2024, 8, 1)
    for i in range(90):
        day = start + timedelta(days=i)
        # Base usage with seasonal component (summer = higher)
        month = day.month
        if month in (6, 7, 8):
            base = 22.0  # Summer: AC
        elif month in (12, 1, 2):
            base = 18.0  # Winter: heating
        else:
            base = 14.0  # Spring/Fall

        # Weekend bump
        if day.weekday() >= 5:
            base += 3.0

        usage = base + random.gauss(0, 2.5)
        usage = max(usage, 2.0)

        records.append(UsageRecord(
            account_id=electric_account.id,
            start_time=datetime.combine(day, datetime.min.time(), tzinfo=PACIFIC),
            end_time=datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=PACIFIC),
            unit_of_measure="KWH",
            usage=round(usage, 2),
            resolution=Resolution.DAY,
            source=DataSource.OPOWER,
        ))
    return records


@pytest.fixture
def sample_hourly_usage(electric_account) -> list[UsageRecord]:
    """30 days of hourly electric usage."""
    random.seed(42)
    records = []
    start = date(2024, 10, 1)
    for day_offset in range(30):
        day = start + timedelta(days=day_offset)
        for hour in range(24):
            # Usage pattern: low at night, peak in evening
            if hour < 6:
                base = 0.3
            elif hour < 9:
                base = 0.7
            elif hour < 16:
                base = 0.5
            elif hour < 21:
                base = 1.2  # Peak hours
            else:
                base = 0.6

            usage = base + random.gauss(0, 0.1)
            usage = max(usage, 0.05)

            dt = datetime(day.year, day.month, day.day, hour, tzinfo=PACIFIC)
            records.append(UsageRecord(
                account_id=electric_account.id,
                start_time=dt,
                end_time=dt + timedelta(hours=1),
                unit_of_measure="KWH",
                usage=round(usage, 3),
                resolution=Resolution.HOUR,
                source=DataSource.OPOWER,
            ))
    return records


@pytest.fixture
def sample_cost_reads(electric_account, sample_daily_usage) -> list[CostRecord]:
    """Cost data matching the daily usage fixture (approx $0.30/kWh)."""
    return [
        CostRecord(
            account_id=electric_account.id,
            start_time=u.start_time,
            end_time=u.end_time,
            usage=u.usage,
            cost=round(u.usage * 0.30, 2),
            resolution=Resolution.DAY,
            source=DataSource.OPOWER,
        )
        for u in sample_daily_usage
    ]
