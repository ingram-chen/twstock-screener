# 台股六面向選股台

上市普通股六面向量化評分：技術面、基本面、籌碼面、大戶、消息輿論、市場氛圍。
每個交易日收盤後（台北時間 17:30）自動執行，選出前三名並更新看板。

**看板**：https://ingram-chen.github.io/twstock-screener/

單一檔案 `screener.py`，資料源全部免費、無金鑰（證交所 OpenAPI、集保結算所、
Yahoo Finance、Google News RSS）。方法與各項判斷條件的取捨都寫在程式的註解裡。

## 本機執行

```bash
pip install -r requirements.txt
python3 screener.py            # 產生 dashboard.html
python3 screener.py --selftest # 跑內建檢查
```

## 排程

`.github/workflows/screener.yml` 用 GitHub Actions 排程，每個交易日跑一次，
結果（`dashboard.html`、`picks.csv`、`cache/tdcc_*.csv`）自動 commit 回這個 repo。

`cache/tdcc_*.csv` 是集保股權分散表的週快照，用來算大戶持股的週變化 ——
這是唯一需要跨執行保留的檔案，其餘快取每次都重抓。

## 免責

本看板是量化資訊整理，不是投資建議。資料可能延遲或有誤，投資請自行判斷並承擔風險。
