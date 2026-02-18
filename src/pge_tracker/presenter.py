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
    HourOfDayStats,
    MeterType,
    PeakDayDetail,
    PeriodSummary,
    RatePlan,
    SeasonalPattern,
    ShiftSavings,
    TouAnalysis,
    TouPeriod,
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
    cost_count: int = 0,
    console: Console | None = None,
) -> None:
    if console is None:
        console = Console()

    lines = [
        f"File: {file_path.name}",
        f"Type: {meter_type}",
        f"Usage records imported: {record_count:,}",
    ]
    if cost_count:
        lines.append(f"Cost records imported:  {cost_count:,}")
    if date_range and date_range[0]:
        lines.append(f"Date range: {date_range[0]} to {date_range[1]}")

    console.print(Panel("\n".join(lines), title="Import Complete", border_style="green"))


# --- Peak analysis views ---

_HOUR_LABELS = [
    "12am", " 1am", " 2am", " 3am", " 4am", " 5am",
    " 6am", " 7am", " 8am", " 9am", "10am", "11am",
    "12pm", " 1pm", " 2pm", " 3pm", " 4pm", " 5pm",
    " 6pm", " 7pm", " 8pm", " 9pm", "10pm", "11pm",
]


def print_hourly_profile(
    profile: list[HourOfDayStats],
    console: Console | None = None,
) -> None:
    """Render a 24-hour bar chart of average usage with cost and TOU markers."""
    if console is None:
        console = Console()

    if not profile or all(h.count == 0 for h in profile):
        console.print("[dim]No hourly data available.[/dim]")
        return

    max_usage = max(h.avg_usage for h in profile) if profile else 1.0

    table = Table(
        show_header=True, show_lines=False, padding=(0, 1),
        title="Your Average Day",
    )
    table.add_column("Hour", width=5)
    table.add_column("", width=22)
    table.add_column("kWh", justify="right", width=6)
    table.add_column("Cost", justify="right", width=7)
    table.add_column("", width=12)

    for h in profile:
        label = _HOUR_LABELS[h.hour]
        bar = _bar(h.avg_usage, max_usage, width=22)

        if h.period == TouPeriod.PEAK:
            bar_str = f"[red]{bar}[/red]"
            marker = "[red bold]◀ PEAK[/red bold]"
            style = ""
        elif h.period == TouPeriod.PART_PEAK:
            bar_str = f"[yellow]{bar}[/yellow]"
            marker = "[yellow]◀ PART-PK[/yellow]"
            style = ""
        else:
            bar_str = f"[green]{bar}[/green]"
            marker = ""
            style = "dim" if h.avg_usage < max_usage * 0.4 else ""

        cost_str = f"${h.est_cost_per_hour:.2f}" if h.count > 0 else "—"

        table.add_row(
            label,
            bar_str,
            f"{h.avg_usage:.2f}",
            cost_str,
            marker,
            style=style,
        )

    # Summary row
    peak_total = sum(h.avg_usage for h in profile if h.period == TouPeriod.PEAK)
    pp_total = sum(h.avg_usage for h in profile if h.period == TouPeriod.PART_PEAK)
    off_total = sum(h.avg_usage for h in profile if h.period == TouPeriod.OFF_PEAK)
    peak_cost = sum(h.est_cost_per_hour for h in profile if h.period == TouPeriod.PEAK)
    pp_cost = sum(h.est_cost_per_hour for h in profile if h.period == TouPeriod.PART_PEAK)
    off_cost = sum(h.est_cost_per_hour for h in profile if h.period == TouPeriod.OFF_PEAK)
    daily_total = peak_total + pp_total + off_total

    console.print(table)
    console.print("")

    peak_pct = peak_total / daily_total * 100 if daily_total > 0 else 0
    lines = [
        f"[red]Peak (4-9pm):[/red]       {peak_total:5.1f} kWh/day  ${peak_cost:.2f}  ({peak_pct:.0f}%)",
    ]
    if pp_total > 0:
        pp_pct = pp_total / daily_total * 100
        lines.append(
            f"[yellow]Part-peak:[/yellow]        {pp_total:5.1f} kWh/day  ${pp_cost:.2f}  ({pp_pct:.0f}%)"
        )
    off_pct = off_total / daily_total * 100 if daily_total > 0 else 0
    lines.append(
        f"[green]Off-peak:[/green]         {off_total:5.1f} kWh/day  ${off_cost:.2f}  ({off_pct:.0f}%)"
    )
    lines.append(f"[bold]Total:[/bold]             {daily_total:5.1f} kWh/day  ${peak_cost + pp_cost + off_cost:.2f}")

    console.print(Panel("\n".join(lines), title="Daily Breakdown", border_style="cyan"))


