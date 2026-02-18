"""PG&E CSV parser for usage data downloads.

Supports the PG&E interval data export format with columns:
    TYPE, DATE, START TIME, END TIME, USAGE (kWh), COST, NOTES
    TYPE, DATE, START TIME, END TIME, USAGE (therms), COST, NOTES
as well as the legacy Green Button format with columns:
    TYPE, DATE, START TIME, END TIME, USAGE, UNITS, COST, NOTES
"""

from __future__ import annotations

import csv
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import CostRecord, DataSource, MeterType, Resolution, UsageRecord

logger = logging.getLogger(__name__)

_PACIFIC = ZoneInfo("America/Los_Angeles")

# Minimum columns required in the header (case-insensitive match).
# We accept both "USAGE" and "USAGE (kWh)" / "USAGE (therms)" style headers.
_REQUIRED_COLUMNS = {"TYPE", "DATE", "START TIME"}


def detect_green_button_format(file_path: Path) -> dict:
    """Read the first rows of a PG&E CSV to determine its format.

    Returns a dict with:
        meter_type: "electric" or "gas"
        resolution: "hourly" or "daily"
        date_range: (earliest_date_str, latest_date_str)
        row_count: estimated number of data rows
        columns: list of column headers found
        account_number: account number from metadata, if present
        service_address: address from metadata, if present
    """
    metadata = _extract_metadata(file_path)

    with open(file_path, newline="", encoding="utf-8-sig") as f:
        header_line, reader = _find_header(f)
        if header_line is None:
            raise ValueError(
                f"Could not find a valid CSV header in {file_path}. "
                f"Expected columns containing at least: {_REQUIRED_COLUMNS}"
            )

        meter_type = None
        dates: list[str] = []
        durations: set[int] = set()
        row_count = 0

        for row in reader:
            row_count += 1
            type_val = row.get("TYPE", "").strip().lower()
            if "electric" in type_val:
                meter_type = "electric"
            elif "gas" in type_val or "natural" in type_val:
                meter_type = "gas"

            date_val = row.get("DATE", "").strip()
            if date_val:
                dates.append(date_val)

            start = row.get("START TIME", "").strip()
            end = row.get("END TIME", "").strip()
            if start and end:
                try:
                    sh, sm = (int(x) for x in start.split(":"))
                    eh, em = (int(x) for x in end.split(":"))
                    dur = (eh * 60 + em) - (sh * 60 + sm)
                    if dur < 0:
                        dur += 24 * 60
                    durations.add(dur)
                except ValueError:
                    pass

        resolution = "hourly" if durations and max(durations) < 120 else "daily"

    return {
        "meter_type": meter_type or "unknown",
        "resolution": resolution,
        "date_range": (min(dates), max(dates)) if dates else (None, None),
        "row_count": row_count,
        "columns": header_line,
        **metadata,
    }


def parse_green_button_csv(
    file_path: Path,
    meter_type: MeterType,
    account_id: str,
) -> list[UsageRecord]:
    """Parse a PG&E CSV into UsageRecord objects.

    Args:
        file_path: Path to the CSV file.
        meter_type: ELECTRIC or GAS.
        account_id: Account ID to associate records with.

    Returns:
        List of UsageRecord ready for database insertion.
    """
    records: list[UsageRecord] = []
    skipped = 0

    unit = "KWH" if meter_type == MeterType.ELECTRIC else "THERM"

    with open(file_path, newline="", encoding="utf-8-sig") as f:
        header, reader = _find_header(f)
        if reader is None:
            raise ValueError(f"Could not find a valid CSV header in {file_path}")

        usage_col = _find_usage_column(header)

        for row_num, row in enumerate(reader, start=2):
            try:
                date_str = row["DATE"].strip()
                start_str = row["START TIME"].strip()
                end_str = row.get("END TIME", "").strip()
                usage_str = row.get(usage_col, "").strip()

                if not date_str or not usage_str:
                    skipped += 1
                    continue

                usage_val = float(usage_str)

                # Check for a separate UNITS column (legacy format)
                units = row.get("UNITS", "").strip().upper()
                if units in ("KWH", "THERM", "CCF"):
                    unit = units
                elif units == "KW":
                    unit = "KWH"

                # Parse start datetime in Pacific time
                start_dt = _parse_datetime(date_str, start_str)

                # Parse end datetime
                if end_str:
                    end_dt = _parse_datetime(date_str, end_str)
                    # Handle midnight wrap (e.g., 23:00 -> 00:00)
                    if end_dt <= start_dt:
                        end_dt += timedelta(days=1)
                else:
                    # No end time: assume daily resolution
                    end_dt = start_dt + timedelta(days=1)

                # Determine resolution from duration
                duration_hours = (end_dt - start_dt).total_seconds() / 3600
                if duration_hours <= 1.5:
                    resolution = Resolution.HOUR
                else:
                    resolution = Resolution.DAY

                records.append(
                    UsageRecord(
                        account_id=account_id,
                        start_time=start_dt,
                        end_time=end_dt,
                        unit_of_measure=unit,
                        usage=usage_val,
                        resolution=resolution,
                        source=DataSource.GREEN_BUTTON,
                    )
                )
            except (KeyError, ValueError) as e:
                skipped += 1
                logger.warning("Skipping row %d: %s", row_num, e)

    if skipped:
        logger.info(
            "Parsed %d records, skipped %d rows from %s",
            len(records),
            skipped,
            file_path,
        )
    return records


