#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trip price crawler — samwi-dev/Notion-script · crawlers/trip_price

2026-08 重寫版，對齊目前 Notion 架構：
- TRIP_DATABASE_ID 指向 Trip database；每一列 = 一個 Journey Task（一個旅程只保留一個 Task）
- 每個 Journey Task 頁面內孌兩個子 database：
  - 「票價網站資料」：各比價網站報價（linked view「整月價格走勢」chart 的資料來源）
  - 「航班追蹤」：本旅程航班（page icon 使用航空公司官網 favicon）
- 爬蟲負責「重建結構」：補齊航班追蹤的直飛航空公司、票價網站資料的各網站報價列
- 去重：航班追蹤以「航空公司」title、票價網站資料以「網站名稱」前綴判斷，可每日重複執行
- 訂票連結：使用各平台真正的深度連結格式（帶航線與日期參數，點開直接顯示搜尋結果）；
  航空公司官網不支援帶日期深度連結，連到訂票頁
- 實際價格由比價流程查詢後回填（統一 TWD，備註保留原始幣別與換算依據）

2026-08-04 觸發機制精簡：不再別外寫「GitHub Actions 執行日誌」資料庫來觸發 Notion AI Agent。
改為執行 `--touch-trigger` 時，直接更新「航班追蹤」子 database 中第一列的「上次觸發時間」
屬性；Notion 端 Agent 監看該屬性變更即會自動觸發，省去一層日誌資料庫與一次額外的頁面建立。

2026-08-04 修正：航班追蹤的航空公司對應原本硬寫死只認泰國清邁（CNX），非清邁旅程完全不會
補上航班列。改為以 Trip database 的 `Nation` 屬性查通用的 COUNTRY_AIRLINES 對照表；
沒列出的國家一律退回 DEFAULT_AIRLINES（長槮／華航／星宇，台灣飛國际線最常見的組合），
確定任何目的地都會有結構列可讓 Notion AI 之後查價校正，不再侵限泰國。

2026-08-04 再次修正：對齊範本頁「New trip」目前的「航班追蹤」子 database 欄位，補上
`航廦位置`（出發機場航廈）結構欄位，避免自動建立的列缺少此欄位；實際航廈仍留待 Notion AI
查價／核實航班資訊時回填。「相關比價紀錄」「相關網站報價」「相關航班」等 relation 欄位維持
不由爬蟲自動填寫（比價紀錄列尚未建立、且分工文件已註明三者雙向 relation 不自動同步）。

2026-08-04 修復舊 bug：部分產生 URL 的 f-string 誤用雙括號轉義，導致實際網址包含字面上的
大括號字元（包括 Notion API 內部呼叫與 Trip.com、KAYAK、momondo、Booking.com、ezTravel、
Thai AirAsia 的訂票深度連結）。已全部改用字串連接（+）重寫，不再使用容易誤寫的
雙括號 f-string 轉義。

2026-08-04 再修正：範本頁「航班追蹤」資料庫的實際屬性名稱是「航廦位置」（非常見的「廈」字
異體字「廦」），先前程式碼誤寫為「航廈位置」，實際呼叫 Notion API 會因屬性不存在而失敗。
已改為與資料庫 schema 完全一致的「航廦位置」。

2026-08-04 新增第 13、14 個票價平台：Kiwi.com（虛擬轉機組合，常能拼出比其他平台更低的
組合價；深度連結格式 https://www.kiwi.com/deep?from=&to=&departure=&return= 已於
Travelpayouts 官方文件證實可用）、Traveloka（東南亞 OTA，區域航線與廉航票價常見更低，但
官網沒有可證實的公開日期深度連結格式，因此只連到機票搜尋頁 base_url，訂票連結／備註需標明
手動輸入航線與日期，避免重演雙括號轉義那類「連結格式猜錯」的問題）。兩者皆與 Google Flights
等既有 5 個動態比價網站一樣，回填的票價只是 web search 當下的路線層級參考值，備註仍需以
「⚠️ 動態即時比價網站」開頭加註提醒。

2026-08-04 新增第 15 個票價平台：Airpaz（印尼發跡的東南亞 OTA，區域廉航票價常見更低，支援
在地幣別與語言介面）。

