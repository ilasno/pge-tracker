"""Tests for the database module."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from pge_tracker.database import Database
from pge_tracker.models import (
    AccountRecord,
    CostRecord,
    DataSource,
    ForecastRecord,
    MeterType,
    Resolution,
    UsageRecord,
)

PACIFIC = ZoneInfo("America/Los_Angeles")


def test_initialize_creates_tables(tmp_db: Database):
    stats = tmp_db.get_db_stats()
    assert "accounts" in stats
    assert "usage_reads" in stats
    assert "cost_reads" in stats
    assert "forecasts" in stats
    assert "fetch_log" in stats


def test_upsert_account(tmp_db: Database, electric_account: AccountRecord):
    tmp_db.upsert_account(electric_account)
    accounts = tmp_db.get_accounts()
    assert len(accounts) == 1
    assert accounts[0].id == electric_account.id
    assert accounts[0].meter_type == MeterType.ELECTRIC


def test_upsert_account_is_idempotent(tmp_db: Database, electric_account: AccountRecord):
    tmp_db.upsert_account(electric_account)
    tmp_db.upsert_account(electric_account)
    accounts = tmp_db.get_accounts()
    assert len(accounts) == 1


def test_upsert_account_updates(tmp_db: Database, electric_account: AccountRecord):
    tmp_db.upsert_account(electric_account)

    updated = AccountRecord(
        id=electric_account.id,
        utility="pge",
        meter_type=MeterType.ELECTRIC,
        customer_id="new-cust",
        account_number="9999999999",
        service_address="456 Oak Ave",
        source=DataSource.OPOWER,
    )
    tmp_db.upsert_account(updated)

    acct = tmp_db.get_account(electric_account.id)
    assert acct is not None
    assert acct.account_number == "9999999999"
    assert acct.service_address == "456 Oak Ave"


def test_upsert_usage_reads(tmp_db: Database, electric_account: AccountRecord):
    tmp_db.upsert_account(electric_account)
    reads = [
        UsageRecord(
            account_id=electric_account.id,
            start_time=datetime(2024, 1, 15, 0, tzinfo=PACIFIC),
            end_time=datetime(2024, 1, 15, 1, tzinfo=PACIFIC),
            unit_of_measure="KWH",
            usage=0.5,
            resolution=Resolution.HOUR,
            source=DataSource.OPOWER,
        ),
        UsageRecord(
            account_id=electric_account.id,
            start_time=datetime(2024, 1, 15, 1, tzinfo=PACIFIC),
            end_time=datetime(2024, 1, 15, 2, tzinfo=PACIFIC),
            unit_of_measure="KWH",
            usage=0.3,
            resolution=Resolution.HOUR,
            source=DataSource.OPOWER,
        ),
    ]
    count = tmp_db.upsert_usage_reads(reads)
    assert count == 2

    stored = tmp_db.get_usage_reads(electric_account.id, Resolution.HOUR)
    assert len(stored) == 2
    assert stored[0].usage == 0.5
    assert stored[1].usage == 0.3


def test_upsert_usage_is_idempotent(tmp_db: Database, electric_account: AccountRecord):
    tmp_db.upsert_account(electric_account)
    read = UsageRecord(
        account_id=electric_account.id,
        start_time=datetime(2024, 1, 15, 0, tzinfo=PACIFIC),
        end_time=datetime(2024, 1, 15, 1, tzinfo=PACIFIC),
        unit_of_measure="KWH",
        usage=0.5,
        resolution=Resolution.HOUR,
        source=DataSource.OPOWER,
    )
    tmp_db.upsert_usage_reads([read])
    tmp_db.upsert_usage_reads([read])

    stored = tmp_db.get_usage_reads(electric_account.id, Resolution.HOUR)
    assert len(stored) == 1


def test_upsert_usage_updates_value(tmp_db: Database, electric_account: AccountRecord):
    tmp_db.upsert_account(electric_account)
    ts = datetime(2024, 1, 15, 0, tzinfo=PACIFIC)

    read_v1 = UsageRecord(
        account_id=electric_account.id,
        start_time=ts,
        end_time=ts.replace(hour=1),
        unit_of_measure="KWH",
        usage=0.5,
        resolution=Resolution.HOUR,
        source=DataSource.OPOWER,
    )
    tmp_db.upsert_usage_reads([read_v1])

    read_v2 = UsageRecord(
        account_id=electric_account.id,
        start_time=ts,
        end_time=ts.replace(hour=1),
        unit_of_measure="KWH",
        usage=0.75,
        resolution=Resolution.HOUR,
        source=DataSource.OPOWER,
    )
    tmp_db.upsert_usage_reads([read_v2])

    stored = tmp_db.get_usage_reads(electric_account.id, Resolution.HOUR)
    assert len(stored) == 1
    assert stored[0].usage == 0.75


def test_get_latest_usage_time(tmp_db: Database, electric_account: AccountRecord):
    tmp_db.upsert_account(electric_account)

    # No data yet
    assert tmp_db.get_latest_usage_time(electric_account.id, Resolution.HOUR) is None

    reads = [
        UsageRecord(
            account_id=electric_account.id,
            start_time=datetime(2024, 1, 15, h, tzinfo=PACIFIC),
            end_time=datetime(2024, 1, 15, h + 1, tzinfo=PACIFIC),
            unit_of_measure="KWH",
            usage=0.5,
            resolution=Resolution.HOUR,
            source=DataSource.OPOWER,
        )
        for h in range(5)
    ]
    tmp_db.upsert_usage_reads(reads)

    latest = tmp_db.get_latest_usage_time(electric_account.id, Resolution.HOUR)
    assert latest is not None
    assert latest.hour == 4


def test_usage_date_range_filter(tmp_db: Database, electric_account: AccountRecord):
    tmp_db.upsert_account(electric_account)

    reads = [
        UsageRecord(
            account_id=electric_account.id,
            start_time=datetime(2024, 1, d, tzinfo=PACIFIC),
            end_time=datetime(2024, 1, d + 1, tzinfo=PACIFIC),
            unit_of_measure="KWH",
            usage=float(d),
            resolution=Resolution.DAY,
            source=DataSource.OPOWER,
        )
        for d in range(1, 11)
    ]
    tmp_db.upsert_usage_reads(reads)

    filtered = tmp_db.get_usage_reads(
        electric_account.id,
        Resolution.DAY,
        start=datetime(2024, 1, 3, tzinfo=PACIFIC),
        end=datetime(2024, 1, 7, tzinfo=PACIFIC),
    )
    assert len(filtered) == 5
    assert filtered[0].usage == 3.0
    assert filtered[-1].usage == 7.0


def test_cost_reads_crud(tmp_db: Database, electric_account: AccountRecord):
    tmp_db.upsert_account(electric_account)

    costs = [
        CostRecord(
            account_id=electric_account.id,
            start_time=datetime(2024, 1, d, tzinfo=PACIFIC),
            end_time=datetime(2024, 1, d + 1, tzinfo=PACIFIC),
            usage=10.0,
            cost=3.0,
            resolution=Resolution.DAY,
            source=DataSource.OPOWER,
        )
        for d in range(1, 4)
    ]
    count = tmp_db.upsert_cost_reads(costs)
    assert count == 3

    stored = tmp_db.get_cost_reads(electric_account.id, Resolution.DAY)
    assert len(stored) == 3
    assert stored[0].cost == 3.0


def test_fetch_log(tmp_db: Database):
    tmp_db.log_fetch("acct-1", "cost_daily", rows_written=100)
    tmp_db.log_fetch("acct-1", "usage_hourly", rows_written=720)

    log = tmp_db.get_fetch_log(limit=10)
    assert len(log) == 2
    # Most recent first
    assert log[0]["fetch_type"] == "usage_hourly"
    assert log[0]["rows_written"] == 720


def test_db_stats(tmp_db: Database, electric_account: AccountRecord):
    tmp_db.upsert_account(electric_account)
    stats = tmp_db.get_db_stats()
    assert stats["accounts"] == 1
    assert stats["usage_reads"] == 0
    assert stats["annotations"] == 0


# --- Annotation tests ---


def test_add_annotation(tmp_db: Database):
    from datetime import date

    from pge_tracker.models import AnnotationRecord

    ann = AnnotationRecord(
        note_date=date(2026, 2, 20),
        note_time="14:30",
        description="Started pre-cooling before peak",
    )
    row_id = tmp_db.add_annotation(ann)
    assert row_id is not None
    assert row_id > 0

    annotations = tmp_db.get_annotations(limit=10)
    assert len(annotations) == 1
    assert annotations[0]["note_date"] == "2026-02-20"
    assert annotations[0]["note_time"] == "14:30"
    assert annotations[0]["description"] == "Started pre-cooling before peak"


def test_get_annotations_ordering(tmp_db: Database):
    from datetime import date

    from pge_tracker.models import AnnotationRecord

    tmp_db.add_annotation(AnnotationRecord(
        note_date=date(2026, 2, 18), note_time=None, description="First note",
    ))
    tmp_db.add_annotation(AnnotationRecord(
        note_date=date(2026, 2, 22), note_time=None, description="Third note",
    ))
    tmp_db.add_annotation(AnnotationRecord(
        note_date=date(2026, 2, 20), note_time=None, description="Second note",
    ))

    annotations = tmp_db.get_annotations(limit=10)
    assert len(annotations) == 3
    # Newest date first
    assert annotations[0]["note_date"] == "2026-02-22"
    assert annotations[1]["note_date"] == "2026-02-20"
    assert annotations[2]["note_date"] == "2026-02-18"


def test_get_annotations_limit(tmp_db: Database):
    from datetime import date, timedelta

    from pge_tracker.models import AnnotationRecord

    for i in range(15):
        d = date(2026, 1, 1) + timedelta(days=i)
        tmp_db.add_annotation(AnnotationRecord(
            note_date=d, note_time=None, description=f"Note {i}",
        ))

    annotations = tmp_db.get_annotations(limit=10)
    assert len(annotations) == 10


def test_get_annotations_for_dates(tmp_db: Database):
    from datetime import date

    from pge_tracker.models import AnnotationRecord

    tmp_db.add_annotation(AnnotationRecord(
        note_date=date(2026, 2, 20), note_time=None, description="Note A",
    ))
    tmp_db.add_annotation(AnnotationRecord(
        note_date=date(2026, 2, 22), note_time=None, description="Note B",
    ))

    result = tmp_db.get_annotations_for_dates(["2026-02-20", "2026-02-21", "2026-02-22"])
    assert "2026-02-20" in result
    assert "2026-02-22" in result
    assert "2026-02-21" not in result


def test_annotations_table_in_stats(tmp_db: Database):
    stats = tmp_db.get_db_stats()
    assert "annotations" in stats
    assert stats["annotations"] == 0
