# USMD Analytics Dashboard

A live business analytics dashboard integrating GoHighLevel and Monday.com data, tracking 6 key performance indicators.

## KPIs Tracked

| KPI | Source | Description |
|-----|--------|-------------|
| **Leads** | GHL Contacts | New contacts added in selected period |
| **Sales** | GHL Opportunities (Won) | Closed/won deals |
| **Churn** | GHL Opportunities (Lost) | Lost opportunities |
| **Calls Made** | GHL Conversations (Phone) | Phone conversations initiated |
| **Revenue** | Monday.com Payments | Subscription fee payments received |
| **Avg $/Sale** | Derived | Total revenue divided by sales count |

## Features

- Date-range filtering (default: current year)
- 4 interactive charts (Revenue, Leads by Month, Pipeline, Sales vs Churn)
- Tabbed data tables with search and pagination
- 5-minute data cache for performance
- Auto-refresh capability
- Responsive dark-theme UI
- GitHub Actions auto-versioning on every push

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/kpis?start=&end=` | All 6 KPIs aggregated |
| `GET /api/leads?start=&end=&page=&limit=` | Leads table data |
| `GET /api/sales?start=&end=` | Won opportunities |
| `GET /api/churn?start=&end=` | Lost opportunities |
| `GET /api/calls?start=&end=&page=&limit=` | Phone conversations |
| `GET /api/revenue?start=&end=` | Monthly revenue from Monday.com |
| `GET /api/pipeline` | Pipeline stage breakdown |
| `GET /api/leads-monthly?start=&end=` | Leads aggregated by month |
| `POST /api/refresh` | Clear server cache |

## Deploy to Heroku

1. Connect this repository to Heroku
2. Set environment variables in Settings > Config Vars:
   - `GHL_API_TOKEN` — GoHighLevel Private Integration Token
   - `GHL_LOCATION_ID` — GHL Location ID
   - `MONDAY_API_TOKEN` — Monday.com API Token
3. Enable automatic deploys from `main` branch

[![Deploy](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy)

## Local Setup

```bash
git clone https://github.com/Bisma-Organization/pokee-simple-dashboard.git
cd pokee-simple-dashboard
pip install -r requirements.txt
export GHL_API_TOKEN="your-token"
export GHL_LOCATION_ID="your-location-id"
export MONDAY_API_TOKEN="your-token"
python server.py
```

Open http://localhost:5000

## Environment Variables

| Variable | Description |
|----------|-------------|
| `GHL_API_TOKEN` | GoHighLevel Private Integration Token |
| `GHL_LOCATION_ID` | GoHighLevel Location ID |
| `MONDAY_API_TOKEN` | Monday.com API Token |

## Auto-Versioning

Every push to `main` triggers a GitHub Actions workflow that:
1. Increments the patch version (e.g., v0.0.1 → v0.0.2)
2. Creates a new GitHub Release with changelog
3. Includes a placeholder for dashboard screenshot
