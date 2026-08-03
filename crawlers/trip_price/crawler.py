#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trip price crawler — samwi-dev/Notion-script · crawlers/trip_price

2026-08 重寫版，對齊目前 Notion 架構：
- TRIP_DATABASE_ID 指向 Trip database；每一列 = 一個 Journey Task（一個旅程只保留一個 Task）
- 每個 Journey Task 頁面內嵌兩個子 database：
  - 「 票價網站資料」：各比價網站報價（linked view「整月價格走勢」chart 的資料來源）
  - 「航班追蹤」：本旅程航班（page icon 使用航空公司官網 favicon）
- 爬蟲負責「重建結構」：補齊航班追蹤的直飛航空公司、票價網站資料的各網站報價列
- 去重：航班追蹤以「航空公司」title、票價網站資料以「網站名稱」前綴判斷，可每日重複執行
- 實際價格由比價流程查詢後回填（統一 TWD，備註保留原始幣別與換算依據）

Required GitHub Actions secrets:
- NOTION_TOKEN
- TRIP_DATABASE_ID
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
from typing import Any, Dict, List, Optional, Set

NOTION_VERSION = "2022-06-28"
WRITE_DELAY_SEC = 0.35

FLIGHT_PRICE_SITES = [
    ("Google Flights", "https://www.google.com/travel/flights", "多家航空公司比價"),
    ("Skyscanner", "https://www.skyscanner.com.tw", "多家航空公司比價"),
    ("Trip.com", "https://www.trip.com/flights", "多家航空公司比價"),
    ("KAYAK", "https://www.kayak.com/flights", "多家航空公司比價"),
    ("Expedia", "https://www.expedia.com/Flights", "多家航空公司比價"),
    ("momondo", "https://www.momondo.com/flight-search", "多家航空公司比價"),
    ("Booking.com", "https://www.booking.com/flights", "多家航空公司比價"),
    ("ezTravel 易遊網", "https://www.eztravel.com.tw", "多家航空公司比價"),
    ("EVA Air 長榮航空", "https://www.evaair.com", "EVA Air 長榮航空"),
    ("China Airlines 中華航空", "https://www.china-airlines.com", "China Airlines 中華航空"),
    ("STARLUX Airlines 星宇航空", "https://www.starlux-airlines.com", "STARLUX Airlines 星宇航空"),
    ("Thai AirAsia 泰國亞洲航空", "https://www.airasia.com", "Thai AirAsia 泰國亞洲航空"),
]

CNX_AIRLINES = [
    ("EVA Air 長榮航空", "www.evaair.com", "BR", "TPE", "CNX"),
    ("China Airlines 中華航空", "www.china-airlines.com", "CI", "TPE", "CNX"),
    ("STARLUX Airlines 星宇航空", "www.starlux-airlines.com", "JX", "TPE", "CNX"),
    ("Thai AirAsia 泰國亞洲航空", "www.airasia.com", "FD", "TPE", "CNX"),
]


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
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=notion_headers())
    try:
        with urllib.request.urlopen(req, timeout=45) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Notion API {method} {url} -> HTTP {e.code}: {body}") from e


def text_prop(page: dict, name: str) -> str:
    prop = page.get("properties", {}).get(name, {})
    typ = prop.get("type")
    if typ == "title":
        return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    if typ == "rich_text":
        return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
    if typ == "select" and prop.get("select"):
        return prop["select"].get("name", "")
    if typ == "url":
        return prop.get("url") or ""
    return ""


def notion_title(value: str) -> dict:
    return {"title": [{"text": {"content": value[:2000]}}]}


def notion_text(value: str) -> dict:
    return {"rich_text": [{"text": {"content": value[:2000]}}]}


def query_all_pages(database_id: str) -> List[dict]:
    pages: List[dict] = []
    cursor = None
    while True:
        payload: Dict[str, Any] = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        data = http_json("POST", f"https://api.notion.com/v1/databases/{database_id}/query", payload)
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return pages


def list_block_children(block_id: str) -> List[dict]:
    blocks: List[dict] = []
    cursor = None
    while True:
        url = f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100"
        if cursor:
            url += "&start_cursor=" + urllib.parse.quote(cursor)
        data = http_json("GET", url)
        blocks.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return blocks


def find_child_databases(page_id: str) -> Dict[str, str]:
    """回傳 {title: database_id}。只收頁面內嵌的子 database，排除 linked view（標題為 'View of ...'）。"""
    found: Dict[str, str] = {}
    for block in list_block_children(page_id):
        if block.get("type") != "child_database":
            continue
        title = block.get("child_database", {}).get("title", "")
        if title.startswith("View of "):
            continue
        found[title.strip()] = block["id"]
    return found


