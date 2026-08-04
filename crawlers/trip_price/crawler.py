#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trip price crawler — samwi-dev/Notion-script · crawlers/trip_price

2026-08 重寫版，對齊目前 Notion 架構：
- TRIP_DATABASE_ID 指向 Trip database；每一列 = 一個 Journey Task（一個旅程只保留一個 Task）
- 每個 Journey Task 頁面內嵌兩個子 database：
  - 「票價網站資料」：各比價網站報價（linked view「整月價格走勢」chart 的資料來源）
  - 「航班追蹤」：本旅程航班（page icon 使用航空公司官網 favicon）
- 爬蟲負責「重建結構」：補齊航班追蹤的直飛航空公司、票價網站資料的各網站報價列
- 去重：航班追蹤以「航空公司」title、票價網站資料以「網站名稱」前綴判斷，可每日重複執行
- 訂票連結：使用各平台真正的深度連結格式（帶航線與日期參數，點開直接顯示搜尋結果）；
  航空公司官網不支援帶日期深度連結，連到訂票頁
- 實際價格由比價流程查詢後回填（統一 TWD，備註保留原始幣別與換算依據）

2026-08-04 觸發機制精簡：不再另外寫「GitHub Actions 執行日誌」資料庫來觸發 Notion AI Agent。
改為執行 `--touch-trigger` 時，直接更新「航班追蹤」子 database 中第一列的「上次觸發時間」
屬性；Notion 端 Agent 監看該屬性變更即會自動觸發，省去一層日誌資料庫與一次額外的頁面建立。

