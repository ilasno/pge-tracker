"""Fetch PG&E usage data via the opower library."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import opower

from .config import Config
from .database import Database
from .models import (
    AccountRecord,
    CostRecord,
    DataSource,
    FetchSummary,
    ForecastRecord,
    MeterType,
    Resolution,
    UsageRecord,
)

logger = logging.getLogger(__name__)

_PACIFIC = ZoneInfo("America/Los_Angeles")

# opower MeterType -> our MeterType
_METER_MAP = {
    opower.MeterType.ELEC: MeterType.ELECTRIC,
    opower.MeterType.GAS: MeterType.GAS,
}


def _session_path(config: Config) -> Path:
    """Path to persist opower login data (avoids repeated MFA)."""
    return config.db_path.parent / ".opower_session"


def _load_login_data(config: Config) -> dict | None:
    """Load saved login data from disk, if it exists and is recent."""
    path = _session_path(config)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        # Check if saved data is less than 180 days old
        saved_at = data.get("_saved_at", "")
        if saved_at:
            saved_dt = datetime.fromisoformat(saved_at)
            if (datetime.now(UTC) - saved_dt).days > 170:
                logger.info("Saved login data is expiring soon, will re-auth")
                return None
        return data
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def _save_login_data(config: Config, login_data: dict) -> None:
    """Persist login data to avoid MFA on next run."""
    login_data["_saved_at"] = datetime.now(UTC).isoformat()
    path = _session_path(config)
    path.write_text(json.dumps(login_data))
    # Restrict permissions to owner only
    path.chmod(0o600)


async def _handle_mfa(mfa: opower.MfaChallenge) -> dict:
    """Interactive MFA handler — prompts in the terminal."""
    handler = mfa.handler

    options = await handler.async_get_mfa_options()
    if options:
        print("\nMFA verification required. Choose a delivery method:")
        option_list = list(options.items())
        for i, (opt_id, description) in enumerate(option_list, 1):
            print(f"  {i}. {description}")

        while True:
            choice = input(f"Select (1-{len(option_list)}): ").strip()
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(option_list):
                    selected_id = option_list[idx][0]
                    break
            except ValueError:
                pass
            print("Invalid selection, try again.")

        await handler.async_select_mfa_option(selected_id)
        print(f"Code sent via {options[selected_id]}.")
    else:
        print("\nMFA verification required.")

    code = input("Enter the verification code: ").strip()
    login_data = await handler.async_submit_mfa_code(code)
    print("MFA verified successfully.\n")
    return login_data


def _opower_account_to_record(acct: opower.Account) -> AccountRecord:
    """Map opower Account to our AccountRecord."""
    return AccountRecord(
        id=acct.id,
        utility="pge",
        meter_type=_METER_MAP.get(acct.meter_type, MeterType.ELECTRIC),
        customer_id=acct.customer.uuid if acct.customer else None,
        account_number=acct.utility_account_id,
        service_address=None,
        source=DataSource.OPOWER,
    )


def _opower_cost_to_record(
    read: opower.CostRead, account_id: str, resolution: Resolution
) -> CostRecord:
    return CostRecord(
        account_id=account_id,
        start_time=read.start_time,
        end_time=read.end_time,
        usage=read.consumption,
        cost=read.provided_cost if read.provided_cost != 0.0 else None,
        resolution=resolution,
        source=DataSource.OPOWER,
    )


def _opower_usage_to_record(
    read: opower.UsageRead, account_id: str, resolution: Resolution, unit: str
) -> UsageRecord:
    return UsageRecord(
        account_id=account_id,
        start_time=read.start_time,
        end_time=read.end_time,
        unit_of_measure=unit,
        usage=read.consumption,
        resolution=resolution,
        source=DataSource.OPOWER,
    )


def _opower_forecast_to_record(
    fc: opower.Forecast, account_id: str
) -> ForecastRecord:
    return ForecastRecord(
        account_id=account_id,
        start_date=fc.start_date,
        end_date=fc.end_date,
        current_date=fc.current_date,
        unit_of_measure=str(fc.unit_of_measure),
        usage_to_date=fc.usage_to_date,
        forecasted_usage=fc.forecasted_usage,
        typical_usage=fc.typical_usage,
        cost_to_date=fc.cost_to_date,
        forecasted_cost=fc.forecasted_cost,
        typical_cost=fc.typical_cost,
    )


async def fetch_all(
    config: Config,
    db: Database,
    incremental: bool = True,
    fetch_hourly: bool = True,
    fetch_daily: bool = True,
    fetch_forecasts: bool = True,
) -> FetchSummary:
    """Fetch all available data from PG&E via opower.

    Args:
        config: Application configuration.
        db: Initialized Database instance.
        incremental: If True, only fetch data newer than what's stored.
        fetch_hourly: Fetch hourly-resolution data (last ~60 days).
        fetch_daily: Fetch daily-resolution cost data.
        fetch_forecasts: Fetch billing forecasts.

    Returns:
        FetchSummary with counts and timing.
    """
    t0 = time.monotonic()
    errors: list[str] = []
    daily_written = 0
    hourly_written = 0
    forecasts_written = 0

    login_data = _load_login_data(config)

    jar = opower.create_cookie_jar()
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        api = opower.Opower(
            session,
            "pge",
            config.username,
            config.password,
            optional_totp_secret=config.totp_secret,
            login_data=login_data,
        )

        # Login (may trigger MFA)
        try:
            await api.async_login()
        except opower.MfaChallenge as mfa:
            new_login_data = await _handle_mfa(mfa)
            _save_login_data(config, new_login_data)
            # Re-login with the MFA login_data to establish a valid session
            api.login_data = new_login_data
            await api.async_login()
        except opower.InvalidAuth:
            # If we had saved login_data that expired, clear it and retry
            if login_data:
                logger.info("Saved login data rejected, retrying fresh login")
                _session_path(config).unlink(missing_ok=True)
                api.login_data = {}
                try:
                    await api.async_login()
                except opower.MfaChallenge as mfa:
                    new_login_data = await _handle_mfa(mfa)
                    _save_login_data(config, new_login_data)
                    api.login_data = new_login_data
                    await api.async_login()
                except opower.InvalidAuth:
                    raise SystemExit(
                        "Authentication failed. Check your PG&E credentials in config.toml."
                    )
            else:
                raise SystemExit(
                    "Authentication failed. Check your PG&E credentials in config.toml."
                )

        # Get accounts
        accounts = await api.async_get_accounts()
        logger.info("Found %d accounts", len(accounts))

        for acct in accounts:
            record = _opower_account_to_record(acct)
            db.upsert_account(record)

            unit = (
                str(acct.meter_type).replace("ELEC", "KWH").replace("GAS", "CCF")
            )
            # Normalize: opower MeterType.ELEC -> "KWH", MeterType.GAS -> "CCF"
            if unit not in ("KWH", "CCF", "THERM"):
                unit = "KWH"

            # --- Daily cost reads ---
            if fetch_daily:
                try:
                    start_date = _compute_start(
                        db, record.id, Resolution.DAY, incremental, config
                    )
                    end_date = datetime.now(_PACIFIC)

                    logger.info(
                        "Fetching daily cost for %s (%s → %s)",
                        record.id,
                        start_date.date(),
                        end_date.date(),
                    )
                    cost_reads = await api.async_get_cost_reads(
                        acct,
                        opower.AggregateType.DAY,
                        start_date=start_date,
                        end_date=end_date,
                    )
                    records = [
                        _opower_cost_to_record(r, record.id, Resolution.DAY)
                        for r in cost_reads
                    ]
                    n = db.upsert_cost_reads(records)
                    daily_written += n
                    db.log_fetch(
                        record.id,
                        "cost_daily",
                        rows_written=n,
                        start_time=start_date.isoformat(),
                        end_time=end_date.isoformat(),
                    )
                except Exception as e:
                    msg = f"Error fetching daily cost for {record.id}: {e}"
                    logger.error(msg)
                    errors.append(msg)
                    db.log_fetch(
                        record.id, "cost_daily", status="error", error_message=str(e)
                    )

            # --- Hourly usage reads (last ~60 days) ---
            if fetch_hourly:
                try:
                    # opower limits hourly data to ~60 days
                    hourly_start = datetime.now(_PACIFIC) - timedelta(days=60)
                    if incremental:
                        latest = db.get_latest_usage_time(record.id, Resolution.HOUR)
                        if latest and latest > hourly_start:
                            hourly_start = latest

                    end_date = datetime.now(_PACIFIC)

                    logger.info(
                        "Fetching hourly usage for %s (%s → %s)",
                        record.id,
                        hourly_start.date(),
                        end_date.date(),
                    )
                    usage_reads = await api.async_get_usage_reads(
                        acct,
                        opower.AggregateType.HOUR,
                        start_date=hourly_start,
                        end_date=end_date,
                    )
                    records = [
                        _opower_usage_to_record(r, record.id, Resolution.HOUR, unit)
                        for r in usage_reads
                    ]
                    n = db.upsert_usage_reads(records)
                    hourly_written += n
                    db.log_fetch(
                        record.id,
                        "usage_hourly",
                        rows_written=n,
                        start_time=hourly_start.isoformat(),
                        end_time=end_date.isoformat(),
                    )
                except Exception as e:
                    err_str = str(e)
                    if "not supported by account's read_resolution" in err_str:
                        # Gas meters only support daily reads — not an error
                        logger.info(
                            "Account %s does not support hourly reads (daily-only meter)",
                            record.id,
                        )
                    else:
                        msg = f"Error fetching hourly usage for {record.id}: {e}"
                        logger.error(msg)
                        errors.append(msg)
                        db.log_fetch(
                            record.id, "usage_hourly", status="error", error_message=err_str
                        )

        # --- Forecasts ---
        if fetch_forecasts:
            try:
                forecasts = await api.async_get_forecast()
                for fc in forecasts:
                    acct_id = fc.account.id
                    db.upsert_forecast(_opower_forecast_to_record(fc, acct_id))
                    forecasts_written += 1
                db.log_fetch(
                    "all",
                    "forecast",
                    rows_written=forecasts_written,
                )
            except Exception as e:
                msg = f"Error fetching forecasts: {e}"
                logger.error(msg)
                errors.append(msg)

    elapsed = time.monotonic() - t0
    return FetchSummary(
        accounts_found=len(accounts),
        daily_records_written=daily_written,
        hourly_records_written=hourly_written,
        forecasts_written=forecasts_written,
        fetch_duration_seconds=round(elapsed, 1),
        errors=errors,
    )


def _compute_start(
    db: Database,
    account_id: str,
    resolution: Resolution,
    incremental: bool,
    config: Config,
) -> datetime:
    """Compute the start date for a fetch operation."""
    if incremental:
        latest = db.get_latest_cost_time(account_id, resolution)
        if latest:
            return latest

    return datetime.now(_PACIFIC) - timedelta(days=config.initial_fetch_days)
