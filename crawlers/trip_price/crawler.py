#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trip price task builder — samwi-dev/Notion-script · crawlers/trip_price

2026-08-05 修正重點：GitHub Actions 不再承諾從動態票價網站「解析即時票價」。

實測問題：GitHub Actions 可以產生／打開平台搜尋頁，但 Google Flights、Skyscanner、KAYAK、
momondo、Booking.com、Expedia、Trip.com、Kiwi、Traveloka、Airpaz 等網站多數使用 JS 動態渲染、
反爬蟲、session / cookie / 地區判斷與內部 API，因此在 Actions 環境中無法穩定產出票價數字。

修正後分工：
- GitHub Actions = task builder / structure maintainer
  1. 讀取 Trip database；每一列 = Journey Task
  2. 確保 Journey Task 頁內「航班追蹤」與「票價網站資料」結構列存在
  3. 產生正確平台訂票連結
  4. 將平台列備註標記為「待 Notion AI / 人工查價」
  5. 週一或手動執行時 touch「航班追蹤」的「上次觸發時間」來觸發 Notion Agent
- Notion AI / 人工 / 未來正式 flight API = 實際查價與回填票價

這樣即使 GitHub Actions 無法解析票價，workflow 仍視為成功，避免整套 Trip 流程因網站反爬而中斷。

2026-08-07 修正：
- extract_airport_code 新增中英文城市名 → IATA 對照（舊版只認 3 碼英文，
  From-To 寫「臺北 → 東京」這類中文地名時目的地代碼為空，build_booking_url 防呆
  回傳網站首頁，所有平台深連結失效）
- 新增 repair_stale_booking_urls()：既有列若訂票連結仍為首頁/過期且尚未回填票價，
  重新計算深連結並更新（已有票價的列不動，避免覆寫人工 / AI 查價成果）

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
from typing import Any, Dict, List, Optional, Set, Tuple

NOTION_VERSION = "2022-06-28"
WRITE_DELAY_SEC = 0.35

DYNAMIC_PRICE_WARNING = (
    "⚠️ 動態即時比價網站：GitHub Actions 只負責建立查價任務與訂票連結，不直接解析即時票價；"
    "請由 Notion AI / 人工開啟連結確認當下票價後回填。票價若由 web search 取得，僅作路線層級參考，"
    "實際點入後可能不同。"
)
MANUAL_PRICE_WARNING = (
    "待查價：GitHub Actions 已建立平台列與訂票連結，但不直接解析票價；"
    "請由 Notion AI / 人工確認即時票價後回填，統一 TWD，備註保留原始幣別、金額與換算依據。"
)

FLIGHT_PRICE_SITES = [
    ("Google Flights", "https://www.google.com/travel/flights", "多家航空公司比價", "dynamic"),
    ("Skyscanner", "https://www.skyscanner.com.tw", "多家航空公司比價", "dynamic"),
    ("Trip.com", "https://www.trip.com/flights", "多家航空公司比價", "dynamic"),
    ("KAYAK", "https://www.kayak.com/flights", "多家航空公司比價", "dynamic"),
    ("Expedia", "https://www.expedia.com/Flights", "多家航空公司比價", "dynamic"),
    ("momondo", "https://www.momondo.com/flight-search", "多家航空公司比價", "dynamic"),
    ("Booking.com", "https://www.booking.com/flights", "多家航空公司比價", "dynamic"),
    ("ezTravel 易遊網", "https://www.eztravel.com.tw", "多家航空公司比價", "manual"),
    ("EVA Air 長榮航空", "https://www.evaair.com", "EVA Air 長榮航空", "manual"),
    ("China Airlines 中華航空", "https://www.china-airlines.com", "China Airlines 中華航空", "manual"),
    ("STARLUX Airlines 星宇航空", "https://www.starlux-airlines.com", "STARLUX Airlines 星宇航空", "manual"),
    ("AirAsia 亞洲航空集團", "https://www.airasia.com", "AirAsia 亞洲航空集團（實際承運子公司需依航線查證）", "manual"),
    ("Kiwi.com", "https://www.kiwi.com", "多家航空公司比價／虛擬轉機組合，常見更低組合價", "dynamic"),
    ("Traveloka", "https://www.traveloka.com/en-en/flight", "東南亞航線與廉航比價", "dynamic"),
    ("Airpaz", "https://www.airpaz.com/en/flight", "印尼發跡東南亞 OTA，區域廉航比價", "dynamic"),
]

