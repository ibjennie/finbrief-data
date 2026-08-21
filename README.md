# FINBRIEF 公開資料

這個儲存庫保存 FINBRIEF 已查證的晨晚報與市場資料，不包含網站原始碼、密鑰、權杖、電子郵件或其他個人資訊。

- `latest.json`：網站目前使用的完整資料包。
- 資料包含當日晨報、晚報、每日知識、事件、14 項市場快照與歷史報表。
- 各市場項目保留真實資料日期、來源名稱與來源網址。
- 尚未收盤或官方來源尚未更新時，會沿用最近完成交易日，不會把舊資料標成今日。
- 只使用免費公開讀取的官方或可信來源。

公開讀取網址：

`https://raw.githubusercontent.com/ibjennie/finbrief-data/main/latest.json`

## 免費自動更新

`.github/workflows/update-finbrief.yml` 由 GitHub Actions 執行：

- 臺北時間每日 08:30 更新晨報。
- 臺北時間每日 18:30 更新晚報。
- 也可在 Actions 頁面手動選擇 `morning` 或 `evening` 測試。
- 工作流程只會提交 `latest.json`，不會重新部署網站。

更新器位於 `scripts/update_finbrief.py`，只使用 Python 標準函式庫與免費公開來源：臺灣證交所、FRED、美國財政部、Cboe、TradingView 與 Coinbase。個別來源失敗時保留上一筆真實資料與原始日期，不會把舊值偽裝成今日。

程式在寫入前會驗證日期、晨晚報結構、14 項行情、冷門雷達事件日期、歷史順序、HTTPS 來源與敏感資料。驗證失敗時工作流程停止，原有 `latest.json` 不會被提交。

本儲存庫不使用付費 API、試用額度、信用卡、Google 服務或 OpenAI API key。