2026-08-04 修正：先前判斷 Airpaz「沒有可證實的帶日期深度連結格式」是誤判——當時只查看了
靜態的 /en/flight 搜尋首頁就下結論，沒有實際打開帶航線與日期的搜尋結果頁驗證；經使用者手動
查詢後證實，Airpaz 官網搜尋結果頁的網址確實支援帶航線與日期的查詢參數（depAirport／
arrAirport／depDate／adult／child／infant／currency／cabin）。已改為組出去程單程深度連結：
https://www.airpaz.com/en/flight/search?depAirport=<出發>&arrAirport=<抵達>&depDate=<日期>
&adult=1&child=0&infant=0&currency=TWD&cabin=economy 。回程日期尚未證實可用同一組 URL
參數直接帶入（結果頁上方有日期切換列，需自行切換至回程日期查看），故僅組出去程深度連結，
備註仍需以「⚠️ 動態即時比價網站」開頭並提醒回程日期需自行切換確認。

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

FLIGHT_PRICE_SITES = [
    ("Google Flights", "https://www.google.com/travel/flights", "多家航空公司比價"),
    ("Skyscanner", "https://www.skyscanner.com.tw", "多家航空公司比價"),
    ("Trip.com", "https://www.trip.com/flights", "多家航空公司比價"),
    ("KAYAK", "https://www.kayak.com/flights", "多家航空公司比價"),
    ("Expedia", "https://www.expedia.com/Flights", "多家航空公司比價"),
    ("momondo", "https://www.momondo.com/flight-search", "多家航空公司比價"),
    ("Booking.com", "https://www.booking.com/flights", "多家航空公司比價"),
    ("ezTravel 易遊網", "https://www.eztravel.com.tw", "多家航空公司比價"),
    ("EVA Air 長槮航空", "https://www.evaair.com", "EVA Air 長槮航空"),
    ("China Airlines 中華航空", "https://www.china-airlines.com", "China Airlines 中華航空"),
    ("STARLUX Airlines 星宇航空", "https://www.starlux-airlines.com", "STARLUX Airlines 星宇航空"),
    ("Thai AirAsia 泰國亞洲航空", "https://www.airasia.com", "Thai AirAsia 泰國亞洲航空"),
    ("Kiwi.com", "https://www.kiwi.com", "多家航空公司比價／虛擬轉機組合，常見更低組合價"),
    ("Traveloka", "https://www.traveloka.com/en-en/flight", "東南亞航線與廉航比價"),
    ("Airpaz", "https://www.airpaz.com/en/flight", "印尼發跡東南亞 OTA，區域廉航比價"),
]

COUNTRY_AIRLINES: Dict[str, List[Tuple[str, str, str]]] = {
    "Thailand": [
        ("EVA Air 長槮航空", "www.evaair.com", "BR"),
        ("China Airlines 中華航空", "www.china-airlines.com", "CI"),
        ("STARLUX Airlines 星宇航空", "www.starlux-airlines.com", "JX"),
        ("Thai AirAsia 泰國亞洲航空", "www.airasia.com", "FD"),
    ],
    "Japan": [
        ("EVA Air 長槮航空", "www.evaair.com", "BR"),
        ("China Airlines 中華航空", "www.china-airlines.com", "CI"),
        ("STARLUX Airlines 星宇航空", "www.starlux-airlines.com", "JX"),
        ("Tigerair Taiwan 台灣虎航", "www.tigerairtw.com", "IT"),
    ],
}

DEFAULT_AIRLINES: List[Tuple[str, str, str]] = [
    ("EVA Air 長槮航空", "www.evaair.com", "BR"),
    ("China Airlines 中華航空", "www.china-airlines.com", "CI"),
    ("STARLUX Airlines 星宇航空", "www.starlux-airlines.com", "JX"),
]


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


