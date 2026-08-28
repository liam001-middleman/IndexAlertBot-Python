# IndexAlertBot-Python

定時（每小時）抓取美股、台股、加密貨幣的行情（Yahoo Finance + MAX 交易所台幣報價），計算 RSI 與 MA20/60/200，
偵測「RSI 超買超賣」、「日內急漲急跌」、「乖離 MA」等警報；只對**新觸發**的警報通知，
由 DeepSeek 整理成繁體中文報告，經 Telegram Bot 推送。排程由 GitHub Actions 執行，
狀態檔 `alert_state.json` 會在每次執行後自動 commit 回 repo。

## 功能

- 多市場標的：美股（AAPL）、台股（2330.TW）、加密貨幣（btctwd / ethtwd）
  - `provider: yahoo`（預設）走 Yahoo Finance，支援美股/台股/USD 計價加密貨幣
  - `provider: max` 走 MAX 台灣交易所公開 API，支援**台幣報價**加密貨幣（免金鑰）
- 技術指標：RSI(14)（Wilder 平滑法）、MA20 / MA60 / MA200、乖離率、日內漲跌幅
- 警報規則（門檻可在 `config.yaml` 調整，亦可依市場別覆寫）：
  - RSI 超買（≥ 70）/ 超賣（≤ 30）
  - 日內急漲 / 急跌（對比前收，預設 ±5%，加密貨幣 ±10%）
  - 正 / 負乖離 MA20、MA60、MA200
- 去重通知：`alert_state.json` 記錄每個標的每種警報的狀態，
  條件解除（clear）後再次觸發才會重新通知
- DeepSeek 中文報告：把新觸發的警報整理成總覽報告；API 失敗時自動退回原始警報清單
- GitHub Actions 每小時排程，執行後自動 commit `alert_state.json`

## 目錄結構

```
.
├── .github/workflows/run_alerts.yml   # 每小時排程 + 測試 + commit 狀態檔
├── src/
│   ├── models.py                      # Quote / Alert 資料類別
│   ├── config.py                      # 讀取 config.yaml + 環境變數
│   ├── fetcher.py                     # 行情抓取（yahoo 走 yfinance / max 走 MAX 交易所）
│   ├── indicators.py                  # RSI / MA / 乖離率計算
│   ├── alerts.py                      # 警報規則 + 新觸發比對
│   ├── state.py                       # alert_state.json 讀寫
│   ├── reporter.py                    # DeepSeek 中文報告
│   └── notifier.py                    # Telegram 發送
├── tests/                             # 單元測試（指標、警報、狀態、報告）
├── main.py                            # 主程式進入點
├── config.yaml                        # 標的、門檻、各項設定
├── alert_state.json                   # 警報狀態（自動更新、commit 回 repo）
├── requirements.txt                   # 執行相依套件
└── requirements-dev.txt               # 開發相依套件（含 pytest）
```

## 本機安裝與執行

```bash
# 安裝相依套件
pip install -r requirements.txt

# 複製環境變數範本並填入金鑰
copy .env.example .env
# Windows PowerShell：Copy-Item .env.example .env

# 設定環境變數（PowerShell 範例）
$env:DEEPSEEK_API_KEY = "sk-xxx"
$env:TELEGRAM_BOT_TOKEN = "123456:ABC..."
$env:TELEGRAM_CHAT_ID = "123456789"

# 預覽模式：抓資料、算警報、印出報告，但不發送、不更新狀態
python main.py --dry-run

# 正式執行（會發送 Telegram 並更新 alert_state.json）
python main.py
```

> 也可以直接編輯 `config.yaml` 改用小額標的（如只留 1 個）先跑 `--dry-run` 驗證流程。

## 設定說明

### config.yaml

| 區塊 | 說明 |
|---|---|
| `assets` | 追蹤標的（symbol / name / market）。market 為 `us`、`tw` 或 `crypto`。`provider` 預設 `yahoo`；加密貨幣台幣報價設 `max`，symbol 用 MAX 交易對（如 `btctwd`） |
| `alerts.defaults` | 所有市場共用的警報門檻 |
| `alerts.overrides` | 依 market 覆寫門檻（例如 crypto 波動大，門檻放寬） |
| `history` | yfinance 抓取期間（預設 2 年日線，供 RSI/MA 計算） |
| `deepseek` | base_url 與 model（api_key 走環境變數） |
| `telegram` | parse_mode（留空 = 純文字） |

### 環境變數（放 GitHub Secrets）

| 變數 | 用途 |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API 金鑰 |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token（@BotFather 建立） |
| `TELEGRAM_CHAT_ID` | 接收通知的 Chat ID（@userinfobot 查詢） |

## 部署到 GitHub Actions

1. 建立 GitHub repo 並 push 此專案
2. 進入 repo → **Settings → Secrets and variables → Actions**，新增上述 3 個 secrets
3. 確認 **Settings → Actions → General → Workflow permissions** 為
   **Read and write permissions**（commit 狀態檔需要）
4. 到 **Actions** 分頁手動執行一次 `Run Index Alerts`（workflow_dispatch）驗證，
   之後會依排程每小時自動執行

排程使用 UTC 時間（`cron: '0 * * * *'`），即台灣時間每小時的 08:00。

## 運作流程

```
GitHub Actions（每小時）
  → python main.py
      → 載入 config + 環境變數
      → 逐標的抓取 Yahoo 行情（2y 日線）
      → 計算 RSI / MA20/60/200 / 乖離率 / 日內漲跌幅
      → 比對 alert_state.json，篩出「新觸發」警報
      → 有新觸發：DeepSeek 生成中文報告 → Telegram 發送
      → 更新 alert_state.json
  → 若有變更，commit + push 回 repo
```

## 已知限制與注意事項

- **休市時段**：美股 / 台股在收盤後抓到的是當日收盤價，日內漲跌幅維持當日結果；
  週末 / 假日則為最後交易日資料。
- **加密貨幣**：24 小時交易，「日內」以對比前一日收盤（約 UTC 0 點）計算。
- **Yahoo Finance**：yfinance 依賴 Yahoo 的公開介面，偶爾可能被限流或暫時失效；
  該回合該標的會跳過並記錄錯誤，其他標的不受影響。
- **時間**：GitHub Actions 的 cron 為 UTC；主程式內的通知時間戳為執行環境本地時間。
- **狀態儲存**：若 Telegram 發送失敗，程式會「不儲存狀態」直接回傳非零，
  下一回合會重送同一批警報，確保不漏接。
- **免責聲明**：本專案僅為技術監控工具，輸出的報告不構成任何投資建議。

## 開發

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```