COUNTRY_AIRLINES: Dict[str, List[Tuple[str, str, str]]] = {
    "Thailand": [
        ("EVA Air 長榮航空", "www.evaair.com", "BR"),
        ("China Airlines 中華航空", "www.china-airlines.com", "CI"),
        ("STARLUX Airlines 星宇航空", "www.starlux-airlines.com", "JX"),
        ("AirAsia 亞洲航空集團", "www.airasia.com", "需查證"),
    ],
    "Japan": [
        ("EVA Air 長榮航空", "www.evaair.com", "BR"),
        ("China Airlines 中華航空", "www.china-airlines.com", "CI"),
        ("STARLUX Airlines 星宇航空", "www.starlux-airlines.com", "JX"),
        ("Tigerair Taiwan 台灣虎航", "www.tigerairtw.com", "IT"),
        ("AirAsia 亞洲航空集團", "www.airasia.com", "需查證"),
    ],
}

DEFAULT_AIRLINES: List[Tuple[str, str, str]] = [
    ("EVA Air 長榮航空", "www.evaair.com", "BR"),
    ("China Airlines 中華航空", "www.china-airlines.com", "CI"),
    ("STARLUX Airlines 星宇航空", "www.starlux-airlines.com", "JX"),
]

# 中英文城市名 → IATA 機場/城市代碼對照表。
# Trip 旅程的 From-To 欄位常寫中文地名（例如「臺北 → 東京」），
# 舊版 extract_airport_code 只認 3 碼大寫英文，導致目的地代碼為空、
# build_booking_url 防呆回傳首頁，所有平台訂票連結失去深連結功能（2026-08-07 修正）。
CITY_TO_AIRPORT: Dict[str, str] = {
    "臺北": "TPE", "台北": "TPE", "taipei": "TPE",
    "桃園": "TPE", "taoyuan": "TPE",
    "高雄": "KHH", "kaohsiung": "KHH",
    "臺中": "RMQ", "台中": "RMQ", "taichung": "RMQ",
    "東京": "TYO", "tokyo": "TYO",
    "成田": "NRT", "narita": "NRT",
    "羽田": "HND", "haneda": "HND",
    "大阪": "OSA", "osaka": "OSA",
    "關西": "KIX", "kansai": "KIX",
    "名古屋": "NGO", "nagoya": "NGO",
    "福岡": "FUK", "fukuoka": "FUK",
    "札幌": "SPK", "sapporo": "SPK",
    "沖繩": "OKA", "那霸": "OKA", "okinawa": "OKA", "naha": "OKA",
    "曼谷": "BKK", "bangkok": "BKK",
    "清邁": "CNX", "chiang mai": "CNX",
    "普吉": "HKT", "phuket": "HKT",
    "首爾": "SEL", "seoul": "SEL",
    "仁川": "ICN", "incheon": "ICN",
    "釜山": "PUS", "busan": "PUS",
    "香港": "HKG", "hong kong": "HKG",
    "澳門": "MFM", "macau": "MFM",
    "新加坡": "SIN", "singapore": "SIN",
    "吉隆坡": "KUL", "kuala lumpur": "KUL",
    "馬尼拉": "MNL", "manila": "MNL",
    "河內": "HAN", "hanoi": "HAN",
    "胡志明": "SGN", "ho chi minh": "SGN",
    "雅加達": "CGK", "jakarta": "CGK",
    "峇里島": "DPS", "峇里": "DPS", "bali": "DPS",
    "雪梨": "SYD", "sydney": "SYD",
    "墨爾本": "MEL", "melbourne": "MEL",
    "洛杉磯": "LAX", "los angeles": "LAX",
    "舊金山": "SFO", "san francisco": "SFO",
    "紐約": "NYC", "new york": "NYC",
    "倫敦": "LON", "london": "LON",
    "巴黎": "PAR", "paris": "PAR",
}


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def notion_headers() -> Dict[str, str]:
    token = env("NOTION_TOKEN")
    if not token:
        raise RuntimeError("Missing NOTION_TOKEN")
    return {
        "Authorization": "Bearer " + token,
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def http_json(method: str, url: str, payload: Optional[dict] = None, retries: int = 3) -> dict:
    """呼叫 Notion API；429 / 5xx / 逾時做遞增等待重試（3 次），其餘錯誤直接拋出。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method, headers=notion_headers())
        try:
            with urllib.request.urlopen(req, timeout=45) as res:
                return json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = 10 * (attempt + 1)
                print("  Notion API HTTP " + str(e.code) + ", waiting " + str(wait) + "s before retry " + str(attempt + 2) + "/" + str(retries) + "...")
                time.sleep(wait)
                continue
            raise RuntimeError("Notion API " + method + " " + url + " -> HTTP " + str(e.code) + ": " + body) from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries - 1:
                wait = 10 * (attempt + 1)
                print("  Notion API connection error (" + str(e) + "), waiting " + str(wait) + "s before retry " + str(attempt + 2) + "/" + str(retries) + "...")
                time.sleep(wait)
                continue
            raise RuntimeError("Notion API " + method + " " + url + " -> connection error: " + str(e)) from e
    raise RuntimeError("Notion API " + method + " " + url + " -> retries exhausted")


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
        url = "https://api.notion.com/v1/databases/" + database_id + "/query"
        data = http_json("POST", url, payload)
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return pages


def list_block_children(block_id: str) -> List[dict]:
    blocks: List[dict] = []
    cursor = None
    while True:
        url = "https://api.notion.com/v1/blocks/" + block_id + "/children?page_size=100"
        if cursor:
            url += "&start_cursor=" + urllib.parse.quote(cursor)
        data = http_json("GET", url)
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


def parse_journey(page: dict) -> Dict[str, str]:
    props = page.get("properties", {})
    title = text_prop(page, "Deal Title")
    from_to = text_prop(page, "From-To")
    summary = text_prop(page, "Summary")
    nation = text_prop(page, "Nation")
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
        "nation": nation,
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
    url = "https://www.google.com/s2/favicons?domain=" + domain + "&sz=128"
    return {"type": "external", "external": {"url": url}}


def _parse_iso_date(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime((value or "")[:10], "%Y-%m-%d")
    except ValueError:
        return None


def extract_airport_code(text: str) -> str:
    """從地名字串取 IATA 代碼。

    先找 3 碼大寫英文代碼（如「TPE」）；找不到再查中英文城市對照表
    （如「東京」→ TYO、「chiang mai」→ CNX）。都找不到回傳空字串。
    對照表比對時先長後短（「峇里島」優先於「峇里」），避免短名誤吞長名。
    """
    text = (text or "").strip()
    match = re.search(r"\b([A-Z]{3})\b", text)
    if match:
        return match.group(1)
    lowered = text.lower()
    for name in sorted(CITY_TO_AIRPORT, key=len, reverse=True):
        if name.lower() in lowered:
            return CITY_TO_AIRPORT[name]
    return ""


def build_booking_url(site: str, base_url: str, dep: str, arr: str, start: str, end: str) -> str:
    d1, d2 = _parse_iso_date(start), _parse_iso_date(end)
    if not (dep and arr and d1 and d2):
        return base_url
    iso1, iso2 = d1.strftime("%Y-%m-%d"), d2.strftime("%Y-%m-%d")
    dep_lower, arr_lower = dep.lower(), arr.lower()
    if site == "Google Flights":
        q = "Flights from " + dep + " to " + arr + " on " + iso1 + " through " + iso2
        return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q)
    if site == "Skyscanner":
        return "https://www.skyscanner.com.tw/transport/flights/" + dep_lower + "/" + arr_lower + "/" + d1.strftime("%y%m%d") + "/" + d2.strftime("%y%m%d") + "/"
    if site == "Trip.com":
        return "https://tw.trip.com/flights/showfarefirst?dcity=" + dep_lower + "&acity=" + arr_lower + "&ddate=" + iso1 + "&rdate=" + iso2 + "&triptype=rt&class=y&quantity=1"
    if site == "KAYAK":
        return "https://www.kayak.com.tw/flights/" + dep + "-" + arr + "/" + iso1 + "/" + iso2 + "?sort=bestflight_a"
    if site == "Expedia":
        us1, us2 = d1.strftime("%m/%d/%Y"), d2.strftime("%m/%d/%Y")
        return "https://www.expedia.com.tw/Flights-Search?trip=roundtrip&leg1=from:" + dep + ",to:" + arr + ",departure:" + us1 + "TANYT&leg2=from:" + arr + ",to:" + dep + ",departure:" + us2 + "TANYT&passengers=adults:1&mode=search"
    if site == "momondo":
        return "https://www.momondo.tw/flight-search/" + dep + "-" + arr + "/" + iso1 + "/" + iso2 + "?sort=bestflight_a"
    if site == "Booking.com":
        return "https://flights.booking.com/flights/" + dep + ".AIRPORT-" + arr + ".AIRPORT/?type=ROUNDTRIP&adults=1&cabinClass=ECONOMY&depart=" + iso1 + "&return=" + iso2
    if site == "ezTravel 易遊網":
        return "https://flight.eztravel.com.tw/tickets-roundtrip-" + dep_lower + "-" + arr_lower + "?outbounddate=" + d1.strftime("%Y/%m/%d") + "&inbounddate=" + d2.strftime("%Y/%m/%d")
    if site == "AirAsia 亞洲航空集團":
        return "https://www.airasia.com/flights/search/?origin=" + dep + "&destination=" + arr + "&departDate=" + d1.strftime("%d/%m/%Y") + "&returnDate=" + d2.strftime("%d/%m/%Y") + "&tripType=R&adult=1&locale=zh-tw&currency=TWD"
    if site == "Kiwi.com":
        return "https://www.kiwi.com/deep?from=" + dep + "&to=" + arr + "&departure=" + iso1 + "&return=" + iso2
    if site == "Traveloka":
        return base_url
    if site == "Airpaz":
        return "https://www.airpaz.com/en/flight/search?depAirport=" + dep + "&arrAirport=" + arr + "&depDate=" + iso1 + "&adult=1&child=0&infant=0&currency=TWD&cabin=economy"
    if site == "EVA Air 長榮航空":
        return "https://www.evaair.com/zh-tw/booking/book-flights/"
    if site == "China Airlines 中華航空":
        return "https://www.china-airlines.com/tw/zh/booking/book-flights"
    if site == "STARLUX Airlines 星宇航空":
        return "https://www.starlux-airlines.com/zh-TW/booking/flights"
    return base_url


def build_price_note(site: str, site_kind: str, booking_url: str) -> str:
    extra = ""
    if site == "Traveloka":
        extra = " Traveloka 無已證實日期深度連結，需手動輸入航線與日期。"
    elif site == "Airpaz":
        extra = " Airpaz 目前只帶入去程日期，回程日期需在結果頁自行切換確認。"
    elif site == "AirAsia 亞洲航空集團":
        extra = " AirAsia 不可預設泰國亞航，需先確認實際承運子公司與航空代碼。"
    elif site in {"EVA Air 長榮航空", "China Airlines 中華航空", "STARLUX Airlines 星宇航空"}:
        extra = " 航空公司官網通常需手動確認日期、行李與票價。"
    if "{" in booking_url or "}" in booking_url:
        extra += " ⚠️ URL 內含大括號，需立即檢查 URL builder。"
    prefix = DYNAMIC_PRICE_WARNING if site_kind == "dynamic" else MANUAL_PRICE_WARNING
    return prefix + extra


def airlines_for_route(info: Dict[str, str]) -> List[tuple]:
    nation = (info.get("nation") or "").strip()
    airlines = COUNTRY_AIRLINES.get(nation)
    if airlines is None:
        if not nation:
            print("  Warn: journey '" + info["title"] + "' has empty Nation; using default airline set.")
        else:
            print("  Note: no airline preset for Nation='" + nation + "'; using default airline set.")
        airlines = DEFAULT_AIRLINES
    dep_code = extract_airport_code(info["origin"]) or "TPE"
    arr_code = extract_airport_code(info["destination"])
    return [(name, domain, code, dep_code, arr_code) for name, domain, code in airlines]


def rebuild_flight_rows(flight_db: str, info: Dict[str, str]) -> int:
    airlines = airlines_for_route(info)
    if not airlines:
        print("  Skip flights: no airline mapping for route '" + (info["from_to"] or info["title"]) + "'.")
        return 0
    existing = existing_titles(flight_db, "航空公司")
    created = 0
    for name, domain, code, dep_airport, arr_airport in airlines:
        if name in existing:
            continue
        props = {
            "航空公司": notion_title(name),
            "到達機場": notion_text(arr_airport),
            "航廦位置": notion_text("（出發航廈待查）"),
            "航班時間": notion_text(info["start"] + " → " + info["end"] + "（實際班表待查）"),
            "直飛還是轉機": {"select": {"name": "直飛"}},
            "是否包含行李": {"checkbox": False},
        }
        create_db_row(flight_db, props, icon=favicon_icon(domain))
        existing.add(name)
        created += 1
    print("  Flights: +" + str(created) + " row(s).")
    return created


def repair_stale_booking_urls(price_db: str, info: Dict[str, str]) -> int:
    """修正既有平台列的訂票連結。

    2026-08-07 新增：早期因中文地名無法轉 IATA 代碼，既有列的訂票連結
    退化成網站首頁且永不更新（rebuild_price_site_rows 只補缺不覆寫）。
    此函式對「尚未回填票價」的既有列重新計算深連結，URL 不同時才 PATCH；
    已有票價的列一律不動，避免覆寫人工 / AI 查價成果。
    """
    if not info["start"]:
        return 0
    dep_code = extract_airport_code(info["origin"]) or "TPE"
    arr_code = extract_airport_code(info["destination"])
    site_lookup = {site: base_url for site, base_url, _, _ in FLIGHT_PRICE_SITES}
    repaired = 0
    for page in query_all_pages(price_db):
        title = text_prop(page, "網站名稱")
        site = next((s for s in site_lookup if title.startswith(s)), None)
        if site is None:
            continue
        price_prop = page.get("properties", {}).get("票價", {})
        if price_prop.get("number") is not None:
            continue
        expected = build_booking_url(site, site_lookup[site], dep_code, arr_code, info["start"], info["end"])
        current = text_prop(page, "訂票連結")
        if expected and expected != current:
            http_json(
                "PATCH",
                "https://api.notion.com/v1/pages/" + page["id"],
                {"properties": {"訂票連結": {"url": expected}}},
            )
            time.sleep(WRITE_DELAY_SEC)
            repaired += 1
            print("  Repaired booking URL for " + site + ".")
    if repaired:
        print("  Price sites: repaired " + str(repaired) + " stale booking URL(s).")
    return repaired


def rebuild_price_site_rows(price_db: str, info: Dict[str, str]) -> int:
    if not info["start"]:
        print("  Skip price sites: journey '" + info["title"] + "' has no Trip Date.")
        return 0
    existing = existing_titles(price_db, "網站名稱")
    dep_code = extract_airport_code(info["origin"]) or "TPE"
    arr_code = extract_airport_code(info["destination"])
    now_iso = datetime.now(timezone.utc).isoformat()
    created = 0
    for site, base_url, airline_hint, site_kind in FLIGHT_PRICE_SITES:
        if any(t.startswith(site) for t in existing):
            continue
        title = site + "｜" + info["origin"] + " → " + info["destination"] + "｜" + info["start"] + "–" + info["end"]
        booking_url = build_booking_url(site, base_url, dep_code, arr_code, info["start"], info["end"])
        props: Dict[str, Any] = {
            "網站名稱": notion_title(title),
            "航空公司": notion_text(airline_hint),
            "航班日期": {"date": {"start": info["start"], "end": info["end"] or None}},
            "幣別": {"select": {"name": "TWD"}},
            "查詢時間": {"date": {"start": now_iso}},
            "訂票連結": {"url": booking_url},
            "含回程託運行李": {"checkbox": False},
            "備註": notion_text(build_price_note(site, site_kind, booking_url)),
        }
        create_db_row(price_db, props)
        existing.add(title)
        created += 1
    print("  Price sites: +" + str(created) + " task row(s); GitHub Actions does not parse live prices.")
    return created


def touch_trigger_property(database_id: str) -> bool:
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
        url = "https://api.notion.com/v1/pages/" + target_id
        http_json("PATCH", url, {"properties": {"上次觸發時間": {"date": {"start": now_iso}}}})
        print("Touched trigger property on flight row " + target_id + " (journey page " + page["id"] + ").")
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
    print("Found " + str(len(journeys)) + " journey task(s) in Trip database.")
    total_flights = 0
    total_sites = 0
    total_repaired = 0

    for page in journeys:
        try:
            info = parse_journey(page)
            print("Journey: " + info["title"])
            dbs = find_child_databases(page["id"])
            flight_db = dbs.get("航班追蹤")
            price_db = dbs.get("票價網站資料")
            if not flight_db and not price_db:
                print("  Skip: page has no 航班追蹤 / 票價網站資料 child database.")
                continue
            if flight_db:
                total_flights += rebuild_flight_rows(flight_db, info)
            if price_db:
                total_repaired += repair_stale_booking_urls(price_db, info)
                total_sites += rebuild_price_site_rows(price_db, info)
        except Exception as e:
            print("  ERROR: journey page " + page.get("id", "?") + " failed, skipping (" + str(e) + ")", file=sys.stderr)
            continue

    print("Done. flights +" + str(total_flights) + ", price task rows +" + str(total_sites) + ", booking URLs repaired " + str(total_repaired) + ".")
    print("Result: GitHub Actions completed structure/task building; live price parsing is delegated to Notion AI / manual check / future flight API.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
