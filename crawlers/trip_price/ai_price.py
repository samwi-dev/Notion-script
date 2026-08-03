#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ai_price.py — samwi-dev/Notion-script · crawlers/trip_price

用途：
  1. 對 Trip database 每個 Journey Task 頁面內的「票價網站資料」database，
     找出尚未有票價（票價欄位為空）的列。
  2. 用 DuckDuckGo HTML 搜尋取得該網站 + 路線的票價摘要（純 Python 標準庫）。
  3. 呼叫 GitHub Models inference API（workflow 內建 GITHUB_TOKEN，需 models: read 權限），
     讓 AI 從搜尋摘要中擷取票價並回傳 JSON。
  4. 把 TWD 票價、備註（含原始幣別與換算依據）、查詢時間回填進 Notion。

Required env:
  NOTION_TOKEN   — Notion integration secret
  TRIP_DATABASE_ID — Trip database ID
  GITHUB_TOKEN   — 由 GitHub Actions 自動注入，需要 models: read 權限
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
from typing import Any, Dict, List, Optional

NOTION_VERSION = "2022-06-28"
WRITE_DELAY_SEC = 0.4
SEARCH_DELAY_SEC = 2.0   # DuckDuckGo 之間的間隔，避免被封
GH_MODELS_API = "https://models.inference.ai.azure.com/chat/completions"
GH_MODEL = "gpt-4o-mini"
USD_TO_TWD = 32.4        # 換算匯率（每次執行固定值，備註中說明）
THB_TO_TWD = 0.9         # 1 THB ≈ 0.9 TWD 約略值


# ─── Notion helpers ─────────────────────────────────────────────────────────

def env(name: str) -> Optional[str]:
    v = os.environ.get(name)
    return v if v else None


def notion_headers() -> Dict[str, str]:
    token = env("NOTION_TOKEN")
    if not token:
        raise RuntimeError("Missing NOTION_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def http_json(method: str, url: str, payload: Optional[dict] = None,
              extra_headers: Optional[Dict[str, str]] = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = notion_headers() if "notion.com" in url else {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {body}") from e


def text_prop(page: dict, name: str) -> str:
    prop = page.get("properties", {}).get(name, {})
    typ = prop.get("type")
    if typ == "title":
        return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    if typ == "rich_text":
        return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
    if typ == "number":
        v = prop.get("number")
        return str(v) if v is not None else ""
    return ""


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
    http_json("PATCH", f"https://api.notion.com/v1/pages/{page_id}",
              {"properties": props})
    time.sleep(WRITE_DELAY_SEC)


# ─── DuckDuckGo search (stdlib only) ─────────────────────────────────────────

def ddg_search(query: str, max_results: int = 5) -> str:
    """回傳純文字摘要（各結果 title + snippet 合併）。"""
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; NotionPriceCrawler/1.0)",
        "Accept-Language": "zh-TW,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            html = res.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"搜尋失敗: {e}"

    # 粗略從 HTML 取出文字段落（不依賴 BeautifulSoup）
    import re
    # 取 result__snippet 內容
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S)
    titles = re.findall(r'class="result__title[^"]*"[^>]*>.*?<a[^>]*>(.*?)</a>', html, re.S)
    tag_re = re.compile(r'<[^>]+>')
    clean = lambda s: tag_re.sub('', s).strip()

    parts = []
    for i, (t, s) in enumerate(zip(titles, snippets)):
        if i >= max_results:
            break
        parts.append(f"[{i+1}] {clean(t)} — {clean(s)}")
    return "\n".join(parts) if parts else "（無搜尋結果）"


# ─── GitHub Models (AI inference) ─────────────────────────────────────────────

