#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ai_price.py - samwi-dev/Notion-script / crawlers/trip_price

Step 3：對 Trip「票價網站資料」15 個平台全部嘗試 Playwright + Groq 查價，
把 browser_fetch.py 實際擷取到的原始價格文字用 Groq 正規化成結構化 JSON，回填 Notion。

⚠️ 2026-07-17 起 Amadeus Flight Offers Search API 已停用，本檔案舊版 Amadeus 查價邏輯已完全移除。

2026-08-06 更新：取消 11 + 4 固定分工，全部平台合併由 GitHub Actions Step 3 嘗試。
Notion AI / 人工只作為補查與修正輔助；不再固定負責 EVA / Kiwi / Traveloka / Airpaz。

2026-08-07 更新：
- browser_fetch 回傳格式改為「多個候選價格以 | 分隔」，prompt 配合說明，
  讓 LLM 從候選中挑選最符合路線與日期的票價，而非被迫接受單一可能誤判的金額
- 已有票價的列直接跳過（「已有數字就不要動」），避免自動查價失敗的備註覆蓋人工查價成果
- prompt 加入合理性檢查：國際線來回票價明顯過低（如低於 TWD 3,000）極可能是誤抓
  單日最低價 / 行李費 / 保險費等非票價金額，此時應回傳 null 而非硬挑最低候選

Notion API 呼叫沿用 crawler.py 既有 helper，不重新發明一份 Notion API 邏輯。

Required secrets:
  NOTION_TOKEN
  TRIP_DATABASE_ID
  GROQ_API_KEY      <- Groq Console 取得 (https://console.groq.com/keys)；未設定時降級為只存原始擷取文字
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

from crawler import (
    env,
    find_child_databases,
    http_json,
    notion_text,
    parse_journey,
    query_all_pages,
    text_prop,
)
from browser_fetch import PLATFORM_CONFIG, fetch_price

GROQ_MODEL = "llama-3.3-70b-versatile"
WRITE_DELAY_SEC = 0.4


def _run_prompt(client: Any, prompt: str, retries: int = 3) -> dict:
    """呼叫 Groq；429 / rate limit 遞增等待重試 3 次。"""
    for attempt in range(retries):
        try:
            response = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.2,
            )
            text = response.choices[0].message.content.strip()
            return json.loads(text)
        except Exception as e:
            if attempt < retries - 1 and ("429" in str(e) or "rate" in str(e).lower()):
                wait = 10 * (attempt + 1)
                print("  Groq rate limited, waiting " + str(wait) + "s before retry " + str(attempt + 2) + "/" + str(retries) + "...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Groq _run_prompt retries exhausted")


