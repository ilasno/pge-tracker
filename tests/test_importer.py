"""Tests for the Green Button CSV importer."""

from __future__ import annotations

from pathlib import Path

from pge_tracker.importer import detect_green_button_format, parse_green_button_csv
from pge_tracker.models import MeterType, Resolution

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_detect_electric_csv():
    info = detect_green_button_format(FIXTURES_DIR / "sample_electric.csv")
    assert info["meter_type"] == "electric"
    assert info["resolution"] == "hourly"
    assert info["row_count"] == 48
    assert info["date_range"][0] == "2024-01-15"
    assert info["date_range"][1] == "2024-01-16"


def test_detect_gas_csv():
    info = detect_green_button_format(FIXTURES_DIR / "sample_gas.csv")
    assert info["meter_type"] == "gas"
    assert info["resolution"] == "hourly"
    assert info["row_count"] == 24


def test_parse_electric_csv():
    records = parse_green_button_csv(
        FIXTURES_DIR / "sample_electric.csv",
        MeterType.ELECTRIC,
        "test-acct",
    )
    assert len(records) == 48
    assert records[0].unit_of_measure == "KWH"
    assert records[0].resolution == Resolution.HOUR
    assert records[0].usage == 0.412
    assert records[0].account_id == "test-acct"


def test_parse_gas_csv():
    records = parse_green_button_csv(
        FIXTURES_DIR / "sample_gas.csv",
        MeterType.GAS,
        "test-gas",
    )
    assert len(records) == 24
    assert records[0].unit_of_measure == "CCF"
    assert records[0].usage == 0.042


def test_parse_timestamps_are_timezone_aware():
    records = parse_green_button_csv(
        FIXTURES_DIR / "sample_electric.csv",
        MeterType.ELECTRIC,
        "test-acct",
    )
    assert records[0].start_time.tzinfo is not None


def test_midnight_wrap():
    """The 23:00-00:00 hour should have end_time on the next day."""
    records = parse_green_button_csv(
        FIXTURES_DIR / "sample_electric.csv",
        MeterType.ELECTRIC,
        "test-acct",
    )
    # Find the 23:00 entry for Jan 15
    last_hour = [r for r in records if r.start_time.hour == 23 and r.start_time.day == 15]
    assert len(last_hour) == 1
    assert last_hour[0].end_time.day == 16
    assert last_hour[0].end_time.hour == 0
