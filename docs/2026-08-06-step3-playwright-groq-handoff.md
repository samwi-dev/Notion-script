# 2026-08-06 交接:GitHub Actions Step 3 上線(Playwright + Groq)

給 Notion AI Agent 與後續維護者看的異動說明。對應 Notion 頁面已同步更新:[子 Skill:Trip 便宜機票與飯店酒店爬蟲設定](https://app.notion.com/p/1d08a2d6d0f44395ad7a0adba0600876)。

## 改動了什麼

原本三步驟只有 Step 1(結構維護)+ Step 2(戳時間戳通知 Notion AI Agent 查價)。查價/分析這一步實際上從沒被程式碼執行過——`ai_price.py` 用的 Amadeus API 早已於 2026-07-17 停用,而且 workflow 從未呼叫它。

新增 **Step 3**:
- `crawlers/trip_price/browser_fetch.py` — Playwright headless Chromium 開啟真實訂票深連結,擷取頁面上的價格文字。
- `crawlers/trip_price/ai_price.py`(重寫) — 用 Groq 把擷取到的原始文字正規化成結構化 JSON(price_local/currency/price_twd/confidence/is_dynamic_estimate/notes),寫回 Notion「票價網站資料」子資料庫。

## 分工怎麼變

15 個平台的查價責任重新分配:

| 平台 | 負責方 | 備註 |
|---|---|---|
| Google Flights | GitHub Actions Step 3 | ✅ 已實測成功擷取真實價格 |
| Trip.com | GitHub Actions Step 3 | ✅ 已實測成功擷取真實價格 |
| Skyscanner / AirAsia / Expedia / 中華航空 | GitHub Actions Step 3 | ⛔ 被 bot 防護擋下,寫入 confidence=0 降級結果 |
| KAYAK / momondo / ezTravel | GitHub Actions Step 3 | ⚠️ 既有深連結本身有問題(404/停用轉址/日期誤判),不在本次修復範圍,尚待修 |
| Booking.com | GitHub Actions Step 3 | ⚠️ 深連結導向飯店首頁非機票結果頁,擷取到的數字不可靠,資料品質風險未解決 |
| 星宇航空 | GitHub Actions Step 3 | 同上,設定表已涵蓋,實測結果視平台當時反應而定 |
| EVA Air 長榮 / Kiwi.com / Traveloka / Airpaz | **Notion AI Agent**(不變) | 維持現況,已驗證可用 |

## 什麼應該要被完美觸發

**自動觸發**:每天 08(UTC 01:00,即台北 09:00)cron 自動跑,Step 1 → Step 2 → Step 3 依序執行,不需人工介入。

**手動觸發**:GitHub repo → Actions →「Trip Price Crawler」workflow →「Run workflow」按鈕(`workflow_dispatch`),效果與自動觸發相同,三步驟都會跑。

**Step 3 的觸發條件**:只要 Step 1 成功就會執行(workflow yml 內部檢查 `step1.outcome`),不需要額外的觸發動作,不依賴 Notion 屬性變化。

**Notion AI Agent 的觸發不變**:GitHub Actions Step 2 PATCH「航班追蹤」列的「上次觸發時間」屬性 → Agent 監看該屬性變更後查價。**唯一關鍵變更**:Agent 觸發後只能覆寫 EVA Air/Kiwi.com/Traveloka/Airpaz 這 4 列,**絕對不能覆寫上表 GitHub Actions Step 3 負責的 11 列**——判斷依據是該列「票價」欄位是否已有數字,已有數字就不要動,否則會用讀不到 JS 渲染頁面的較差猜測蓋掉真實擷取值。

## 尚未解決(不在本次範圍)

1. KAYAK/momondo/ezTravel 既有深連結本身壞掉,需要重新查證正確格式。
2. Booking.com 深連結指向飯店首頁,需改成機票結果頁格式,否則擷取到的價格不可信。
3. GitHub Secret `GROQ_API_KEY` 需要人工在 repo Settings → Secrets 設定實際值,程式碼只讀取這個環境變數名稱,沒有預設值。

## 需要人工確認才能上線

本次改動目前只在本機 `C:\Users\User\Developer\Notion-script` 完成並測試,**尚未 commit/push 到 GitHub**,需要使用者審查 diff 後才會推上去、並手動 Run workflow 驗證一次。
