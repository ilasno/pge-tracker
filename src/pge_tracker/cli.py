"""CLI entry point for pge-tracker."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from . import __version__
from .config import Config, load_config
from .database import Database
from .models import MeterType, Resolution

app = typer.Typer(
    name="pge-tracker",
    help="PG&E energy usage tracker and analyzer.",
    no_args_is_help=True,
)
console = Console()


def _get_config(config_path: Optional[Path]) -> Config:
    try:
        return load_config(config_path)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


def _get_db(config: Config) -> Database:
    db = Database(config.db_path)
    db.initialize()
    return db


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    """PG&E energy usage tracker and analyzer."""
    if verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)


@app.command()
def sync(
    config_path: Optional[Path] = typer.Option(None, "--config", "-c", help="Config file path"),
    full: bool = typer.Option(False, "--full", help="Re-fetch all history"),
    no_hourly: bool = typer.Option(False, "--no-hourly", help="Skip hourly data"),
    no_forecast: bool = typer.Option(False, "--no-forecast", help="Skip forecasts"),
) -> None:
    """Fetch latest usage data from PG&E."""
    from .fetcher import fetch_all
    from .presenter import print_fetch_summary

    config = _get_config(config_path)
    db = _get_db(config)

    console.print("Connecting to PG&E via Opower...\n")
    try:
        summary = asyncio.run(
            fetch_all(
                config=config,
                db=db,
                incremental=not full,
                fetch_hourly=not no_hourly,
                fetch_daily=True,
                fetch_forecasts=not no_forecast,
            )
        )
        print_fetch_summary(summary, console)
        if not summary.errors:
            console.print("\nRun [bold]pge-tracker analyze[/bold] to see insights.")
    finally:
        db.close()


@app.command("import")
def import_csv(
    file: Path = typer.Argument(..., help="Path to Green Button CSV file"),
    config_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    meter_type: str = typer.Option("auto", "--meter-type", "-m",
                                   help="electric, gas, or auto"),
    account_id: str = typer.Option("", "--account-id", "-a",
                                   help="Account ID (auto-generated if empty)"),
    preview: bool = typer.Option(False, "--preview", help="Preview without importing"),
) -> None:
    """Import a PG&E CSV file (Green Button or interval data export)."""
    from .importer import detect_green_button_format, parse_cost_records, parse_green_button_csv
    from .presenter import print_import_summary

    if not file.exists():
        console.print(f"[red]File not found: {file}[/red]")
        raise typer.Exit(1)

    # Detect format
    info = detect_green_button_format(file)
    console.print(f"Detected: {info['meter_type']} data, "
                  f"{info['resolution']} resolution, "
                  f"{info['row_count']} rows")
    if info["date_range"][0]:
        console.print(f"Date range: {info['date_range'][0]} to {info['date_range'][1]}")
    if info.get("account_number"):
        console.print(f"Account: {info['account_number']}")

    if preview:
        return

    # Determine meter type
    if meter_type == "auto":
        detected = info["meter_type"]
        if detected == "electric":
            mt = MeterType.ELECTRIC
        elif detected == "gas":
            mt = MeterType.GAS
        else:
            console.print("[red]Could not auto-detect meter type. "
                          "Use --meter-type electric or --meter-type gas[/red]")
            raise typer.Exit(1)
    elif meter_type.lower().startswith("e"):
        mt = MeterType.ELECTRIC
    else:
        mt = MeterType.GAS

    # Account ID
    if not account_id:
        account_id = f"gb-{mt.value.lower()}"

    config = _get_config(config_path)
    db = _get_db(config)

    try:
        from .models import AccountRecord, DataSource

        db.upsert_account(AccountRecord(
            id=account_id,
            utility="pge",
            meter_type=mt,
            customer_id=None,
            account_number=info.get("account_number"),
            service_address=info.get("service_address"),
            source=DataSource.GREEN_BUTTON,
        ))

        records = parse_green_button_csv(file, mt, account_id)
        usage_count = db.upsert_usage_reads(records)

        # Also import cost data if present
        cost_records = parse_cost_records(file, mt, account_id)
        cost_count = db.upsert_cost_reads(cost_records)

        print_import_summary(
            file_path=file,
            record_count=usage_count,
            meter_type=mt.value,
            date_range=info["date_range"],
            cost_count=cost_count,
            console=console,
        )
    finally:
        db.close()


@app.command()
def status(
    config_path: Optional[Path] = typer.Option(None, "--config", "-c"),
) -> None:
    """Show database status and sync history."""
    from .presenter import print_status

    config = _get_config(config_path)
    db = _get_db(config)

    try:
        accounts = db.get_accounts()
        stats = db.get_db_stats()
        fetch_log = db.get_fetch_log(limit=5)
        print_status(accounts, stats, config.db_path, fetch_log, console)
    finally:
        db.close()


@app.command()
def show(
    config_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    days: int = typer.Option(30, "--days", "-d", help="Days to display"),
    meter: str = typer.Option("both", "--meter", "-m", help="e, g, or both"),
    resolution: str = typer.Option("d", "--resolution", "-r", help="h=hourly, d=daily, m=monthly"),
) -> None:
    """Display usage and cost data."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from .analyzer import daily_stats, period_summaries
    from .presenter import print_period_table, print_usage_chart

    config = _get_config(config_path)
    db = _get_db(config)

    try:
        accounts = db.get_accounts()
        if not accounts:
            console.print("[yellow]No data yet. Run 'pge-tracker sync' first.[/yellow]")
            return

        tz = ZoneInfo(config.timezone)
        now = datetime.now(tz)
        start = now - timedelta(days=days)

        meter_filter = _parse_meter_filter(meter)

        for acct in accounts:
            if meter_filter and acct.meter_type not in meter_filter:
                continue

            unit = "kWh" if acct.meter_type == MeterType.ELECTRIC else "CCF"
            label = f"{acct.meter_type.value} ({acct.account_number or acct.id})"
            console.print(f"\n[bold]{label}[/bold]")

            if resolution == "h":
                usage = db.get_usage_reads(acct.id, Resolution.HOUR, start, now)
                if not usage:
                    console.print(f"[dim]No hourly data for this account.[/dim]")
                    continue
                # Show as daily chart from hourly data
                costs = _get_all_cost_reads(db, acct.id, start, now)
                ds = daily_stats(usage, costs)
                print_usage_chart(ds, unit, console)
            elif resolution == "m":
                costs = _get_all_cost_reads(db, acct.id, start, now)
                usage_reads = db.get_usage_reads(acct.id, Resolution.HOUR, start, now)
                if not costs and not usage_reads:
                    # Try daily usage from cost reads
                    pass
                ds = daily_stats(usage_reads, costs)
                summaries = period_summaries(ds, "monthly")
                print_period_table(summaries, unit, console)
            else:
                # Daily view
                costs = _get_all_cost_reads(db, acct.id, start, now)
                usage_reads = db.get_usage_reads(acct.id, Resolution.HOUR, start, now)
                ds = daily_stats(usage_reads if usage_reads else [], costs)
                if not ds:
                    # Build daily stats from cost reads alone
                    ds = daily_stats([], costs)
                print_usage_chart(ds, unit, console)
    finally:
        db.close()


