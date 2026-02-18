"""Tests for the analysis engine."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pge_tracker.analyzer import (
    cost_projection,
    daily_stats,
    detect_anomalies,
    generate_recommendations,
    highest_usage_days,
    hourly_profile,
    peak_day_drilldown,
    period_summaries,
    seasonal_patterns,
    shift_savings_estimate,
    tou_analysis,
    weekly_heatmap,
    yoy_comparison,
)
from pge_tracker.models import (
    CostRecord,
    DailyStats,
    DataSource,
    EV2A,
    MeterType,
    Resolution,
    TouPeriod,
    UsageRecord,
)

PACIFIC = ZoneInfo("America/Los_Angeles")


class TestDailyStats:
    def test_aggregates_hourly_to_daily(self, sample_hourly_usage, sample_cost_reads):
        ds = daily_stats(sample_hourly_usage, sample_cost_reads)
        assert len(ds) > 0
        # Hourly fixture starts Oct 1; cost fixture starts Aug 1.
        # Find the first day that actually has hourly usage data.
        oct1 = date(2024, 10, 1)
        hourly_days = [d for d in ds if d.day >= oct1]
        assert len(hourly_days) > 0
        first_hourly = hourly_days[0]
        assert first_hourly.usage > 0

    def test_joins_usage_and_cost(self, sample_daily_usage, sample_cost_reads):
        ds = daily_stats(sample_daily_usage, sample_cost_reads)
        assert len(ds) == 90
        # Cost should be present for all days
        with_cost = [d for d in ds if d.cost is not None]
        assert len(with_cost) == 90

    def test_weekend_flag(self, sample_daily_usage, sample_cost_reads):
        ds = daily_stats(sample_daily_usage, sample_cost_reads)
        weekends = [d for d in ds if d.is_weekend]
        weekdays = [d for d in ds if not d.is_weekend]
        assert len(weekends) > 0
        assert len(weekdays) > 0

    def test_empty_inputs(self):
        ds = daily_stats([], [])
        assert ds == []


class TestPeriodSummaries:
    def test_monthly_grouping(self, sample_daily_usage, sample_cost_reads):
        ds = daily_stats(sample_daily_usage, sample_cost_reads)
        summaries = period_summaries(ds, "monthly")
        assert len(summaries) >= 2  # At least 2 months in 90 days
        for s in summaries:
            assert s.total_usage > 0
            assert s.avg_daily_usage > 0

    def test_weekly_grouping(self, sample_daily_usage, sample_cost_reads):
        ds = daily_stats(sample_daily_usage, sample_cost_reads)
        summaries = period_summaries(ds, "weekly")
        assert len(summaries) >= 12  # ~13 weeks in 90 days

    def test_peak_day_identified(self, sample_daily_usage, sample_cost_reads):
        ds = daily_stats(sample_daily_usage, sample_cost_reads)
        summaries = period_summaries(ds, "monthly")
        for s in summaries:
            assert s.peak_day is not None
            assert s.peak_day_usage is not None
            assert s.peak_day_usage > 0


class TestTouAnalysis:
    def test_classifies_peak_hours(self, sample_hourly_usage):
        tou = tou_analysis(sample_hourly_usage, peak_start_hour=16, peak_end_hour=21, tz=PACIFIC)
        assert tou.peak_total_kwh > 0
        assert tou.offpeak_total_kwh > 0
        assert 0 < tou.peak_pct < 1.0

    def test_weekends_are_offpeak(self):
        """All weekend hours should be classified as off-peak."""
        # Create a Saturday with 24 hours of usage
        saturday = date(2024, 10, 5)  # A Saturday
        sunday = date(2024, 10, 6)
        reads = [
            UsageRecord(
                account_id="test",
                start_time=datetime(2024, 10, 5, h, tzinfo=PACIFIC),
                end_time=(
                    datetime(2024, 10, 5, h + 1, tzinfo=PACIFIC) if h < 23
                    else datetime(2024, 10, 6, 0, tzinfo=PACIFIC)
                ),
                unit_of_measure="KWH",
                usage=1.0,
                resolution=Resolution.HOUR,
                source=DataSource.OPOWER,
            )
            for h in range(24)
        ]
        tou = tou_analysis(reads, peak_start_hour=16, peak_end_hour=21, tz=PACIFIC)
        assert tou.peak_total_kwh == 0.0
        assert tou.offpeak_total_kwh == 24.0

    def test_top_peak_days(self, sample_hourly_usage):
        tou = tou_analysis(sample_hourly_usage, tz=PACIFIC)
        assert len(tou.peak_days) <= 5
        if len(tou.peak_days) >= 2:
            # Should be sorted descending
            assert tou.peak_days[0][1] >= tou.peak_days[1][1]


class TestAnomalyDetection:
    def test_detects_injected_spike(self):
        """A known spike should be detected."""
        ds = []
        base_date = date(2024, 1, 1)
        for i in range(60):
            d = base_date + timedelta(days=i)
            usage = 15.0  # Normal baseline
            if i == 50:
                usage = 45.0  # 3x spike
            ds.append(DailyStats(day=d, usage=usage, cost=None, is_weekend=d.weekday() >= 5))

        anomalies = detect_anomalies(ds, z_threshold=2.5)
        spike_dates = [a.timestamp.date() for a in anomalies]
        assert (base_date + timedelta(days=50)) in spike_dates

    def test_no_anomalies_in_flat_data(self):
        """Constant usage should produce no anomalies."""
        ds = [
            DailyStats(
                day=date(2024, 1, 1) + timedelta(days=i),
                usage=15.0,
                cost=None,
                is_weekend=(date(2024, 1, 1) + timedelta(days=i)).weekday() >= 5,
            )
            for i in range(60)
        ]
        anomalies = detect_anomalies(ds)
        assert len(anomalies) == 0

    def test_separates_weekday_weekend(self):
        """Normal weekend variation shouldn't trigger weekday anomalies."""
        ds = []
        for i in range(60):
            d = date(2024, 1, 1) + timedelta(days=i)
            # Weekends consistently higher than weekdays
            usage = 20.0 if d.weekday() >= 5 else 12.0
            ds.append(DailyStats(day=d, usage=usage, cost=None, is_weekend=d.weekday() >= 5))

        anomalies = detect_anomalies(ds)
        # Should find zero anomalies — consistent pattern
        assert len(anomalies) == 0


