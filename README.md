# pge-tracker

A personal PG&E energy usage tracker with CLI tools, peak-hour analysis, and a local web dashboard for your household.

Pulls data from the PG&E/Opower API (with MFA support), imports historical CSV exports, stores everything in SQLite, and provides time-of-use analysis tuned to PG&E rate plans like **EV2-A** (Home Charging) and **E-TOU-C**.

## Features

- **API sync** &mdash; Fetches daily cost, hourly usage, and billing forecasts from PG&E via the Opower API. Handles MFA and token refresh automatically.
- **CSV import** &mdash; Imports PG&E Green Button / interval CSV exports (electric and gas, usage and cost).
- **Peak analysis** &mdash; Hour-by-hour usage profiles, weekly heatmaps, worst peak-day drilldowns, and estimated savings from shifting load to off-peak.
- **Web dashboard** &mdash; A dark-themed, responsive single-page dashboard with interactive charts, accessible to anyone on your local WiFi.
- **Automated sync** &mdash; macOS launchd plists for daily data sync and always-on dashboard hosting.
- **Full analysis suite** &mdash; Monthly trends, year-over-year comparison, anomaly detection (z-score), seasonal patterns, cost projections, and actionable recommendations.

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/ilasno/pge-tracker.git
cd pge-tracker
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. Configure

```bash
cp config.example.toml config.toml
# Edit config.toml with your PG&E login credentials
```

See [`config.example.toml`](config.example.toml) for all options including rate plan, timezone, and peak hour settings.

### 3. Sync your data

```bash
pge-tracker sync
```

You'll be prompted for an MFA verification code on first run. Subsequent syncs reuse the saved session.

### 4. View the dashboard

```bash
pge-tracker web
```

Open **http://localhost:8080** (or the network IP shown in the terminal) on any device on your WiFi.

## Commands

| Command | Description |
|---------|-------------|
| `pge-tracker sync` | Fetch latest usage data from PG&E |
| `pge-tracker import <file>` | Import a PG&E CSV file |
| `pge-tracker show` | Display usage and cost data in the terminal |
| `pge-tracker analyze` | Run full analysis with insights report |
| `pge-tracker peak` | Analyze peak-hour usage and savings opportunities |
| `pge-tracker web` | Launch the web dashboard |
| `pge-tracker status` | Show database status and sync history |
| `pge-tracker config --show` | Display current configuration |
| `pge-tracker db --stats` | Show database table row counts |
| `pge-tracker version` | Show version |

### Common Options

```bash
# Most commands accept these flags:
--config, -c PATH     # Use a specific config file
--days, -d N          # Number of days to analyze (default: 30)
--meter, -m TYPE      # Filter by meter: e, g, or both

# sync
--full                # Re-fetch all history (not just incremental)
--no-hourly           # Skip hourly data
--no-forecast         # Skip forecast data

# show
--resolution, -r RES  # h=hourly, d=daily, m=monthly

# peak
--weekdays-only, -w   # Only analyze weekday usage

# web
--host, -H ADDR       # Bind address (default: 0.0.0.0)
--port, -p PORT       # Port number (default: 8080)

# analyze
--section, -s NAME    # Run specific sections (trends, monthly, tou, yoy, seasonal, anomalies, forecast, top, recommendations)
--report TYPE         # full or quick
```

## Web Dashboard

The dashboard provides six views of your energy data:

- **KPI cards** &mdash; Total usage, total cost, daily average, peak percentage, 30-day projection, and estimated savings
- **Daily chart** &mdash; Bar chart of daily kWh with a cost overlay line
- **Hourly profile** &mdash; 24-hour average usage colored by TOU period (peak / part-peak / off-peak)
- **Weekly heatmap** &mdash; 7&times;24 intensity grid showing usage patterns across the week
- **Monthly trend** &mdash; Long-term usage and cost trends
- **Worst peak days** &mdash; Ranked list of highest peak-hour usage days with cost estimates
- **Savings banner** &mdash; Estimated monthly/annual savings from shifting peak usage, with actionable tips

