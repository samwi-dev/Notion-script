# 2026-08-08 交接：反爬蟲與等待邏輯強化（patchright / 事件驅動等待 / fast-flights / 代理掛鉤）

給後續維護者看的異動說明。承接 [`2026-08-06-step3-playwright-groq-handoff.md`](2026-08-06-step3-playwright-groq-handoff.md) 與
2026-08-07 的反爬蟲修正（commit 74101ee/16dc82b/f9f323c），這次是使用者核准的第二輪強化。

## 改動了什麼

`crawlers/trip_price/browser_fetch.py` 新增四項邏輯改動，`crawlers/trip_price/ai_price.py` 配合改一行參數傳遞：

1. **引擎切換（patchright 優先）** — 原本固定用 `playwright.sync_api.sync_playwright`，改成
   top-level `try: from patchright.sync_api import sync_playwright ... except ImportError: from
   playwright.sync_api import sync_playwright`。patchright 已在 CDP 協定層處理掉常見自動化偵測
   訊號，因此 `_ENGINE == "patchright"` 時**不疊加**手動的 `STEALTH_INIT_SCRIPT`
   （`context.add_init_script`）與 `--disable-blink-features=AutomationControlled` 這個啟動參數
   ——這兩個只在 fallback 的 `playwright` 分支套用，避免疊加後反而製造出 patchright 原本就避開的
   可偵測特徵。`_disable-dev-shm-usage` / `--no-sandbox` 兩個引擎都保留。debug log 會印目前用的
   引擎（`engine=patchright` 或 `engine=playwright`），方便之後從 Actions log 判斷。

2. **事件驅動等待取代死等固定秒數** — 新增 `_wait_for_price_or_timeout(page, price_regex,
   max_wait_ms)`：每 1.5 秒讀一次 `page.inner_text("body")` 跑既有的 `extract_price_candidates()`，
   連續兩輪拿到的候選價格清單完全相同且非空就提早結束；逾時就靜默放棄（不拋例外），讓呼叫端用
   當下畫面繼續走既有流程 —— 最壞情況跟今天死等固定秒數完全一樣，只是通常會更快。只用
   Playwright/Patchright 原生 API，沒有引入新套件。

3. **Google Flights 改走 fast-flights，兩層 fallback** — 新增 `crawlers/trip_price/
   fast_flights_fetch.py`，封裝 fast-flights 套件直接呼叫 Google Flights 內部查詢介面取結構化
   報價（不必真的開頁渲染）。`PLATFORM_CONFIG["Google Flights"]` 加 `"engine": "fast_flights"`；
   `fetch_price()` 一開始檢查這個 key，是的話先呼叫 `fetch_google_flights_price(journey_info)`，
   成功就直接回傳，失敗（套件未安裝、`journey_info` 缺資料、或 API 呼叫例外）**不 return**，落到
   原本的 Playwright/Patchright 開頁流程繼續跑（兩層 fallback，不會讓 fast-flights 失敗變成整個
   平台失敗）。`fetch_price()` 簽名新增 `journey_info: Optional[Dict[str, str]] = None`，
   `fetch_all_for_journey()` 與 `ai_price.py` 的 `process_price_db()` 都已改成呼叫時帶入
   `journey_info=info`。

4. **代理伺服器掛鉤** — `browser_fetch._fetch_once()` 用 `crawler.env()` 讀
   `SCRAPE_PROXY_URL` / `SCRAPE_PROXY_USERNAME` / `SCRAPE_PROXY_PASSWORD`；有設定 `SCRAPE_PROXY_URL`
   才組出 Playwright 的 `proxy` 參數傳給 `p.chromium.launch()`，帳密為選填。未設定時 `launch()`
   呼叫方式與今天完全相同（不會多傳 `proxy=None`）。

## fast-flights smoke test 結果（已驗證，非憑空假設）

2026-08-08 在有網路環境下實測（見 `fast_flights_fetch.py` 檔頭註解）：

```
pip install fast-flights   # 實測版本 3.0.2，另需 typing_extensions 依賴
```

