# MLB 賽事趨勢爬蟲與互動式分析儀表板 (MLB Sports Trends)

本專案建立了一個高效能、零外部依賴的 Python 數據抓取與智慧分析工具，專門用來抓取 covers.com 每日 MLB 對戰數據，篩選出具有高投資回報率 (ROI) 的黃金對戰趨勢，並生成極具現代感且支援完全中文化的本機互動式網頁儀表板。

---

## 📋 專案實作成果與已完成事項

本專案已全面開發完成，主要完成了以下核心工作：

### 1. 🕷️ 高效能零依賴爬蟲腳本 ([scrape.py](file:///C:/Users/johnn/OneDrive/桌面/Projects/sports/scrape.py))
- **零外部庫依賴**：完全基於 Python 標準庫 (`urllib.request`, `re`, `json` 等) 實作，不需安裝任何第三方套件 (如 `requests`、`BeautifulSoup` 或 `Selenium`)，隨插即用。
- **動態參數支援**：可透過參數抓取今日或指定歷史日期的賽事（例如 `python scrape.py --date 2026-05-28`）。
- **MLB 官方 SVG 隊徽解析**：內建自動提取官方高畫質 SVG 隊徽路徑的邏輯，完美支援所有 30 支 MLB 球隊，載入速度極快。
- **連線保護與穩定度**：內建防擋機制（模擬瀏覽器標頭 User-Agent、1秒延遲間隔等），並具備單場錯誤容錯機制，確保部分頁面失效時腳本能自動跳過並繼續抓取，不崩潰。

### 2. 🧮 智能投注推薦媒合演算法
- **🔥 雙向正面推薦 (大小分總分盤)**：比對同場賽事中，雙方球隊在全場大小分盤口均看好大分（Over）或均看好小分（Under）的黃金交叉組合。
- **🎯 一正一反推薦 (勝負/讓分盤)**：比對同一個獨贏盤 (Moneyline) 或讓分盤 (Run Line)，當一隊呈現正面獲利趨勢（High / 正 Units），而另一隊呈現負面虧損趨勢（Low / 負 Units）時，自動篩選推薦強勢隊伍。
- **🤖 今日 AI 智慧精選 Top 5**：
  - **自動衝突過濾**：智慧分析同場比賽中是否存在相互矛盾的推薦（例如同時推薦全場大分與小分），並僅保留期望值高者，避免自相矛盾。
  - **樣本數加權排序**：解析每條趨勢的戰績樣本數（如 7-2 代表 9 場），過濾樣本過小（< 8 場）的雜訊趨勢，並以 `ROI × 樣本數/(樣本數+10)` 收縮加權排序，避免小樣本高 ROI 趨勢霸榜。網頁最上方渲染「今日 AI 推薦 Top 5」精選卡片，並動態生成專業的 AI 分析推薦語。

### 3. 🎨 極致美觀的繁體中文互動儀表板 ([index.html](file:///C:/Users/johnn/OneDrive/桌面/Projects/sports/index.html))
- **高端毛玻璃設計**：採用深邃 slate 質感底色（`#0b0f19`）搭配半透明毛玻璃卡片（Glassmorphism），支援平滑懸停缩放與霓虹發光等流暢微動畫。
- **完全中文化**：全面將英文盤口術語中文化（例如：`Moneyline` ➡️ `獨贏`，`Run Line` ➡️ `讓分 / 受讓` 等）。
- **解耦式即時頁籤切換**：頂部提供「全部對戰」、「🔥 大小分總分」、「🎯 勝負/讓分盤」三種分類頁籤。切換頁籤時，卡片內部的推薦內容會隨之動態過濾，提供極佳的閱讀體驗。
- **即時搜尋與可摺疊詳情**：
  - 內建搜尋框，可輸入球隊名稱即時過濾。
  - 每場比賽的卡片可一鍵展開或摺疊，查看從 covers.com 抓取來的原始 High/Low 趨勢詳情。

---

## 📂 專案檔案結構說明

*   [**scrape.py**](file:///C:/Users/johnn/OneDrive/桌面/Projects/sports/scrape.py)：爬蟲、數據處理、推薦媒合、AI 精選與 HTML Dashboard 生成的核心程式。
*   [**index.html**](file:///C:/Users/johnn/OneDrive/桌面/Projects/sports/index.html)：由腳本自動生成的繁體中文化互動式網頁儀表板（GitHub Pages 亦直接使用此檔）。
*   [**search_cache_picks.py**](file:///C:/Users/johnn/OneDrive/桌面/Projects/sports/search_cache_picks.py)：開發過程中用於測試與優化 HTML 趨勢解析與正則表達式的輔助腳本。
*   [**task.md**](file:///C:/Users/johnn/OneDrive/桌面/Projects/sports/task.md)：任務開發進度追蹤清單，記錄了所有已完成的 Bug 修復與新功能。
*   [**walkthrough.md**](file:///C:/Users/johnn/OneDrive/桌面/Projects/sports/walkthrough.md)：專案開發成果亮點與實測紀錄詳細文檔。
*   [**implementation_plan.md**](file:///C:/Users/johnn/OneDrive/桌面/Projects/sports/implementation_plan.md)：初期設計規劃與實作策略文檔。

---

## 🚀 執行與使用指南

### 1. 執行爬蟲抓取數據
打開終端機（PowerShell 或 CMD），進到專案目錄後執行：
```powershell
# 抓取今日最新 MLB 賽事趨勢
python scrape.py

# 抓取特定歷史日期的 MLB 賽事趨勢
python scrape.py --date 2026-05-28
```

### 2. 開啟儀表板網頁
執行結束後，會在專案根目錄下生成或更新 [**index.html**](file:///C:/Users/johnn/OneDrive/桌面/Projects/sports/index.html)。
- **操作方式**：在您的 Windows 系統中，直接**按兩下開啟 `index.html`** 即可。網頁為純前端本機靜態網頁，無需開啟任何伺服器即可完美運行。