Required GitHub Actions secrets:
- NOTION_TOKEN
- TRIP_DATABASE_ID
"""

from __future__ import annotations

import json
import os
import re
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


def _parse_iso_date(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime((value or "")[:10], "%Y-%m-%d")
    except ValueError:
        return None


def extract_airport_code(text: str) -> str:
    """從 '台灣 TPE/TSA' 這類字串取出第一個 IATA 三碼。"""
    match = re.search(r"\b([A-Z]{3})\b", text or "")
    return match.group(1) if match else ""


def build_booking_url(site: str, base_url: str, dep: str, arr: str, start: str, end: str) -> str:
    """產生各平台可直接顯示搜尋結果的深度連結。

    重要：不可使用「首頁 + ?q=搜尋字串」假參數，各網站不認得這種格式，
    點開後不會出現任何搜尋結果。缺日期或機場代碼時退回官網首頁。
    """
    d1, d2 = _parse_iso_date(start), _parse_iso_date(end)
    if not (dep and arr and d1 and d2):
        return base_url
    iso1, iso2 = d1.strftime("%Y-%m-%d"), d2.strftime("%Y-%m-%d")
    if site == "Google Flights":
        q = f"Flights from {dep} to {arr} on {iso1} through {iso2}"
        return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q)
    if site == "Skyscanner":
        return (
            f"https://www.skyscanner.com.tw/transport/flights/"
            f"{dep.lower()}/{arr.lower()}/{d1.strftime('%y%m%d')}/{d2.strftime('%y%m%d')}/"
        )
    if site == "Trip.com":
        return (
            f"https://tw.trip.com/flights/showfarefirst?dcity={dep.lower()}&acity={arr.lower()}"
            f"&ddate={iso1}&rdate={iso2}&triptype=rt&class=y&quantity=1"
        )
    if site == "KAYAK":
        return f"https://www.kayak.com.tw/flights/{dep}-{arr}/{iso1}/{iso2}?sort=bestflight_a"
    if site == "Expedia":
        us1, us2 = d1.strftime("%m/%d/%Y"), d2.strftime("%m/%d/%Y")
        return (
            "https://www.expedia.com.tw/Flights-Search?trip=roundtrip"
            f"&leg1=from:{dep},to:{arr},departure:{us1}TANYT"
            f"&leg2=from:{arr},to:{dep},departure:{us2}TANYT"
            "&passengers=adults:1&mode=search"
        )
    if site == "momondo":
        return f"https://www.momondo.tw/flight-search/{dep}-{arr}/{iso1}/{iso2}?sort=bestflight_a"
    if site == "Booking.com":
        return (
            f"https://flights.booking.com/flights/{dep}.AIRPORT-{arr}.AIRPORT/"
            f"?type=ROUNDTRIP&adults=1&cabinClass=ECONOMY&depart={iso1}&return={iso2}"
        )
    if site == "ezTravel 易遊網":
        return (
            f"https://flight.eztravel.com.tw/tickets-roundtrip-{dep.lower()}-{arr.lower()}"
            f"?outbounddate={d1.strftime('%Y/%m/%d')}&inbounddate={d2.strftime('%Y/%m/%d')}"
        )
    if site == "Thai AirAsia 泰國亞洲航空":
        return (
            f"https://www.airasia.com/flights/search/?origin={dep}&destination={arr}"
            f"&departDate={d1.strftime('%d/%m/%Y')}&returnDate={d2.strftime('%d/%m/%Y')}"
            "&tripType=R&adult=1&locale=zh-tw&currency=TWD"
        )
    if site == "EVA Air 長榮航空":
        return "https://www.evaair.com/zh-tw/booking/book-flights/"
    if site == "China Airlines 中華航空":
        return "https://www.china-airlines.com/tw/zh/booking/book-flights"
    if site == "STARLUX Airlines 星宇航空":
        return "https://www.starlux-airlines.com/zh-TW/booking/flights"
    return base_url


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
    dep_code = extract_airport_code(info["origin"]) or "TPE"
    arr_code = extract_airport_code(info["destination"])
    now_iso = datetime.now(timezone.utc).isoformat()
    created = 0
    for site, base_url, airline_hint in FLIGHT_PRICE_SITES:
        if any(t.startswith(site) for t in existing):
            continue
        title = f"{site}｜{info['origin']} → {info['destination']}｜{info['start']}–{info['end']}"
        booking_url = build_booking_url(site, base_url, dep_code, arr_code, info["start"], info["end"])
        props: Dict[str, Any] = {
            "網站名稱": notion_title(title),
            "航空公司": notion_text(airline_hint),
            "航班日期": {"date": {"start": info["start"], "end": info["end"] or None}},
            "幣別": {"select": {"name": "TWD"}},
            "查詢時間": {"date": {"start": now_iso}},
            "訂票連結": {"url": booking_url},
            "含回程託運行李": {"checkbox": False},
            "備註": notion_text("結構列：等待查價後回填票價（統一 TWD），備註保留原始幣別、金額與換算依據。"),
        }
        create_db_row(price_db, props)
        existing.add(title)
        created += 1
    print(f"  Price sites: +{created} row(s).")
    return created


def touch_trigger_property(database_id: str) -> bool:
    """更新第一個找到的 Journey Task 之「航班追蹤」子 database 中任一列的
    「上次觸發時間」屬性，藉此觸發 Notion 端監看該屬性變更的 Agent 開始查價。

    取代舊的「在 GitHub Actions 執行日誌 database 新增一列」機制：
    不用另外建立日誌資料庫，也不用多一次頁面建立 API 呼叫。
    """
    journeys = query_all_pages(database_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    for page in journeys:
        dbs = find_child_databases(page["id"])
        flight_db = dbs.get("航班追蹤")
        if not flight_db:
            continue
        rows = query_all_pages(flight_db)
        if not rows:
            continue
        target_id = rows[0]["id"]
        http_json(
            "PATCH",
            f"https://api.notion.com/v1/pages/{target_id}",
            {"properties": {"上次觸發時間": {"date": {"start": now_iso}}}},
        )
        print(f"Touched trigger property on flight row {target_id} (journey page {page['id']}).")
        return True
    print("No journey with a 航班追蹤 child database found; nothing to touch.", file=sys.stderr)
    return False


def main() -> int:
    database_id = env("TRIP_DATABASE_ID") or env("NOTION_DATABASE_ID")
    if not database_id:
        print("Missing TRIP_DATABASE_ID", file=sys.stderr)
        return 2

    if "--touch-trigger" in sys.argv[1:]:
        return 0 if touch_trigger_property(database_id) else 1

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