- 單程 TPE → NRT（2026-11-15）：成功取回 16 筆真實報價，最低 NT$6,199（Tigerair Taiwan）。
- 來回 TPE → NRT → TPE（2026-11-15 / 2026-11-20）：成功取回 18 筆真實報價，最低約 NT$10,983。
- 城市代碼（如 `TYO` 東京）與機場代碼（如 `NRT`）皆可正確查詢，不限 3 碼機場代碼。
- 實際 API 形狀跟一般網路教學印象的舊版（`FlightData`/`get_flights` 舊簽名）不同，本次是照
  `pip install fast-flights` 裝到的最新版本（3.0.2）現場跑出來的：
  `from fast_flights import create_query, get_flights, FlightQuery, Passengers`，
  `create_query(flights=[FlightQuery(date=..., from_airport=..., to_airport=...)], trip=...,
  seat="economy", passengers=Passengers(adults=1), currency="TWD")`，
  `get_flights(query)` 回傳 `ResultList`（`list[Flights]`），每筆 `.price` 為 int。
- 因為 smoke test 完整成功，`fast_flights_fetch.py` 內沒有需要「未驗證，先吞例外」的部分；
  仍保留 try/except 包住整段呼叫，是針對 Actions 執行環境（可能被暫時擋下、rate limit 等）
  的降級措施，不是因為 API 形狀本身不確定。

## GitHub Actions workflow 改動

`.github/workflows/trip-price-crawler.yml`：

- `pip install playwright groq` → `pip install playwright patchright groq fast-flights`
  （playwright 保留作為 fallback 引擎的依賴）。
- 新增一個 step：`patchright install chromium --with-deps`（原本的
  `playwright install chromium --with-deps` 保留）。
- Step 3 的 `env:` 加三個 key：`SCRAPE_PROXY_URL` / `SCRAPE_PROXY_USERNAME` /
  `SCRAPE_PROXY_PASSWORD`（對應同名 repo secrets）。未建立這三個 secret 時 GitHub Actions 會解析
  成空字串，`crawler.env()` 已把空字串當未設定處理，不影響現有行為。

## 使用者日後可選加開的三件事（本次不在範圍內）

1. **申請住宅代理（residential proxy）帳號** — 目前 `SCRAPE_PROXY_URL` 只是掛鉤，沒有預設代理
   服務；如果之後仍常被平台擋下，可以申請住宅代理帳號，把 URL/帳密填進對應的 repo secrets。
2. **側錄真實查價 API 端點回填 `wait_response_pattern`** — 目前的等待邏輯只看頁面文字有沒有
   穩定下來，如果之後想更精準判斷「資料真的載入完成」，可以實際側錄各平台查價時打的內部 API
   （瀏覽器開發者工具 Network 分頁），把端點 pattern 回填成新的 `wait_response_pattern` 設定，
   改用 `page.wait_for_response()` 取代目前的文字輪詢。
3. **若 patchright headless 效果不足，改用 xvfb + headful** — 如果之後實測發現 patchright 在
   headless 模式下仍被部分平台判定為自動化流量，可以考慮在 Actions runner 上裝 `xvfb`，改用
   `headless=False` 的 headful 模式搭配虛擬顯示器執行，進一步降低被偵測的機率（代價是資源消耗
   較高、執行時間較長）。

## 尚未解決（沿用 2026-08-06 交接文件，不在本次範圍）

1. KAYAK/momondo/ezTravel 既有深連結本身壞掉，需要重新查證正確格式。
2. Booking.com 深連結指向飯店首頁，需改成機票結果頁格式，否則擷取到的價格不可信。

## 需要人工確認才能上線

本次改動在 branch `feature/trip-price-anti-detection-upgrade` 完成並本機驗證，**尚未 push**，
也**未觸發 GitHub Actions**，需要使用者審查 diff 後才會推上去、並手動 Run workflow 驗證一次
（尤其是 patchright headless 在 Actions runner 上的實際反爬效果，本機無法完全模擬）。