def normalize_price(client: Any, platform: str, raw_text: str, route_hint: str) -> Dict[str, Any]:
    """把 browser_fetch 擷取到的原始價格文字正規化成結構化 JSON；LLM 失敗時降級保留原始文字，不中斷整批。"""
    prompt = (
        "你是機票比價資料正規化助手。以下是從訂票網站「" + platform + "」頁面實際擷取到的原始文字"
        "（可能包含多個以「 | 」分隔的候選價格、雜訊或不完整資訊），請判斷其中最相關的票價，並以 JSON 回傳結構化結果（notes 請用繁體中文）：\n\n"
        "原始擷取文字：\n" + raw_text[:1500] + "\n\n"
        "路線與需求提示：" + route_hint + "\n\n"
        "判斷重點：\n"
        "- 若原始文字以「 | 」分隔多個候選價格，請從中挑選最符合上述路線與日期需求的機票票價；留意排除行李加價、保險費、訂閱費等非票價金額\n"
        "- 找出最符合上述路線與日期需求的票價數字與幣別（若原始文字包含多個價格，選最低或最相關的一個）\n"
        "- 合理性檢查：國際線來回機票若換算後低於約 TWD 3,000，極可能是誤抓到非票價金額（日曆單日最低價、行李費、保險費、訂閱費等），"
        "此時 price_local 與 price_twd 應回傳 null、confidence 設為 0，並在 notes 說明懷疑誤抓的原因\n"
        "- 換算為台幣（price_twd）；若原始幣別已是 TWD 則直接帶入，否則用你所知的約略匯率換算並在 notes 註明換算依據\n"
        "- is_dynamic_estimate：若原始文字明顯只是「起價」「日曆最低價」等估計值，而非該確切航班／日期的即時完整報價，標記為 true\n\n"
        "請回傳純 JSON（不要 markdown code block）：\n"
        "{\n"
        "  \"platform\": \"" + platform + "\",\n"
        "  \"price_local\": 原始幣別下的數字（找不到則 null）,\n"
        "  \"currency\": \"原始幣別代碼，例如 TWD/USD/THB/JPY（找不到則 null）\",\n"
        "  \"price_twd\": 換算為台幣後的整數（找不到則 null）,\n"
        "  \"confidence\": 0到5的整數（0=完全無法判斷, 5=非常確定的即時報價）,\n"
        "  \"is_dynamic_estimate\": true 或 false,\n"
        "  \"notes\": \"1-2句繁體中文說明，含換算依據與資料品質備註\"\n"
        "}"
    )
    try:
        data = _run_prompt(client, prompt)
        data.setdefault("platform", platform)
        return data
    except Exception as e:
        print("  [" + platform + "] Groq normalize failed (" + str(e) + "); falling back to raw text in notes.")
        return {
            "platform": platform,
            "price_local": None,
            "currency": None,
            "price_twd": None,
            "confidence": 0,
            "is_dynamic_estimate": True,
            "notes": "LLM 正規化失敗，顯示原始擷取值：" + raw_text[:300],
        }


def get_database_properties(database_id: str) -> Set[str]:
    """查詢子資料庫現有 schema 的屬性名稱；寫入前用來檢查欄位是否存在。"""
    try:
        data = http_json("GET", "https://api.notion.com/v1/databases/" + database_id)
        return set(data.get("properties", {}).keys())
    except Exception as e:
        print("  WARNING: cannot read database schema for " + database_id + " (" + str(e) + "); assuming minimal schema.", file=sys.stderr)
        return set()


def route_hint_text(info: Dict[str, str]) -> str:
    return info.get("origin", "") + " → " + info.get("destination", "") + "，" + info.get("start", "") + " 至 " + info.get("end", "") + "，來回，直飛"


def build_note(result: Dict[str, Any]) -> str:
    parts = []
    if result.get("price_local") is not None and result.get("currency"):
        parts.append("原始報價：" + str(result["currency"]).upper() + " " + str(result["price_local"]))
    parts.append("confidence=" + str(result.get("confidence", 0)))
    if result.get("is_dynamic_estimate"):
        parts.append("⚠️ 非即時精確報價，僅供參考")
    if result.get("notes"):
        parts.append(str(result["notes"]))
    return "Playwright + Groq 自動查價 | " + " | ".join(parts)


