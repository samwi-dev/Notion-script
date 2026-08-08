# Trip Price Crawler

本 crawler 是 `samwi-dev/Notion-script` repo 的一部分：`crawlers/trip_price/`。
用途：維護 Notion Trip 資料庫裡每個行程（Journey Task）的比價任務結構，並自動查價回填。

## 需求範例

- 出發地：台北 TPE / TSA
- 目的地：例如清邁 CNX、東京 TYO 等（依 Trip database 各筆 Journey Task 的 From-To / Nation 決定）
- 航班規則：直飛
- 行李規則：回程需含託運行李
- 每筆 Journey Task 皆會建立「航班追蹤」（追蹤各航空公司官網資訊）與「票價網站資料」（各比價平台報價）兩個子資料庫

## 運作方式（三步驟）

管線分三步，對應 `.github/workflows/trip-price-crawler.yml` 的三個 Step：

1. **Step 1 — `crawlers/trip_price/crawler.py`**：讀取 Trip database 的每個 Journey Task，
   確保「航班追蹤」「票價網站資料」子資料庫結構存在，產生各平台正確的訂票深連結。GitHub Actions
   不直接解析即時票價，只建立可查價的 task rows。
2. **Step 2 — Notion AI Agent**：`crawler.py --touch-trigger` 戳動「上次觸發時間」屬性，通知
   Notion 內建 AI Agent 補查、修正票價，作為輔助手段，不再固定負責特定平台。為節省 Notion AI
   credit，只在每週一或手動觸發時執行。
3. **Step 3 — `crawlers/trip_price/browser_fetch.py` + `fast_flights_fetch.py` + `ai_price.py`**：
   15 個平台（Google Flights、Skyscanner、Trip.com、KAYAK、Expedia、momondo、Booking.com、
   ezTravel 易遊網、EVA Air 長榮航空、China Airlines 中華航空、STARLUX Airlines 星宇航空、
   AirAsia 亞洲航空集團、Kiwi.com、Traveloka、Airpaz）全部合併不分工，由 GitHub Actions 全部
   嘗試查價。Google Flights 優先用 `fast_flights_fetch.py`（fast-flights 套件）直接取結構化
   報價，不必真的開頁；失敗才 fallback 到跟其他 14 個平台一樣的流程 —— 用 `browser_fetch.py`
   優先以 patchright（import 失敗才 fallback playwright）headless Chromium 實際開啟訂票深連結
   擷取價格文字，再用 Groq 正規化成結構化 JSON（platform / price_local / currency /
   price_twd / confidence / is_dynamic_estimate / notes）寫回 Notion。

完整平台清單與各平台深連結產生規則見 `crawlers/trip_price/crawler.py` 的 `FLIGHT_PRICE_SITES`；
Step 3 覆蓋的目標平台設定表見 `crawlers/trip_price/browser_fetch.py` 的 `PLATFORM_CONFIG`。

## ⚠️ Amadeus API 已廢棄

`ai_price.py` 舊版曾規劃使用 Amadeus Flight Offers Search API 查詢真實票價，但該路徑從未被
workflow 呼叫過，且 **Amadeus API 已於 2026-07-17 停用**。程式碼已全面改用 Playwright + Groq
架構（見上方 Step 3），不再依賴 Amadeus；`AMADEUS_API_KEY` / `AMADEUS_API_SECRET` 已是廢棄的
歷史 secret 名稱，不在目前需要設定的清單內。

## Secrets 設定

在 `samwi-dev/Notion-script` → Settings → Secrets and variables → Actions 設定：

| Secret | 必填 | 說明 |
|---|---:|---|
| `NOTION_TOKEN` | 是 | Notion Integration Token |
| `TRIP_DATABASE_ID` | 是 | Trip 資料庫 ID |
| `GROQ_API_KEY` | 是（Step 3 用） | Groq API Key，用於價格文字正規化；未設定時 Step 3 仍會執行 Playwright 擷取，但只存原始文字、不做 LLM 正規化 |
| `SCRAPE_PROXY_URL` | 否（選填） | 代理伺服器位址；未設定時行為與不用代理完全相同 |
| `SCRAPE_PROXY_USERNAME` | 否（選填，可選加開） | 代理伺服器帳號，`SCRAPE_PROXY_URL` 需要帳密驗證時才設定 |
| `SCRAPE_PROXY_PASSWORD` | 否（選填，可選加開） | 代理伺服器密碼，`SCRAPE_PROXY_URL` 需要帳密驗證時才設定 |

## Workflow

```text
.github/workflows/trip-price-crawler.yml
```

手動觸發：

1. 開啟 `samwi-dev/Notion-script`
2. 進入 Actions
3. 選擇 `Trip Price Task Builder`
4. 點擊 `Run workflow`
5. Branch：`main`

自動排程：`cron: "0 1 * * *"`（UTC 01:00 = 台北時間 09:00，每天執行）。