def ai_extract_price(site: str, route: str, date_range: str, snippets: str) -> Dict[str, Any]:
    """呼叫 GitHub Models API，從搜尋摘要擷取票價，回傳 dict。

    回傳格式 (JSON):
    {
      "price_twd": <int or null>,
      "original_price": "<原始金額 + 幣別>",
      "currency": "<TWD/USD/THB/...>",
      "exchange_rate": "<換算說明>",
      "note": "<備註，含是否含稅、行李、轉機等>"
    }
    """
    github_token = env("GITHUB_TOKEN")
    if not github_token:
        return {"price_twd": None, "note": "Missing GITHUB_TOKEN"}

    system_prompt = (
        "你是機票票價解析助手。從提供的搜尋摘要中，找出最接近指定路線與日期的票價。"
        "若找不到具體票價，price_twd 回傳 null。"
        "所有價格統一換算為新台幣（TWD），換算匯率：1 USD = 32.4 TWD，1 THB = 0.9 TWD。"
        "回傳純 JSON，不要加任何說明文字或 markdown。"
    )
    user_prompt = (
        f"比價網站：{site}\n"
        f"路線：{route}\n"
        f"日期：{date_range}\n\n"
        f"搜尋摘要：\n{snippets}\n\n"
        "請回傳 JSON：{\"price_twd\": <int or null>, \"original_price\": \"<金額+幣別>\", "
        "\"currency\": \"TWD\", \"exchange_rate\": \"<換算說明>\", \"note\": \"<備註>\"}"
    )

    payload = {
        "model": GH_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 300,
    }
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Content-Type": "application/json",
    }
    try:
        data = http_json("POST", GH_MODELS_API, payload, extra_headers=headers)
        content = data["choices"][0]["message"]["content"].strip()
        # 去除可能的 markdown code block
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        return json.loads(content)
    except Exception as e:
        return {"price_twd": None, "note": f"AI 解析失敗: {e}"}


# ─── Main ─────────────────────────────────────────────────────────────────────

def process_price_db(price_db_id: str, journey_info: Dict[str, str]) -> int:
    """找出票價為空的列，查價後回填。回傳回填筆數。"""
    rows = query_all_pages(price_db_id)
    updated = 0
    route = f"{journey_info['origin']} → {journey_info['destination']}"
    date_range = f"{journey_info['start']} – {journey_info['end']}"

    for row in rows:
        price_str = text_prop(row, "票價")
        if price_str:  # 已有票價，跳過
            continue

        site_title = text_prop(row, "網站名稱")
        # 從標題取出網站名稱（格式：「網站名稱｜路線｜日期」）
        site_name = site_title.split("｜")[0].strip() if "｜" in site_title else site_title

        print(f"    Searching: {site_name} ...")
        query = f"{site_name} {journey_info['origin']} {journey_info['destination']} 機票 {journey_info['start']} 含行李"
        snippets = ddg_search(query)
        time.sleep(SEARCH_DELAY_SEC)

        result = ai_extract_price(site_name, route, date_range, snippets)
        price_twd = result.get("price_twd")
        note_text = (
            f"原始報價：{result.get('original_price', 'N/A')}｜"
            f"換算：{result.get('exchange_rate', f'1 USD=32.4 TWD, 1 THB=0.9 TWD')}｜"
            f"{result.get('note', '')}"
        )[:2000]

        now_iso = datetime.now(timezone.utc).isoformat()
        props: Dict[str, Any] = {
            "查詢時間": {"date": {"start": now_iso}},
            "備註": {"rich_text": [{"text": {"content": note_text}}]},
        }
        if price_twd is not None:
            props["票價"] = {"number": int(price_twd)}
            props["幣別"] = {"select": {"name": "TWD"}}
            print(f"      → TWD {price_twd:,}")
        else:
            print(f"      → 無法取得票價（{result.get('note', '')}）")

        page_id = row["id"]
        try:
            update_page_props(page_id, props)
            updated += 1
        except Exception as e:
            print(f"      ⚠️  回填失敗: {e}")

    return updated


def main() -> int:
    database_id = env("TRIP_DATABASE_ID") or env("NOTION_DATABASE_ID")
    if not database_id:
        print("Missing TRIP_DATABASE_ID", file=sys.stderr)
        return 2

    journeys = query_all_pages(database_id)
    print(f"Found {len(journeys)} journey task(s).")
    total_updated = 0

    for page in journeys:
        props = page.get("properties", {})
        title_prop = props.get("Deal Title") or {}
        title = "".join(t.get("plain_text", "") for t in title_prop.get("title", []))
        print(f"Journey: {title}")

        trip_date = (props.get("Trip Date") or {}).get("date") or {}
        info = {
            "title": title,
            "origin": "TPE",
            "destination": "CNX",
            "start": trip_date.get("start") or "",
            "end": trip_date.get("end") or "",
        }

        dbs = find_child_databases(page["id"])
        price_db = dbs.get("票價網站資料")
        if not price_db:
            print("  Skip: no 票價網站資料 child database.")
            continue

        count = process_price_db(price_db, info)
        print(f"  Updated {count} row(s).")
        total_updated += count

    print(f"Done. Total updated: {total_updated}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