class TestYoyComparison:
    def test_returns_none_with_insufficient_data(self):
        ds = [
            DailyStats(day=date(2024, 1, 1) + timedelta(days=i), usage=10.0, cost=None, is_weekend=False)
            for i in range(10)
        ]
        result = yoy_comparison(ds, days=30)
        assert result is None

    def test_computes_change(self):
        # Create 2 years of data
        ds = []
        for i in range(730):
            d = date(2023, 1, 1) + timedelta(days=i)
            # Second year has 20% higher usage
            usage = 10.0 if d.year == 2023 else 12.0
            ds.append(DailyStats(day=d, usage=usage, cost=None, is_weekend=d.weekday() >= 5))

        result = yoy_comparison(ds, days=30, reference_date=date(2024, 6, 15))
        assert result is not None
        assert result.change_pct > 0  # Usage went up


class TestSeasonalPatterns:
    def test_groups_by_season(self):
        ds = []
        for i in range(365):
            d = date(2024, 1, 1) + timedelta(days=i)
            month = d.month
            if month in (6, 7, 8):
                usage = 25.0  # Summer
            elif month in (12, 1, 2):
                usage = 20.0  # Winter
            else:
                usage = 12.0  # Spring/Fall
            ds.append(DailyStats(day=d, usage=usage, cost=None, is_weekend=d.weekday() >= 5))

        patterns = seasonal_patterns(ds)
        assert len(patterns) == 4

        summer = next(p for p in patterns if p.season == "Summer")
        spring = next(p for p in patterns if p.season == "Spring")
        assert summer.avg_daily_usage > spring.avg_daily_usage

    def test_december_is_winter(self):
        ds = [
            DailyStats(day=date(2024, 12, 15), usage=20.0, cost=None, is_weekend=False),
        ]
        patterns = seasonal_patterns(ds)
        assert len(patterns) == 1
        assert patterns[0].season == "Winter"


class TestCostProjection:
    def test_insufficient_data(self):
        ds = [DailyStats(day=date(2024, 1, 1), usage=10.0, cost=5.0, is_weekend=False)]
        result = cost_projection(ds)
        assert result["trend"] == "insufficient_data"

    def test_stable_trend(self):
        ds = [
            DailyStats(
                day=date(2024, 1, 1) + timedelta(days=i),
                usage=10.0,
                cost=3.00,
                is_weekend=False,
            )
            for i in range(60)
        ]
        result = cost_projection(ds)
        assert result["trend"] == "stable"
        assert result["projected_cost"] is not None
        # 30 days at $3/day = ~$90
        assert 80 <= result["projected_cost"] <= 100

    def test_increasing_trend(self):
        ds = [
            DailyStats(
                day=date(2024, 1, 1) + timedelta(days=i),
                usage=10.0 + i * 0.1,
                cost=3.00 + i * 0.05,
                is_weekend=False,
            )
            for i in range(60)
        ]
        result = cost_projection(ds)
        assert result["trend"] == "increasing"

    def test_never_negative(self):
        ds = [
            DailyStats(
                day=date(2024, 1, 1) + timedelta(days=i),
                usage=1.0,
                cost=0.5,
                is_weekend=False,
            )
            for i in range(60)
        ]
        result = cost_projection(ds)
        assert result["projected_cost"] >= 0


class TestHighestUsage:
    def test_returns_top_n(self, sample_daily_usage, sample_cost_reads):
        ds = daily_stats(sample_daily_usage, sample_cost_reads)
        top = highest_usage_days(ds, top_n=5)
        assert len(top) == 5
        # Should be sorted descending
        assert top[0].usage >= top[1].usage


