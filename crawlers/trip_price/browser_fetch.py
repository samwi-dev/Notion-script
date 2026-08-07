#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""browser_fetch.py - samwi-dev/Notion-script / crawlers/trip_price

用 Playwright headless Chromium 實際開啟訂票深連結，擷取頁面上的價格文字。

2026-08-06 更新：Step 3 改為「全部平台合併不分工」。
GitHub Actions 會對 Trip 票價網站資料的 15 個平台全部嘗試 Playwright + Groq 查價；
Notion AI / 人工只作為補查與修正輔助，不再固定負責特定 4 個平台。

2026-08-07 強化：
- 反爬蟲：停用 AutomationControlled、偽裝 zh-TW 語系 / Asia/Taipei 時區、
  隱藏 navigator.webdriver 指紋、帶 Accept-Language 標頭
- 擷取：不再只回傳第一個金額（常誤抓到行李加價、保險費等雜訊），
  改為收集前 N 個不重複候選價格，用「 | 」串接後交給 LLM 挑選
- 導覽失敗自動重試一次（逾時加倍）；擷取前捲動頁面觸發 lazy-load
- 除錯：比對不到價格時，把頁面標題與內文前 600 字印到 log，
  用來判斷是 cookie/consent 牆、人機驗證頁，還是價格格式與 regex 不符

深連結產生邏輯直接重用 crawler.py 既有的 build_booking_url() / FLIGHT_PRICE_SITES，
本檔案不重複維護一份平台 URL 模板。

設計：設定表（PLATFORM_CONFIG）驅動 + 共用 fetch_price()，而不是每平台一個檔案。

Required:
  pip install playwright
  playwright install chromium
