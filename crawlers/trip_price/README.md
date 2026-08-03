# Trip Price Crawler

自動抓取台北→清邁機票價格並回填 Notion。

## 運作方式

1. **Step 1 (`crawler.py`)** - 爬取各訂票網站的基本資訊（網站名稱、訂票連結等）
2. **Step 2 (`ai_price.py`)** - 使用 **Amadeus Flight Offers Search API** 查詢真實票價並回填 Notion

## 必要 Secrets 設定

在 GitHub Repo → Settings → Secrets and variables → Actions 新增：

| Secret 名稱 | 說明 |
|---|---|
| `NOTION_TOKEN` | Notion Integration Token |
| `TRIP_DATABASE_ID` | Notion 行程資料庫 ID |
| `AMADEUS_API_KEY` | Amadeus Developer API Key |
| `AMADEUS_API_SECRET` | Amadeus Developer API Secret |

## 取得 Amadeus 免費 API 金鑰

1. 前往 [https://developers.amadeus.com/register](https://developers.amadeus.com/register) 免費註冊
2. 登入後進入 My Apps → Create New App
3. 複製 **API Key** 和 **API Secret**
4. 貼到 GitHub Secrets

> ✅ 免費帳號每月 2,000 次 API 呼叫，足夠每日自動排程使用

## Amadeus API 說明

- 使用 `/v2/shopping/flight-offers` 端點
- 可指定出發地、目的地、日期、航空公司
- 回傳來回含稅 TWD 票價
- 直飛優先（`nonStop=true`），若查無直飛自動改含轉機

## 觸發方式

```yaml
# 手動觸發
github.com → Actions → Trip Price Crawler → Run workflow

# 自動排程（workflow 內設定）
cron: '0 2 * * 1'  # 每週一 UTC 02:00 (台灣時間 10:00)
```
