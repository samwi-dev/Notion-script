#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ai_price.py - samwi-dev/Notion-script / crawlers/trip_price

使用 Amadeus Flight Offers Search API 查詢真實票價
https://developers.amadeus.com

Required secrets:
  NOTION_TOKEN
  TRIP_DATABASE_ID
  AMADEUS_API_KEY      <- 在 Amadeus 免費開發者帳號取得
  AMADEUS_API_SECRET   <- 在 Amadeus 免費開發者帳號取得

Amadeus 免費帳號申請：https://developers.amadeus.com/register
每月 2000 次 API 呼叫免費
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

NOTION_VERSION = "2022-06-28"
WRITE_DELAY_SEC = 0.4
AMADEUS_TOKEN_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
AMADEUS_SEARCH_URL = "https://test.api.amadeus.com/v2/shopping/flight-offers"

# IATA airport mapping for common Taiwan/Thailand airports
AIRPORT_MAP = {
    "台北": "TPE", "臺北": "TPE", "taipei": "TPE",
    "清邁": "CNX", "chiang mai": "CNX",
    "曼谷": "BKK", "bangkok": "BKK",
    "東京": "TYO", "osaka": "KIX",
}

# Site name → airline IATA code (for filtering results)
SITE_AIRLINE_MAP = {
    "China Airlines": "CI",
    "中華航空": "CI",
    "EVA Air": "BR",
    "長榮航空": "BR",
    "STARLUX Airlines": "JX",
    "星宇航空": "JX",
    "Thai AirAsia": "FD",
    "泰國亞洲航空": "FD",
}


# ---------------------------------------------------------------------------
# Notion helpers
# ---------------------------------------------------------------------------

def env(name: str) -> Optional[str]:
    v = os.environ.get(name)
    return v.strip() if v else None


def notion_headers() -> Dict[str, str]:
    token = env("NOTION_TOKEN")
    if not token:
        raise RuntimeError("Missing NOTION_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def http_request(
    method: str,
    url: str,
    headers: Dict[str, str],
    payload: Optional[dict | str] = None,
    form_data: Optional[str] = None,
) -> dict:
    if form_data:
        data = form_data.encode("utf-8")
    elif payload is not None:
        data = json.dumps(payload).encode("utf-8")
    else:
        data = None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=45) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:600]
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {body}") from e


def notion_get(path: str) -> dict:
    return http_request("GET", f"https://api.notion.com/v1{path}", notion_headers())


def notion_post(path: str, payload: dict) -> dict:
    return http_request("POST", f"https://api.notion.com/v1{path}", notion_headers(), payload)


def notion_patch(path: str, payload: dict) -> dict:
    return http_request("PATCH", f"https://api.notion.com/v1{path}", notion_headers(), payload)


def text_prop(page: dict, name: str) -> str:
    prop = page.get("properties", {}).get(name, {})
    typ = prop.get("type")
    if typ == "title":
        return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    if typ == "rich_text":
        return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
    return ""


def query_all_pages(database_id: str) -> List[dict]:
    pages: List[dict] = []
    cursor = None
    while True:
        payload: Dict[str, Any] = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        data = notion_post(f"/databases/{database_id}/query", payload)
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return pages


def list_block_children(block_id: str) -> List[dict]:
    blocks: List[dict] = []
    cursor = None
    while True:
        path = f"/blocks/{block_id}/children?page_size=100"
        if cursor:
            path += "&start_cursor=" + urllib.parse.quote(cursor)
        data = notion_get(path)
        blocks.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return blocks


def find_child_databases(page_id: str) -> Dict[str, str]:
    found: Dict[str, str] = {}
    for block in list_block_children(page_id):
        if block.get("type") != "child_database":
            continue
        title = block.get("child_database", {}).get("title", "")
        if title.startswith("View of "):
            continue
        found[title.strip()] = block["id"]
    return found


def update_page_props(page_id: str, props: dict) -> None:
    notion_patch(f"/pages/{page_id}", {"properties": props})
    time.sleep(WRITE_DELAY_SEC)


# ---------------------------------------------------------------------------
# Amadeus helpers
# ---------------------------------------------------------------------------

_amadeus_token: Optional[str] = None
_amadeus_token_expiry: float = 0.0


