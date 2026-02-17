"""Rich terminal output for pge-tracker."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .models import (
    AccountRecord,
    AnomalyResult,
    DailyStats,
    FetchSummary,
    ForecastRecord,
    MeterType,
    PeriodSummary,
    SeasonalPattern,
    TouAnalysis,
    UsageRecord,
    YoyComparison,
)

# Unicode block characters for bar charts
_BLOCKS = " ▁▂▃▄▅▆▇█"


def _bar(value: float, max_value: float, width: int = 20) -> str:
    """Render a horizontal bar using unicode block chars."""
    if max_value <= 0:
        return ""
    ratio = min(value / max_value, 1.0)
    full_blocks = int(ratio * width)
    remainder = (ratio * width) - full_blocks
    partial = _BLOCKS[int(remainder * 8)] if remainder > 0.1 else ""
    return "█" * full_blocks + partial


def print_fetch_summary(summary: FetchSummary, console: Console) -> None:
    lines = [
        f"Accounts found: {summary.accounts_found}",
        f"Daily cost records: {summary.daily_records_written}",
        f"Hourly usage records: {summary.hourly_records_written}",
        f"Forecasts: {summary.forecasts_written}",
        f"Duration: {summary.fetch_duration_seconds}s",
    ]
    if summary.errors:
        lines.append("")
        lines.append(f"[red]Errors ({len(summary.errors)}):[/red]")
        for err in summary.errors:
            lines.append(f"  - {err}")

    console.print(Panel(
        "\n".join(lines),
        title="Sync Complete",
        border_style="green" if not summary.errors else "yellow",
    ))
    console.print(
        "[dim]Note: PG&E data has approximately 48-hour delay.[/dim]"
    )


def print_account_table(
    accounts: list[AccountRecord],
    db_stats: dict | None = None,
    console: Console | None = None,
) -> None:
    if console is None:
        console = Console()

    table = Table(title="PG&E Accounts")
    table.add_column("ID", style="cyan")
    table.add_column("Type")
    table.add_column("Account #")
    table.add_column("Source")

    for acct in accounts:
        meter_icon = "⚡" if acct.meter_type == MeterType.ELECTRIC else "🔥"
        table.add_row(
            acct.id,
            f"{meter_icon} {acct.meter_type.value}",
            acct.account_number or "—",
            acct.source.value,
        )
    console.print(table)

    if db_stats:
        console.print(f"\nDatabase: {db_stats.get('usage_reads', 0)} usage reads, "
                       f"{db_stats.get('cost_reads', 0)} cost reads, "
                       f"{db_stats.get('forecasts', 0)} forecasts")


def print_status(
    accounts: list[AccountRecord],
    db_stats: dict,
    db_path: Path,
    fetch_log: list[dict],
    console: Console,
) -> None:
    # DB file size
    try:
        size_mb = db_path.stat().st_size / (1024 * 1024)
        size_str = f"{size_mb:.1f} MB"
    except OSError:
        size_str = "unknown"

    lines = [f"Database: {db_path} ({size_str})"]

    elec = sum(1 for a in accounts if a.meter_type == MeterType.ELECTRIC)
    gas = sum(1 for a in accounts if a.meter_type == MeterType.GAS)
    lines.append(f"Accounts: {len(accounts)} ({elec} electric, {gas} gas)")
    lines.append("")

    for key, count in db_stats.items():
        lines.append(f"  {key}: {count:,} rows")

    if fetch_log:
        lines.append("")
        last = fetch_log[0]
        lines.append(f"Last sync: {last.get('fetched_at', 'unknown')} "
                      f"({last.get('fetch_type', '')} — {last.get('status', '')})")

    console.print(Panel("\n".join(lines), title="Database Status"))


def print_usage_chart(
    daily: list[DailyStats],
    unit: str = "kWh",
    console: Console | None = None,
) -> None:
    """Render a daily usage bar chart in the terminal."""
    if console is None:
        console = Console()
    if not daily:
        console.print("[dim]No usage data to display.[/dim]")
        return

    max_usage = max(d.usage for d in daily) if daily else 1.0

    table = Table(show_header=True, show_lines=False, padding=(0, 1))
    table.add_column("Date", style="dim", width=12)
    table.add_column("Day", width=3)
    table.add_column(unit, justify="right", width=8)
    table.add_column("Cost", justify="right", width=8)
    table.add_column("", width=25)

    for d in daily:
        day_name = d.day.strftime("%a")
        bar = _bar(d.usage, max_usage, width=25)
        style = "dim" if d.is_weekend else ""
        cost_str = f"${d.cost:.2f}" if d.cost is not None else "—"

        table.add_row(
            str(d.day),
            day_name,
            f"{d.usage:.1f}",
            cost_str,
            f"[green]{bar}[/green]",
            style=style,
        )
    console.print(table)


def print_period_table(
    summaries: list[PeriodSummary],
    unit: str = "kWh",
    console: Console | None = None,
) -> None:
    if console is None:
        console = Console()

    table = Table(title="Period Summary")
    table.add_column("Period", style="cyan")
    table.add_column(f"Total {unit}", justify="right")
    table.add_column("Total Cost", justify="right")
    table.add_column(f"Avg Daily {unit}", justify="right")
    table.add_column("Peak Day", justify="right")

    prev_usage = None
    for s in summaries:
        # Trend arrow
        if prev_usage is not None and prev_usage > 0:
            change = (s.total_usage - prev_usage) / prev_usage * 100
            if change > 5:
                trend = f"[red]↑ {change:.0f}%[/red]"
            elif change < -5:
                trend = f"[green]↓ {abs(change):.0f}%[/green]"
            else:
                trend = "→"
        else:
            trend = ""

        cost_str = f"${s.total_cost:.2f}" if s.total_cost is not None else "—"
        peak_str = (
            f"{s.peak_day} ({s.peak_day_usage:.1f})"
            if s.peak_day
            else "—"
        )

        table.add_row(
            f"{s.label} {trend}",
            f"{s.total_usage:.1f}",
            cost_str,
            f"{s.avg_daily_usage:.1f}",
            peak_str,
        )
        prev_usage = s.total_usage

    console.print(table)


def print_tou_breakdown(
    analysis: TouAnalysis,
    console: Console | None = None,
) -> None:
    if console is None:
        console = Console()

    total = analysis.peak_total_kwh + analysis.offpeak_total_kwh
    if total <= 0:
        console.print("[dim]No hourly data for TOU analysis.[/dim]")
        return

    peak_pct = analysis.peak_pct * 100
    offpeak_pct = 100 - peak_pct

    # Visual bar
    bar_width = 40
    peak_chars = int(peak_pct / 100 * bar_width)
    offpeak_chars = bar_width - peak_chars
    bar = f"[red]{'█' * peak_chars}[/red][green]{'█' * offpeak_chars}[/green]"

    lines = [
        f"Peak (weekdays 4-9pm):    {analysis.peak_total_kwh:.1f} kWh ({peak_pct:.1f}%)",
        f"Off-Peak:                 {analysis.offpeak_total_kwh:.1f} kWh ({offpeak_pct:.1f}%)",
        "",
        bar,
        f"[red]■[/red] Peak  [green]■[/green] Off-Peak",
    ]

    if analysis.peak_cost_estimate is not None:
        lines.append("")
        lines.append(f"Estimated peak cost:      ${analysis.peak_cost_estimate:.2f}")
        lines.append(f"Estimated off-peak cost:  ${analysis.offpeak_cost_estimate:.2f}")

        # Savings estimate: what if peak usage was shifted to off-peak
        if analysis.peak_cost_estimate > 0 and analysis.offpeak_cost_estimate is not None:
            # Rough savings if 30% of peak could be shifted
            potential = analysis.peak_cost_estimate * 0.3 * 0.23  # 23% rate diff estimate
            if potential > 1:
                lines.append(
                    f"\nShifting 30% of peak usage could save ~${potential:.0f}/month"
                )

    console.print(Panel("\n".join(lines), title="Time-of-Use Analysis"))

    if analysis.peak_days:
        table = Table(title="Top Peak-Hour Days")
        table.add_column("Date")
        table.add_column("Peak kWh", justify="right")
        for day, kwh in analysis.peak_days:
            table.add_row(str(day), f"{kwh:.1f}")
        console.print(table)


def print_anomalies(
    anomalies: list[AnomalyResult],
    console: Console | None = None,
) -> None:
    if console is None:
        console = Console()

    if not anomalies:
        console.print("[green]No unusual usage spikes detected.[/green]")
        return

    table = Table(title=f"Usage Anomalies ({len(anomalies)} detected)")
    table.add_column("Date")
    table.add_column("Actual", justify="right")
    table.add_column("Expected", justify="right")
    table.add_column("Z-Score", justify="right")
    table.add_column("Severity")

    severity_style = {
        "moderate": "yellow",
        "high": "dark_orange",
        "extreme": "red bold",
    }

    for a in anomalies[:15]:
        style = severity_style.get(a.severity, "")
        table.add_row(
            a.timestamp.strftime("%Y-%m-%d"),
            f"{a.usage:.1f}",
            f"{a.expected_usage:.1f}",
            f"{a.z_score:.1f}",
            f"[{style}]{a.severity}[/{style}]",
        )
    console.print(table)


def print_yoy_comparison(
    yoy: YoyComparison | None,
    unit: str = "kWh",
    console: Console | None = None,
) -> None:
    if console is None:
        console = Console()

    if yoy is None:
        console.print("[dim]Insufficient data for year-over-year comparison.[/dim]")
        return

    if yoy.change_pct > 0:
        change_str = f"[red]↑ {yoy.change_pct:.1f}%[/red]"
    elif yoy.change_pct < 0:
        change_str = f"[green]↓ {abs(yoy.change_pct):.1f}%[/green]"
    else:
        change_str = "→ no change"

    lines = [
        f"Current period:  {yoy.current_usage:.1f} {unit}",
        f"Same period LY:  {yoy.prior_usage:.1f} {unit}",
        f"Change:          {change_str}",
    ]
    if yoy.current_cost is not None and yoy.prior_cost is not None:
        cost_change = yoy.current_cost - yoy.prior_cost
        cost_dir = "more" if cost_change > 0 else "less"
        lines.append(
            f"Cost: ${yoy.current_cost:.2f} vs ${yoy.prior_cost:.2f} "
            f"(${abs(cost_change):.2f} {cost_dir})"
        )

    console.print(Panel("\n".join(lines), title="Year-over-Year Comparison"))


def print_seasonal_breakdown(
    patterns: list[SeasonalPattern],
    unit: str = "kWh",
    console: Console | None = None,
) -> None:
    if console is None:
        console = Console()

    if not patterns:
        console.print("[dim]Insufficient data for seasonal analysis.[/dim]")
        return

    table = Table(title="Seasonal Usage Patterns")
    table.add_column("Season")
    table.add_column("Months")
    table.add_column(f"Avg Daily {unit}", justify="right")
    table.add_column("Avg Daily Cost", justify="right")
    table.add_column("", width=20)

    max_usage = max(p.avg_daily_usage for p in patterns) if patterns else 1.0

    for p in patterns:
        bar = _bar(p.avg_daily_usage, max_usage, width=20)
        cost_str = f"${p.avg_daily_cost:.2f}" if p.avg_daily_cost is not None else "—"
        table.add_row(
            p.season,
            ", ".join(p.months),
            f"{p.avg_daily_usage:.1f}",
            cost_str,
            f"[cyan]{bar}[/cyan]",
        )
    console.print(table)


def print_forecast(
    forecasts: list[ForecastRecord],
    projection: dict,
    console: Console | None = None,
) -> None:
    if console is None:
        console = Console()

    if forecasts:
        for fc in forecasts:
            meter = "Electric" if fc.unit_of_measure in ("KWH",) else "Gas"
            lines = [
                f"Billing period: {fc.start_date} to {fc.end_date}",
                f"Usage to date:     {fc.usage_to_date:.1f} ({fc.unit_of_measure})",
                f"Forecasted total:  {fc.forecasted_usage:.1f}",
                f"Typical (last yr): {fc.typical_usage:.1f}",
                "",
                f"Cost to date:      ${fc.cost_to_date:.2f}",
                f"Forecasted cost:   ${fc.forecasted_cost:.2f}",
                f"Typical cost:      ${fc.typical_cost:.2f}",
            ]

            if fc.forecasted_cost > fc.typical_cost * 1.1:
                diff = fc.forecasted_cost - fc.typical_cost
                lines.append(
                    f"\n[yellow]Tracking ${diff:.2f} above typical[/yellow]"
                )
            elif fc.forecasted_cost < fc.typical_cost * 0.9:
                diff = fc.typical_cost - fc.forecasted_cost
                lines.append(
                    f"\n[green]Tracking ${diff:.2f} below typical[/green]"
                )

            console.print(Panel("\n".join(lines), title=f"{meter} Forecast"))

    if projection and projection.get("projected_cost") is not None:
        console.print(
            f"\n30-day cost projection: "
            f"${projection['projected_cost']:.2f} "
            f"(daily avg ${projection['daily_average']:.2f}, "
            f"trend: {projection['trend']}, "
            f"confidence: {projection['confidence']})"
        )


def print_top_usage(
    items: list[DailyStats],
    unit: str = "kWh",
    console: Console | None = None,
) -> None:
    if console is None:
        console = Console()

    if not items:
        return

    table = Table(title=f"Top {len(items)} Highest Usage Days")
    table.add_column("#", justify="right", width=3)
    table.add_column("Date")
    table.add_column("Day")
    table.add_column(unit, justify="right")
    table.add_column("Cost", justify="right")

    for i, d in enumerate(items, 1):
        cost_str = f"${d.cost:.2f}" if d.cost is not None else "—"
        table.add_row(
            str(i),
            str(d.day),
            d.day.strftime("%a"),
            f"{d.usage:.1f}",
            cost_str,
        )
    console.print(table)


def print_recommendations(
    recs: list[str],
    console: Console | None = None,
) -> None:
    if console is None:
        console = Console()

    lines = []
    for i, rec in enumerate(recs, 1):
        lines.append(f"{i}. {rec}")

    console.print(Panel(
        "\n\n".join(lines),
        title="Efficiency Recommendations",
        border_style="bright_cyan",
    ))


def print_import_summary(
    file_path: Path,
    record_count: int,
    meter_type: str,
    date_range: tuple | None,
    console: Console | None = None,
) -> None:
    if console is None:
        console = Console()

    lines = [
        f"File: {file_path}",
        f"Type: {meter_type}",
        f"Records imported: {record_count}",
    ]
    if date_range and date_range[0]:
        lines.append(f"Date range: {date_range[0]} to {date_range[1]}")

    console.print(Panel("\n".join(lines), title="Import Complete", border_style="green"))