"""

from __future__ import annotations

import re
import sys
from typing import Any, Dict, List, Optional

from crawler import FLIGHT_PRICE_SITES, build_booking_url, extract_airport_code

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 反爬蟲啟動參數：移除 headless Chromium 常見的自動化指紋。
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
]

# 偽裝成台灣使用者的瀏覽環境，降低被地區/語系判定攔截的機率。
CONTEXT_OPTIONS: Dict[str, Any] = {
    "locale": "zh-TW",
    "timezone_id": "Asia/Taipei",
    "viewport": {"width": 1366, "height": 900},
    "extra_http_headers": {"Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"},
}

# 隱藏 navigator.webdriver 等自動化指紋。
STEALTH_INIT_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    "Object.defineProperty(navigator, 'languages', {get: () => ['zh-TW', 'zh', 'en']});"
)

# 通用價格文字 fallback：抓不到平台專屬 pattern 時，退而求其次抓頁面上看起來像貨幣金額的字串。
GENERIC_PRICE_REGEX = r"(?:NT\$|US\$|HK\$|S\$|TWD\s?|THB\s?|JPY\s?|¥|\$)\s?[\d][\d,]{2,7}"

# 擷取時最多收集幾個不重複候選價格（交給 LLM 判斷，而不是只回傳第一個）。
MAX_PRICE_CANDIDATES = 8

# 平台設定表：wait_selector（等待渲染完成用的 CSS selector，可為 None 只靠 extra_wait_ms）、
# price_selector（優先用的 CSS selector，抓不到再退用 price_regex）、price_regex（抓不到 selector 時
# 對整頁文字做的正規表示式）、timeout（頁面 navigation 逾時 ms）、extra_wait_ms（JS 動態渲染緩衝等待）。
# 2026-08-06：15 個平台全部列入 Step 3；抓不到則 confidence=0 並保留待補查備註。
PLATFORM_CONFIG: Dict[str, Dict[str, Any]] = {
    "Google Flights": {
        "wait_selector": "div[role='main']",
        "price_selector": None,
        "price_regex": r"Cheapest\s+from\s+(?:NT\$|US\$|\$)[\d,]+",
        "timeout": 30000,
        "extra_wait_ms": 15000,
    },
    "Skyscanner": {
        "wait_selector": None,
        "price_selector": None,
        "price_regex": GENERIC_PRICE_REGEX,
        "timeout": 25000,
        "extra_wait_ms": 10000,
    },
    "Trip.com": {
        "wait_selector": None,
        "price_selector": None,
        "price_regex": r"(?:Cheapest|最低價)\s+(?:US\$|NT\$|TWD|\$)[\d,]+",
        "timeout": 30000,
        "extra_wait_ms": 15000,
    },
    "KAYAK": {
        "wait_selector": None,
        "price_selector": None,
        "price_regex": GENERIC_PRICE_REGEX,
        "timeout": 25000,
        "extra_wait_ms": 10000,
    },
    "Expedia": {
        "wait_selector": None,
        "price_selector": None,
        "price_regex": GENERIC_PRICE_REGEX,
        "timeout": 25000,
        "extra_wait_ms": 10000,
    },
    "momondo": {
        "wait_selector": None,
        "price_selector": None,
        "price_regex": GENERIC_PRICE_REGEX,
        "timeout": 25000,
        "extra_wait_ms": 10000,
    },
    "Booking.com": {
        "wait_selector": None,
        "price_selector": None,
        "price_regex": GENERIC_PRICE_REGEX,
        "timeout": 25000,
        "extra_wait_ms": 10000,
    },
    "ezTravel 易遊網": {
        "wait_selector": None,
        "price_selector": None,
        "price_regex": GENERIC_PRICE_REGEX,
        "timeout": 25000,
        "extra_wait_ms": 8000,
    },
    "EVA Air 長榮航空": {
        "wait_selector": None,
        "price_selector": None,
        "price_regex": GENERIC_PRICE_REGEX,
        "timeout": 25000,
        "extra_wait_ms": 10000,
    },
    "China Airlines 中華航空": {
        "wait_selector": None,
        "price_selector": None,
        "price_regex": GENERIC_PRICE_REGEX,
        "timeout": 25000,
        "extra_wait_ms": 10000,
    },
    "STARLUX Airlines 星宇航空": {
        "wait_selector": None,
        "price_selector": None,
        "price_regex": GENERIC_PRICE_REGEX,
        "timeout": 25000,
        "extra_wait_ms": 10000,
    },
    "AirAsia 亞洲航空集團": {
        "wait_selector": None,
        "price_selector": None,
        "price_regex": GENERIC_PRICE_REGEX,
        "timeout": 25000,
        "extra_wait_ms": 10000,
    },
    "Kiwi.com": {
        "wait_selector": None,
        "price_selector": None,
        "price_regex": GENERIC_PRICE_REGEX,
        "timeout": 30000,
        "extra_wait_ms": 12000,
    },
    "Traveloka": {
        "wait_selector": None,
        "price_selector": None,
        "price_regex": GENERIC_PRICE_REGEX,
        "timeout": 25000,
        "extra_wait_ms": 10000,
    },
    "Airpaz": {
        "wait_selector": None,
        "price_selector": None,
        "price_regex": GENERIC_PRICE_REGEX,
        "timeout": 25000,
        "extra_wait_ms": 10000,
    },
}


def extract_price_candidates(body_text: str, price_regex: str, limit: int = MAX_PRICE_CANDIDATES) -> List[str]:
    """從整頁文字收集前 N 個不重複的價格候選字串（比對順序即出現順序）。"""
    normalized = re.sub(r"\s+", " ", body_text)
    candidates: List[str] = []
    for match in re.finditer(price_regex, normalized):
        value = match.group(0).strip()
        if value not in candidates:
            candidates.append(value)
        if len(candidates) >= limit:
            break
    return candidates


def fetch_price(platform_key: str, url: str, timeout: Optional[int] = None) -> Optional[Dict[str, str]]:
    """開啟 url，等待渲染，擷取價格文字。單平台失敗一律回傳 None（降級，不拋例外）。

    導覽失敗會自動重試一次（逾時加倍），提高動態網站在 Actions 環境的成功率。
    """
    config = PLATFORM_CONFIG.get(platform_key)
    if not config:
        print("  [" + platform_key + "] no PLATFORM_CONFIG entry, skip.", file=sys.stderr)
        return None

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401 延遲載入，避免未安裝 playwright 時整檔案 import 失敗
    except ImportError as e:
        print("  [" + platform_key + "] playwright not installed: " + str(e), file=sys.stderr)
        return None

    nav_timeout = timeout or config.get("timeout", 20000)

    for attempt in range(2):
        result = _fetch_once(platform_key, url, config, nav_timeout * (attempt + 1))
        if result is not None:
            return result
        if attempt == 0:
            print("  [" + platform_key + "] first attempt failed, retrying once with doubled timeout...")
    return None


def _fetch_once(platform_key: str, url: str, config: Dict[str, Any], nav_timeout: int) -> Optional[Dict[str, str]]:
    """單次擷取嘗試；任何例外都回傳 None 由 fetch_price 決定是否重試。"""
    from playwright.sync_api import sync_playwright

    extra_wait_ms = config.get("extra_wait_ms", 8000)
    wait_selector = config.get("wait_selector")
    price_selector = config.get("price_selector")
    price_regex = config.get("price_regex") or GENERIC_PRICE_REGEX

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
            try:
                context = browser.new_context(user_agent=USER_AGENT, **CONTEXT_OPTIONS)
                context.add_init_script(STEALTH_INIT_SCRIPT)
                page = context.new_page()
                page.goto(url, timeout=nav_timeout, wait_until="domcontentloaded")
                if wait_selector:
                    try:
                        page.wait_for_selector(wait_selector, timeout=nav_timeout)
                    except Exception:
                        pass
                page.wait_for_timeout(extra_wait_ms)
                # 捲動頁面觸發 lazy-load（部分比價網站的價格區塊要捲動才渲染）
                try:
                    page.mouse.wheel(0, 2500)
                    page.wait_for_timeout(2000)
                except Exception:
                    pass

                raw_text = ""
                if price_selector:
                    try:
                        raw_text = page.locator(price_selector).first.inner_text(timeout=5000)
                    except Exception:
                        raw_text = ""

                body_text = ""
                if not raw_text:
                    body_text = page.inner_text("body")
                    candidates = extract_price_candidates(body_text, price_regex)
                    raw_text = " | ".join(candidates)

                if not raw_text:
                    # 除錯（2026-08-07）：比對不到價格時輸出頁面標題與內文前段，
                    # 用來判斷實際停在 cookie/consent 牆、人機驗證頁，還是價格格式與 regex 不符。
                    try:
                        page_title = page.title()
                    except Exception:
                        page_title = "(unavailable)"
                    snippet = re.sub(r"\s+", " ", body_text).strip()[:600]
                    print("  [" + platform_key + "] page loaded but no price pattern matched. title=" + page_title)
                    print("  [" + platform_key + "] body snippet: " + snippet)
                    return None

                raw_text = raw_text.strip()
                print("  [" + platform_key + "] raw price text: " + raw_text[:80])
                return {"platform": platform_key, "url": url, "raw_text": raw_text}
            finally:
                browser.close()
    except Exception as e:
        print("  [" + platform_key + "] fetch failed: " + str(e), file=sys.stderr)
        return None


def fetch_all_for_journey(info: Dict[str, str]) -> List[Dict[str, str]]:
    """對 info（同 crawler.parse_journey 回傳格式）用 PLATFORM_CONFIG 目標平台各自嘗試擷取價格文字。"""
    dep_code = extract_airport_code(info.get("origin", "")) or "TPE"
    arr_code = extract_airport_code(info.get("destination", ""))
    site_lookup = {site: base_url for site, base_url, _, _ in FLIGHT_PRICE_SITES}

    results: List[Dict[str, str]] = []
    for platform_key in PLATFORM_CONFIG:
        base_url = site_lookup.get(platform_key)
        if base_url is None:
            print("  [" + platform_key + "] not found in crawler.FLIGHT_PRICE_SITES, skip.", file=sys.stderr)
            continue
        url = build_booking_url(platform_key, base_url, dep_code, arr_code, info.get("start", ""), info.get("end", ""))
        result = fetch_price(platform_key, url)
        if result:
            results.append(result)
    return results


if __name__ == "__main__":
    # 手動測試入口：python browser_fetch.py "<platform_key>" "<url>"
    if len(sys.argv) >= 3:
        platform_arg, url_arg = sys.argv[1], sys.argv[2]
        print(fetch_price(platform_arg, url_arg))
    else:
        print("Usage: python browser_fetch.py \"<platform_key>\" \"<url>\"", file=sys.stderr)