def get_amadeus_token() -> str:
    global _amadeus_token, _amadeus_token_expiry
    if _amadeus_token and time.time() < _amadeus_token_expiry:
        return _amadeus_token

    api_key = env("AMADEUS_API_KEY")
    api_secret = env("AMADEUS_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError(
            "Missing AMADEUS_API_KEY or AMADEUS_API_SECRET.\n"
            "Register free at: https://developers.amadeus.com/register"
        )

    form = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": api_key,
        "client_secret": api_secret,
    })
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    result = http_request("POST", AMADEUS_TOKEN_URL, headers, form_data=form)
    _amadeus_token = result["access_token"]
    _amadeus_token_expiry = time.time() + result.get("expires_in", 1799) - 60
    return _amadeus_token


def search_flights(
    origin: str,
    destination: str,
    depart_date: str,
    return_date: Optional[str] = None,
    adults: int = 1,
    airline_code: Optional[str] = None,
    non_stop: bool = True,
    currency: str = "TWD",
) -> List[dict]:
    """Search flight offers via Amadeus API."""
    token = get_amadeus_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    params: Dict[str, Any] = {
        "originLocationCode": origin,
        "destinationLocationCode": destination,
        "departureDate": depart_date,
        "adults": adults,
        "nonStop": str(non_stop).lower(),
        "currencyCode": currency,
        "max": 10,
    }
    if return_date:
        params["returnDate"] = return_date
    if airline_code:
        params["includedAirlineCodes"] = airline_code

    qs = urllib.parse.urlencode(params)
    url = f"{AMADEUS_SEARCH_URL}?{qs}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=45) as res:
            data = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:600]
        raise RuntimeError(f"Amadeus search -> HTTP {e.code}: {body}") from e

    return data.get("data", [])


def get_cheapest_price(
    origin: str,
    destination: str,
    depart_date: str,
    return_date: Optional[str],
    airline_code: Optional[str] = None,
    non_stop: bool = True,
) -> Tuple[Optional[int], str]:
    """Return (price_twd, note) for the cheapest matching offer."""
    try:
        offers = search_flights(
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            return_date=return_date,
            airline_code=airline_code,
            non_stop=non_stop,
        )
    except Exception as e:
        # If airline-specific filter found nothing, try without filter
        if airline_code:
            print(f"      airline filter failed ({e}), retrying without filter")
            try:
                offers = search_flights(
                    origin=origin,
                    destination=destination,
                    depart_date=depart_date,
                    return_date=return_date,
                    non_stop=non_stop,
                )
            except Exception as e2:
                return None, f"Amadeus error: {e2}"
        else:
            return None, f"Amadeus error: {e}"

    if not offers:
        # Retry with non_stop=False if nothing found
        if non_stop:
            try:
                offers = search_flights(
                    origin=origin,
                    destination=destination,
                    depart_date=depart_date,
                    return_date=return_date,
                    airline_code=airline_code,
                    non_stop=False,
                )
            except Exception as e:
                return None, f"Amadeus error (no non-stop fallback): {e}"

    if not offers:
        return None, "Amadeus: 查無航班，請確認日期或航線是否有直飛班次"

    # Find cheapest
    best_offer = None
    best_price = float("inf")
    for offer in offers:
        try:
            price_str = offer["price"]["grandTotal"]
            price = float(price_str)
            if price < best_price:
                best_price = price
                best_offer = offer
        except (KeyError, ValueError):
            continue

    if best_offer is None:
        return None, "Amadeus: 無法解析票價"

    price_twd = int(best_price)
    currency = best_offer["price"].get("currency", "TWD")

    # Extract airline info from first itinerary
    airlines_str = ""
    try:
        segments = best_offer["itineraries"][0]["segments"]
        codes = list(dict.fromkeys(s["carrierCode"] for s in segments))
        airlines_str = "+".join(codes)
    except (KeyError, IndexError):
        pass

    trip_type = "來回" if return_date else "單程"
    note = (
        f"Amadeus API | {trip_type} | 幣別: {currency} | "
        f"最低票價: {currency} {best_price:,.0f} | 航空: {airlines_str}"
    )
    return price_twd, note


# ---------------------------------------------------------------------------
# Journey parsing helpers
# ---------------------------------------------------------------------------

def parse_iata(city_name: str) -> Optional[str]:
    """Try to resolve a city name to IATA code."""
    lower = city_name.lower().strip()
    for key, code in AIRPORT_MAP.items():
        if key in lower or lower in key:
            return code
    # If already looks like IATA (3 uppercase letters)
    if city_name.strip().isupper() and len(city_name.strip()) == 3:
        return city_name.strip()
    return None