def parse_cost_records(
    file_path: Path,
    meter_type: MeterType,
    account_id: str,
) -> list[CostRecord]:
    """Parse cost data from a PG&E CSV.

    Returns CostRecord objects for rows that contain a COST column.
    Returns an empty list if no cost data is present.
    """
    records: list[CostRecord] = []

    with open(file_path, newline="", encoding="utf-8-sig") as f:
        header, reader = _find_header(f)
        if reader is None:
            return []

        cost_col = _find_cost_column(header)
        if cost_col is None:
            return []

        usage_col = _find_usage_column(header)

        for row_num, row in enumerate(reader, start=2):
            try:
                date_str = row["DATE"].strip()
                start_str = row["START TIME"].strip()
                end_str = row.get("END TIME", "").strip()
                cost_str = row.get(cost_col, "").strip()
                usage_str = row.get(usage_col, "").strip()

                if not date_str or not cost_str:
                    continue

                # Strip dollar signs and parse
                cost_val = float(cost_str.replace("$", "").replace(",", ""))
                usage_val = float(usage_str) if usage_str else None

                start_dt = _parse_datetime(date_str, start_str)

                if end_str:
                    end_dt = _parse_datetime(date_str, end_str)
                    if end_dt <= start_dt:
                        end_dt += timedelta(days=1)
                else:
                    end_dt = start_dt + timedelta(days=1)

                duration_hours = (end_dt - start_dt).total_seconds() / 3600
                resolution = Resolution.HOUR if duration_hours <= 1.5 else Resolution.DAY

                records.append(
                    CostRecord(
                        account_id=account_id,
                        start_time=start_dt,
                        end_time=end_dt,
                        usage=usage_val,
                        cost=cost_val,
                        resolution=resolution,
                        source=DataSource.GREEN_BUTTON,
                    )
                )
            except (KeyError, ValueError) as e:
                logger.warning("Skipping cost row %d: %s", row_num, e)

    return records


# --- Internal helpers ---


def _extract_metadata(file_path: Path) -> dict:
    """Extract account metadata from the header lines of a PG&E CSV."""
    metadata: dict = {}
    with open(file_path, newline="", encoding="utf-8-sig") as f:
        for _ in range(10):  # Only check the first 10 lines
            line = f.readline()
            if not line:
                break
            line = line.strip()
            if line.upper().startswith("ACCOUNT NUMBER,"):
                parts = line.split(",", 1)
                if len(parts) == 2:
                    metadata["account_number"] = parts[1].strip()
            elif line.upper().startswith("ADDRESS,"):
                parts = line.split(",", 1)
                if len(parts) == 2:
                    metadata["service_address"] = parts[1].strip().strip('"')
    return metadata


def _find_header(
    f,
) -> tuple[list[str] | None, csv.DictReader | None]:
    """Scan for the header row in a PG&E CSV.

    PG&E CSVs have metadata lines (Name, Address, Account Number)
    before the actual column header. We look for a line containing
    the required column names.
    """
    for line in f:
        line = line.strip()
        if not line:
            continue
        # Normalise column names for comparison
        parts = [p.strip() for p in line.split(",")]
        upper_parts = {p.upper() for p in parts}
        if _REQUIRED_COLUMNS.issubset(upper_parts):
            # Use the original-cased parts as the fieldnames
            reader = csv.DictReader(f, fieldnames=parts)
            return parts, reader
    return None, None


def _find_usage_column(header: list[str]) -> str:
    """Find the usage column in the header.

    Handles both "USAGE" and "USAGE (kWh)" / "USAGE (therms)" formats.
    """
    for col in header:
        if col.upper().startswith("USAGE"):
            return col
    return "USAGE"


def _find_cost_column(header: list[str]) -> str | None:
    """Find the cost column in the header, if present."""
    for col in header:
        if col.upper() == "COST":
            return col
    return None


def _parse_datetime(date_str: str, time_str: str) -> datetime:
    """Parse date + time strings into a timezone-aware datetime (Pacific)."""
    if time_str:
        dt_str = f"{date_str} {time_str}"
        # Try common formats
        for fmt in ("%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M", "%m/%d/%y %H:%M"):
            try:
                naive = datetime.strptime(dt_str, fmt)
                return naive.replace(tzinfo=_PACIFIC)
            except ValueError:
                continue
    # Date only
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            naive = datetime.strptime(date_str, fmt)
            return naive.replace(tzinfo=_PACIFIC)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date/time: {date_str!r} {time_str!r}")