@app.command()
def analyze(
    config_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    meter: str = typer.Option("both", "--meter", "-m", help="e, g, or both"),
    section: Optional[list[str]] = typer.Option(None, "--section", "-s",
                                                 help="Run specific sections"),
    report: str = typer.Option("full", "--report", help="full or quick"),
) -> None:
    """Run analysis and generate an insights report."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from . import analyzer
    from . import presenter

    config = _get_config(config_path)
    db = _get_db(config)

    try:
        accounts = db.get_accounts()
        if not accounts:
            console.print("[yellow]No data yet. Run 'pge-tracker sync' first.[/yellow]")
            return

        tz = ZoneInfo(config.timezone)
        now = datetime.now(tz)
        sections = set(section) if section else None

        meter_filter = _parse_meter_filter(meter)

        for acct in accounts:
            if meter_filter and acct.meter_type not in meter_filter:
                continue

            unit = "kWh" if acct.meter_type == MeterType.ELECTRIC else "CCF"
            label = f"{acct.meter_type.value} ({acct.account_number or acct.id})"
            console.print(f"\n[bold cyan]{'=' * 60}[/bold cyan]")
            console.print(f"[bold cyan]  {label}[/bold cyan]")
            console.print(f"[bold cyan]{'=' * 60}[/bold cyan]\n")

            # Load data (combine DAY + HOUR cost reads for full coverage)
            all_cost = _get_all_cost_reads(db, acct.id)
            hourly = db.get_usage_reads(acct.id, Resolution.HOUR)
            ds = analyzer.daily_stats(hourly if hourly else [], all_cost)

            if not ds:
                console.print("[dim]No data available for analysis.[/dim]")
                continue

            # --- Section: Trends ---
            if _should_run("trends", sections):
                console.print("\n[bold]Usage Trends (Last 90 Days)[/bold]")
                recent = [d for d in ds if d.day >= (now.date() - timedelta(days=90))]
                if recent:
                    presenter.print_usage_chart(recent, unit, console)
                else:
                    console.print("[dim]No recent data.[/dim]")

            # --- Section: Monthly summary ---
            if _should_run("monthly", sections):
                console.print("\n[bold]Monthly Summary[/bold]")
                summaries = analyzer.period_summaries(ds, "monthly")
                if report == "quick":
                    summaries = summaries[-6:]  # Last 6 months only
                presenter.print_period_table(summaries, unit, console)

            # --- Section: TOU (electric only) ---
            if (
                _should_run("tou", sections)
                and acct.meter_type == MeterType.ELECTRIC
                and hourly
            ):
                console.print("\n[bold]Time-of-Use Analysis[/bold]")
                recent_hourly = [
                    r for r in hourly
                    if r.start_time >= now - timedelta(days=30)
                ]
                if recent_hourly:
                    tou = analyzer.tou_analysis(
                        recent_hourly,
                        config.peak_hours_start,
                        config.peak_hours_end,
                        tz,
                    )
                    recent_ds = [d for d in ds if d.day >= (now.date() - timedelta(days=30))]
                    tou = analyzer.tou_with_cost_estimates(tou, recent_ds)
                    presenter.print_tou_breakdown(tou, console)

            # --- Section: Year-over-Year ---
            if _should_run("yoy", sections):
                console.print("\n[bold]Year-over-Year Comparison[/bold]")
                yoy_30 = analyzer.yoy_comparison(ds, days=30)
                presenter.print_yoy_comparison(yoy_30, unit, console)

            # --- Section: Seasonal ---
            if _should_run("seasonal", sections) and report == "full":
                console.print("\n[bold]Seasonal Patterns[/bold]")
                seasonal = analyzer.seasonal_patterns(ds)
                presenter.print_seasonal_breakdown(seasonal, unit, console)

            # --- Section: Anomalies ---
            if _should_run("anomalies", sections):
                console.print("\n[bold]Anomaly Detection[/bold]")
                anomalies = analyzer.detect_anomalies(ds)
                presenter.print_anomalies(anomalies, console)

            # --- Section: Forecast ---
            if _should_run("forecast", sections):
                console.print("\n[bold]Cost Forecast[/bold]")
                forecasts = db.get_latest_forecasts()
                acct_forecasts = [f for f in forecasts if f.account_id == acct.id]
                projection = analyzer.cost_projection(ds)
                presenter.print_forecast(acct_forecasts, projection, console)

            # --- Section: Top usage ---
            if _should_run("top", sections):
                console.print("\n[bold]Top Usage Days[/bold]")
                top = analyzer.highest_usage_days(ds, top_n=10)
                presenter.print_top_usage(top, unit, console)

            # --- Section: Recommendations ---
            if _should_run("recommendations", sections):
                console.print("")
                tou_result = None
                if acct.meter_type == MeterType.ELECTRIC and hourly:
                    recent_hourly = [
                        r for r in hourly
                        if r.start_time >= now - timedelta(days=30)
                    ]
                    if recent_hourly:
                        tou_result = analyzer.tou_analysis(
                            recent_hourly,
                            config.peak_hours_start,
                            config.peak_hours_end,
                            tz,
                        )

                yoy_result = analyzer.yoy_comparison(ds, days=30)
                anomaly_result = analyzer.detect_anomalies(ds)
                seasonal_result = analyzer.seasonal_patterns(ds)

                recs = analyzer.generate_recommendations(
                    tou=tou_result,
                    yoy=yoy_result,
                    anomalies=anomaly_result,
                    seasonal=seasonal_result,
                    daily=ds,
                )
                presenter.print_recommendations(recs, console)

    finally:
        db.close()


@app.command()
def config(
    config_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    show: bool = typer.Option(False, "--show", help="Print current config"),
    check: bool = typer.Option(False, "--check", help="Validate config"),
) -> None:
    """Show or validate configuration."""
    cfg = _get_config(config_path)

    if show or not check:
        console.print(f"Username:         {cfg.username}")
        console.print(f"Password:         {'*' * len(cfg.password)}")
        console.print(f"Database:         {cfg.db_path}")
        console.print(f"Timezone:         {cfg.timezone}")
        console.print(f"Peak hours:       {cfg.peak_hours_start}:00 - {cfg.peak_hours_end}:00")
        console.print(f"Default meter:    {cfg.default_meter}")
        console.print(f"Initial fetch:    {cfg.initial_fetch_days} days")

    if check:
        console.print("\n[green]Configuration is valid.[/green]")
        db = _get_db(cfg)
        console.print(f"[green]Database connection OK: {cfg.db_path}[/green]")
        db.close()


@app.command()
def db(
    config_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    vacuum: bool = typer.Option(False, "--vacuum", help="Compact the database"),
    stats: bool = typer.Option(False, "--stats", help="Show table row counts"),
    export: Optional[Path] = typer.Option(None, "--export", help="Export to CSV directory"),
) -> None:
    """Database utilities."""
    cfg = _get_config(config_path)
    database = _get_db(cfg)

    try:
        if stats or (not vacuum and export is None):
            s = database.get_db_stats()
            for table, count in s.items():
                console.print(f"  {table}: {count:,} rows")
            try:
                size = cfg.db_path.stat().st_size / (1024 * 1024)
                console.print(f"\n  Total size: {size:.1f} MB")
            except OSError:
                pass

        if vacuum:
            console.print("Running VACUUM...")
            database.vacuum()
            console.print("[green]Done.[/green]")

        if export:
            _export_csv(database, export)
    finally:
        database.close()


@app.command()
def peak(
    config_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    days: int = typer.Option(30, "--days", "-d", help="Days of data to analyze"),
    weekdays_only: bool = typer.Option(False, "--weekdays-only", "-w",
                                        help="Only analyze weekday usage"),
) -> None:
    """Analyze peak-hour usage patterns and savings opportunities."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from . import analyzer
    from . import presenter
    from .models import RATE_PLANS

    config = _get_config(config_path)
    db = _get_db(config)

    try:
        accounts = db.get_accounts()
        if not accounts:
            console.print("[yellow]No data yet. Run 'pge-tracker sync' first.[/yellow]")
            return

        tz = ZoneInfo(config.timezone)
        now = datetime.now(tz)
        start = now - timedelta(days=days)

        rate_plan = RATE_PLANS.get(config.rate_plan)
        if rate_plan is None:
            console.print(f"[red]Unknown rate plan: {config.rate_plan}. "
                          f"Available: {', '.join(RATE_PLANS.keys())}[/red]")
            raise typer.Exit(1)

        # Only electric accounts have TOU
        electric_accounts = [a for a in accounts if a.meter_type == MeterType.ELECTRIC]
        if not electric_accounts:
            console.print("[yellow]No electric accounts found.[/yellow]")
            return

        for acct in electric_accounts:
            hourly = db.get_usage_reads(acct.id, Resolution.HOUR, start, now)
            if not hourly:
                continue

            label = f"ELECTRIC ({acct.account_number or acct.id})"
            console.print(f"\n[bold cyan]{'=' * 60}[/bold cyan]")
            console.print(f"[bold cyan]  {label} — {rate_plan.name} Rate Plan[/bold cyan]")
            console.print(f"[bold cyan]  Analyzing {days} days ({start.date()} to {now.date()})[/bold cyan]")
            console.print(f"[bold cyan]{'=' * 60}[/bold cyan]\n")

            # View 1: Hour-of-Day Profile
            profile = analyzer.hourly_profile(
                hourly, rate_plan, tz, weekdays_only=weekdays_only,
            )
            presenter.print_hourly_profile(profile, console)

            # View 2: Weekly Heatmap
            console.print("")
            heatmap = analyzer.weekly_heatmap(hourly, tz)
            presenter.print_weekly_heatmap(heatmap, rate_plan, console)

            # View 3: Peak Day Drill-Down
            console.print("")
            worst_days = analyzer.peak_day_drilldown(hourly, rate_plan, tz, top_n=5)
            presenter.print_peak_day_drilldown(worst_days, profile, console)

            # View 4: Shift Savings Estimate
            console.print("")
            savings = analyzer.shift_savings_estimate(profile, rate_plan, num_days=30)
            presenter.print_shift_recommendations(savings, rate_plan, console)

    finally:
        db.close()


