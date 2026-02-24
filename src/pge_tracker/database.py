"""SQLite storage layer for pge-tracker."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import (
    AccountRecord,
    AnnotationRecord,
    CostRecord,
    DataSource,
    ForecastRecord,
    MeterType,
    Resolution,
    UsageRecord,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id               TEXT PRIMARY KEY,
    utility          TEXT NOT NULL,
    meter_type       TEXT NOT NULL,
    customer_id      TEXT,
    account_number   TEXT,
    service_address  TEXT,
    source           TEXT NOT NULL DEFAULT 'opower',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_reads (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       TEXT NOT NULL REFERENCES accounts(id),
    start_time       TEXT NOT NULL,
    end_time         TEXT NOT NULL,
    unit_of_measure  TEXT NOT NULL,
    usage            REAL NOT NULL,
    resolution       TEXT NOT NULL,
    source           TEXT NOT NULL,
    imported_at      TEXT NOT NULL,
    UNIQUE(account_id, start_time, resolution)
);

CREATE INDEX IF NOT EXISTS idx_usage_account_time
    ON usage_reads(account_id, start_time);
CREATE INDEX IF NOT EXISTS idx_usage_resolution
    ON usage_reads(resolution);

CREATE TABLE IF NOT EXISTS cost_reads (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       TEXT NOT NULL REFERENCES accounts(id),
    start_time       TEXT NOT NULL,
    end_time         TEXT NOT NULL,
    usage            REAL,
    cost             REAL,
    resolution       TEXT NOT NULL,
    source           TEXT NOT NULL,
    imported_at      TEXT NOT NULL,
    UNIQUE(account_id, start_time, resolution)
);

CREATE INDEX IF NOT EXISTS idx_cost_account_time
    ON cost_reads(account_id, start_time);

CREATE TABLE IF NOT EXISTS forecasts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       TEXT NOT NULL REFERENCES accounts(id),
    start_date       TEXT NOT NULL,
    end_date         TEXT NOT NULL,
    "current_date"   TEXT NOT NULL,
    unit_of_measure  TEXT NOT NULL,
    usage_to_date    REAL,
    forecasted_usage REAL,
    typical_usage    REAL,
    cost_to_date     REAL,
    forecasted_cost  REAL,
    typical_cost     REAL,
    fetched_at       TEXT NOT NULL,
    UNIQUE(account_id, start_date, "current_date")
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       TEXT NOT NULL,
    fetch_type       TEXT NOT NULL,
    start_time       TEXT,
    end_time         TEXT,
    rows_written     INTEGER DEFAULT 0,
    status           TEXT NOT NULL,
    error_message    TEXT,
    fetched_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS annotations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    note_date        TEXT NOT NULL,
    note_time        TEXT,
    description      TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_annotations_date
    ON annotations(note_date);
"""


