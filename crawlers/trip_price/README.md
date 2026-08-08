# Trip Price Crawler

自動維護機票／飯店比價任務結構，並針對 Notion AI Agent 查不到精確價的動態渲染網站，
用 Playwright 實際開頁擷取價格後交給 Groq 正規化，回填 Notion。

## 運作方式（三步驟）

1. **Step 1（`crawler.py`）** — 讀取 Trip database 的每個 Journey Task；確保「航班追蹤」與
   「票價網站資料」子資料庫存在，產生各平台正確的訂票深連結，標記為待查價。
2. **Step 2（Notion AI Agent）** — 用 `crawler.py --touch-trigger` 戳動「上次觸發時間」屬性，交由
   Notion 內建 AI Agent 補查、修正票價，作為輔助手段，維持原本節省 Notion AI credit 的排程
   （每週一或手動觸發）。
3. **Step 3（`browser_fetch.py` + `ai_price.py`）** — 15 個平台（Google Flights、Skyscanner、
   Trip.com、KAYAK、Expedia、momondo、Booking.com、ezTravel 易遊網、EVA Air 長榮航空、
   China Airlines 中華航空、STARLUX Airlines 星宇航空、AirAsia 亞洲航空集團、Kiwi.com、
   Traveloka、Airpaz）全部合併不分工，由 GitHub Actions 全部嘗試查價：Google Flights 優先用
   `fast_flights_fetch.py`（fast-flights 套件）直接取結構化報價，失敗才 fallback 到跟其他平台
   一樣的 `browser_fetch.py` 流程 —— 優先用 patchright、import 失敗才 fallback playwright，
   實際開啟訂票深連結擷取價格文字，再由 `ai_price.py` 呼叫 Groq 正規化成結構化 JSON 回填 Notion。

## ⚠️ Amadeus API 已廢棄

`ai_price.py` 舊版使用 Amadeus Flight Offers Search API 查價，但該路徑從未被
`.github/workflows/trip-price-crawler.yml` 呼叫過，且 **Amadeus API 已於 2026-07-17 停用**。
`ai_price.py` 已改寫為 Playwright + Groq 架構，不再使用 Amadeus，`AMADEUS_API_KEY` /
`AMADEUS_API_SECRET` 兩個舊 secret 名稱已不再需要（歷史備註，非需要設定的 secrets）。

## 必要 Secrets 設定

在 GitHub Repo → Settings → Secrets and variables → Actions 新增：

| Secret 名稱 | 說明 |
|---|---|
| `NOTION_TOKEN` | Notion Integration Token |
| `TRIP_DATABASE_ID` | Notion Trip 資料庫 ID |
| `GROQ_API_KEY` | Groq API Key，用於 Step 3 價格文字正規化（於 [console.groq.com/keys](https://console.groq.com/keys) 免費取得） |
| `SCRAPE_PROXY_URL`（選填） | 代理伺服器位址；未設定時行為與不用代理完全相同 |
| `SCRAPE_PROXY_USERNAME`（選填，可選加開） | 代理伺服器帳號，`SCRAPE_PROXY_URL` 需要帳密驗證時才設定 |
| `SCRAPE_PROXY_PASSWORD`（選填，可選加開） | 代理伺服器密碼，`SCRAPE_PROXY_URL` 需要帳密驗證時才設定 |

未設定 `GROQ_API_KEY` 時，Step 3 仍會執行 `browser_fetch.py` 擷取，但只會把原始擷取文字存進「備註」，
不做 LLM 正規化（降級行為，不會讓整個 workflow 失敗）。

## 本機測試 browser_fetch.py

```bash
pip install playwright patchright fast-flights
playwright install chromium
patchright install chromium
python crawlers/trip_price/browser_fetch.py "Google Flights" "https://www.google.com/travel/flights?q=..."
```

## 觸發方式

```text
手動觸發：github.com → Actions → Trip Price Task Builder → Run workflow
自動排程：cron: "0 1 * * *"（UTC 01:00 = 台北時間 09:00，每天執行）
```