def parse_airline_from_site(site_name: str) -> Optional[str]:
    """Extract airline IATA code from site title (for airline official sites)."""
    for keyword, code in SITE_AIRLINE_MAP.items():
        if keyword.lower() in site_name.lower():
            return code
    return None


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def process_price_db(
    price_db_id: str,
    journey_info: Dict[str, str],
) -> int:
    rows = query_all_pages(price_db_id)
    updated = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    origin_iata = parse_iata(journey_info.get("origin_city", "")) or "TPE"
    dest_iata = parse_iata(journey_info.get("dest_city", "")) or "CNX"
    depart_date = journey_info.get("start", "")[:10]  # YYYY-MM-DD
    return_date = journey_info.get("end", "")[:10] or None

    if not depart_date:
        print("  Skip: no departure date found")
        return 0

    print(f"  Route: {origin_iata} → {dest_iata} | {depart_date} – {return_date}")

    for row in rows:
        # Skip rows that already have a valid price
        price_val = row.get("properties", {}).get("票價", {}).get("number")
        if price_val is not None:
            site_title = text_prop(row, "網站名稱")
            print(f"    [{site_title[:30]}] skip (already has price {price_val})")
            continue

        site_title = text_prop(row, "網站名稱")
        # Extract just the site/airline name (before ｜)
        site_name = site_title.split("｜")[0].strip() if "｜" in site_title else site_title

        # For airline official sites, filter by that airline
        airline_code = parse_airline_from_site(site_name)
        is_direct = True  # 行程要求直飛

        print(f"    [{site_name}] querying Amadeus (airline={airline_code or 'any'})...")
        price_twd, note = get_cheapest_price(
            origin=origin_iata,
            destination=dest_iata,
            depart_date=depart_date,
            return_date=return_date,
            airline_code=airline_code,
            non_stop=is_direct,
        )
        time.sleep(0.5)  # rate limit

        props: Dict[str, Any] = {
            "查詢時間": {"date": {"start": now_iso}},
            "備註": {"rich_text": [{"text": {"content": note[:2000]}}]},
        }
        if price_twd is not None:
            props["票價"] = {"number": price_twd}
            props["幣別"] = {"select": {"name": "TWD"}}
            print(f"      -> TWD {price_twd:,}")
        else:
            print(f"      -> no price ({note})")

        page_id = row["id"]
        try:
            update_page_props(page_id, props)
            updated += 1
        except Exception as e:
            print(f"      WARNING: Notion update failed: {e}")

    return updated


def main() -> int:
    database_id = env("TRIP_DATABASE_ID") or env("NOTION_DATABASE_ID")
    if not database_id:
        print("Missing TRIP_DATABASE_ID", file=sys.stderr)
        return 2

    # Quick Amadeus token test
    print("Testing Amadeus credentials...")
    try:
        token = get_amadeus_token()
        print(f"Amadeus token OK (length={len(token)})")
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    journeys = query_all_pages(database_id)
    print(f"Found {len(journeys)} journey task(s).")
    total_updated = 0

    for page in journeys:
        props = page.get("properties", {})
        title = "".join(
            t.get("plain_text", "")
            for t in (props.get("Deal Title") or {}).get("title", [])
        )
        from_to = "".join(
            t.get("plain_text", "")
            for t in (props.get("From-To") or {}).get("rich_text", [])
        )
        trip_date = (props.get("Trip Date") or {}).get("date") or {}
        start = trip_date.get("start") or ""
        end = trip_date.get("end") or ""

        origin_city = "台北"
        dest_city = "清邁"
        if "→" in from_to:
            left, right = from_to.split("→", 1)
            origin_city = left.strip()
            dest_city = right.strip()

        info: Dict[str, str] = {
            "title": title,
            "origin_city": origin_city,
            "dest_city": dest_city,
            "start": start,
            "end": end,
        }

        print(f"\nJourney: {title}")
        dbs = find_child_databases(page["id"])
        price_db = dbs.get("票價網站資料")
        if not price_db:
            print("  Skip: no child database named 票價網站資料")
            continue

        count = process_price_db(price_db, info)
        print(f"  Updated {count} row(s).")
        total_updated += count

    print(f"\nDone. Total updated: {total_updated}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
