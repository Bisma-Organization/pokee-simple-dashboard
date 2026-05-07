# USMD Simple Dashboard

A lightweight business dashboard integrating GoHighLevel and Monday.com data.

## Features
- 50 most recent GHL contacts with pipeline stages
- 50 most recent Monday.com items with status and owner
- Email-based matching between platforms
- Bar chart showing contacts by pipeline stage
- Live data refresh button

## Deploy to Heroku

1. Click the button below or connect this repo to Heroku
2. Set environment variables in Heroku Settings > Config Vars:
   - `GHL_API_TOKEN` — your GoHighLevel Private Integration Token
   - `GHL_LOCATION_ID` — your GHL Location ID
   - `MONDAY_API_TOKEN` — your Monday.com API Token

[![Deploy](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy)

## Local Setup

```bash
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
