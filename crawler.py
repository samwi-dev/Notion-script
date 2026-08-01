#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Notion-script 自動爬蟲 v2 — 設定驅動多爬蟲

流程：
1. 讀取 Notion「爬蟲設定 Crawler Config」資料庫中 Status = 啟用 的爬蟲
2. 每隻爬蟲依「來源類型」搜尋：YouTube / Google News / RSS / 特定網站
3. 「關鍵字」觸發：標題含任一關鍵字才收錄
4. 過濾 seen_urls.json 已抓過的連結
5. 寫入該爬蟲指定的「目的地 Database」（自動適配目的地欄位結構）
6. 回寫「上次執行」時間
"""

import datetime
import html
import json
import os
import re
import urllib.parse
import urllib.request

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
CONFIG_DB_ID = os.environ.get("CONFIG_DB_ID", "").strip()
CONFIG_DB_NAME = "爬蟲設定 Crawler Config"
NOTION_VERSION = "2022-06-28"
STATE_FILE = "seen_urls.json"
MAX_PER_KEYWORD = 8
MAX_ITEMS_PER_CRAWLER = 30
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# ---------- Notion API ----------

def notion_api(path, payload=None, method=None):
    url = "https://api.notion.com/v1" + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    if method is None:
        method = "POST" if data is not None else "GET"
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + NOTION_TOKEN)
    req.add_header("Notion-Version", NOTION_VERSION)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def plain(prop, key):
    return "".join(t.get("plain_text", "") for t in ((prop or {}).get(key) or []))


def find_database_id(name):
    res = notion_api("/search", {
        "query": name,
        "filter": {"value": "database", "property": "object"},
    })
    for item in res.get("results", []):
        title = "".join(t.get("plain_text", "") for t in item.get("title", []))
        if title.strip() == name:
            return item["id"]
    raise RuntimeError("找不到資料庫「%s」，請確認已分享給 integration" % name)


def extract_db_id(url):
    """從 Notion 連結中取出資料庫 ID。"""
    base = url.split("?")[0].replace("-", "").lower()
    matches = re.findall(r"[0-9a-f]{32}", base)
    if not matches:
        raise RuntimeError("無法從連結解析資料庫 ID：" + url)
    h = matches[-1]
    return "-".join([h[0:8], h[8:12], h[12:16], h[16:20], h[20:32]])


def get_crawlers(db_id):
    res = notion_api("/databases/%s/query" % db_id, {
        "filter": {"property": "Status", "select": {"equals": "啟用"}},
    })
    crawlers = []
    for page in res.get("results", []):
        p = page["properties"]
        crawlers.append({
            "page_id": page["id"],
            "name": plain(p.get("爬蟲名稱"), "title"),
            "topic": plain(p.get("主題"), "rich_text").strip(),
            "sources": [o["name"] for o in ((p.get("來源類型") or {}).get("multi_select") or [])],
            "urls": [u.strip() for u in re.split(r"[,\n]", plain(p.get("自訂網址"), "rich_text")) if u.strip()],
            "keywords": [k.strip() for k in plain(p.get("關鍵字"), "rich_text").replace("\uff0c", ",").split(",") if k.strip()],
            "dest": (p.get("目的地 Database") or {}).get("url") or "",
        })
    return crawlers


# ---------- 來源抓取 ----------

def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "ignore")


def match_any(title, keywords):
    if not keywords:
        return True
    low = title.lower()
    return any(k.lower() in low for k in keywords)


def search_youtube(query, limit=MAX_PER_KEYWORD):
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


def parse_rss(xml, source_label, limit=20):
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
                "source": source_label,
            })
    return results


def search_google_news(query, limit=MAX_PER_KEYWORD):
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(query)
           + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
    return parse_rss(http_get(url), "Google News", limit)


def fetch_rss(url, limit=20):
    label = urllib.parse.urlparse(url).netloc or "RSS"
    return parse_rss(http_get(url), label, limit)


def fetch_site_links(url, keywords, limit=20):
    """特定網站：抓頁面上標題含關鍵字的連結。"""
    page = http_get(url)
    label = urllib.parse.urlparse(url).netloc or "Web"
    out, seen = [], set()
    for href, text in re.findall(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', page, re.S):
        text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        if not text or len(text) < 8:
            continue
        if not match_any(text, keywords):
            continue
        full = urllib.parse.urljoin(url, href)
        if full in seen:
            continue
        seen.add(full)
        out.append({"title": text, "url": full, "published": "", "source": label})
        if len(out) >= limit:
            break
    return out


def collect(crawler):
    kws = crawler["keywords"] or ([crawler["topic"]] if crawler["topic"] else [crawler["name"]])
    items = []
    sources = crawler["sources"] or ["YouTube"]
    for src in sources:
        try:
            if src == "YouTube":
                for kw in kws:
                    items += search_youtube(kw)
            elif src == "Google News":
                for kw in kws:
                    items += search_google_news(kw)
            elif src == "RSS":
                for u in crawler["urls"]:
                    items += [i for i in fetch_rss(u) if match_any(i["title"], kws)]
            elif src == "特定網站":
                for u in crawler["urls"]:
                    items += fetch_site_links(u, kws)
            else:
                print("[%s] 不支援的來源類型：%s" % (crawler["name"], src))
        except Exception as e:
            print("[%s] %s 抓取失敗：%s" % (crawler["name"], src, e))
    seen, out = set(), []
    for i in items:
        if i["url"] not in seen:
            seen.add(i["url"])
            out.append(i)
    return out[:MAX_ITEMS_PER_CRAWLER]


# ---------- 寫入 Notion ----------

def build_properties(dest_schema, title, topic, summary, today):
    """依目的地資料庫實際欄位自動適配，只寫存在的欄位。"""
    props = {}
    for name, spec in dest_schema.get("properties", {}).items():
        t = spec.get("type")
        if t == "title":
            props[name] = {"title": [{"text": {"content": title}}]}
        elif t == "date" and name in ("Crawled", "抓取日期"):
            props[name] = {"date": {"start": today}}
        elif t == "rich_text" and name in ("Summary", "摘要"):
            props[name] = {"rich_text": [{"text": {"content": summary}}]}
        elif t == "select" and name in ("Category", "主題") and topic:
            props[name] = {"select": {"name": topic}}
    return props


def create_digest(dest_db_id, crawler, items, today):
    dest_schema = notion_api("/databases/" + dest_db_id)
    label = crawler["topic"] or crawler["name"]
    title = "Notion Script 彙整 — %s — %s（共 %d 筆）" % (label, today, len(items))
    summary = "自動彙整 %d 筆。關鍵字：%s" % (
        len(items), "、".join(crawler["keywords"]) or label)
    props = build_properties(dest_schema, title, crawler["topic"], summary, today)
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
        "parent": {"database_id": dest_db_id},
        "properties": props,
        "children": children[:90],
    })
    return title


def main():
    config_id = CONFIG_DB_ID or find_database_id(CONFIG_DB_NAME)

    seen = set()
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            seen = set(json.load(f))

    today = datetime.date.today().isoformat()
    crawlers = get_crawlers(config_id)
    print("啟用中的爬蟲共 %d 隻" % len(crawlers))

    for crawler in crawlers:
        if not crawler["dest"]:
            print("[%s] 未設定目的地 Database，跳過" % crawler["name"])
            continue
        try:
            dest_db_id = extract_db_id(crawler["dest"])
        except Exception as e:
            print("[%s] %s" % (crawler["name"], e))
            continue

        items = collect(crawler)
        new_items = [i for i in items if i["url"] not in seen]
        if not new_items:
            print("[%s] 沒有新內容" % crawler["name"])
        else:
            try:
                title = create_digest(dest_db_id, crawler, new_items, today)
                seen.update(i["url"] for i in new_items)
                print("[%s] 已建立：%s" % (crawler["name"], title))
            except Exception as e:
                print("[%s] 寫入失敗：%s" % (crawler["name"], e))
                continue

        try:
            notion_api("/pages/" + crawler["page_id"], {
                "properties": {"上次執行": {"date": {"start": today}}},
            }, method="PATCH")
        except Exception as e:
            print("[%s] 更新上次執行時間失敗：%s" % (crawler["name"], e))

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
