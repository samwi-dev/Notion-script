#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fast_flights_fetch.py - samwi-dev/Notion-script / crawlers/trip_price

封裝 fast-flights 套件的呼叫，取代 Google Flights 走 Playwright/Patchright 開頁擷取的做法。
fast-flights 直接打 Google Flights 內部查詢介面取回結構化航班資料（含整數 price 欄位），
不必真的開瀏覽器渲染頁面，速度較快，也比較不容易被判定成自動化流量。

2026-08-08 新增：browser_fetch.fetch_price() 對 "Google Flights" 平台會先呼叫本檔案的
fetch_google_flights_price()；成功就直接採用其結果，失敗（套件未安裝、journey_info 缺
必要欄位、或 API 呼叫例外）一律回傳 None，讓 browser_fetch 落回原本的 Playwright/Patchright
開頁流程（兩層 fallback，不能讓 fast-flights 失敗變成整個平台失敗）。

已驗證（2026-08-08 在有網路環境下實測 smoke test，非憑空假設的 API）：
  pip install fast-flights（實測版本 3.0.2，另需 typing_extensions 依賴）
  from fast_flights import create_query, get_flights, FlightQuery, Passengers
  query = create_query(flights=[FlightQuery(date="YYYY-MM-DD", from_airport=dep, to_airport=arr), ...],
                        trip="one-way" 或 "round-trip", seat="economy",
                        passengers=Passengers(adults=1), currency="TWD")
  result = get_flights(query)  # -> ResultList（list[Flights]）
  每筆 Flights 物件的 .price 為 int（已依 currency 換算後的整數金額）。
  實測 TPE→NRT（單程）與 TPE→NRT／NRT→TPE（來回）皆成功取回多筆真實報價（例如 NT$6,199 起）；
  機場欄位帶 3 碼 IATA 代碼（TPE/NRT）與城市代碼（TYO）皆可正確查詢。

Required:
  pip install fast-flights
"""

from __future__ import annotations

from typing import Dict, Optional

from crawler import FLIGHT_PRICE_SITES, build_booking_url, extract_airport_code

# raw_text 最多帶幾個不重複候選價格，跟 browser_fetch.MAX_PRICE_CANDIDATES 同量級。
MAX_CANDIDATES = 8


def fetch_google_flights_price(journey_info: Dict[str, str]) -> Optional[Dict[str, str]]:
    """用 fast-flights 直接查 Google Flights 結構化報價，取代開瀏覽器擷取。

    journey_info 格式同 crawler.parse_journey() 回傳（含 origin/destination/start/end 等 key）。
    end 為空字串時視為單程查詢，不因為沒有回程日期就放棄整個查詢。
    任何失敗（套件未安裝、機場代碼解析失敗、API 呼叫例外）一律回傳 None，不拋例外，
    讓呼叫端 fallback 到 Playwright/Patchright 開頁流程。
    """
    try:
        from fast_flights import FlightQuery, Passengers, create_query, get_flights
    except ImportError as e:
        print("  [Google Flights] fast-flights not installed, fallback to browser flow: " + str(e))
        return None

    dep_code = extract_airport_code(journey_info.get("origin", "")) or "TPE"
    arr_code = extract_airport_code(journey_info.get("destination", ""))
    start = journey_info.get("start", "")
    end = journey_info.get("end", "")
    if not (dep_code and arr_code and start):
        print("  [Google Flights] journey_info missing airport code or date, fallback to browser flow.")
        return None

    try:
        if end:
            flights = [
                FlightQuery(date=start, from_airport=dep_code, to_airport=arr_code),
                FlightQuery(date=end, from_airport=arr_code, to_airport=dep_code),
            ]
            trip = "round-trip"
        else:
            flights = [FlightQuery(date=start, from_airport=dep_code, to_airport=arr_code)]
            trip = "one-way"

        query = create_query(
            flights=flights,
            trip=trip,
            seat="economy",
            passengers=Passengers(adults=1),
            currency="TWD",
        )
        result = get_flights(query)
        prices = sorted({int(f.price) for f in result if getattr(f, "price", None) is not None})
    except Exception as e:
        print("  [Google Flights] fast-flights query failed, fallback to browser flow: " + str(e))
        return None

    if not prices:
        print("  [Google Flights] fast-flights returned no priced flights, fallback to browser flow.")
        return None

    raw_text = " | ".join("NT$" + format(p, ",") for p in prices[:MAX_CANDIDATES])
    site_lookup = {site: base_url for site, base_url, _, _ in FLIGHT_PRICE_SITES}
    url = build_booking_url("Google Flights", site_lookup.get("Google Flights", ""), dep_code, arr_code, start, end)
    print("  [Google Flights] fast-flights raw price text: " + raw_text[:80])
    return {"platform": "Google Flights", "url": url, "raw_text": raw_text}
