"""Tests for the web dashboard API endpoints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pge_tracker.config import Config
from pge_tracker.database import Database
from pge_tracker.models import (
    AccountRecord,
    CostRecord,
    DataSource,
    MeterType,
    Resolution,
    UsageRecord,
)
from pge_tracker.web import create_app

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")


@pytest.fixture
def test_config(tmp_path):
    """Config pointing at a temp DB."""
    return Config(
        username="test@example.com",
        password="testpass",
        totp_secret=None,
        db_path=tmp_path / "test.db",
        timezone="America/Los_Angeles",
        peak_hours_start=16,
        peak_hours_end=21,
        default_meter="both",
        initial_fetch_days=365,
        rate_plan="EV2-A",
    )


def _seed_electric_account(db, account_id="test-elec-001", account_number="1234567890"):
    """Insert an electric account with 7 days of hourly usage + daily cost."""
    acct = AccountRecord(
        id=account_id,
        utility="pge",
        meter_type=MeterType.ELECTRIC,
        customer_id="cust-001",
        account_number=account_number,
        service_address="123 Main St",
        source=DataSource.OPOWER,
    )
    db.upsert_account(acct)

    now = datetime.now(PACIFIC)
    records = []
    cost_records = []
    for day_offset in range(7):
        day_dt = now - timedelta(days=7 - day_offset)
        for hour in range(24):
            dt = day_dt.replace(hour=hour, minute=0, second=0, microsecond=0)
            usage = 1.5 if 16 <= hour < 21 else 0.5
            records.append(UsageRecord(
                account_id=account_id,
                start_time=dt,
                end_time=dt + timedelta(hours=1),
                unit_of_measure="KWH",
                usage=usage,
                resolution=Resolution.HOUR,
                source=DataSource.OPOWER,
            ))

        cost_records.append(CostRecord(
            account_id=account_id,
            start_time=day_dt.replace(hour=0, minute=0, second=0, microsecond=0),
            end_time=day_dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1),
            usage=24.0,
            cost=8.50,
            resolution=Resolution.DAY,
            source=DataSource.OPOWER,
        ))

    db.upsert_usage_reads(records)
    db.upsert_cost_reads(cost_records)


@pytest.fixture
def populated_db(test_config):
    """Database with a single electric account."""
    db = Database(test_config.db_path)
    db.initialize()
    _seed_electric_account(db)
    yield db
    db.close()


@pytest.fixture
def multi_account_db(test_config):
    """Database with two electric accounts (simulating CSV + API imports)."""
    db = Database(test_config.db_path)
    db.initialize()
    _seed_electric_account(db, "gb-electric", "1111111111")
    _seed_electric_account(db, "api-electric", "2222222222")
    yield db
    db.close()


@pytest.fixture
def client(test_config, populated_db):
    """Flask test client with single electric account."""
    app = create_app(test_config)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def multi_client(test_config, multi_account_db):
    """Flask test client with two electric accounts."""
    app = create_app(test_config)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


class TestDashboardPage:
    def test_dashboard_returns_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"PG&amp;E Energy Dashboard" in resp.data

    def test_dashboard_contains_chart_elements(self, client):
        resp = client.get("/")
        assert b"dailyChart" in resp.data
        assert b"hourlyChart" in resp.data
        assert b"heatmapContainer" in resp.data

    def test_dashboard_has_meter_selector(self, client):
        resp = client.get("/")
        assert b"meterSelect" in resp.data
        assert b"All Electric" in resp.data


class TestAccountsAPI:
    def test_returns_grouped_meters(self, client):
        resp = client.get("/api/accounts")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data) == 1
        assert data[0]["meter_type"] == "electric"
        assert data[0]["label"] == "All Electric"
        assert data[0]["account_count"] == 1

    def test_multi_account_grouped(self, multi_client):
        resp = multi_client.get("/api/accounts")
        data = json.loads(resp.data)
        assert len(data) == 1  # Both are electric, grouped into one
        assert data[0]["account_count"] == 2


class TestDailyAPI:
    def test_returns_daily_data(self, client):
        resp = client.get("/api/daily?meter_type=electric&days=7")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data) > 0
        assert "day" in data[0]
        assert "usage" in data[0]
        assert "cost" in data[0]

    def test_defaults_to_electric(self, client):
        resp = client.get("/api/daily?days=7")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data) > 0

    def test_invalid_meter_type(self, client):
        resp = client.get("/api/daily?meter_type=invalid")
        assert resp.status_code == 400


class TestHourlyProfileAPI:
    def test_returns_24_hours(self, client):
        resp = client.get("/api/hourly-profile?meter_type=electric&days=7")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data) == 24

    def test_peak_hours_marked(self, client):
        resp = client.get("/api/hourly-profile?meter_type=electric&days=7")
        data = json.loads(resp.data)
        for h in data:
            if 16 <= h["hour"] < 21:
                assert h["period"] == "peak"


class TestHeatmapAPI:
    def test_returns_7x24_matrix(self, client):
        resp = client.get("/api/heatmap?meter_type=electric&days=7")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data["matrix"]) == 7
        for row in data["matrix"]:
            assert len(row) == 24

    def test_includes_peak_hours_info(self, client):
        resp = client.get("/api/heatmap?meter_type=electric&days=7")
        data = json.loads(resp.data)
        assert "peak_hours" in data
        assert data["peak_hours"] == [16, 21]


class TestPeakDaysAPI:
    def test_returns_peak_days(self, client):
        resp = client.get("/api/peak-days?meter_type=electric&days=7&top_n=3")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert isinstance(data, list)


class TestSummaryAPI:
    def test_returns_summary(self, client):
        resp = client.get("/api/summary?meter_type=electric&days=7")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "total_usage_kwh" in data
        assert "total_cost" in data
        assert "avg_daily_usage" in data
        assert "tou" in data
        assert "projection" in data
        assert "rate_plan" in data
        assert data["rate_plan"] == "EV2-A"


class TestMonthlyAPI:
    def test_returns_monthly(self, client):
        resp = client.get("/api/monthly?meter_type=electric&months=1")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert isinstance(data, list)
        if data:
            assert "label" in data[0]
            assert "total_usage" in data[0]


class TestMultiAccountMerging:
    """Verify data from multiple electric accounts is combined."""

    def test_daily_combines_accounts(self, multi_client):
        resp = multi_client.get("/api/daily?meter_type=electric&days=7")
        data = json.loads(resp.data)
        assert len(data) > 0
        # Each account has 19*0.5 + 5*1.5 = 17 kWh/day usage.
        # daily_stats sums by date, so combined = ~34 kWh/day
        for d in data:
            if d["usage"] > 0:
                assert d["usage"] > 17  # more than a single account

    def test_summary_combines_accounts(self, multi_client):
        resp = multi_client.get("/api/summary?meter_type=electric&days=7")
        data = json.loads(resp.data)
        # Two accounts: usage is summed per day, so avg daily should be > 17
        assert data["avg_daily_usage"] > 17

    def test_summary_combines_costs(self, multi_client):
        resp = multi_client.get("/api/summary?meter_type=electric&days=7")
        data = json.loads(resp.data)
        # Two accounts each with $8.50/day cost
        assert data["total_cost"] > 50  # 7 days * $8.50 * 2 accounts ~ $119

    def test_hourly_profile_includes_all_readings(self, multi_client):
        resp = multi_client.get("/api/hourly-profile?meter_type=electric&days=7")
        data = json.loads(resp.data)
        assert len(data) == 24
        # hourly_profile averages per reading — with identical accounts
        # the avg stays the same, but count should be doubled
        peak_hour = next(h for h in data if h["hour"] == 17)
        assert peak_hour["count"] == 14  # 7 days * 2 accounts
