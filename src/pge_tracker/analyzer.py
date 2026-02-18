"""Analysis engine for PG&E usage data.

All functions are pure: they take lists of records and return result objects.
No database calls, no side effects. Uses only stdlib (statistics module).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from itertools import groupby
from statistics import mean, stdev
from zoneinfo import ZoneInfo

from .models import (
    AnomalyResult,
    CostRecord,
    DailyStats,
    HourOfDayStats,
    PeakDayDetail,
    PeriodSummary,
    RatePlan,
    Resolution,
    SeasonalPattern,
    ShiftSavings,
    TouAnalysis,
    TouPeriod,
    UsageRecord,
    YoyComparison,
)


def daily_stats(
    usage_reads: list[UsageRecord],
    cost_reads: list[CostRecord],
) -> list[DailyStats]:
    """Join daily usage and cost data by date.

    If usage_reads contains hourly data, it's aggregated to daily first.
    """
    # Aggregate usage by date
    usage_by_day: dict[date, float] = defaultdict(float)
    for r in usage_reads:
        day = r.start_time.date() if isinstance(r.start_time, datetime) else r.start_time
        usage_by_day[day] += r.usage

    # Aggregate cost (and fallback usage) by date
    cost_by_day: dict[date, float] = defaultdict(float)
    cost_usage_by_day: dict[date, float] = defaultdict(float)
    cost_days: set[date] = set()
    for r in cost_reads:
        day = r.start_time.date() if isinstance(r.start_time, datetime) else r.start_time
        if r.cost is not None:
            cost_by_day[day] += r.cost
            cost_days.add(day)
        if r.usage is not None:
            cost_usage_by_day[day] += r.usage

    all_days = sorted(set(usage_by_day.keys()) | cost_days)
    return [
        DailyStats(
            day=d,
            # Prefer usage from usage_reads; fall back to cost_reads' usage field
            usage=usage_by_day[d] if d in usage_by_day else cost_usage_by_day.get(d, 0.0),
            cost=cost_by_day[d] if d in cost_days else None,
            is_weekend=d.weekday() >= 5,
        )
        for d in all_days
    ]


def period_summaries(
    daily: list[DailyStats],
    period: str = "monthly",
) -> list[PeriodSummary]:
    """Group daily stats into weekly or monthly periods."""

    def key_func(d: DailyStats) -> str:
        if period == "weekly":
            # ISO week: "2024-W03"
            iso = d.day.isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        else:
            return d.day.strftime("%Y-%m")

    def label_func(key: str) -> str:
        if period == "weekly":
            return f"Week of {key}"
        else:
            # "2024-01" -> "January 2024"
            try:
                dt = datetime.strptime(key, "%Y-%m")
                return dt.strftime("%B %Y")
            except ValueError:
                return key

    results: list[PeriodSummary] = []
    for key, group in groupby(daily, key=key_func):
        items = list(group)
        total_usage = sum(d.usage for d in items)
        costs = [d.cost for d in items if d.cost is not None]
        total_cost = sum(costs) if costs else None
        avg_daily = total_usage / len(items) if items else 0.0

        peak = max(items, key=lambda d: d.usage) if items else None

        results.append(
            PeriodSummary(
                label=label_func(key),
                total_usage=round(total_usage, 2),
                total_cost=round(total_cost, 2) if total_cost is not None else None,
                avg_daily_usage=round(avg_daily, 2),
                peak_day=peak.day if peak else None,
                peak_day_usage=round(peak.usage, 2) if peak else None,
            )
        )
    return results


def tou_analysis(
    hourly_reads: list[UsageRecord],
    peak_start_hour: int = 16,
    peak_end_hour: int = 21,
    tz: ZoneInfo | None = None,
) -> TouAnalysis:
    """Classify hourly usage as peak or off-peak.

    Peak = weekdays during peak_start_hour to peak_end_hour (PG&E E-TOU-C).
    """
    if tz is None:
        tz = ZoneInfo("America/Los_Angeles")

    peak_kwh = 0.0
    offpeak_kwh = 0.0
    peak_by_day: dict[date, float] = defaultdict(float)

    for r in hourly_reads:
        local_time = r.start_time.astimezone(tz) if r.start_time.tzinfo else r.start_time
        is_weekday = local_time.weekday() < 5
        hour = local_time.hour
        is_peak = is_weekday and (peak_start_hour <= hour < peak_end_hour)

        if is_peak:
            peak_kwh += r.usage
            peak_by_day[local_time.date()] += r.usage
        else:
            offpeak_kwh += r.usage

    total = peak_kwh + offpeak_kwh
    peak_pct = peak_kwh / total if total > 0 else 0.0

    # Top 5 worst peak days
    sorted_days = sorted(peak_by_day.items(), key=lambda x: x[1], reverse=True)
    top_peak_days = [(d, round(v, 2)) for d, v in sorted_days[:5]]

    return TouAnalysis(
        peak_total_kwh=round(peak_kwh, 2),
        offpeak_total_kwh=round(offpeak_kwh, 2),
        peak_pct=round(peak_pct, 4),
        peak_cost_estimate=None,
        offpeak_cost_estimate=None,
        peak_days=top_peak_days,
    )


def tou_with_cost_estimates(
    tou: TouAnalysis,
    daily: list[DailyStats],
    peak_rate_premium: float = 1.3,
) -> TouAnalysis:
    """Add cost estimates to a TOU analysis using daily cost data.

    Uses a simple ratio: peak cost is estimated as a larger share of total cost
    based on the peak_rate_premium multiplier.
    """
    total_cost = sum(d.cost for d in daily if d.cost is not None)
    if total_cost <= 0:
        return tou

    total_kwh = tou.peak_total_kwh + tou.offpeak_total_kwh
    if total_kwh <= 0:
        return tou

    # Weighted cost allocation
    peak_weighted = tou.peak_total_kwh * peak_rate_premium
    offpeak_weighted = tou.offpeak_total_kwh
    total_weighted = peak_weighted + offpeak_weighted

    peak_cost = total_cost * (peak_weighted / total_weighted) if total_weighted > 0 else 0
    offpeak_cost = total_cost - peak_cost

    return TouAnalysis(
        peak_total_kwh=tou.peak_total_kwh,
        offpeak_total_kwh=tou.offpeak_total_kwh,
        peak_pct=tou.peak_pct,
        peak_cost_estimate=round(peak_cost, 2),
        offpeak_cost_estimate=round(offpeak_cost, 2),
        peak_days=tou.peak_days,
    )


def detect_anomalies(
    daily: list[DailyStats],
    window_days: int = 30,
    z_threshold: float = 2.5,
) -> list[AnomalyResult]:
    """Detect unusually high usage days using rolling z-scores.

    Separates weekday and weekend baselines to reduce false positives.
    """
    anomalies: list[AnomalyResult] = []

    # Build separate series for weekdays and weekends
    weekday_vals: list[tuple[int, DailyStats]] = []
    weekend_vals: list[tuple[int, DailyStats]] = []
    for i, d in enumerate(daily):
        if d.is_weekend:
            weekend_vals.append((i, d))
        else:
            weekday_vals.append((i, d))

    for series in (weekday_vals, weekend_vals):
        for pos, (orig_idx, stat) in enumerate(series):
            # Lookback window of same day-type
            start_pos = max(0, pos - window_days)
            window = [s.usage for _, s in series[start_pos:pos]]

            if len(window) < 7:
                continue

            mu = mean(window)
            sd = stdev(window) if len(window) > 1 else 0.0

            if sd == 0:
                # With zero variance, any deviation from the mean is extreme.
                # Use a small epsilon to produce a meaningful z-score.
                if stat.usage <= mu:
                    continue
                z = (stat.usage - mu) / (mu * 0.01) if mu > 0 else 10.0
            else:
                z = (stat.usage - mu) / sd
            if z < z_threshold:
                continue

            if z >= 5.0:
                severity = "extreme"
            elif z >= 3.5:
                severity = "high"
            else:
                severity = "moderate"

            day_type = "weekend" if stat.is_weekend else "weekday"
            anomalies.append(
                AnomalyResult(
                    timestamp=datetime.combine(stat.day, datetime.min.time()),
                    usage=round(stat.usage, 2),
                    expected_usage=round(mu, 2),
                    z_score=round(z, 2),
                    severity=severity,
                    description=(
                        f"{stat.day}: {stat.usage:.1f} vs {mu:.1f} expected "
                        f"({day_type} avg) — {z:.1f} standard deviations above normal"
                    ),
                )
            )

    anomalies.sort(key=lambda a: a.z_score, reverse=True)
    return anomalies


def yoy_comparison(
    daily: list[DailyStats],
    days: int = 30,
    reference_date: date | None = None,
) -> YoyComparison | None:
    """Compare recent usage to the same period one year prior.

    Returns None if insufficient data for comparison.
    """
    if not daily:
        return None

    ref = reference_date or daily[-1].day
    current_start = ref - timedelta(days=days)
    prior_start = current_start - timedelta(days=365)
    prior_end = ref - timedelta(days=365)

    by_day = {d.day: d for d in daily}

    current_days = [by_day[d] for d in _date_range(current_start, ref) if d in by_day]
    prior_days = [by_day[d] for d in _date_range(prior_start, prior_end) if d in by_day]

    # Require at least 50% coverage for both periods
    min_coverage = days * 0.5
    if len(current_days) < min_coverage or len(prior_days) < min_coverage:
        return None

    current_usage = sum(d.usage for d in current_days)
    prior_usage = sum(d.usage for d in prior_days)
    change_pct = ((current_usage - prior_usage) / prior_usage * 100) if prior_usage > 0 else 0.0

    current_costs = [d.cost for d in current_days if d.cost is not None]
    prior_costs = [d.cost for d in prior_days if d.cost is not None]

    return YoyComparison(
        current_period_label=f"{current_start} to {ref}",
        prior_period_label=f"{prior_start} to {prior_end}",
        current_usage=round(current_usage, 2),
        prior_usage=round(prior_usage, 2),
        change_pct=round(change_pct, 1),
        current_cost=round(sum(current_costs), 2) if current_costs else None,
        prior_cost=round(sum(prior_costs), 2) if prior_costs else None,
    )


def seasonal_patterns(daily: list[DailyStats]) -> list[SeasonalPattern]:
    """Average daily usage by meteorological season across all years."""
    season_map = {
        12: "Winter",
        1: "Winter",
        2: "Winter",
        3: "Spring",
        4: "Spring",
        5: "Spring",
        6: "Summer",
        7: "Summer",
        8: "Summer",
        9: "Fall",
        10: "Fall",
        11: "Fall",
    }
    season_months = {
        "Winter": ["Dec", "Jan", "Feb"],
        "Spring": ["Mar", "Apr", "May"],
        "Summer": ["Jun", "Jul", "Aug"],
        "Fall": ["Sep", "Oct", "Nov"],
    }

    usage_by_season: dict[str, list[float]] = defaultdict(list)
    cost_by_season: dict[str, list[float]] = defaultdict(list)

    for d in daily:
        season = season_map[d.day.month]
        usage_by_season[season].append(d.usage)
        if d.cost is not None:
            cost_by_season[season].append(d.cost)

    results = []
    for season in ("Winter", "Spring", "Summer", "Fall"):
        usages = usage_by_season.get(season, [])
        costs = cost_by_season.get(season, [])
        if not usages:
            continue
        results.append(
            SeasonalPattern(
                season=season,
                avg_daily_usage=round(mean(usages), 2),
                avg_daily_cost=round(mean(costs), 2) if costs else None,
                months=season_months[season],
            )
        )
    return results


def highest_usage_days(
    daily: list[DailyStats],
    top_n: int = 10,
) -> list[DailyStats]:
    """Return the top-N highest usage days."""
    return sorted(daily, key=lambda d: d.usage, reverse=True)[:top_n]


def highest_usage_hours(
    hourly_reads: list[UsageRecord],
    top_n: int = 10,
) -> list[UsageRecord]:
    """Return the top-N highest usage hours."""
    return sorted(hourly_reads, key=lambda r: r.usage, reverse=True)[:top_n]


def cost_projection(
    daily: list[DailyStats],
    days_ahead: int = 30,
) -> dict:
    """Project cost forward using a simple linear regression on recent data.

    Uses the last 60 days (or all available if less).
    Returns: {projected_cost, daily_average, trend, confidence}
    """
    recent = [d for d in daily if d.cost is not None][-60:]
    if len(recent) < 7:
        return {
            "projected_cost": None,
            "daily_average": None,
            "trend": "insufficient_data",
            "confidence": "low",
        }

    costs = [d.cost for d in recent]
    n = len(costs)
    x = list(range(n))

    x_bar = sum(x) / n
    y_bar = sum(costs) / n

    numerator = sum((xi - x_bar) * (yi - y_bar) for xi, yi in zip(x, costs))
    denominator = sum((xi - x_bar) ** 2 for xi in x)

    if denominator == 0:
        slope = 0.0
    else:
        slope = numerator / denominator

    intercept = y_bar - slope * x_bar

    # R-squared for confidence
    y_hat = [slope * xi + intercept for xi in x]
    ss_res = sum((yi - yh) ** 2 for yi, yh in zip(costs, y_hat))
    ss_tot = sum((yi - y_bar) ** 2 for yi in costs)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    if r_squared > 0.7:
        confidence = "high"
    elif r_squared > 0.4:
        confidence = "medium"
    else:
        confidence = "low"

    daily_avg = mean(costs)
    projected = max(0.0, daily_avg * days_ahead)

    if slope > 0.01:
        trend = "increasing"
    elif slope < -0.01:
        trend = "decreasing"
    else:
        trend = "stable"

    return {
        "projected_cost": round(projected, 2),
        "daily_average": round(daily_avg, 2),
        "trend": trend,
        "confidence": confidence,
        "days_ahead": days_ahead,
    }


def generate_recommendations(
    tou: TouAnalysis | None,
    yoy: YoyComparison | None,
    anomalies: list[AnomalyResult],
    seasonal: list[SeasonalPattern],
    daily: list[DailyStats],
) -> list[str]:
    """Generate actionable efficiency recommendations based on analysis."""
    recs: list[str] = []

    # TOU-based recommendations
    if tou and tou.peak_pct > 0.35:
        pct = round(tou.peak_pct * 100)
        recs.append(
            f"{pct}% of your electricity usage is during peak hours (4-9pm). "
            "Consider running the dishwasher, laundry, and EV charging after 9pm "
            "or before 4pm to lower your bill."
        )
    if tou and tou.peak_days:
        worst_day, worst_kwh = tou.peak_days[0]
        recs.append(
            f"Your highest peak-hour day was {worst_day} ({worst_kwh} kWh). "
            "Check what was running that evening."
        )

    # YoY-based recommendations
    if yoy and yoy.change_pct > 10:
        recs.append(
            f"Your usage is up {yoy.change_pct}% compared to the same period "
            "last year. Consider an energy audit to identify what changed."
        )
    elif yoy and yoy.change_pct < -5:
        recs.append(
            f"Great news — your usage is down {abs(yoy.change_pct)}% vs last year!"
        )

    # Anomaly-based recommendations
    high_anomalies = [a for a in anomalies if a.severity in ("high", "extreme")]
    if high_anomalies:
        dates = ", ".join(
            a.timestamp.strftime("%b %d") for a in high_anomalies[:3]
        )
        recs.append(
            f"Unusual usage spikes detected on: {dates}. "
            "Check for appliance issues, space heaters, or AC running overtime."
        )

    # Seasonal recommendations
    if len(seasonal) >= 2:
        summer = next((s for s in seasonal if s.season == "Summer"), None)
        winter = next((s for s in seasonal if s.season == "Winter"), None)
        spring = next((s for s in seasonal if s.season == "Spring"), None)

        if summer and spring and summer.avg_daily_usage > spring.avg_daily_usage * 1.5:
            recs.append(
                f"Summer usage ({summer.avg_daily_usage:.1f}/day) is "
                f"{summer.avg_daily_usage / spring.avg_daily_usage:.1f}x your "
                "spring baseline. AC is your biggest opportunity — consider "
                "raising the thermostat 2-3 degrees or using fans."
            )
        if winter and spring and winter.avg_daily_usage > spring.avg_daily_usage * 1.5:
            recs.append(
                f"Winter usage ({winter.avg_daily_usage:.1f}/day) is elevated. "
                "Check insulation, seal drafts, and consider a programmable thermostat."
            )

    # General baseline recommendation
    if daily:
        recent = daily[-30:] if len(daily) >= 30 else daily
        avg = mean(d.usage for d in recent)
        weekday_avg = mean(d.usage for d in recent if not d.is_weekend) if any(not d.is_weekend for d in recent) else avg
        weekend_avg = mean(d.usage for d in recent if d.is_weekend) if any(d.is_weekend for d in recent) else avg

        if weekend_avg > weekday_avg * 1.3:
            recs.append(
                f"Weekend usage ({weekend_avg:.1f}/day) is significantly higher "
                f"than weekdays ({weekday_avg:.1f}/day). Check what's different "
                "on weekends."
            )

    if not recs:
        recs.append(
            "Your usage patterns look normal. Keep it up! "
            "Run this analysis monthly to catch any changes early."
        )

    return recs


# --- Peak analysis ---


def hourly_profile(
    hourly_reads: list[UsageRecord],
    rate_plan: RatePlan,
    tz: ZoneInfo | None = None,
    weekdays_only: bool = False,
) -> list[HourOfDayStats]:
    """Compute average usage for each hour of the day (0-23).

    Returns 24 HourOfDayStats sorted by hour, with cost estimates
    computed from the rate plan.
    """
    if tz is None:
        tz = ZoneInfo("America/Los_Angeles")

    # Bucket usage by hour of day
    by_hour: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for r in hourly_reads:
        lt = r.start_time.astimezone(tz) if r.start_time.tzinfo else r.start_time
        if weekdays_only and lt.weekday() >= 5:
            continue
        by_hour[lt.hour].append((r.usage, lt.month))

    results: list[HourOfDayStats] = []
    for hour in range(24):
        entries = by_hour.get(hour, [])
        if not entries:
            period = rate_plan.classify_hour(hour)
            results.append(HourOfDayStats(
                hour=hour, avg_usage=0.0, max_usage=0.0, count=0,
                period=period, est_cost_per_hour=0.0,
            ))
            continue

        usages = [u for u, _ in entries]
        avg_u = mean(usages)
        max_u = max(usages)
        period = rate_plan.classify_hour(hour)

        # Weighted average rate across all months for this hour
        total_cost = sum(rate_plan.rate_for(m, hour) * u for u, m in entries)
        avg_cost = total_cost / len(entries)

        results.append(HourOfDayStats(
            hour=hour,
            avg_usage=round(avg_u, 3),
            max_usage=round(max_u, 2),
            count=len(entries),
            period=period,
            est_cost_per_hour=round(avg_cost, 4),
        ))

    return results


def weekly_heatmap(
    hourly_reads: list[UsageRecord],
    tz: ZoneInfo | None = None,
) -> list[list[float]]:
    """Build a 7×24 matrix of average usage (day-of-week × hour-of-day).

    Returns a list of 7 lists (Mon=0 through Sun=6), each with 24 floats.
    """
    if tz is None:
        tz = ZoneInfo("America/Los_Angeles")

    # accumulator: [dow][hour] -> list of usage values
    acc: dict[int, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in hourly_reads:
        lt = r.start_time.astimezone(tz) if r.start_time.tzinfo else r.start_time
        acc[lt.weekday()][lt.hour].append(r.usage)

    matrix: list[list[float]] = []
    for dow in range(7):
        row: list[float] = []
        for hour in range(24):
            vals = acc[dow][hour]
            row.append(round(mean(vals), 2) if vals else 0.0)
        matrix.append(row)
    return matrix


def peak_day_drilldown(
    hourly_reads: list[UsageRecord],
    rate_plan: RatePlan,
    tz: ZoneInfo | None = None,
    top_n: int = 5,
) -> list[PeakDayDetail]:
    """Find the N days with highest peak-window usage and return hour-by-hour detail."""
    if tz is None:
        tz = ZoneInfo("America/Los_Angeles")

    ps, pe = rate_plan.peak_hours

    # Group reads by date
    by_day: dict[date, list[tuple[int, float, int]]] = defaultdict(list)
    for r in hourly_reads:
        lt = r.start_time.astimezone(tz) if r.start_time.tzinfo else r.start_time
        by_day[lt.date()].append((lt.hour, r.usage, lt.month))

    # Calculate peak total per day (weekdays only — TOU peak only on weekdays)
    day_peak: list[tuple[date, float]] = []
    for d, hours in by_day.items():
        if d.weekday() >= 5:
            continue
        peak_kwh = sum(u for h, u, _ in hours if ps <= h < pe)
        if peak_kwh > 0:
            day_peak.append((d, peak_kwh))

    day_peak.sort(key=lambda x: x[1], reverse=True)

    results: list[PeakDayDetail] = []
    for d, peak_total in day_peak[:top_n]:
        hours = by_day[d]
        daily_total = sum(u for _, u, _ in hours)

        # Include peak + part-peak hours for context
        pp_ranges = rate_plan.part_peak_hours
        detail_hours: list[tuple[int, float]] = []
        est_cost = 0.0
        for h, u, m in sorted(hours, key=lambda x: x[0]):
            is_peak_or_pp = (ps <= h < pe) or any(s <= h < e for s, e in pp_ranges)
            if is_peak_or_pp:
                detail_hours.append((h, round(u, 2)))
                est_cost += u * rate_plan.rate_for(m, h)

        results.append(PeakDayDetail(
            day=d,
            peak_total=round(peak_total, 2),
            daily_total=round(daily_total, 2),
            hourly=detail_hours,
            est_peak_cost=round(est_cost, 2),
        ))

    return results


def shift_savings_estimate(
    profile: list[HourOfDayStats],
    rate_plan: RatePlan,
    num_days: int = 30,
) -> ShiftSavings:
    """Estimate monthly savings from shifting peak usage to off-peak.

    Assumes a portion of peak-hour usage could be moved to off-peak hours.
    """
    peak_hours = [h for h in profile if h.period == TouPeriod.PEAK]
    offpeak_hours = [h for h in profile if h.period == TouPeriod.OFF_PEAK]

    daily_peak_kwh = sum(h.avg_usage for h in peak_hours)
    daily_peak_cost = sum(h.est_cost_per_hour for h in peak_hours)

    # Use winter rates as baseline (since we're currently in winter)
    peak_rate = rate_plan.winter_peak
    offpeak_rate = rate_plan.winter_off_peak
    rate_diff = peak_rate - offpeak_rate

    monthly_peak_cost = daily_peak_cost * num_days

    # Sort peak hours by avg usage descending to find heaviest
    heaviest = sorted(peak_hours, key=lambda h: h.avg_usage, reverse=True)[:3]
    heaviest_hours = [(h.hour, round(h.avg_usage, 2)) for h in heaviest]

    # Conservative estimate: shift 25-40% of peak usage
    shiftable_low = daily_peak_kwh * 0.25
    shiftable_high = daily_peak_kwh * 0.40
    savings_low = shiftable_low * rate_diff * num_days
    savings_high = shiftable_high * rate_diff * num_days

    return ShiftSavings(
        avg_daily_peak_kwh=round(daily_peak_kwh, 2),
        avg_daily_peak_cost=round(daily_peak_cost, 2),
        monthly_peak_cost=round(monthly_peak_cost, 2),
        heaviest_hours=heaviest_hours,
        est_monthly_savings_low=round(max(0.0, savings_low), 2),
        est_monthly_savings_high=round(max(0.0, savings_high), 2),
        peak_rate=peak_rate,
        offpeak_rate=offpeak_rate,
    )


# --- Helpers ---


def _date_range(start: date, end: date) -> list[date]:
    """Generate a list of dates from start to end (inclusive)."""
    days = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days