def parse_journey(page: dict) -> Dict[str, str]:
    props = page.get("properties", {})
    title = text_prop(page, "Deal Title")
    from_to = text_prop(page, "From-To")
    summary = text_prop(page, "Summary")
    trip_date = (props.get("Trip Date") or {}).get("date") or {}
    start = trip_date.get("start") or ""
    end = trip_date.get("end") or ""
    origin, destination = "", ""
    if "→" in from_to:
        left, right = from_to.split("→", 1)
        origin, destination = left.strip(), right.strip()
    if not destination:
        destination = title
    return {
        "title": title,
        "from_to": from_to,
        "summary": summary,
        "origin": origin,
        "destination": destination,
        "start": start,
        "end": end,
    }


def existing_titles(database_id: str, prop: str) -> Set[str]:
    titles: Set[str] = set()
    for page in query_all_pages(database_id):
        value = text_prop(page, prop)
        if value:
            titles.add(value)
    return titles


def create_db_row(database_id: str, props: dict, icon: Optional[dict] = None) -> None:
    payload: Dict[str, Any] = {"parent": {"database_id": database_id}, "properties": props}
    if icon:
        payload["icon"] = icon
    http_json("POST", "https://api.notion.com/v1/pages", payload)
    time.sleep(WRITE_DELAY_SEC)


def favicon_icon(domain: str) -> dict:
    return {"type": "external", "external": {"url": f"https://www.google.com/s2/favicons?domain={domain}&sz=128"}}


def build_search_url(base: str, query: str) -> str:
    sep = "&" if "?" in base else "?"
    return base + sep + "q=" + urllib.parse.quote(query)


def airlines_for_route(info: Dict[str, str]) -> List[tuple]:
    text = f"{info['destination']} {info['title']} {info['summary']}"
    if any(k in text for k in ("清邁", "Chiang Mai", "CNX")):
        return CNX_AIRLINES
    return []


def rebuild_flight_rows(flight_db: str, info: Dict[str, str]) -> int:
    airlines = airlines_for_route(info)
    if not airlines:
        print(f"  Skip flights: no airline mapping for route '{info['from_to'] or info['title']}'.")
        return 0
    existing = existing_titles(flight_db, "航空公司")
    created = 0
    for name, domain, code, dep_airport, arr_airport in airlines:
        if name in existing:
            continue
        props = {
            "航空公司": notion_title(name),
            "到達機場": notion_text(arr_airport),
            "航班時間": notion_text(f"{info['start']} → {info['end']}（實際班表待查）"),
            "直飛還是轉機": {"select": {"name": "直飛"}},
            "是否包含行李": {"checkbox": False},
        }
        create_db_row(flight_db, props, icon=favicon_icon(domain))
        existing.add(name)
        created += 1
    print(f"  Flights: +{created} row(s).")
    return created


def rebuild_price_site_rows(price_db: str, info: Dict[str, str]) -> int:
    if not info["start"]:
        print(f"  Skip price sites: journey '{info['title']}' has no Trip Date.")
        return 0
    existing = existing_titles(price_db, "網站名稱")
    query = f"{info['origin']} to {info['destination']} direct flight {info['start']} {info['end']} return checked baggage"
    now_iso = datetime.now(timezone.utc).isoformat()
    created = 0
    for site, base_url, airline_hint in FLIGHT_PRICE_SITES:
        if any(t.startswith(site) for t in existing):
            continue
        title = f"{site}｜{info['origin']} → {info['destination']}｜{info['start']}–{info['end']}"
        props: Dict[str, Any] = {
            "網站名稱": notion_title(title),
            "航空公司": notion_text(airline_hint),
            "航班日期": {"date": {"start": info["start"], "end": info["end"] or None}},
            "幣別": {"select": {"name": "TWD"}},
            "查詢時間": {"date": {"start": now_iso}},
            "訂票連結": {"url": build_search_url(base_url, query)},
            "含回程託運行李": {"checkbox": False},
            "備註": notion_text("結構列：等待查價後回填票價（統一 TWD），備註保留原始幣別、金額與換算依據。"),
        }
        create_db_row(price_db, props)
        existing.add(title)
        created += 1
    print(f"  Price sites: +{created} row(s).")
    return created


def main() -> int:
    database_id = env("TRIP_DATABASE_ID") or env("NOTION_DATABASE_ID")
    if not database_id:
        print("Missing TRIP_DATABASE_ID", file=sys.stderr)
        return 2

    journeys = query_all_pages(database_id)
    print(f"Found {len(journeys)} journey task(s) in Trip database.")
    total_flights = 0
    total_sites = 0

    for page in journeys:
        info = parse_journey(page)
        print(f"Journey: {info['title']}")
        dbs = find_child_databases(page["id"])
        flight_db = dbs.get("航班追蹤")
        price_db = dbs.get("票價網站資料")
        if not flight_db and not price_db:
            print("  Skip: page has no 航班追蹤 / 票價網站資料 child database.")
            continue
        if flight_db:
            total_flights += rebuild_flight_rows(flight_db, info)
        if price_db:
            total_sites += rebuild_price_site_rows(price_db, info)

    print(f"Done. flights +{total_flights}, price sites +{total_sites}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