Use the account selector to switch between electric and gas meters, and the period picker to adjust the time window (7 days to 1 year).

## Automated Background Services (macOS)

Two launchd plists are included in `scripts/`:

### Daily data sync (6 AM)

```bash
cp scripts/com.pge-tracker.daily-sync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.pge-tracker.daily-sync.plist
```

### Always-on web dashboard

```bash
cp scripts/com.pge-tracker.web-dashboard.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.pge-tracker.web-dashboard.plist
```

The dashboard will start at login and auto-restart if it crashes.

### Managing services

```bash
# Stop a service
launchctl unload ~/Library/LaunchAgents/com.pge-tracker.web-dashboard.plist

# Restart a service
launchctl unload ~/Library/LaunchAgents/com.pge-tracker.web-dashboard.plist
launchctl load ~/Library/LaunchAgents/com.pge-tracker.web-dashboard.plist

# Check running services
launchctl list | grep pge-tracker

# View logs
cat data/logs/web-stderr.log
cat data/logs/sync-$(date +%Y-%m-%d).log
```

## Rate Plans

Two PG&E rate plans are built in:

### EV2-A (Home Charging)

| Period | Hours | Summer (Jun-Sep) | Winter (Oct-May) |
|--------|-------|-------------------|-------------------|
| Peak | 4&ndash;9 PM | $0.60/kWh | $0.47/kWh |
| Part-Peak | 3&ndash;4 PM, 9 PM&ndash;12 AM | $0.49/kWh | $0.45/kWh |
| Off-Peak | 12&ndash;3 PM | $0.28/kWh | $0.28/kWh |

### E-TOU-C (Standard TOU)

| Period | Hours | Summer | Winter |
|--------|-------|--------|--------|
| Peak | 4&ndash;9 PM | $0.55/kWh | $0.45/kWh |
| Off-Peak | All other hours | $0.30/kWh | $0.30/kWh |

Set your rate plan in `config.toml`:

```toml
[preferences]
rate_plan = "EV2-A"
```

## Project Structure

```
pge-tracker/
  src/pge_tracker/
    cli.py          # Typer CLI commands
    config.py       # TOML config loader
    database.py     # SQLite storage layer (WAL mode)
    fetcher.py      # Opower API client with MFA handling
    importer.py     # PG&E CSV parser
    models.py       # Dataclass models and rate plan definitions
    analyzer.py     # Pure analysis functions (no side effects)
    presenter.py    # Rich terminal output
    web.py          # Flask web dashboard and JSON API
    templates/
      dashboard.html  # Single-page dashboard (Chart.js)
  scripts/
    daily-sync.sh                       # Bash script for cron/launchd sync
    com.pge-tracker.daily-sync.plist    # macOS launchd: daily 6 AM sync
    com.pge-tracker.web-dashboard.plist # macOS launchd: always-on dashboard
  tests/
    test_analyzer.py  # 37 analysis tests
    test_database.py  # 12 database tests
    test_importer.py  # 6 CSV import tests
    test_web.py       # 14 web API tests
  config.example.toml
  pyproject.toml
```

## Development

```bash
# Run tests
pytest

# Run tests with coverage
pytest --cov=pge_tracker

# Verbose output
pge-tracker -v sync
```

## Requirements

- Python 3.10+
- macOS (for launchd automation; CLI and web work on any platform)
- A PG&E account with online access

## Dependencies

- [opower](https://github.com/tronikos/opower) &mdash; PG&E/Opower API client
- [typer](https://typer.tiangolo.com/) + [rich](https://rich.readthedocs.io/) &mdash; CLI framework
- [Flask](https://flask.palletsprojects.com/) &mdash; Web dashboard server
- [Chart.js](https://www.chartjs.org/) &mdash; Browser charting (loaded from CDN)
- SQLite (stdlib) &mdash; Local data storage
