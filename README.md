# Notion-script

自動化新聞/影片爬蟲：依照 Notion「News List」資料庫中的追蹤主題，定時搜尋最新影片與新聞，並將彙整結果回寫到「News Monitor update」資料庫。

## 運作方式

1. GitHub Actions 每天 **09:00（台北時間）** 自動執行 `crawler.py`（也可手動觸發）
2. 腳本讀取 **News List** 中 `Status = 追蹤中` 的主題與 Keywords
3. 依關鍵字搜尋 YouTube 最新影片（失敗時改用 Google News）
4. 過濾已抓過的連結（記錄在 `seen_urls.json`）
5. 每個主題建立一筆彙整頁到 **News Monitor update**：
   - 標題格式：`Notion Script 影片彙整 — 主題 — 日期（共 N 支）`
   - 自動帶入 Category、Crawled 日期、News List 關聯、Summary
   - 內文為影片/新聞連結清單

## 設定步驟（只需做一次）

### 1. 建立 Notion Integration

1. 前往 notion.so/my-integrations → **New integration**
2. 名稱例如 `Notion-script`，workspace 選你的工作區
3. 複製 **Internal Integration Secret**（`ntn_` 開頭）

### 2. 把資料庫分享給 Integration

對 **News List** 與 **News Monitor update** 兩個資料庫分別：

1. 開啟資料庫頁面 → 右上角 **⋯** → **Connections / 連接**
2. 搜尋並加入你剛建立的 integration

### 3. 設定 GitHub Secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**：

| Secret 名稱 | 必填 | 說明 |
|---|---|---|
| `NOTION_TOKEN` | ✅ | Integration Secret（`ntn_` 開頭） |
| `NEWS_LIST_DB_ID` | 選填 | News List 資料庫 ID（不填會自動用名稱搜尋） |
| `MONITOR_DB_ID` | 選填 | News Monitor update 資料庫 ID（不填會自動用名稱搜尋） |

> 資料庫 ID 取得方式：開啟資料庫的完整頁面，網址中 `notion.so/` 之後的 32 位英數字串（`?v=` 之前）。

### 4. 測試執行

1. Repo → **Actions** 分頁 → 啟用 workflows
2. 選 **Crawl news to Notion** → **Run workflow** 手動執行
3. 執行成功後，到 Notion 的 News Monitor update 檢查新頁面

## 檔案結構

```
crawler.py                    # 主爬蟲腳本（純標準函式庫，無需安裝套件）
.github/workflows/crawl.yml   # 排程設定（每天 09:00 台北時間）
seen_urls.json                # 已抓取連結記錄（自動更新）
```

## 調整排程

編輯 `.github/workflows/crawl.yml` 中的 cron（UTC 時間，台北時間 −8 小時）：

```yaml
schedule:
  - cron: "0 1 * * *"   # UTC 01:00 = 台北 09:00
```
