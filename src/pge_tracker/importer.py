"""Green Button CSV parser for PG&E data downloads."""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import DataSource, MeterType, Resolution, UsageRecord

logger = logging.getLogger(__name__)

_PACIFIC = ZoneInfo("America/Los_Angeles")

# PG&E Green Button CSV has this header row (after possible metadata lines):
# TYPE,DATE,START TIME,END TIME,USAGE,UNITS,COST,NOTES
# or sometimes:
# TYPE,DATE,START TIME,END TIME,USAGE,UNITS,NOTES
_EXPECTED_COLUMNS = {"TYPE", "DATE", "START TIME", "USAGE", "UNITS"}


def detect_green_button_format(file_path: Path) -> dict:
    """Read the first rows of a Green Button CSV to determine its format.

    Returns a dict with:
        meter_type: "electric" or "gas"
        resolution: "hourly" or "daily"
        date_range: (earliest_date_str, latest_date_str)
        row_count: estimated number of data rows
        columns: list of column headers found
    """
    with open(file_path, newline="", encoding="utf-8-sig") as f:
        # Skip any metadata lines before the header
        header_line, reader = _find_header(f)
        if header_line is None:
            raise ValueError(
                f"Could not find a valid CSV header in {file_path}. "
                f"Expected columns: {_EXPECTED_COLUMNS}"
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

        resolution = "hourly" if 60 in durations else "daily"

    return {
        "meter_type": meter_type or "unknown",
        "resolution": resolution,
        "date_range": (min(dates), max(dates)) if dates else (None, None),
        "row_count": row_count,
        "columns": header_line,
    }


def parse_green_button_csv(
    file_path: Path,
    meter_type: MeterType,
    account_id: str,
) -> list[UsageRecord]:
    """Parse a PG&E Green Button CSV into UsageRecord objects.

    Args:
        file_path: Path to the CSV file.
        meter_type: ELECTRIC or GAS.
        account_id: Account ID to associate records with.

    Returns:
        List of UsageRecord ready for database insertion.
    """
    records: list[UsageRecord] = []
    skipped = 0

    unit = "KWH" if meter_type == MeterType.ELECTRIC else "CCF"

    with open(file_path, newline="", encoding="utf-8-sig") as f:
        _header, reader = _find_header(f)
        if reader is None:
            raise ValueError(f"Could not find a valid CSV header in {file_path}")

        for row_num, row in enumerate(reader, start=2):
            try:
                date_str = row["DATE"].strip()
                start_str = row["START TIME"].strip()
                end_str = row.get("END TIME", "").strip()
                usage_str = row["USAGE"].strip()
                units = row.get("UNITS", "").strip().upper()

                if not date_str or not usage_str:
                    skipped += 1
                    continue

                usage_val = float(usage_str)

                # Override unit if CSV specifies it
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


def _find_header(
    f,
) -> tuple[list[str] | None, csv.DictReader | None]:
    """Scan for the header row in a Green Button CSV.

    PG&E CSVs sometimes have metadata lines before the actual header.
    We look for a line containing the expected column names.
    """
    for line in f:
        line = line.strip()
        if not line:
            continue
        # Check if this line looks like a header
        parts = [p.strip().upper() for p in line.split(",")]
        if _EXPECTED_COLUMNS.issubset(set(parts)):
            # Re-create a DictReader using the remaining lines
            # with this line's fields as the header
            header = [p.strip() for p in line.split(",")]
            reader = csv.DictReader(f, fieldnames=header)
            return header, reader
    return None, None


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
