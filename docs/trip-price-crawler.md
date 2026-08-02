# Trip Price Crawler

This crawler lives under the main `samwi-dev/Notion-script` repo. Do not create a separate repo unless explicitly requested.

## First request

- Origin: Taiwan / TPE / TSA
- Destination: Chiang Mai, Thailand / CNX
- Dates: 2026-10-22 to 2026-10-26
- Flight rule: direct flight
- Baggage rule: return flight must include checked baggage
- Hotel rule: search Chiang Mai hotels for the same dates

## Secrets

Set these in `samwi-dev/Notion-script` → Settings → Secrets and variables → Actions:

| Secret | Required | Notes |
|---|---:|---|
| `NOTION_TOKEN` | Yes | Existing Notion integration token can be reused |
| `TRIP_DATABASE_ID` | Yes | Trip database ID, currently `3b0a0e5b9b3580bfbf58ea6504ff227a` |

## Workflow

GitHub Actions workflow:

```text
.github/workflows/trip-price-crawler.yml
```

Manual run:

1. Open `samwi-dev/Notion-script`
2. Go to Actions
3. Select `Trip Price Crawler`
4. Click `Run workflow`
5. Branch: `main`

## What it creates

For a matching `Trip` request row with `Status = New`, it creates platform-specific rows for:

### Flights

- Google Flights
- Skyscanner
- Trip.com
- Klook
- Expedia
- Traveloka
- Kayak
- Momondo
- EVA Air
- China Airlines
- STARLUX Airlines
- Tigerair Taiwan
- Thai Airways
- AirAsia

### Hotels

- Google Travel / Hotel Search
- Agoda
- Booking.com
- Trip.com
- Klook
- Expedia
- Hotels.com
- Trivago
- Kayak Hotels
- Momondo Hotels

Each row includes platform source, search URL, dates, duplicate key, and review notes.