def process_price_db(client: Optional[Any], price_db_id: str, info: Dict[str, str]) -> int:
    rows = query_all_pages(price_db_id)
    schema_props = get_database_properties(price_db_id)
    has_fetched_at = "Fetched At" in schema_props
    has_notified = "Notified" in schema_props
    if not has_fetched_at or not has_notified:
        missing = [name for name, present in [("Fetched At", has_fetched_at), ("Notified", has_notified)] if not present]
        print("  Note: 票價網站資料 schema 目前缺少 " + " / ".join(missing) + "；沿用既有「查詢時間」欄位記錄擷取時間，缺少的欄位略過不寫（需要通知機制的話請於 Notion 手動新增這些屬性）。")

    route_hint = route_hint_text(info)
    now_iso = datetime.now(timezone.utc).isoformat()
    updated = 0

    for row in rows:
        try:
            site_title = text_prop(row, "網站名稱")
            site_name = site_title.split("｜")[0].strip() if "｜" in site_title else site_title
            if site_name not in PLATFORM_CONFIG:
                continue

            # 「已有數字就不要動」：票價欄已有值的列直接跳過，
            # 避免自動查價失敗的備註覆蓋人工 / AI 已查到的價格成果（2026-08-07 新增）。
            existing_price = row.get("properties", {}).get("票價", {}).get("number")
            if existing_price is not None:
                print("  [" + site_name + "] skip (已有票價 " + str(existing_price) + "，保留既有查價成果)")
                continue

            booking_url = text_prop(row, "訂票連結")
            if not booking_url or "{" in booking_url or "}" in booking_url:
                print("  [" + site_name + "] skip (missing/malformed 訂票連結)")
                continue

            print("  [" + site_name + "] fetching via browser_fetch...")
            fetched = fetch_price(site_name, booking_url)

            if fetched is None:
                result: Dict[str, Any] = {
                    "platform": site_name,
                    "price_local": None,
                    "currency": None,
                    "price_twd": None,
                    "confidence": 0,
                    "is_dynamic_estimate": True,
                    "notes": "browser_fetch 擷取失敗（頁面無法載入、逾時或比對不到價格文字），維持待查價狀態。",
                }
            elif client is None:
                result = {
                    "platform": site_name,
                    "price_local": None,
                    "currency": None,
                    "price_twd": None,
                    "confidence": 0,
                    "is_dynamic_estimate": True,
                    "notes": "GROQ_API_KEY 未設定，未做 LLM 正規化，顯示原始擷取值：" + fetched["raw_text"][:300],
                }
            else:
                result = normalize_price(client, site_name, fetched["raw_text"], route_hint)

            props: Dict[str, Any] = {
                "查詢時間": {"date": {"start": now_iso}},
                "備註": notion_text(build_note(result)),
            }
            if has_fetched_at:
                props["Fetched At"] = {"date": {"start": now_iso}}
            if has_notified:
                props["Notified"] = {"checkbox": False}

            price_twd = result.get("price_twd")
            if isinstance(price_twd, (int, float)):
                props["票價"] = {"number": price_twd}
                # 票價欄位一律存「換算後 TWD 數字」；原始幣別只保留在備註，避免出現 6800 USD 這種誤導。
                props["幣別"] = {"select": {"name": "TWD"}}
            elif "幣別" in schema_props:
                props["幣別"] = {"select": {"name": "TWD"}}

            http_json("PATCH", "https://api.notion.com/v1/pages/" + row["id"], {"properties": props})
            time.sleep(WRITE_DELAY_SEC)
            updated += 1
            print("  [" + site_name + "] -> price_twd=" + str(price_twd) + " confidence=" + str(result.get("confidence")))
        except Exception as e:
            print("  ERROR: row " + row.get("id", "?") + " failed, skipping (" + str(e) + ")", file=sys.stderr)
            continue

    return updated


def main() -> int:
    database_id = env("TRIP_DATABASE_ID") or env("NOTION_DATABASE_ID")
    if not database_id:
        print("Missing TRIP_DATABASE_ID", file=sys.stderr)
        return 2

    api_key = env("GROQ_API_KEY")
    client = None
    if not api_key:
        print("WARNING: No GROQ_API_KEY, will store raw browser_fetch text without LLM normalization.", file=sys.stderr)
    else:
        from groq import Groq
        client = Groq(api_key=api_key)

    journeys = query_all_pages(database_id)
    print("Found " + str(len(journeys)) + " journey task(s).")
    total_updated = 0

    for page in journeys:
        try:
            info = parse_journey(page)
            print("Journey: " + info["title"])
            dbs = find_child_databases(page["id"])
            price_db = dbs.get("票價網站資料")
            if not price_db:
                print("  Skip: no child database named 票價網站資料")
                continue
            count = process_price_db(client, price_db, info)
            print("  Updated " + str(count) + " row(s).")
            total_updated += count
        except Exception as e:
            print("  ERROR: journey page " + page.get("id", "?") + " failed, skipping (" + str(e) + ")", file=sys.stderr)
            continue

    print("Done. Total updated: " + str(total_updated) + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
