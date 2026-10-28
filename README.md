# market-data

每天自動抓台股收盤與美股收盤後的市場與金融壓力指標，供晨報與「台美晨報終端」使用。

- `data/latest.json`：每個欄位的最新值與資料日期
- `data/history/YYYY-MM-DD.json`：每天一份快照（台北日期）
- `data/raw/`：原始回應，解析出錯時可回溯
- `data/log.txt`：每次執行、每個資料源的成功或失敗

排程：台北時間週一至週五 15:40（台股）、週二至週六 05:40（美股）。也可在 Actions 頁手動執行。
需要 repo secret：`FRED_API_KEY`。