class TestRecommendations:
    def test_generates_at_least_one(self):
        ds = [
            DailyStats(
                day=date(2024, 1, 1) + timedelta(days=i),
                usage=10.0,
                cost=3.0,
                is_weekend=(date(2024, 1, 1) + timedelta(days=i)).weekday() >= 5,
            )
            for i in range(30)
        ]
        recs = generate_recommendations(
            tou=None, yoy=None, anomalies=[], seasonal=[], daily=ds
        )
        assert len(recs) >= 1


class TestHourlyProfile:
    def test_returns_24_hours(self, sample_hourly_usage):
        profile = hourly_profile(sample_hourly_usage, EV2A, tz=PACIFIC)
        assert len(profile) == 24
        assert profile[0].hour == 0
        assert profile[23].hour == 23

    def test_peak_hours_marked(self, sample_hourly_usage):
        profile = hourly_profile(sample_hourly_usage, EV2A, tz=PACIFIC)
        for h in profile:
            if 16 <= h.hour < 21:
                assert h.period == TouPeriod.PEAK
            elif h.hour == 15 or 21 <= h.hour < 24:
                assert h.period == TouPeriod.PART_PEAK
            else:
                assert h.period == TouPeriod.OFF_PEAK

    def test_peak_hours_have_higher_usage(self, sample_hourly_usage):
        """Fixture has 1.2 base in peak vs 0.5 midday — peak should be higher."""
        profile = hourly_profile(sample_hourly_usage, EV2A, tz=PACIFIC)
        peak_avg = sum(h.avg_usage for h in profile if h.period == TouPeriod.PEAK) / 5
        midday_avg = sum(h.avg_usage for h in profile if 10 <= h.hour < 15) / 5
        assert peak_avg > midday_avg

    def test_cost_estimates_positive(self, sample_hourly_usage):
        profile = hourly_profile(sample_hourly_usage, EV2A, tz=PACIFIC)
        for h in profile:
            if h.count > 0:
                assert h.est_cost_per_hour > 0

    def test_weekdays_only_filter(self, sample_hourly_usage):
        all_days = hourly_profile(sample_hourly_usage, EV2A, tz=PACIFIC)
        weekdays = hourly_profile(sample_hourly_usage, EV2A, tz=PACIFIC, weekdays_only=True)
        # Weekday-only should have fewer data points
        assert weekdays[0].count < all_days[0].count


class TestWeeklyHeatmap:
    def test_returns_7x24_matrix(self, sample_hourly_usage):
        hm = weekly_heatmap(sample_hourly_usage, tz=PACIFIC)
        assert len(hm) == 7
        for row in hm:
            assert len(row) == 24

    def test_values_nonnegative(self, sample_hourly_usage):
        hm = weekly_heatmap(sample_hourly_usage, tz=PACIFIC)
        for row in hm:
            for val in row:
                assert val >= 0


class TestPeakDayDrilldown:
    def test_returns_top_n(self, sample_hourly_usage):
        days = peak_day_drilldown(sample_hourly_usage, EV2A, tz=PACIFIC, top_n=3)
        assert len(days) <= 3

    def test_sorted_by_peak_usage(self, sample_hourly_usage):
        days = peak_day_drilldown(sample_hourly_usage, EV2A, tz=PACIFIC, top_n=5)
        if len(days) >= 2:
            assert days[0].peak_total >= days[1].peak_total

    def test_hourly_detail_within_peak_window(self, sample_hourly_usage):
        days = peak_day_drilldown(sample_hourly_usage, EV2A, tz=PACIFIC, top_n=1)
        if days:
            for hour, kwh in days[0].hourly:
                # Should include peak (16-21) and part-peak (15, 21-23) hours
                assert 15 <= hour <= 23

    def test_excludes_weekends(self, sample_hourly_usage):
        days = peak_day_drilldown(sample_hourly_usage, EV2A, tz=PACIFIC, top_n=10)
        for d in days:
            assert d.day.weekday() < 5  # No weekends


class TestShiftSavings:
    def test_positive_savings(self, sample_hourly_usage):
        profile = hourly_profile(sample_hourly_usage, EV2A, tz=PACIFIC)
        savings = shift_savings_estimate(profile, EV2A, num_days=30)
        assert savings.avg_daily_peak_kwh > 0
        assert savings.monthly_peak_cost > 0
        assert savings.est_monthly_savings_high >= savings.est_monthly_savings_low
        assert savings.est_monthly_savings_low >= 0

    def test_heaviest_hours_in_peak(self, sample_hourly_usage):
        profile = hourly_profile(sample_hourly_usage, EV2A, tz=PACIFIC)
        savings = shift_savings_estimate(profile, EV2A)
        for hour, kwh in savings.heaviest_hours:
            assert 16 <= hour < 21

    def test_rates_match_plan(self, sample_hourly_usage):
        profile = hourly_profile(sample_hourly_usage, EV2A, tz=PACIFIC)
        savings = shift_savings_estimate(profile, EV2A)
        assert savings.peak_rate == EV2A.winter_peak
        assert savings.offpeak_rate == EV2A.winter_off_peak
