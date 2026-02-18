"""Flask web dashboard for pge-tracker.

Serves a single-page dashboard with Chart.js visualizations and
JSON API endpoints backed by the existing analyzer module.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request

from .config import Config
from .database import Database
from .models import MeterType, RATE_PLANS, Resolution


def _json_default(obj: object) -> object:
    """Custom JSON serializer for dataclasses, dates, and enums."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def create_app(config: Config) -> Flask:
    """Create and configure the Flask application."""
    template_dir = Path(__file__).parent / "templates"
    static_dir = Path(__file__).parent / "static"

    app = Flask(
        __name__,
        template_folder=str(template_dir),
        static_folder=str(static_dir),
    )
    app.json.sort_keys = False

    # Store config for use in routes
    app.config["PGE_CONFIG"] = config

    def _get_db() -> Database:
        db = Database(config.db_path)
        db.initialize()
        return db

    def _tz() -> ZoneInfo:
        return ZoneInfo(config.timezone)

    # --- Dashboard page ---

    @app.route("/")
    def dashboard():
        """Serve the main dashboard page."""
        db = _get_db()
        try:
            accounts = db.get_accounts()
            electric_accounts = [a for a in accounts if a.meter_type == MeterType.ELECTRIC]
            gas_accounts = [a for a in accounts if a.meter_type == MeterType.GAS]

            # Pass basic info to template
            return render_template(
                "dashboard.html",
                electric_accounts=electric_accounts,
                gas_accounts=gas_accounts,
                rate_plan=config.rate_plan,
                timezone=config.timezone,
            )
        finally:
            db.close()

    # --- JSON API endpoints ---

    @app.route("/api/accounts")
    def api_accounts():
        """Return all accounts."""
        db = _get_db()
        try:
            accounts = db.get_accounts()
            return jsonify([
                {
                    "id": a.id,
                    "meter_type": a.meter_type.value,
                    "account_number": a.account_number,
                    "service_address": a.service_address,
                    "source": a.source.value,
                }
                for a in accounts
            ])
        finally:
            db.close()

    @app.route("/api/daily")
    def api_daily():
        """Return daily usage and cost data.

        Query params:
            account_id: str (required)
            days: int (default 30)
        """
        from . import analyzer

        account_id = request.args.get("account_id")
        days = int(request.args.get("days", 30))

        if not account_id:
            return jsonify({"error": "account_id required"}), 400

        db = _get_db()
        try:
            tz = _tz()
            now = datetime.now(tz)
            start = now - timedelta(days=days)

            hourly = db.get_usage_reads(account_id, Resolution.HOUR, start, now)
            cost_day = db.get_cost_reads(account_id, Resolution.DAY, start, now)
            cost_hour = db.get_cost_reads(account_id, Resolution.HOUR, start, now)
            all_cost = cost_day + cost_hour

            ds = analyzer.daily_stats(hourly if hourly else [], all_cost)

            return jsonify([
                {
                    "day": d.day.isoformat(),
                    "usage": round(d.usage, 2),
                    "cost": round(d.cost, 2) if d.cost is not None else None,
                    "is_weekend": d.is_weekend,
                }
                for d in ds
            ])
        finally:
            db.close()

    @app.route("/api/hourly-profile")
    def api_hourly_profile():
        """Return 24-hour average usage profile.

        Query params:
            account_id: str (required)
            days: int (default 30)
            weekdays_only: bool (default false)
        """
        from . import analyzer

        account_id = request.args.get("account_id")
        days = int(request.args.get("days", 30))
        weekdays_only = request.args.get("weekdays_only", "false").lower() == "true"

        if not account_id:
            return jsonify({"error": "account_id required"}), 400

        rate_plan = RATE_PLANS.get(config.rate_plan)
        if not rate_plan:
            return jsonify({"error": f"Unknown rate plan: {config.rate_plan}"}), 500

        db = _get_db()
        try:
            tz = _tz()
            now = datetime.now(tz)
            start = now - timedelta(days=days)

            hourly = db.get_usage_reads(account_id, Resolution.HOUR, start, now)
            profile = analyzer.hourly_profile(hourly, rate_plan, tz, weekdays_only)

            return jsonify([
                {
                    "hour": h.hour,
                    "avg_usage": h.avg_usage,
                    "max_usage": h.max_usage,
                    "count": h.count,
                    "period": h.period.value,
                    "est_cost": h.est_cost_per_hour,
                }
                for h in profile
            ])
        finally:
            db.close()

    @app.route("/api/heatmap")
    def api_heatmap():
        """Return 7x24 weekly heatmap data.

        Query params:
            account_id: str (required)
            days: int (default 30)
        """
        from . import analyzer

        account_id = request.args.get("account_id")
        days = int(request.args.get("days", 30))

        if not account_id:
            return jsonify({"error": "account_id required"}), 400

        db = _get_db()
        try:
            tz = _tz()
            now = datetime.now(tz)
            start = now - timedelta(days=days)

            hourly = db.get_usage_reads(account_id, Resolution.HOUR, start, now)
            heatmap = analyzer.weekly_heatmap(hourly, tz)

            rate_plan = RATE_PLANS.get(config.rate_plan)

            return jsonify({
                "matrix": heatmap,
                "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                "peak_hours": list(rate_plan.peak_hours) if rate_plan else [16, 21],
                "part_peak_hours": rate_plan.part_peak_hours if rate_plan else [],
            })
        finally:
            db.close()

    @app.route("/api/peak-days")
    def api_peak_days():
        """Return top peak usage days with hourly detail.

        Query params:
            account_id: str (required)
            days: int (default 30)
            top_n: int (default 5)
        """
        from . import analyzer

        account_id = request.args.get("account_id")
        days = int(request.args.get("days", 30))
        top_n = int(request.args.get("top_n", 5))

        if not account_id:
            return jsonify({"error": "account_id required"}), 400

        rate_plan = RATE_PLANS.get(config.rate_plan)
        if not rate_plan:
            return jsonify({"error": f"Unknown rate plan: {config.rate_plan}"}), 500

        db = _get_db()
        try:
            tz = _tz()
            now = datetime.now(tz)
            start = now - timedelta(days=days)

            hourly = db.get_usage_reads(account_id, Resolution.HOUR, start, now)
            peak_days = analyzer.peak_day_drilldown(hourly, rate_plan, tz, top_n)

            return jsonify([
                {
                    "day": d.day.isoformat(),
                    "day_name": d.day.strftime("%a"),
                    "peak_total": d.peak_total,
                    "daily_total": d.daily_total,
                    "est_peak_cost": d.est_peak_cost,
                    "hourly": [{"hour": h, "kwh": k} for h, k in d.hourly],
                }
                for d in peak_days
            ])
        finally:
            db.close()

    @app.route("/api/summary")
    def api_summary():
        """Return a summary dashboard overview.

        Query params:
            account_id: str (required)
            days: int (default 30)
        """
        from . import analyzer

        account_id = request.args.get("account_id")
        days = int(request.args.get("days", 30))

        if not account_id:
            return jsonify({"error": "account_id required"}), 400

        rate_plan = RATE_PLANS.get(config.rate_plan)

        db = _get_db()
        try:
            tz = _tz()
            now = datetime.now(tz)
            start = now - timedelta(days=days)

            hourly = db.get_usage_reads(account_id, Resolution.HOUR, start, now)
            cost_day = db.get_cost_reads(account_id, Resolution.DAY, start, now)
            cost_hour = db.get_cost_reads(account_id, Resolution.HOUR, start, now)
            all_cost = cost_day + cost_hour

            ds = analyzer.daily_stats(hourly if hourly else [], all_cost)

            total_usage = sum(d.usage for d in ds)
            total_cost = sum(d.cost for d in ds if d.cost is not None)
            avg_daily = total_usage / len(ds) if ds else 0
            cost_days = [d for d in ds if d.cost is not None]
            avg_daily_cost = total_cost / len(cost_days) if cost_days else 0

            # TOU breakdown
            tou = None
            if hourly and rate_plan:
                profile = analyzer.hourly_profile(hourly, rate_plan, tz)
                peak_kwh = sum(h.avg_usage for h in profile if h.period.value == "peak")
                offpeak_kwh = sum(h.avg_usage for h in profile if h.period.value == "off_peak")
                pp_kwh = sum(h.avg_usage for h in profile if h.period.value == "part_peak")
                daily_total = peak_kwh + offpeak_kwh + pp_kwh
                peak_pct = peak_kwh / daily_total * 100 if daily_total > 0 else 0

                # Shift savings
                savings = analyzer.shift_savings_estimate(profile, rate_plan, 30)

                tou = {
                    "peak_kwh_daily": round(peak_kwh, 2),
                    "offpeak_kwh_daily": round(offpeak_kwh, 2),
                    "part_peak_kwh_daily": round(pp_kwh, 2),
                    "peak_pct": round(peak_pct, 1),
                    "peak_rate": rate_plan.winter_peak,
                    "offpeak_rate": rate_plan.winter_off_peak,
                    "est_monthly_savings_low": savings.est_monthly_savings_low,
                    "est_monthly_savings_high": savings.est_monthly_savings_high,
                }

            # Cost projection
            projection = analyzer.cost_projection(ds)

            # Forecasts
            forecasts = db.get_latest_forecasts()
            acct_fc = [f for f in forecasts if f.account_id == account_id]
            fc_data = None
            if acct_fc:
                fc = acct_fc[0]
                fc_data = {
                    "start_date": fc.start_date.isoformat(),
                    "end_date": fc.end_date.isoformat(),
                    "usage_to_date": fc.usage_to_date,
                    "forecasted_usage": fc.forecasted_usage,
                    "typical_usage": fc.typical_usage,
                    "cost_to_date": fc.cost_to_date,
                    "forecasted_cost": fc.forecasted_cost,
                    "typical_cost": fc.typical_cost,
                }

            return jsonify({
                "days_analyzed": len(ds),
                "total_usage_kwh": round(total_usage, 2),
                "total_cost": round(total_cost, 2),
                "avg_daily_usage": round(avg_daily, 2),
                "avg_daily_cost": round(avg_daily_cost, 2),
                "tou": tou,
                "projection": projection,
                "forecast": fc_data,
                "rate_plan": config.rate_plan,
            })
        finally:
            db.close()

    @app.route("/api/monthly")
    def api_monthly():
        """Return monthly usage and cost summaries.

        Query params:
            account_id: str (required)
            months: int (default 12)
        """
        from . import analyzer

        account_id = request.args.get("account_id")
        months = int(request.args.get("months", 12))

        if not account_id:
            return jsonify({"error": "account_id required"}), 400

        db = _get_db()
        try:
            tz = _tz()
            now = datetime.now(tz)
            start = now - timedelta(days=months * 31)

            hourly = db.get_usage_reads(account_id, Resolution.HOUR, start, now)
            cost_day = db.get_cost_reads(account_id, Resolution.DAY, start, now)
            cost_hour = db.get_cost_reads(account_id, Resolution.HOUR, start, now)
            all_cost = cost_day + cost_hour

            ds = analyzer.daily_stats(hourly if hourly else [], all_cost)
            summaries = analyzer.period_summaries(ds, "monthly")

            return jsonify([
                {
                    "label": s.label,
                    "total_usage": s.total_usage,
                    "total_cost": s.total_cost,
                    "avg_daily_usage": s.avg_daily_usage,
                    "peak_day": s.peak_day.isoformat() if s.peak_day else None,
                    "peak_day_usage": s.peak_day_usage,
                }
                for s in summaries
            ])
        finally:
            db.close()

    return app


def run_dashboard(config: Config, host: str = "0.0.0.0", port: int = 8080) -> None:
    """Start the Flask development server."""
    app = create_app(config)
    app.run(host=host, port=port, debug=False)
