"""Flask web dashboard for pge-tracker.

Serves a single-page dashboard with Chart.js visualizations and
JSON API endpoints backed by the existing analyzer module.

All endpoints accept `meter_type=electric` or `meter_type=gas` to
automatically combine data from all accounts of that type.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request

from .config import Config
from .database import Database
from .models import MeterType, RATE_PLANS, Resolution


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

    app.config["PGE_CONFIG"] = config

    def _get_db() -> Database:
        db = Database(config.db_path)
        db.initialize()
        return db

    def _tz() -> ZoneInfo:
        return ZoneInfo(config.timezone)

    def _resolve_meter_type(db: Database) -> tuple[MeterType, list[str]] | tuple[None, None]:
        """Parse meter_type param and return (MeterType, account_ids) or 400."""
        raw = request.args.get("meter_type", "electric").lower()
        if raw.startswith("e"):
            mt = MeterType.ELECTRIC
        elif raw.startswith("g"):
            mt = MeterType.GAS
        else:
            return None, None
        account_ids = db.get_account_ids_by_meter_type(mt)
        return mt, account_ids

    def _get_combined_reads(db, account_ids, start, end):
        """Fetch and combine hourly usage + all cost reads for multiple accounts."""
        hourly = db.get_usage_reads_multi(account_ids, Resolution.HOUR, start, end)
        cost_day = db.get_cost_reads_multi(account_ids, Resolution.DAY, start, end)
        cost_hour = db.get_cost_reads_multi(account_ids, Resolution.HOUR, start, end)
        return hourly, cost_day + cost_hour

    # --- Dashboard page ---

    @app.route("/")
    def dashboard():
        """Serve the main dashboard page."""
        db = _get_db()
        try:
            accounts = db.get_accounts()
            has_electric = any(a.meter_type == MeterType.ELECTRIC for a in accounts)
            has_gas = any(a.meter_type == MeterType.GAS for a in accounts)

            return render_template(
                "dashboard.html",
                has_electric=has_electric,
                has_gas=has_gas,
                rate_plan=config.rate_plan,
                timezone=config.timezone,
            )
        finally:
            db.close()

    # --- JSON API endpoints ---

    @app.route("/api/accounts")
    def api_accounts():
        """Return meter types available (grouped)."""
        db = _get_db()
        try:
            accounts = db.get_accounts()

            electric = [a for a in accounts if a.meter_type == MeterType.ELECTRIC]
            gas = [a for a in accounts if a.meter_type == MeterType.GAS]

            result = []
            if electric:
                acct_nums = sorted(set(a.account_number for a in electric if a.account_number))
                addresses = sorted(set(a.service_address for a in electric if a.service_address))
                result.append({
                    "meter_type": "electric",
                    "label": "All Electric",
                    "account_count": len(electric),
                    "account_numbers": acct_nums,
                    "service_address": addresses[0] if addresses else None,
                })
            if gas:
                acct_nums = sorted(set(a.account_number for a in gas if a.account_number))
                addresses = sorted(set(a.service_address for a in gas if a.service_address))
                result.append({
                    "meter_type": "gas",
                    "label": "All Gas",
                    "account_count": len(gas),
                    "account_numbers": acct_nums,
                    "service_address": addresses[0] if addresses else None,
                })

            return jsonify(result)
        finally:
            db.close()

    @app.route("/api/daily")
    def api_daily():
        """Return daily usage and cost data.

        Query params:
            meter_type: str (default "electric")
            days: int (default 30)
        """
        from . import analyzer

        days = int(request.args.get("days", 30))

        db = _get_db()
        try:
            mt, account_ids = _resolve_meter_type(db)
            if mt is None:
                return jsonify({"error": "Invalid meter_type"}), 400
            if not account_ids:
                return jsonify([])

            tz = _tz()
            now = datetime.now(tz)
            start = now - timedelta(days=days)

            hourly, all_cost = _get_combined_reads(db, account_ids, start, now)
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
            meter_type: str (default "electric")
            days: int (default 30)
            weekdays_only: bool (default false)
        """
        from . import analyzer

        days = int(request.args.get("days", 30))
        weekdays_only = request.args.get("weekdays_only", "false").lower() == "true"

        rate_plan = RATE_PLANS.get(config.rate_plan)
        if not rate_plan:
            return jsonify({"error": f"Unknown rate plan: {config.rate_plan}"}), 500

        db = _get_db()
        try:
            mt, account_ids = _resolve_meter_type(db)
            if mt is None:
                return jsonify({"error": "Invalid meter_type"}), 400
            if not account_ids:
                return jsonify([])

            tz = _tz()
            now = datetime.now(tz)
            start = now - timedelta(days=days)

            hourly = db.get_usage_reads_multi(account_ids, Resolution.HOUR, start, now)
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
            meter_type: str (default "electric")
            days: int (default 30)
        """
        from . import analyzer

        days = int(request.args.get("days", 30))

        db = _get_db()
        try:
            mt, account_ids = _resolve_meter_type(db)
            if mt is None:
                return jsonify({"error": "Invalid meter_type"}), 400
            if not account_ids:
                return jsonify({"matrix": [[0]*24]*7, "days": [], "peak_hours": [], "part_peak_hours": []})

            tz = _tz()
            now = datetime.now(tz)
            start = now - timedelta(days=days)

            hourly = db.get_usage_reads_multi(account_ids, Resolution.HOUR, start, now)
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
            meter_type: str (default "electric")
            days: int (default 30)
            top_n: int (default 5)
        """
        from . import analyzer

        days = int(request.args.get("days", 30))
        top_n = int(request.args.get("top_n", 5))

        rate_plan = RATE_PLANS.get(config.rate_plan)
        if not rate_plan:
            return jsonify({"error": f"Unknown rate plan: {config.rate_plan}"}), 500

        db = _get_db()
        try:
            mt, account_ids = _resolve_meter_type(db)
            if mt is None:
                return jsonify({"error": "Invalid meter_type"}), 400
            if not account_ids:
                return jsonify([])

            tz = _tz()
            now = datetime.now(tz)
            start = now - timedelta(days=days)

            hourly = db.get_usage_reads_multi(account_ids, Resolution.HOUR, start, now)
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
            meter_type: str (default "electric")
            days: int (default 30)
        """
        from . import analyzer

        days = int(request.args.get("days", 30))

        rate_plan = RATE_PLANS.get(config.rate_plan)

        db = _get_db()
        try:
            mt, account_ids = _resolve_meter_type(db)
            if mt is None:
                return jsonify({"error": "Invalid meter_type"}), 400
            if not account_ids:
                return jsonify({"days_analyzed": 0, "total_usage_kwh": 0, "total_cost": 0})

            tz = _tz()
            now = datetime.now(tz)
            start = now - timedelta(days=days)

            hourly, all_cost = _get_combined_reads(db, account_ids, start, now)
            ds = analyzer.daily_stats(hourly if hourly else [], all_cost)

            total_usage = sum(d.usage for d in ds)
            total_cost = sum(d.cost for d in ds if d.cost is not None)
            avg_daily = total_usage / len(ds) if ds else 0
            cost_days = [d for d in ds if d.cost is not None]
            avg_daily_cost = total_cost / len(cost_days) if cost_days else 0

            # TOU breakdown (electric only)
            tou = None
            if hourly and rate_plan and mt == MeterType.ELECTRIC:
                profile = analyzer.hourly_profile(hourly, rate_plan, tz)
                peak_kwh = sum(h.avg_usage for h in profile if h.period.value == "peak")
                offpeak_kwh = sum(h.avg_usage for h in profile if h.period.value == "off_peak")
                pp_kwh = sum(h.avg_usage for h in profile if h.period.value == "part_peak")
                daily_total = peak_kwh + offpeak_kwh + pp_kwh
                peak_pct = peak_kwh / daily_total * 100 if daily_total > 0 else 0

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

            # Forecasts (combine from all matching accounts)
            forecasts = db.get_latest_forecasts()
            acct_fc = [f for f in forecasts if f.account_id in account_ids]
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
            meter_type: str (default "electric")
            months: int (default 12)
        """
        from . import analyzer

        months = int(request.args.get("months", 12))

        db = _get_db()
        try:
            mt, account_ids = _resolve_meter_type(db)
            if mt is None:
                return jsonify({"error": "Invalid meter_type"}), 400
            if not account_ids:
                return jsonify([])

            tz = _tz()
            now = datetime.now(tz)
            start = now - timedelta(days=months * 31)

            hourly, all_cost = _get_combined_reads(db, account_ids, start, now)
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