def print_weekly_heatmap(
    heatmap: list[list[float]],
    rate_plan: RatePlan,
    console: Console | None = None,
) -> None:
    """Render a 7×24 heatmap grid using intensity shading."""
    if console is None:
        console = Console()

    # Intensity thresholds based on data range
    all_vals = [v for row in heatmap for v in row if v > 0]
    if not all_vals:
        console.print("[dim]No data for heatmap.[/dim]")
        return

    p25 = sorted(all_vals)[len(all_vals) // 4]
    p50 = sorted(all_vals)[len(all_vals) // 2]
    p75 = sorted(all_vals)[len(all_vals) * 3 // 4]

    def _shade(val: float) -> str:
        if val == 0:
            return "  "
        elif val <= p25:
            return "░░"
        elif val <= p50:
            return "▒▒"
        elif val <= p75:
            return "▓▓"
        else:
            return "██"

    def _color_shade(val: float, hour: int) -> str:
        shade = _shade(val)
        period = rate_plan.classify_hour(hour)
        if period == TouPeriod.PEAK:
            return f"[red]{shade}[/red]"
        elif period == TouPeriod.PART_PEAK:
            return f"[yellow]{shade}[/yellow]"
        return shade

    dow_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    table = Table(
        title="Weekly Usage Heatmap (kWh/hr)",
        show_lines=False, padding=(0, 0),
    )
    table.add_column("Hour", width=5, style="dim")
    for d in dow_labels:
        table.add_column(d, width=4, justify="center")
    table.add_column("", width=10)

    ps, pe = rate_plan.peak_hours
    pp_ranges = rate_plan.part_peak_hours

    for hour in range(24):
        vals = [heatmap[dow][hour] for dow in range(7)]
        shaded = [_color_shade(v, hour) for v in vals]

        # Period marker
        is_peak = ps <= hour < pe
        is_pp = any(s <= hour < e for s, e in pp_ranges)
        if is_peak:
            marker = "[red bold]◀ PEAK[/red bold]"
        elif is_pp:
            marker = "[yellow]◀ PART[/yellow]"
        else:
            marker = ""

        table.add_row(_HOUR_LABELS[hour], *shaded, marker)

    console.print(table)
    console.print(
        f"  Legend: [dim]░░[/dim] <{p25:.1f}  ▒▒ {p25:.1f}-{p50:.1f}"
        f"  ▓▓ {p50:.1f}-{p75:.1f}  ██ >{p75:.1f} kWh"
    )


def print_peak_day_drilldown(
    days: list[PeakDayDetail],
    avg_profile: list[HourOfDayStats],
    console: Console | None = None,
) -> None:
    """Render hour-by-hour detail panels for worst peak days."""
    if console is None:
        console = Console()

    if not days:
        console.print("[dim]No peak days to display.[/dim]")
        return

    # Build avg lookup for comparison
    avg_by_hour = {h.hour: h.avg_usage for h in avg_profile}

    for rank, day in enumerate(days, 1):
        max_hourly = max(u for _, u in day.hourly) if day.hourly else 1.0

        lines: list[str] = []
        for hour, kwh in day.hourly:
            bar = _bar(kwh, max_hourly, width=18)
            avg = avg_by_hour.get(hour, 0)

            # Flag if significantly above average
            if avg > 0 and kwh > avg * 2:
                flag = f"  [red bold]← {kwh / avg:.0f}× avg[/red bold]"
            elif avg > 0 and kwh > avg * 1.5:
                flag = f"  [yellow]← {kwh / avg:.1f}× avg[/yellow]"
            else:
                flag = ""

            period_marker = ""
            if 16 <= hour < 21:
                period_marker = "[red]█[/red]"
            elif hour in (15, 21, 22, 23):
                period_marker = "[yellow]▪[/yellow]"

            lines.append(
                f"  {_HOUR_LABELS[hour]}  {period_marker} [red]{bar}[/red]"
                f"  {kwh:5.1f} kWh{flag}"
            )

        lines.append("")
        lines.append(
            f"  Peak cost est: [bold]${day.est_peak_cost:.2f}[/bold]"
            f"  │  Daily total: {day.daily_total:.1f} kWh"
        )

        title = (
            f"#{rank}  {day.day.strftime('%b %d')} ({day.day.strftime('%a')})"
            f" — {day.peak_total:.1f} kWh peak"
        )
        console.print(Panel(
            "\n".join(lines),
            title=title,
            border_style="red",
        ))


def print_shift_recommendations(
    savings: ShiftSavings,
    rate_plan: RatePlan,
    console: Console | None = None,
) -> None:
    """Render actionable peak-shift savings summary."""
    if console is None:
        console = Console()

    rate_diff_cents = (savings.peak_rate - savings.offpeak_rate) * 100

    lines = [
        f"[bold]Your peak window (4-9pm weekdays):[/bold]",
        f"  Avg daily peak usage:   {savings.avg_daily_peak_kwh:.1f} kWh",
        f"  Avg daily peak cost:    ${savings.avg_daily_peak_cost:.2f}",
        f"  Monthly peak cost:      [bold]${savings.monthly_peak_cost:.0f}[/bold]",
        "",
        f"[bold]Rate plan:[/bold] {rate_plan.name}",
        f"  Peak rate:     ${savings.peak_rate:.2f}/kWh",
        f"  Off-peak rate: ${savings.offpeak_rate:.2f}/kWh",
        f"  Difference:    [bold]${savings.peak_rate - savings.offpeak_rate:.2f}/kWh"
        f" ({rate_diff_cents:.0f}¢)[/bold]",
        "",
        "[bold]Heaviest peak hours:[/bold]",
    ]

    for hour, kwh in savings.heaviest_hours:
        lines.append(f"  {_HOUR_LABELS[hour]}:  {kwh:.1f} kWh avg")

    lines.extend([
        "",
        "[bold]What you can shift to off-peak (before 3pm or after midnight):[/bold]",
        "  • EV charging → schedule to start at 12am",
        "  • Laundry & dishwasher → run before 3pm or after midnight",
        "  • Pre-heat/cool house by 3:30pm, coast through peak",
        "  • Pool pump → schedule for morning hours",
        "",
    ])

    if savings.est_monthly_savings_high > 1:
        lines.append(
            f"[bold green]Estimated monthly savings: "
            f"${savings.est_monthly_savings_low:.0f}–${savings.est_monthly_savings_high:.0f}[/bold green]"
        )
        annual_low = savings.est_monthly_savings_low * 12
        annual_high = savings.est_monthly_savings_high * 12
        lines.append(
            f"[green]Annual potential: ${annual_low:.0f}–${annual_high:.0f}[/green]"
        )
    else:
        lines.append(
            "[green]Your peak usage is already low — nice work![/green]"
        )

    console.print(Panel(
        "\n".join(lines),
        title="Peak Shift Opportunities",
        border_style="bright_cyan",
    ))
