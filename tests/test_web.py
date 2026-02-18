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


@pytest.fixture
def populated_db(test_config):
    """Database with a sample electric account and usage data."""
    db = Database(test_config.db_path)
    db.initialize()

    acct = AccountRecord(
        id="test-elec-001",
        utility="pge",
        meter_type=MeterType.ELECTRIC,
        customer_id="cust-001",
        account_number="1234567890",
        service_address="123 Main St",
        source=DataSource.OPOWER,
    )
    db.upsert_account(acct)

    # Generate 7 days of hourly usage
    now = datetime.now(PACIFIC)
    records = []
    cost_records = []
    for day_offset in range(7):
        day_dt = now - timedelta(days=7 - day_offset)
        for hour in range(24):
            dt = day_dt.replace(hour=hour, minute=0, second=0, microsecond=0)
            # Higher usage during peak
            usage = 1.5 if 16 <= hour < 21 else 0.5
            records.append(UsageRecord(
                account_id="test-elec-001",
                start_time=dt,
                end_time=dt + timedelta(hours=1),
                unit_of_measure="KWH",
                usage=usage,
                resolution=Resolution.HOUR,
                source=DataSource.OPOWER,
            ))

        # Daily cost
        cost_records.append(CostRecord(
            account_id="test-elec-001",
            start_time=day_dt.replace(hour=0, minute=0, second=0, microsecond=0),
            end_time=day_dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1),
            usage=24.0,
            cost=8.50,
            resolution=Resolution.DAY,
            source=DataSource.OPOWER,
        ))

    db.upsert_usage_reads(records)
    db.upsert_cost_reads(cost_records)

    yield db
    db.close()


@pytest.fixture
def client(test_config, populated_db):
    """Flask test client."""
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


class TestAccountsAPI:
    def test_returns_accounts(self, client):
        resp = client.get("/api/accounts")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data) == 1
        assert data[0]["id"] == "test-elec-001"
        assert data[0]["meter_type"] == "ELECTRIC"


class TestDailyAPI:
    def test_returns_daily_data(self, client):
        resp = client.get("/api/daily?account_id=test-elec-001&days=7")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data) > 0
        assert "day" in data[0]
        assert "usage" in data[0]
        assert "cost" in data[0]

    def test_requires_account_id(self, client):
        resp = client.get("/api/daily")
        assert resp.status_code == 400


class TestHourlyProfileAPI:
    def test_returns_24_hours(self, client):
        resp = client.get("/api/hourly-profile?account_id=test-elec-001&days=7")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data) == 24

    def test_peak_hours_marked(self, client):
        resp = client.get("/api/hourly-profile?account_id=test-elec-001&days=7")
        data = json.loads(resp.data)
        for h in data:
            if 16 <= h["hour"] < 21:
                assert h["period"] == "peak"

    def test_requires_account_id(self, client):
        resp = client.get("/api/hourly-profile")
        assert resp.status_code == 400


class TestHeatmapAPI:
    def test_returns_7x24_matrix(self, client):
        resp = client.get("/api/heatmap?account_id=test-elec-001&days=7")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data["matrix"]) == 7
        for row in data["matrix"]:
            assert len(row) == 24

    def test_includes_peak_hours_info(self, client):
        resp = client.get("/api/heatmap?account_id=test-elec-001&days=7")
        data = json.loads(resp.data)
        assert "peak_hours" in data
        assert data["peak_hours"] == [16, 21]


class TestPeakDaysAPI:
    def test_returns_peak_days(self, client):
        resp = client.get("/api/peak-days?account_id=test-elec-001&days=7&top_n=3")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        # May have fewer if weekends are in the 7-day window
        assert isinstance(data, list)

    def test_requires_account_id(self, client):
        resp = client.get("/api/peak-days")
        assert resp.status_code == 400


class TestSummaryAPI:
    def test_returns_summary(self, client):
        resp = client.get("/api/summary?account_id=test-elec-001&days=7")
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
        resp = client.get("/api/monthly?account_id=test-elec-001&months=1")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert isinstance(data, list)
        if data:
            assert "label" in data[0]
            assert "total_usage" in data[0]