@app.command()
def web(
    config_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    host: str = typer.Option("0.0.0.0", "--host", "-H", help="Bind address"),
    port: int = typer.Option(8080, "--port", "-p", help="Port number"),
) -> None:
    """Launch the web dashboard (accessible on your local network)."""
    import socket

    from .web import run_dashboard

    config = _get_config(config_path)

    # Show helpful startup info
    console.print(f"\n[bold cyan]PG&E Energy Dashboard[/bold cyan]")
    console.print(f"  Local:   [link]http://localhost:{port}[/link]")

    # Try to get the LAN IP for family access
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        console.print(f"  Network: [link]http://{local_ip}:{port}[/link]")
    except Exception:
        pass

    console.print(f"\n  Press [bold]Ctrl+C[/bold] to stop.\n")

    run_dashboard(config, host=host, port=port)


@app.command()
def version() -> None:
    """Show version."""
    console.print(f"pge-tracker {__version__}")


# --- Helpers ---


def _get_all_cost_reads(
    db: Database,
    account_id: str,
    start: "datetime | None" = None,
    end: "datetime | None" = None,
) -> list:
    """Load cost reads at all resolutions (DAY + HOUR) and combine them."""
    from .models import CostRecord

    daily = db.get_cost_reads(account_id, Resolution.DAY, start, end)
    hourly = db.get_cost_reads(account_id, Resolution.HOUR, start, end)
    return daily + hourly


def _parse_meter_filter(meter: str) -> set[MeterType] | None:
    m = meter.lower()
    if m in ("e", "electric", "elec"):
        return {MeterType.ELECTRIC}
    elif m in ("g", "gas"):
        return {MeterType.GAS}
    return None  # both


def _should_run(section_name: str, sections: set[str] | None) -> bool:
    if sections is None:
        return True
    return section_name in sections


def _export_csv(database: Database, output_dir: Path) -> None:
    """Export all tables to CSV files."""
    import csv

    output_dir.mkdir(parents=True, exist_ok=True)

    for table in ("accounts", "usage_reads", "cost_reads", "forecasts", "fetch_log"):
        rows = database._conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
        if not rows:
            continue
        path = output_dir / f"{table}.csv"
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(rows[0].keys())
            writer.writerows(rows)
        console.print(f"  Exported {len(rows)} rows to {path}")

    console.print(f"\n[green]Export complete: {output_dir}[/green]")
