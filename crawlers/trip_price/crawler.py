#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trip price crawler — under samwi-dev/Notion-script

Purpose:
- Read Trip crawler request rows from the Notion Trip database.
- For the first request: Taiwan → Chiang Mai, 2026-10-22 to 2026-10-26,
  direct flight, return flight must include checked baggage.
- Create platform-specific search rows back into the same Trip database.

Required GitHub Actions secrets:
- NOTION_TOKEN
- TRIP_DATABASE_ID
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

NOTION_VERSION = "2022-06-28"

FLIGHT_PLATFORMS = [
    ("Google Flights", "https://www.google.com/travel/flights"),
    ("Skyscanner", "https://www.skyscanner.com.tw"),
    ("Trip.com", "https://www.trip.com/flights"),
    ("Klook", "https://www.klook.com"),
    ("Expedia", "https://www.expedia.com/Flights"),
    ("Traveloka", "https://www.traveloka.com/en-en/flight"),
    ("Kayak", "https://www.kayak.com/flights"),
    ("Momondo", "https://www.momondo.com/flight-search"),
    ("EVA Air", "https://www.evaair.com"),
    ("China Airlines", "https://www.china-airlines.com"),
    ("STARLUX Airlines", "https://www.starlux-airlines.com"),
    ("Tigerair Taiwan", "https://www.tigerairtw.com"),
    ("Thai Airways", "https://www.thaiairways.com"),
    ("AirAsia", "https://www.airasia.com"),
]

HOTEL_PLATFORMS = [
    ("Google Travel / Hotel Search", "https://www.google.com/travel/hotels"),
    ("Agoda", "https://www.agoda.com"),
    ("Booking.com", "https://www.booking.com"),
    ("Trip.com", "https://www.trip.com/hotels"),
    ("Klook", "https://www.klook.com"),
    ("Expedia", "https://www.expedia.com/Hotels"),
    ("Hotels.com", "https://www.hotels.com"),
    ("Trivago", "https://www.trivago.com"),
    ("Kayak Hotels", "https://www.kayak.com/hotels"),
    ("Momondo Hotels", "https://www.momondo.com/hotels"),
]

SOURCE_SELECT_OPTIONS = {"Google Flights", "Skyscanner", "Trip.com", "Agoda", "Booking.com", "Klook", "Other"}


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def notion_headers() -> Dict[str, str]:
    token = env("NOTION_TOKEN")
    if not token:
        raise RuntimeError("Missing NOTION_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def http_json(method: str, url: str, payload: Optional[dict] = None) -> dict:
    data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=notion_headers())
    with urllib.request.urlopen(req, timeout=45) as res:
        return json.loads(res.read().decode("utf-8"))


def text_prop(page: dict, name: str) -> str:
    prop = page.get("properties", {}).get(name, {})
    typ = prop.get("type")
    if typ == "title":
        return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    if typ == "rich_text":
        return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
    if typ == "select" and prop.get("select"):
        return prop["select"].get("name", "")
    if typ == "status" and prop.get("status"):
        return prop["status"].get("name", "")
    if typ == "url":
        return prop.get("url") or ""
    return ""


def number_prop(page: dict, name: str) -> Optional[float]:
    prop = page.get("properties", {}).get(name, {})
    return prop.get("number") if prop.get("type") == "number" else None


def date_start_prop(page: dict, name: str) -> str:
    prop = page.get("properties", {}).get(name, {})
    if prop.get("type") == "date" and prop.get("date"):
        return prop["date"].get("start") or ""
    return ""


def notion_title(value: str) -> dict:
    return {"title": [{"text": {"content": value[:2000]}}]}


def notion_text(value: str) -> dict:
    return {"rich_text": [{"text": {"content": value[:2000]}}]}


def notion_select(value: str) -> dict:
    return {"select": {"name": value if value in SOURCE_SELECT_OPTIONS else "Other"}}


def notion_deal_type(value: str) -> dict:
    return {"select": {"name": value}}


def notion_status(value: str) -> dict:
    return {"status": {"name": value}}


def notion_date(value: str) -> dict:
    return {"date": {"start": value} if value else None}


def notion_now() -> dict:
    return {"date": {"start": datetime.now(timezone.utc).isoformat()}}


def duplicate_key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:28]


def build_search_url(base: str, query: str) -> str:
    sep = "&" if "?" in base else "?"
    return base + sep + "q=" + urllib.parse.quote(query)


def query_request_pages(database_id: str) -> List[dict]:
    pages: List[dict] = []
    cursor = None
    while True:
        payload: Dict[str, Any] = {
            "page_size": 100,
            "filter": {"property": "Status", "status": {"equals": "New"}},
        }
        if cursor:
            payload["start_cursor"] = cursor
        data = http_json("POST", f"https://api.notion.com/v1/databases/{database_id}/query", payload)
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return pages


def existing_keys(database_id: str) -> set[str]:
    keys: set[str] = set()
    cursor = None
    while True:
        payload: Dict[str, Any] = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        data = http_json("POST", f"https://api.notion.com/v1/databases/{database_id}/query", payload)
        for page in data.get("results", []):
            key = text_prop(page, "Duplicate Key")
            if key:
                keys.add(key)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return keys


