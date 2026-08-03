#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ai_price.py - samwi-dev/Notion-script / crawlers/trip_price

Flow:
  1. Find rows in ticket_price_db where price is empty
  2. DuckDuckGo HTML search (pure Python stdlib, no extra packages)
  3. Regex extract price numbers, convert to TWD
  4. Backfill price / note / query_time into Notion

Required secrets:
  NOTION_TOKEN
  TRIP_DATABASE_ID
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
from typing import Any, Dict, List, Optional, Tuple

NOTION_VERSION = "2022-06-28"
WRITE_DELAY_SEC = 0.4
SEARCH_DELAY_SEC = 3.0

USD_TO_TWD = 32.4
THB_TO_TWD = 0.9


# --- Notion helpers ---

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


def http_json(method: str, url: str, payload: Optional[dict] = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=notion_headers())
    try:
        with urllib.request.urlopen(req, timeout=45) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:400]
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
        data = http_json(
            "POST",
            f"https://api.notion.com/v1/databases/{database_id}/query",
            payload,
        )
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
    http_json(
        "PATCH",
        f"https://api.notion.com/v1/pages/{page_id}",
        {"properties": props},
    )
    time.sleep(WRITE_DELAY_SEC)


# --- DuckDuckGo search (stdlib only) ---

def ddg_search(query: str, max_results: int = 8) -> str:
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept-Language": "zh-TW,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            html = res.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"search failed: {e}"

    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S)
    titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', html, re.S)
    tag_re = re.compile(r"<[^>]+>")

    def clean(s: str) -> str:
        return tag_re.sub("", s).strip()

    parts = []
    for i, (t, s) in enumerate(zip(titles, snippets)):
        if i >= max_results:
            break
        parts.append(f"[{i+1}] {clean(t)} -- {clean(s)}")
    return "\n".join(parts) if parts else "(no results)"


# --- Price extraction via regex ---

def extract_price_twd(text: str) -> Tuple[Optional[int], str]:
    """Find the lowest plausible flight price in text, convert to TWD."""
    candidates: List[Tuple[int, str, str]] = []  # (twd, original, currency)

    # NT$ / TWD / NTD / Chinese
    for m in re.finditer(
        r"(?:NT\$|TWD|NTD|\u65b0\u53f0\u5e63|\u53f0\u5e63)[\s]*([\d,]+)", text
    ):
        val = int(m.group(1).replace(",", ""))
        if 3000 <= val <= 200000:
            candidates.append((val, m.group(0), "TWD"))

    # THB
    for m in re.finditer(
        r"(?:THB|\u6cf0\u9296|\u0e3f)[\s]*([\d,]+)", text
    ):
        val = int(m.group(1).replace(",", ""))
        twd = int(val * THB_TO_TWD)
        if 3000 <= twd <= 200000:
            candidates.append(
                (twd, f"{m.group(0)} (~TWD {twd:,}, rate 1 THB=0.9 TWD)", "THB")
            )

    # USD / US$
    for m in re.finditer(r"(?:USD|US\$|\$)[\s]*([\d,]+(?:\.\d+)?)", text):
        try:
            val = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        twd = int(val * USD_TO_TWD)
        if 3000 <= twd <= 200000:
            candidates.append(
                (twd, f"{m.group(0)} (~TWD {twd:,}, rate 1 USD=32.4 TWD)", "USD")
            )

    # Bare numbers near price keywords
    price_kw = re.compile(
        r"(?:price|fare|\u7968\u50f9|\u6a5f\u7968|\u8cbb\u7528|\u8d77|from|\u5143|\u5e63)",
        re.IGNORECASE,
    )
    for m in re.finditer(r"\b([1-9]\d{3,5})\b", text):
        val = int(m.group(1))
        if 3000 <= val <= 80000:
            start = max(0, m.start() - 80)
            end = min(len(text), m.end() + 80)
            if price_kw.search(text[start:end]):
                candidates.append(
                    (val, f"{val} (inferred TWD from context)", "TWD-inferred")
                )

    if not candidates:
        return None, "regex: no price found in search snippets"

    candidates.sort(key=lambda x: x[0])
    best = candidates[0]
    return best[0], f"\u539f\u59cb\u5831\u50f9: {best[1]} | \u5e63\u5225: {best[2]}"


# --- Core logic ---

def process_price_db(price_db_id: str, journey_info: Dict[str, str]) -> int:
    rows = query_all_pages(price_db_id)
    updated = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for row in rows:
        # Skip rows that already have a price
        price_val = row.get("properties", {}).get("\u7968\u50f9", {}).get("number")
        if price_val is not None:
            continue

        site_title = text_prop(row, "\u7db2\u7ad9\u540d\u7a31")
        site_name = (
            site_title.split("\uff5c")[0].strip() if "\uff5c" in site_title else site_title
        )

        query = (
            f"{site_name} "
            f"{journey_info['origin_city']} {journey_info['dest_city']} "
            f"\u6a5f\u7968 {journey_info['start']} \u76f4\u98db \u542b\u6258\u904b\u884c\u674e \u50f9\u683c"
        )
        print(f"    [{site_name}] searching...")
        snippets = ddg_search(query)
        time.sleep(SEARCH_DELAY_SEC)

        price_twd, note = extract_price_twd(snippets)

        props: Dict[str, Any] = {
            "\u67e5\u8a62\u6642\u9593": {"date": {"start": now_iso}},
            "\u5099\u8a3b": {"rich_text": [{"text": {"content": note[:2000]}}]},
        }
        if price_twd is not None:
            props["\u7968\u50f9"] = {"number": price_twd}
            props["\u5e63\u5225"] = {"select": {"name": "TWD"}}
            print(f"      -> TWD {price_twd:,}")
        else:
            print(f"      -> no price ({note})")

        page_id = row["id"]
        try:
            update_page_props(page_id, props)
            updated += 1
        except Exception as e:
            print(f"      WARNING backfill failed: {e}")

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

        # Parse city names for search queries
        origin_city = "\u53f0\u5317"
        dest_city = "\u6e05\u9081"
        if "\u2192" in from_to:
            left, right = from_to.split("\u2192", 1)
            origin_city = left.strip()
            dest_city = right.strip()

        info: Dict[str, str] = {
            "title": title,
            "origin_city": origin_city,
            "dest_city": dest_city,
            "start": start,
            "end": end,
        }

        print(f"Journey: {title}")
        dbs = find_child_databases(page["id"])
        price_db = dbs.get("\u7968\u50f9\u7db2\u7ad9\u8cc7\u6599")
        if not price_db:
            print("  Skip: no child database named \u7968\u50f9\u7db2\u7ad9\u8cc7\u6599")
            continue

        count = process_price_db(price_db, info)
        print(f"  Updated {count} row(s).")
        total_updated += count

    print(f"Done. Total updated: {total_updated}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
