# Notion-script

機票／飯店比價爬蟲：依照 Notion「Trip」資料庫中的行程需求（出發地、目的地、日期、直飛／行李規則），
自動維護各比價平台的查價任務，並將查到的票價回填到 Notion。

## 運作方式

專案主體在 `crawlers/trip_price/`，由 GitHub Actions 排程執行（每天 09:00 台北時間，也可手動觸發），
分三步驟：

1. **Step 1（`crawler.py`）** — 讀取 Trip database 的每個 Journey Task，建立「航班追蹤」「票價網站
   資料」子資料庫結構，產生各平台正確的訂票深連結。
2. **Step 2（Notion AI Agent）** — 對 Notion AI Agent 能查到精確價的平台（Kiwi.com、Traveloka、
   Airpaz、EVA Air 等），戳動觸發屬性請 Notion 內建 AI Agent 查價回填。
3. **Step 3（`browser_fetch.py` + `ai_price.py`）** — 對 Notion AI Agent 查不到精確價的動態渲染
   平台（Google Flights、Skyscanner、Trip.com、KAYAK、Expedia、momondo、Booking.com、
   ezTravel、中華航空、星宇航空、AirAsia），用 Playwright headless Chromium 實際開頁擷取價格文字，
   再用 Groq 正規化成結構化資料寫回 Notion。

詳細說明見 [`crawlers/trip_price/README.md`](crawlers/trip_price/README.md) 與
[`docs/trip-price-crawler.md`](docs/trip-price-crawler.md)。

## 設定步驟（只需做一次）

### 1. 建立 Notion Integration

1. 前往 notion.so/my-integrations → **New integration**
2. 名稱例如 `Notion-script`，workspace 選你的工作區
3. 複製 **Internal Integration Secret**（`ntn_` 開頭）

### 2. 把 Trip 資料庫分享給 Integration

開啟 Trip 資料庫頁面 → 右上角 **⋯** → **Connections / 連接** → 搜尋並加入剛建立的 integration。

### 3. 設定 GitHub Secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**：

| Secret 名稱 | 必填 | 說明 |
|---|---|---|
| `NOTION_TOKEN` | ✅ | Notion Integration Secret（`ntn_` 開頭） |
| `TRIP_DATABASE_ID` | ✅ | Trip 資料庫 ID |
| `GROQ_API_KEY` | ✅（Step 3 用） | Groq API Key，用於價格文字正規化；未設定時 Step 3 仍會擷取但不做 LLM 正規化 |

> ⚠️ 舊版 `ai_price.py` 曾規劃用 Amadeus Flight Offers Search API 查價，但從未真正接入 workflow，
> 且該 API 已於 2026-07-17 停用。目前已全面改用 Playwright + Groq，`AMADEUS_API_KEY` /
> `AMADEUS_API_SECRET` 為已廢棄的舊 secret 名稱，不需要設定。

### 4. 測試執行

1. Repo → **Actions** 分頁 → 啟用 workflows
2. 選 **Trip Price Task Builder** → **Run workflow** 手動執行
3. 執行成功後，到 Notion Trip 資料庫檢查各 Journey Task 的子資料庫是否已更新

## 檔案結構

```
crawlers/trip_price/crawler.py        # Step 1：結構維護、訂票深連結產生
crawlers/trip_price/browser_fetch.py  # Step 3：Playwright 開頁擷取價格文字
crawlers/trip_price/ai_price.py       # Step 3：Groq 正規化 + 回填 Notion
.github/workflows/trip-price-crawler.yml  # 排程設定（每天 09:00 台北時間）
```

## 調整排程

編輯 `.github/workflows/trip-price-crawler.yml` 中的 cron（UTC 時間，台北時間 −8 小時）：

```yaml
schedule:
  - cron: "0 1 * * *"   # UTC 01:00 = 台北 09:00
```
