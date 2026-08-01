#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Notion-script 自動爬蟲

流程：
1. 讀取 News List 資料庫中 Status = 追蹤中 的主題（Name / Keywords / Category）
2. 依關鍵字搜尋 YouTube 最新影片；失敗或無結果時改用 Google News RSS
3. 過濾 seen_urls.json 中已抓取過的連結
4. 每個主題有新內容時，在 News Monitor update 建立一筆彙整頁面
"""

import datetime
import html
import json
import os
import re
import urllib.parse
import urllib.request

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NEWS_LIST_DB_ID = os.environ.get("NEWS_LIST_DB_ID", "").strip()
MONITOR_DB_ID = os.environ.get("MONITOR_DB_ID", "").strip()
NOTION_VERSION = "2022-06-28"
STATE_FILE = "seen_urls.json"
MAX_PER_TOPIC = 10
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def notion_api(path, payload=None):
    url = "https://api.notion.com/v1" + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    req.add_header("Authorization", "Bearer " + NOTION_TOKEN)
    req.add_header("Notion-Version", NOTION_VERSION)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def find_database_id(name):
    res = notion_api("/search", {
        "query": name,
        "filter": {"value": "database", "property": "object"},
    })
    for item in res.get("results", []):
        title = "".join(t.get("plain_text", "") for t in item.get("title", []))
        if title.strip() == name:
            return item["id"]
    raise RuntimeError("找不到資料庫「%s」，請確認已將它分享給 integration" % name)


def get_tracking_topics(db_id):
    res = notion_api("/databases/%s/query" % db_id, {
        "filter": {"property": "Status", "select": {"equals": "追蹤中"}},
    })
    topics = []
    for page in res.get("results", []):
        props = page["properties"]
        name = "".join(t["plain_text"] for t in props.get("Name", {}).get("title", []))
        keywords = "".join(
            t["plain_text"] for t in (props.get("Keywords", {}).get("rich_text") or [])
        )
        category = (props.get("Category", {}).get("select") or {}).get("name")
        topics.append({
            "page_id": page["id"],
            "name": name,
            "keywords": keywords.strip(),
            "category": category,
        })
    return topics


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "ignore")


def search_youtube(query, limit=MAX_PER_TOPIC):
    """搜尋 YouTube（依上傳日期排序），回傳影片清單。"""
    url = ("https://www.youtube.com/results?search_query="
           + urllib.parse.quote(query) + "&sp=CAI%253D")
    page = http_get(url)
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", page, re.S)
    if not m:
        return []
    data = json.loads(m.group(1))
    videos = []

    def walk(node):
        if isinstance(node, dict):
            v = node.get("videoRenderer")
            if isinstance(v, dict) and v.get("videoId"):
                title = "".join(r.get("text", "") for r in v.get("title", {}).get("runs", []))
                published = v.get("publishedTimeText", {}).get("simpleText", "")
                videos.append({
                    "title": title,
                    "url": "https://www.youtube.com/watch?v=" + v["videoId"],
                    "published": published,
                    "source": "YouTube",
                })
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    seen, out = set(), []
    for v in videos:
        if v["url"] not in seen:
            seen.add(v["url"])
            out.append(v)
    return out[:limit]


def search_google_news(query, limit=MAX_PER_TOPIC):
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(query)
           + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
    xml = http_get(url)
    results = []
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S)[:limit]:
        t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S)
        l = re.search(r"<link>(.*?)</link>", item, re.S)
        p = re.search(r"<pubDate>(.*?)</pubDate>", item, re.S)
        if t and l:
            results.append({
                "title": html.unescape(t.group(1).strip()),
                "url": l.group(1).strip(),
                "published": p.group(1).strip() if p else "",
                "source": "Google News",
            })
    return results


def create_digest(monitor_db_id, topic, items, today):
    label = topic["category"] or topic["name"]
    title = "Notion Script 影片彙整 — %s — %s（共 %d 支）" % (label, today, len(items))
    properties = {
        "Name": {"title": [{"text": {"content": title}}]},
        "Crawled": {"date": {"start": today}},
        "Summary": {"rich_text": [{"text": {"content": "自動彙整 %d 筆。搜尋關鍵字：%s" % (len(items), topic["keywords"] or topic["name"])}}]},
        "News List": {"relation": [{"id": topic["page_id"]}]},
    }
    if topic["category"]:
        properties["Category"] = {"select": {"name": topic["category"]}}
    children = []
    for item in items:
        suffix = " ・ ".join(x for x in [item["source"], item["published"]] if x)
        text = item["title"] + ("（" + suffix + "）" if suffix else "")
        children.append({
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {
                "rich_text": [{
                    "type": "text",
                    "text": {"content": text[:1900], "link": {"url": item["url"]}},
                }],
            },
        })
    notion_api("/pages", {
        "parent": {"database_id": monitor_db_id},
        "properties": properties,
        "children": children[:90],
    })
    return title


def main():
    news_list_id = NEWS_LIST_DB_ID or find_database_id("News List")
    monitor_id = MONITOR_DB_ID or find_database_id("News Monitor update")

    seen = set()
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            seen = set(json.load(f))

    today = datetime.date.today().isoformat()
    topics = get_tracking_topics(news_list_id)
    print("追蹤中的主題共 %d 個" % len(topics))

    for topic in topics:
        query = topic["keywords"] or topic["name"]
        items = []
        try:
            items = search_youtube(query)
        except Exception as e:
            print("[%s] YouTube 搜尋失敗：%s" % (topic["name"], e))
        if not items:
            try:
                items = search_google_news(query)
            except Exception as e:
                print("[%s] Google News 搜尋失敗：%s" % (topic["name"], e))
        new_items = [i for i in items if i["url"] not in seen]
        if not new_items:
            print("[%s] 沒有新內容" % topic["name"])
            continue
        title = create_digest(monitor_id, topic, new_items, today)
        seen.update(i["url"] for i in new_items)
        print("[%s] 已建立：%s" % (topic["name"], title))

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