class Database:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def initialize(self) -> None:
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Database:
        self.initialize()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- Accounts ---

    def upsert_account(self, account: AccountRecord) -> None:
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            """INSERT INTO accounts
               (id, utility, meter_type, customer_id, account_number,
                service_address, source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   meter_type = excluded.meter_type,
                   customer_id = excluded.customer_id,
                   account_number = excluded.account_number,
                   service_address = excluded.service_address,
                   updated_at = excluded.updated_at
            """,
            (
                account.id,
                account.utility,
                account.meter_type.value,
                account.customer_id,
                account.account_number,
                account.service_address,
                account.source.value,
                now,
                now,
            ),
        )
        self._conn.commit()

    def get_accounts(self) -> list[AccountRecord]:
        rows = self._conn.execute("SELECT * FROM accounts").fetchall()
        return [_row_to_account(r) for r in rows]

    def get_account(self, account_id: str) -> AccountRecord | None:
        row = self._conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        return _row_to_account(row) if row else None

    def get_account_ids_by_meter_type(self, meter_type: MeterType) -> list[str]:
        """Return all account IDs for a given meter type."""
        rows = self._conn.execute(
            "SELECT id FROM accounts WHERE meter_type = ?",
            (meter_type.value,),
        ).fetchall()
        return [r["id"] for r in rows]

    # --- Usage reads ---

    def upsert_usage_reads(self, reads: list[UsageRecord]) -> int:
        if not reads:
            return 0
        now = datetime.now(UTC).isoformat()
        self._conn.executemany(
            """INSERT INTO usage_reads
               (account_id, start_time, end_time, unit_of_measure,
                usage, resolution, source, imported_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(account_id, start_time, resolution) DO UPDATE SET
                   end_time = excluded.end_time,
                   usage = excluded.usage,
                   imported_at = excluded.imported_at
            """,
            [
                (
                    r.account_id,
                    r.start_time.isoformat(),
                    r.end_time.isoformat(),
                    r.unit_of_measure,
                    r.usage,
                    r.resolution.value,
                    r.source.value,
                    now,
                )
                for r in reads
            ],
        )
        self._conn.commit()
        return len(reads)

    def get_usage_reads(
        self,
        account_id: str,
        resolution: Resolution,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[UsageRecord]:
        query = (
            "SELECT * FROM usage_reads "
            "WHERE account_id = ? AND resolution = ?"
        )
        params: list[str] = [account_id, resolution.value]
        if start:
            query += " AND start_time >= ?"
            params.append(start.isoformat())
        if end:
            query += " AND start_time <= ?"
            params.append(end.isoformat())
        query += " ORDER BY start_time"
        rows = self._conn.execute(query, params).fetchall()
        return [_row_to_usage(r) for r in rows]

    def get_latest_usage_time(
        self, account_id: str, resolution: Resolution
    ) -> datetime | None:
        row = self._conn.execute(
            "SELECT MAX(start_time) as t FROM usage_reads "
            "WHERE account_id = ? AND resolution = ?",
            (account_id, resolution.value),
        ).fetchone()
        if row and row["t"]:
            return datetime.fromisoformat(row["t"])
        return None

    def get_usage_reads_multi(
        self,
        account_ids: list[str],
        resolution: Resolution,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[UsageRecord]:
        """Get usage reads for multiple accounts combined."""
        if not account_ids:
            return []
        placeholders = ",".join("?" for _ in account_ids)
        query = (
            f"SELECT * FROM usage_reads "
            f"WHERE account_id IN ({placeholders}) AND resolution = ?"
        )
        params: list[str] = list(account_ids) + [resolution.value]
        if start:
            query += " AND start_time >= ?"
            params.append(start.isoformat())
        if end:
            query += " AND start_time <= ?"
            params.append(end.isoformat())
        query += " ORDER BY start_time"
        rows = self._conn.execute(query, params).fetchall()
        return [_row_to_usage(r) for r in rows]

    def get_cost_reads_multi(
        self,
        account_ids: list[str],
        resolution: Resolution,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[CostRecord]:
        """Get cost reads for multiple accounts combined."""
        if not account_ids:
            return []
        placeholders = ",".join("?" for _ in account_ids)
        query = (
            f"SELECT * FROM cost_reads "
            f"WHERE account_id IN ({placeholders}) AND resolution = ?"
        )
        params: list[str] = list(account_ids) + [resolution.value]
        if start:
            query += " AND start_time >= ?"
            params.append(start.isoformat())
        if end:
            query += " AND start_time <= ?"
            params.append(end.isoformat())
        query += " ORDER BY start_time"
        rows = self._conn.execute(query, params).fetchall()
        return [_row_to_cost(r) for r in rows]

    # --- Cost reads ---

    def upsert_cost_reads(self, reads: list[CostRecord]) -> int:
        if not reads:
            return 0
        now = datetime.now(UTC).isoformat()
        self._conn.executemany(
            """INSERT INTO cost_reads
               (account_id, start_time, end_time, usage, cost,
                resolution, source, imported_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(account_id, start_time, resolution) DO UPDATE SET
                   end_time = excluded.end_time,
                   usage = excluded.usage,
                   cost = excluded.cost,
                   imported_at = excluded.imported_at
            """,
            [
                (
                    r.account_id,
                    r.start_time.isoformat(),
                    r.end_time.isoformat(),
                    r.usage,
                    r.cost,
                    r.resolution.value,
                    r.source.value,
                    now,
                )
                for r in reads
            ],
        )
        self._conn.commit()
        return len(reads)

    def get_cost_reads(
        self,
        account_id: str,
        resolution: Resolution,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[CostRecord]:
        query = (
            "SELECT * FROM cost_reads "
            "WHERE account_id = ? AND resolution = ?"
        )
        params: list[str] = [account_id, resolution.value]
        if start:
            query += " AND start_time >= ?"
            params.append(start.isoformat())
        if end:
            query += " AND start_time <= ?"
            params.append(end.isoformat())
        query += " ORDER BY start_time"
        rows = self._conn.execute(query, params).fetchall()
        return [_row_to_cost(r) for r in rows]

    def get_latest_cost_time(
        self, account_id: str, resolution: Resolution
    ) -> datetime | None:
        row = self._conn.execute(
            "SELECT MAX(start_time) as t FROM cost_reads "
            "WHERE account_id = ? AND resolution = ?",
            (account_id, resolution.value),
        ).fetchone()
        if row and row["t"]:
            return datetime.fromisoformat(row["t"])
        return None

    # --- Forecasts ---

    def upsert_forecast(self, forecast: ForecastRecord) -> None:
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            """INSERT INTO forecasts
               (account_id, start_date, end_date, "current_date",
                unit_of_measure, usage_to_date, forecasted_usage,
                typical_usage, cost_to_date, forecasted_cost,
                typical_cost, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(account_id, start_date, "current_date") DO UPDATE SET
                   usage_to_date = excluded.usage_to_date,
                   forecasted_usage = excluded.forecasted_usage,
                   typical_usage = excluded.typical_usage,
                   cost_to_date = excluded.cost_to_date,
                   forecasted_cost = excluded.forecasted_cost,
                   typical_cost = excluded.typical_cost,
                   fetched_at = excluded.fetched_at
            """,
            (
                forecast.account_id,
                forecast.start_date.isoformat(),
                forecast.end_date.isoformat(),
                forecast.current_date.isoformat(),
                forecast.unit_of_measure,
                forecast.usage_to_date,
                forecast.forecasted_usage,
                forecast.typical_usage,
                forecast.cost_to_date,
                forecast.forecasted_cost,
                forecast.typical_cost,
                now,
            ),
        )
        self._conn.commit()

    def get_latest_forecasts(self) -> list[ForecastRecord]:
        rows = self._conn.execute(
            """SELECT f.* FROM forecasts f
               INNER JOIN (
                   SELECT account_id, MAX(fetched_at) as max_fetched
                   FROM forecasts GROUP BY account_id
               ) latest ON f.account_id = latest.account_id
                       AND f.fetched_at = latest.max_fetched
               ORDER BY f.account_id
            """
        ).fetchall()
        return [_row_to_forecast(r) for r in rows]

    # --- Fetch log ---

    def log_fetch(
        self,
        account_id: str,
        fetch_type: str,
        rows_written: int = 0,
        status: str = "success",
        error_message: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            """INSERT INTO fetch_log
               (account_id, fetch_type, start_time, end_time,
                rows_written, status, error_message, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                fetch_type,
                start_time,
                end_time,
                rows_written,
                status,
                error_message,
                now,
            ),
        )
        self._conn.commit()

    def get_fetch_log(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM fetch_log ORDER BY fetched_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Annotations ---

    def add_annotation(self, annotation: AnnotationRecord) -> int:
        """Insert a new annotation and return its id."""
        now = datetime.now(UTC).isoformat()
        cursor = self._conn.execute(
            """INSERT INTO annotations
               (note_date, note_time, description, created_at)
               VALUES (?, ?, ?, ?)
            """,
            (
                annotation.note_date.isoformat(),
                annotation.note_time,
                annotation.description,
                now,
            ),
        )
        self._conn.commit()
        return cursor.lastrowid

    def get_annotations(self, limit: int = 10) -> list[dict]:
        """Return the most recent annotations, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM annotations ORDER BY note_date DESC, note_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_annotations_for_dates(self, dates: list[str]) -> set[str]:
        """Return the set of dates (ISO strings) that have at least one annotation."""
        if not dates:
            return set()
        placeholders = ",".join("?" for _ in dates)
        rows = self._conn.execute(
            f"SELECT DISTINCT note_date FROM annotations WHERE note_date IN ({placeholders})",  # noqa: S608
            dates,
        ).fetchall()
        return {r["note_date"] for r in rows}

    # --- Utilities ---

    def get_db_stats(self) -> dict:
        stats = {}
        for table in ("accounts", "usage_reads", "cost_reads", "forecasts", "fetch_log", "annotations"):
            row = self._conn.execute(
                f"SELECT COUNT(*) as cnt FROM {table}"  # noqa: S608
            ).fetchone()
            stats[table] = row["cnt"]
        return stats

    def vacuum(self) -> None:
        self._conn.execute("VACUUM")


# --- Row-to-model mappers ---


def _row_to_account(row: sqlite3.Row) -> AccountRecord:
    return AccountRecord(
        id=row["id"],
        utility=row["utility"],
        meter_type=MeterType(row["meter_type"]),
        customer_id=row["customer_id"],
        account_number=row["account_number"],
        service_address=row["service_address"],
        source=DataSource(row["source"]),
    )


def _row_to_usage(row: sqlite3.Row) -> UsageRecord:
    return UsageRecord(
        account_id=row["account_id"],
        start_time=datetime.fromisoformat(row["start_time"]),
        end_time=datetime.fromisoformat(row["end_time"]),
        unit_of_measure=row["unit_of_measure"],
        usage=row["usage"],
        resolution=Resolution(row["resolution"]),
        source=DataSource(row["source"]),
    )


def _row_to_cost(row: sqlite3.Row) -> CostRecord:
    return CostRecord(
        account_id=row["account_id"],
        start_time=datetime.fromisoformat(row["start_time"]),
        end_time=datetime.fromisoformat(row["end_time"]),
        usage=row["usage"],
        cost=row["cost"],
        resolution=Resolution(row["resolution"]),
        source=DataSource(row["source"]),
    )


def _row_to_forecast(row: sqlite3.Row) -> ForecastRecord:
    from datetime import date as date_type

    return ForecastRecord(
        account_id=row["account_id"],
        start_date=date_type.fromisoformat(row["start_date"]),
        end_date=date_type.fromisoformat(row["end_date"]),
        current_date=date_type.fromisoformat(row["current_date"]),
        unit_of_measure=row["unit_of_measure"],
        usage_to_date=row["usage_to_date"],
        forecasted_usage=row["forecasted_usage"],
        typical_usage=row["typical_usage"],
        cost_to_date=row["cost_to_date"],
        forecasted_cost=row["forecasted_cost"],
        typical_cost=row["typical_cost"],
    )