def http_json(method: str, url: str, payload: Optional[dict] = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=notion_headers())
    try:
        with urllib.request.urlopen(req, timeout=45) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError("Notion API " + method + " " + url + " -> HTTP " + str(e.code) + ": " + body) from e


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
    match = re.search(r"\b([A-Z]{3})\b", text or "")
    return match.group(1) if match else ""


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
        return (
            "https://www.skyscanner.com.tw/transport/flights/"
            + dep_lower + "/" + arr_lower + "/" + d1.strftime("%y%m%d") + "/" + d2.strftime("%y%m%d") + "/"
        )
    if site == "Trip.com":
        return (
            "https://tw.trip.com/flights/showfarefirst?dcity=" + dep_lower + "&acity=" + arr_lower
            + "&ddate=" + iso1 + "&rdate=" + iso2 + "&triptype=rt&class=y&quantity=1"
        )
    if site == "KAYAK":
        return "https://www.kayak.com.tw/flights/" + dep + "-" + arr + "/" + iso1 + "/" + iso2 + "?sort=bestflight_a"
    if site == "Expedia":
        us1, us2 = d1.strftime("%m/%d/%Y"), d2.strftime("%m/%d/%Y")
        return (
            "https://www.expedia.com.tw/Flights-Search?trip=roundtrip"
            + "&leg1=from:" + dep + ",to:" + arr + ",departure:" + us1 + "TANYT"
            + "&leg2=from:" + arr + ",to:" + dep + ",departure:" + us2 + "TANYT"
            + "&passengers=adults:1&mode=search"
        )
    if site == "momondo":
        return "https://www.momondo.tw/flight-search/" + dep + "-" + arr + "/" + iso1 + "/" + iso2 + "?sort=bestflight_a"
    if site == "Booking.com":
        return (
            "https://flights.booking.com/flights/" + dep + ".AIRPORT-" + arr + ".AIRPORT/"
            + "?type=ROUNDTRIP&adults=1&cabinClass=ECONOMY&depart=" + iso1 + "&return=" + iso2
        )
    if site == "ezTravel 易遊網":
        return (
            "https://flight.eztravel.com.tw/tickets-roundtrip-" + dep_lower + "-" + arr_lower
            + "?outbounddate=" + d1.strftime("%Y/%m/%d") + "&inbounddate=" + d2.strftime("%Y/%m/%d")
        )
    if site == "Thai AirAsia 泰國亞洲航空":
        return (
            "https://www.airasia.com/flights/search/?origin=" + dep + "&destination=" + arr
            + "&departDate=" + d1.strftime("%d/%m/%Y") + "&returnDate=" + d2.strftime("%d/%m/%Y")
            + "&tripType=R&adult=1&locale=zh-tw&currency=TWD"
        )
    if site == "Kiwi.com":
        # 官方 affiliate 文件證實的深度連結格式：/deep?from=&to=&departure=&return=（IATA 三字碼、YYYY-MM-DD）
        return (
            "https://www.kiwi.com/deep?from=" + dep + "&to=" + arr
            + "&departure=" + iso1 + "&return=" + iso2
        )
    if site == "Traveloka":
        # Traveloka 官網沒有可證實的公開日期深度連結格式；為避免重演雙括號轉義那類
        # 「連結格式猜錯」問題，一律回傳機票搜尋頁 base_url，備註標明需手動輸入航線與日期。
        return base_url
    if site == "Airpaz":
        # 2026-08-04 修正：先前誤判「Airpaz 沒有可證實的帶日期深度連結格式」——當時只看了
        # 靜態首頁 /en/flight 就下結論，沒有實際打開搜尋結果頁驗證。使用者手動查詢後證實，
        # Airpaz 搜尋結果頁的網址確實支援帶航線與日期的查詢參數，改為組出去程單程深度連結；
        # 回程日期尚未證實可用同一組參數直接帶入，需在結果頁上方日期列自行切換確認。
        return (
            "https://www.airpaz.com/en/flight/search?depAirport=" + dep + "&arrAirport=" + arr
            + "&depDate=" + iso1 + "&adult=1&child=0&infant=0&currency=TWD&cabin=economy"
        )
    if site == "EVA Air 長槮航空":
        return "https://www.evaair.com/zh-tw/booking/book-flights/"
    if site == "China Airlines 中華航空":
        return "https://www.china-airlines.com/tw/zh/booking/book-flights"
    if site == "STARLUX Airlines 星宇航空":
        return "https://www.starlux-airlines.com/zh-TW/booking/flights"
    return base_url


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


def rebuild_price_site_rows(price_db: str, info: Dict[str, str]) -> int:
    if not info["start"]:
        print("  Skip price sites: journey '" + info["title"] + "' has no Trip Date.")
        return 0
    existing = existing_titles(price_db, "網站名稱")
    dep_code = extract_airport_code(info["origin"]) or "TPE"
    arr_code = extract_airport_code(info["destination"])
    now_iso = datetime.now(timezone.utc).isoformat()
    created = 0
    for site, base_url, airline_hint in FLIGHT_PRICE_SITES:
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
            "備註": notion_text("結構列：等待查價後回填票價（統一 TWD），備註保留原始幣別、金額與換算依據。"),
        }
        create_db_row(price_db, props)
        existing.add(title)
        created += 1
    print("  Price sites: +" + str(created) + " row(s).")
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
        http_json(
            "PATCH",
            url,
            {"properties": {"上次觸發時間": {"date": {"start": now_iso}}}},
        )
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

    for page in journeys:
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
            total_sites += rebuild_price_site_rows(price_db, info)

    print("Done. flights +" + str(total_flights) + ", price sites +" + str(total_sites) + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