def create_page(database_id: str, props: dict, content: str) -> None:
    http_json(
        "POST",
        "https://api.notion.com/v1/pages",
        {
            "parent": {"database_id": database_id},
            "properties": props,
            "children": [{
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": content[:2000]}}]},
            }],
        },
    )


def update_page_status(page_id: str, status: str, notes: str) -> None:
    http_json(
        "PATCH",
        f"https://api.notion.com/v1/pages/{page_id}",
        {"properties": {"Status": notion_status(status), "Notes": notion_text(notes)}},
    )


def create_flight_platform_rows(database_id: str, request: dict, keys: set[str]) -> int:
    origin = text_prop(request, "Origin") or "Taiwan TPE TSA"
    destination = text_prop(request, "Destination") or "Chiang Mai CNX"
    depart = date_start_prop(request, "Departure Date")
    ret = date_start_prop(request, "Return Date")
    keyword = text_prop(request, "Keyword") or f"{origin} {destination} direct flight {depart} {ret} checked baggage"
    query = f"{origin} to {destination} direct flight {depart} {ret} return checked baggage"
    created = 0
    for rank, (source, base_url) in enumerate(FLIGHT_PLATFORMS, start=1):
        key = duplicate_key("flight-search", source, origin, destination, depart, ret, "return-baggage")
        if key in keys:
            continue
        url = build_search_url(base_url, query)
        title = f"[{rank}] {source}｜{origin} → {destination}｜{depart}–{ret}｜直飛＋回程託運"
        props = {
            "Deal Title": notion_title(title),
            "Deal Type": notion_deal_type("Flight"),
            "Status": notion_status("Reviewing"),
            "Origin": notion_text(origin),
            "Destination": notion_text(destination),
            "Country / Region": notion_text("Thailand"),
            "Departure Date": notion_date(depart),
            "Return Date": notion_date(ret),
            "Source": notion_select(source),
            "URL": {"url": url},
            "Keyword": notion_text(keyword),
            "Summary": notion_text("待確認：直飛航班、價格、班機時間、回程託運行李是否包含。"),
            "Fetched At": notion_now(),
            "Duplicate Key": notion_text(key),
            "Notified": {"checkbox": False},
            "Notes": notion_text(f"平台：{source}。條件：直飛；回程需包含託運行李。確認價格是否含稅、含行李與是否跳價。"),
        }
        content = f"Search URL: {url}\nRequired fields: price, airline, flight number, departure/arrival time, duration, direct, return checked baggage."
        create_page(database_id, props, content)
        keys.add(key)
        created += 1
    return created


def create_hotel_platform_rows(database_id: str, request: dict, keys: set[str]) -> int:
    destination = text_prop(request, "Destination") or "Chiang Mai"
    depart = date_start_prop(request, "Departure Date")
    ret = date_start_prop(request, "Return Date")
    nights = number_prop(request, "Nights") or 4
    query = f"{destination} hotel {depart} {ret} {int(nights)} nights"
    created = 0
    for rank, (source, base_url) in enumerate(HOTEL_PLATFORMS, start=1):
        key = duplicate_key("hotel-search", source, destination, depart, ret)
        if key in keys:
            continue
        url = build_search_url(base_url, query)
        title = f"[{rank}] {source}｜{destination} 飯店酒店｜{depart}–{ret}"
        props = {
            "Deal Title": notion_title(title),
            "Deal Type": notion_deal_type("Hotel"),
            "Status": notion_status("Reviewing"),
            "Destination": notion_text(destination),
            "Country / Region": notion_text("Thailand"),
            "Departure Date": notion_date(depart),
            "Return Date": notion_date(ret),
            "Nights": {"number": nights},
            "Source": notion_select(source),
            "URL": {"url": url},
            "Keyword": notion_text(query),
            "Summary": notion_text("待確認：住宿總價、每晚價格、區域、評分、早餐、取消政策與稅費。"),
            "Fetched At": notion_now(),
            "Duplicate Key": notion_text(key),
            "Notified": {"checkbox": False},
            "Notes": notion_text(f"平台：{source}。確認是否含稅費、含早餐、可免費取消、會員價與是否跳價。"),
        }
        content = f"Search URL: {url}\nRequired fields: hotel name, area, room type, total price, price/night, rating, breakfast, cancellation, taxes/fees."
        create_page(database_id, props, content)
        keys.add(key)
        created += 1
    return created


def main() -> int:
    database_id = env("TRIP_DATABASE_ID") or env("NOTION_DATABASE_ID")
    if not database_id:
        print("Missing TRIP_DATABASE_ID", file=sys.stderr)
        return 2

    requests = query_request_pages(database_id)
    keys = existing_keys(database_id)
    total_created = 0
    processed = 0

    for request in requests:
        title = text_prop(request, "Deal Title")
        key = text_prop(request, "Duplicate Key")
        if not key or not ("trip-tw-cnx" in key or "清邁" in title or "Chiang Mai" in title):
            continue
        created = 0
        created += create_flight_platform_rows(database_id, request, keys)
        created += create_hotel_platform_rows(database_id, request, keys)
        total_created += created
        update_page_status(
            request["id"],
            "Reviewing",
            f"已建立平台搜尋列，共新增 {created} 筆候選搜尋資料。下一步：逐平台確認價格、班機時間與住宿條件。",
        )
        processed += 1
        print(f"Processed request: {title}; created {created} row(s).")

    print(f"Processed {processed} request(s); created {total_created} platform row(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
