# IndexAlertBot-Python

定時抓取美股、台股、加密貨幣的行情（Yahoo Finance + MAX 交易所台幣報價），計算 RSI 與 MA20/60/200，
偵測「RSI 超買超賣」、「日內急漲急跌」、「乖離 MA」等警報；只對**新觸發**的警報通知，
由 DeepSeek 整理成繁體中文報告，經 Telegram Bot 推送。排程由 GitHub Actions 執行，
狀態檔會在每次執行後自動 commit 回 repo。

另外附上一層**總體經濟脈絡**：BLS（物價／就業）＋ FRED（PCE／政策利率／殖利率／通膨預期）＋ Fed RSS
＋ Yahoo 市場價格（美元／原油／黃金／VIX），除了「總體警報」（CPI 年增過高、殖利率急升…）之外，
還會附上**環境判讀**、**風險分數**與**資產 × 總體因子相關性／背離**，讓 AI 的報告有可查證的數字依據。

## 排程（GitHub Actions，UTC 時間）

| Workflow | 頻率 | 時段（UTC） | 狀態檔 |
|---|---|---|---|
| `Alerts - Crypto` | 每 5 分鐘 | 24/7 | `alert_state_crypto.json` |
| `Alerts - TW Stocks` | 每 5 分鐘 | 週一~五 01:00–05:55（= 台灣 09:00–13:55） | `alert_state_tw.json` |
| `Alerts - US Stocks` | 每 5 分鐘 | 週一~五 13:00–20:55（涵蓋冬夏令，DST 邊界 ±30~60 分） | `alert_state_us.json` |

> **總體快照只有一個寫入者**：`macro_snapshot.json` 由 `Alerts - US Stocks`（`--market us`，
> **刻意不加** `--macro-readonly`）負責更新與 commit；TW / Crypto 以 `--macro-readonly` **唯讀**取用（不連網、不寫檔），
> 避免三個排程互搶同一個檔案、也避免同一則總體警報被通知三次。
> 第一次執行前檔案還不存在，那個回合的報告只會暫時缺少總體脈絡。

## 功能

- 多市場標的：美股（AAPL）、台股（2330.TW）、加密貨幣（btctwd / ethtwd）
  - `provider: yahoo`（預設）走 Yahoo Finance，支援美股/台股/USD 計價加密貨幣
  - `provider: max` 走 MAX 台灣交易所公開 API，支援**台幣報價**加密貨幣（免金鑰）
- 技術指標：RSI(14)（Wilder 平滑法）、MA20 / MA60 / MA200、乖離率、日內漲跌幅
- 警報規則（門檻可在 `config.yaml` 調整，亦可依市場別覆寫）：
  - RSI 超買（≥ 70）/ 超賣（≤ 30）
  - 日內急漲 / 急跌（對比前收，預設 ±5%，加密貨幣 ±10%）
  - 正 / 負乖離 MA20、MA60、MA200
- 去重通知：`alert_state_*.json`（依市場分檔）記錄每個標的每種警報的狀態，
  條件解除（clear）後再次觸發才會重新通知
- 總體經濟層（`src/macro.py`，免金鑰資料為主）：
  - BLS：CPI／核心 CPI、非農就業、失業率、平均時薪（月度，含年增／月增／前值／3 個月變化）
  - FRED：PCE／核心 PCE、政策利率（上下限＋上次升降碼數）、10Y 殖利率、通膨預期
    - 需環境變數 `FRED_API_KEY`；**未設定時自動略過**，該區塊不影響其他資料
    - 沒有 FRED 時，10Y 殖利率會退回 Yahoo `^TNX` 日線推算（水位＋20 日 bp 變化）
  - Fed RSS：FOMC 聲明與官員談話標題；Yahoo 新聞：關鍵字過濾（Fed／通膨／關稅…）
  - 快照 `macro_snapshot.json` 以**區塊為單位**快取（月度 24h／市場 1h TTL），
    個別區塊抓取失敗會保留上次成功資料並記錄原因，下回合自動重試
  - 總體價格（`^TNX`／`DX-Y.NYB`／`CL=F`／`GC=F`／`^VIX`）是**資料來源，不是追蹤標的**：
    設定在 `config.yaml` 的 `macro.price_symbols`，**不會**出現在報告的標的清單、也不會產生個股式的技術面警報
- 總體警報（`src/macro_alerts.py`）：CPI／核心 CPI 年增過高、CPI 月增加速、PCE 年增過高、
  10Y 殖利率 20 日急升/急降、美元急升、原油急漲、失業率 3 個月跳升、時薪年增過高；
  同樣只通知**新觸發**（與技術面共用邏輯，狀態同樣記在狀態檔）
- 環境判讀（`src/regime.py`）：由「通膨方向 × 利率方向」判讀宏觀情境（如停滯性通膨壓力、通縮風險）
- 風險分數（`src/risk_score.py`）：`0.6 × 技術面 + 0.4 × 總體面` 的 0–100 分數（權重可在
  `config.yaml` 調整）；**權重未經回測**，只用於相對排序，不是機率也不是買賣建議
- 相關性與背離（`src/correlations.py`）：資產日線對總體因子（10Y／美元／原油／黃金／VIX）的
  20 日與 60 日相關係數；只有 |r| ≥ `correlation_min_abs`（預設 0.3）且近 5 日方向相反時，
  才會寫成「背離」句子，避免把雜訊當訊號
- DeepSeek 中文報告：把新觸發的警報整理成純文字總覽報告；
  總體警報、風險分數、相關性、環境背景等**數字區塊一律由程式產生**（不經 AI 改寫），
  AI 只負責串成白話說明；API 失敗時自動退回程式產生的原始清單
- GitHub Actions 三市場獨立排程（每 5 分鐘），執行後自動 commit 各自狀態檔（US 另含總體快照）

## 目錄結構

```
.
├── .github/workflows/
│   ├── alerts-crypto.yml             # 加密貨幣 24/7，每 5 分鐘
│   ├── alerts-tw.yml                 # 台股開盤時段，每 5 分鐘
│   ├── alerts-us.yml                 # 美股開盤時段，每 5 分鐘
│   └── secret-scan.yml               # gitleaks 金鑰外洩掃描（push / PR / 每週排程）
├── src/
│   ├── models.py                      # Quote / Alert 資料類別
│   ├── config.py                      # 讀取 config.yaml + 環境變數
│   ├── fetcher.py                     # 行情抓取（yahoo 走 yfinance / max 走 MAX 交易所）
│   ├── indicators.py                  # RSI / MA / 乖離率計算
│   ├── alerts.py                      # 警報規則 + 新觸發比對
│   ├── macro.py                       # 總體經濟抓取（BLS/FRED/Fed RSS/Yahoo）＋快照快取＋報告文字
│   ├── macro_alerts.py                # 總體警報規則 + 新觸發比對
│   ├── regime.py                      # 總體環境判讀（通膨 × 利率）
│   ├── risk_score.py                  # 風險分數（技術面 + 總體面）
│   ├── correlations.py                # 資產 × 總體因子相關性與背離
│   ├── state.py                       # 狀態檔（alert_state_*.json）讀寫
│   ├── reporter.py                    # DeepSeek 中文報告
│   └── notifier.py                    # Telegram 發送
├── tests/                             # 單元測試（指標、警報、狀態、報告）
├── main.py                            # 主程式進入點
├── config.yaml                        # 標的、門檻、各項設定
├── .gitleaks.toml                     # gitleaks 規則（官方預設 + 放行文件示意字串）
├── .env.example                       # 環境變數範本（值留空；真金鑰只放 GitHub Secrets）
├── alert_state_crypto.json            # 加密貨幣警報狀態（自動更新、commit 回 repo）
├── alert_state_tw.json                # 台股警報狀態（自動更新、commit 回 repo）
├── alert_state_us.json                # 美股警報狀態（自動更新、commit 回 repo）
├── macro_snapshot.json                # 總體經濟快照（US workflow 更新、commit 回 repo）
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
# 選用：設定後才會抓到 FRED 的 PCE／政策利率／通膨預期（未設定會自動略過該區塊）
$env:FRED_API_KEY = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# 預覽模式：抓資料、算警報、印出報告，但不發送、不更新狀態
python main.py --dry-run

# 只處理指定市場（依市場分流，與 GitHub Actions 排程對應）
python main.py --market crypto --state alert_state_crypto.json
python main.py --market tw --state alert_state_tw.json
# US workflow 同時更新總體快照（--market us，刻意不加 --macro-readonly）
python main.py --market us --state alert_state_us.json

# 總體相關的參數
python main.py --market us --dry-run --macro-force             # 忽略 TTL 強制重抓總體資料
python main.py --market tw --macro-readonly --dry-run          # 只讀既有總體快照（不連網、不寫檔）
python main.py --market us --macro-file tmp_snapshot.json      # 指定快照檔路徑（測試用）

# 正式執行（會發送 Telegram 並更新狀態檔）
python main.py
```

> 也可以直接編輯 `config.yaml` 改用小額標的（如只留 1 個）先跑 `--dry-run` 驗證流程。

## 設定說明

### config.yaml

| 區塊 | 說明 |
|---|---|
| `assets` | 追蹤標的（symbol / name / market）。market 只能是 `us`、`tw`、`crypto`（總體資產請設在 `macro.price_symbols`，不要放這裡）。`provider` 預設 `yahoo`；加密貨幣台幣報價設 `max`，symbol 用 MAX 交易對（如 `btctwd`） |
| `alerts.defaults` | 所有市場共用的警報門檻 |
| `alerts.overrides` | 依 market 覆寫門檻（例如 crypto 波動大，門檻放寬；`us` 的 MA 乖離門檻 3.0%） |
| `history` | yfinance 抓取期間（預設 2 年日線，供 RSI/MA 計算） |
| `deepseek` | base_url 與 model（api_key 走環境變數） |
| `telegram` | parse_mode（留空 = 純文字） |
| `report` | 報告樣式（`intuitive` / `technical`）、是否附程式產生的原始數據、長度指引 |
| `macro` | 總體層設定：`enabled`、快照檔名、月度／市場 TTL、`price_symbols`（總體價格代號，只是資料來源）、BLS／FRED 序列代號、總體警報門檻、相關窗口與 `risk_weights`（詳見 `config.yaml` 註解） |

> `macro` 區塊完整註解在 `config.yaml`（`thresholds` 每一項都有中文說明）。
> `macro.enabled: false` 時**完全不連網、不抓總體資料，也不會產生總體警報**；
> 若磁碟上已有 `macro_snapshot.json`，報告仍會引用其中的既有數字（等同唯讀），方便臨時降載。

### 環境變數（放 GitHub Secrets）

| 變數 | 用途 | 必要性 |
|---|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API 金鑰 | 必要（缺了會退回程式產生的原始清單） |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token（@BotFather 建立） | 必要 |
| `TELEGRAM_CHAT_ID` | 接收通知的 Chat ID（@userinfobot 查詢） | 必要 |
| `FRED_API_KEY` | FRED API 金鑰（PCE／政策利率／10Y／通膨預期） | 選用；未設定自動略過，10Y 殖利率改由 Yahoo `^TNX` 推算 |

> 未設定時，`alerts-*.yml` 的 fail-fast 檢查（`Check required secrets`）會直接讓 workflow 失敗，
> 並在摘要印出缺少的變數名稱；只檢查變數是否存在，不會印出內容（詳見「金鑰安全」）。

## 部署到 GitHub Actions

1. 建立 GitHub repo 並 push 此專案
2. **將 repo 設為 Public（重要）**：每 5 分鐘級排程一個月會執行數千次；
   公開 repo 的 Actions **免費不限量**，私有 repo 免費額度僅 2,000 分鐘/月，數天就會耗盡
3. 進入 repo → **Settings → Secrets and variables → Actions**，新增上述 3 個 secrets
   （`FRED_API_KEY` 為選用，沒設也能跑，只是少了 PCE／政策利率／通膨預期）
4. 確認 **Settings → Actions → General → Workflow permissions** 為
   **Read and write permissions**（commit 狀態檔需要）
5. 到 **Actions** 分頁手動執行 `Alerts - Crypto`、`Alerts - TW Stocks`、`Alerts - US Stocks`
   各一次（workflow_dispatch）驗證，之後依上表排程自動執行

> 排程一律使用 UTC 時間；台股時段 = 台灣 09:00–13:55，美股時段含冬夏令誤差（見下方注意事項）。

## 金鑰安全（Public repo 必讀）

本 repo 是 **public**，程式碼與 **commit 歷史**任何人都讀得到，所以金鑰一律只放在 GitHub Secrets，
repo 內任何檔案（含歷史）都不會、也不應該出現金鑰：

- 程式只從**環境變數**讀取：`DEEPSEEK_API_KEY`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` 與選用的
  `FRED_API_KEY`（見 `src/config.py`）
- `.gitignore` 已忽略 `.env`、`.env.*`、`*.env`、`*.pem`、`*.key`；repo 內只有**值為空**的 `.env.example`
- Workflow 以 `${{ secrets.XXX }}` 注入；Secrets 的值不會出現在 repo 或執行日誌中
- 程式不會把金鑰印出來（`src/reporter.py` 只把 key 放進 `Authorization` header，`requests` 的錯誤訊息不含 header）；
  log 等級固定 `INFO`，**請勿改成 `DEBUG`**（`urllib3` / `http.client` 的 DEBUG 會印出 request headers）
- public repo 的 **Actions 執行日誌是公開的**，任何會被印出的內容都等於公開

### 三道防線

| 防線 | 位置 | 預設狀態 | 作用 |
|---|---|---|---|
| **Push protection for users**（帳號層） | GitHub 個人帳號層級設定 | **開啟** | 擋住「你自己」把金鑰推到**任何 public repo**（不會產生 repo 告警） |
| **Secret scanning**（repo 層） | repo → Settings → **Security and quality** → **Advanced Security** → 啟用 **Secret Protection** | 需啟用 | 掃描 repo 找金鑰 → 在 **Security and quality** 分頁產生告警，並通知支援的 partner 供應商 |
| **Push protection**（repo 層） | 同一頁 **Secret Protection** 區塊 → **Push protection** | **預設關閉** | push 到 repo 前就擋下含金鑰的 push；bypass 會留稽核記錄與通知 |
| **gitleaks CI** | `.github/workflows/secret-scan.yml` + `.gitleaks.toml` | 已上線 | push（排除狀態檔）／PR／每週排程掃**全部 git 歷史**，疑似金鑰就讓 CI 紅燈 |
| **Secrets fail-fast 檢查** | 三個 `alerts-*.yml` 的 `Check required secrets` | 已上線 | 任一 Secret 未設定就立刻失敗，避免空金鑰靜默降級成 fallback 報告 |

> ⚠️ **Secret scanning 沒有獨立開關**：它會隨 **Secret Protection** 一起啟用，
> 所以 Settings 頁面只會看到 `Secret Protection` 與 `Push protection`，不會看到單獨的 `Secret scanning` 開關（這是正常的）。
> 另外 **Code scanning** 是另一套功能（CodeQL / AI Scan 掃程式碼漏洞），跟金鑰無關，不必為了 secret scanning 去開它。

> `secret-scan.yml` 用 `paths-ignore: alert_state_*.json` 避開每 5 分鐘一次的狀態檔 commit（否則會被排程洗版），
> 並以 `cron: '0 0 * * 1'`（每週一 00:00 UTC）做一次全歷史複掃補漏。
> gitleaks 版本與 sha256 皆鎖定，安裝後以 `sha256sum -c` 驗證才執行。

### 需要手動做的一次性設定

1. repo → **Settings** → 左側 **「Security and quality」** 區段 → **Advanced Security**
   （新版介面可能顯示為 **Code security**；認關鍵字 `Secret Protection` 就不會找錯）
2. 若 **Secret Protection** 右側有 **Enable** 按鈕 → 點它 → 檢視影響後按 **Enable Secret Protection**
   - **Secret scanning 沒有獨立開關**，啟用 Secret Protection 之後就開始掃描與告警
3. 在同一頁的 **Secret Protection** 區塊，把 **Push protection** 按 **Enable**
   - 這項**預設是關閉的**（官方文件：*Is disabled by default*），必須手動開
   - 開啟後，含金鑰的 push 會被直接擋下，並在 **Security and quality** 分頁留下告警／稽核記錄
4. （可選，同一頁免費）加開 **Generic patterns**、**Generic secret detection**（AI 偵測非結構化機密）、**Validity checks**
5. repo → **Settings → Secrets and variables → Actions**：確認 3 個 Secrets 都存在

驗證方式：

- `https://github.com/<owner>/<repo>/security/secret-scanning` → 應出現 **Secret scanning** 與告警數（0 筆是正常的）
- `https://github.com/<owner>/<repo>/security` → **Security and quality** 分頁（所有安全告警的入口）

> 本專案兩把金鑰都在 gitleaks 的守備範圍內（本機以 `gitleaks stdin` 實測）：
> Telegram Bot Token 由 `telegram-bot-api-token` 規則攔下；
> DeepSeek 金鑰（`sk-` 開頭的高熵字串）由 `generic-api-key` 規則攔下。
> GitHub 內建 pattern 清單有數百個供應商、本專案未逐一確認 DeepSeek 是否在內，
> 所以 CI 這層（可自行維護規則）才是涵蓋第 3 方 pattern 的保底。

### 本機自行掃描（可選）

```bash
# 先安裝 gitleaks（https://github.com/gitleaks/gitleaks/releases）
gitleaks git . --config .gitleaks.toml --log-opts="--all" --redact --no-banner -v  # 掃全部歷史
gitleaks dir . --config .gitleaks.toml --redact --no-banner -v                     # 掃工作目錄
```

### 萬一金鑰外洩的處理順序（重要）

1. **先撤銷、再清理**：立刻到 DeepSeek 平台撤銷該金鑰並重新簽發（Telegram 則用 @BotFather `/revoke`），
   把新值更新到 GitHub Secrets
   - 這一步必須最先做：金鑰一旦被爬蟲掃走，再怎麼改 git 歷史都救不回已經流出的事實
2. 檢查該金鑰的用量／帳單是否有異常呼叫
3. 最後才清理歷史（`git filter-repo` 或 BFG）並 force push
4. 若金鑰曾出現在 Actions 日誌中，記得一併刪除那些 run 的日誌

## 運作流程

```
GitHub Actions（三個獨立排程，每 5 分鐘，依市場分流）
  → python main.py --market {crypto|tw|us} [--macro-readonly] --state alert_state_{market}.json
      （US 不加 --macro-readonly，負責更新 macro_snapshot.json）
      → 載入 config + 環境變數
      → 總體快照：依 TTL 只重抓過期區塊（BLS / FRED / Yahoo 價格 / Fed RSS / 新聞）
          → 沒 FRED 金鑰就略過該區塊；FRED 缺值時用 ^TNX 推算 10Y 殖利率
          → 唯讀模式（--macro-readonly）完全不連網、不寫檔
      → 依市場篩選標的，逐標的抓取行情（2y 日線）
      → 計算 RSI / MA20/60/200 / 乖離率 / 日內漲跌幅
      → 比對該市場狀態檔，篩出「新觸發」技術面警報
      → 非唯讀市場再算「新觸發」總體警報（避免重複通知）
      → 有新觸發任一類：組出總體脈絡（風險分數 / 相關性 / 背離 / 環境判讀 / 總體數據）
          → DeepSeek 生成中文報告（失敗則用程式產生的清單）→ Telegram 發送
      → 更新該市場狀態檔
  → 若有變更，commit + push 回 repo（US 另含 macro_snapshot.json）
```

## 已知限制與注意事項

- **休市時段**：美股 / 台股在收盤後抓到的是當日收盤價，日內漲跌幅維持當日結果；
  週末 / 國定假日休市時則為前一個交易日資料（排程仍會照跑，無資料變化就不會觸發新警報）。
- **美股 DST**：冬夏令各差 1 小時，cron 取兩者的聯集時段（13:00–20:55 UTC），
  換季時邊界約有 30~60 分鐘誤差，多跑無害。
- **加密貨幣**：24 小時交易，「日內」以對比前一日收盤（約 UTC 0 點）計算。
- **Yahoo Finance**：yfinance 依賴 Yahoo 的公開介面，偶爾可能被限流或暫時失效；
  該回合該標的會跳過並記錄錯誤，其他標的不受影響。
- **時間**：GitHub Actions 的 cron 為 UTC；主程式內的通知時間戳為執行環境本地時間。
- **狀態儲存**：若 Telegram 發送失敗，程式會「不儲存狀態」直接回傳非零，
  下一回合會重送同一批警報，確保不漏接。
- **總體資料延遲**：BLS 月度統計每月才更新一次（快照 24h TTL 只是重抓頻率，不是資料頻率），
  FRED 的 PCE 也比 CPI 晚公佈；報告會標示「資料時間」與資料期別，避免把舊數字當最新。
- **總體警報去重**：總體警報只由**非唯讀**的 workflow 觸發（US），TW / Crypto 不會重複通知同一件事；
  但條件解除後再次符合仍會重新通知（與技術面同一套邏輯）。
- **唯讀市場的快照可能較舊**：TW / Crypto 讀的是 US workflow 上次 commit 的快照；
  美股時段外（UTC 21:00–13:00）US 不執行，市場區塊可能已超過 1h TTL，報告會照實標示資料時間，
  不會假裝是最新。需要更即時就本機執行 `python main.py --market us --dry-run --macro-force` 更新。
- **FRED 為選用**：未設定 `FRED_API_KEY` 時 PCE／政策利率／通膨預期會缺席（報告會列在「本回合無法取得」），
  10Y 殖利率改用 Yahoo `^TNX` 日線推算，因此該欄位在無 FRED 時仍可用。
- **相關性只是關聯**：相關係數高不代表因果，也可能隨時間改變；程式只在 |r| ≥ 門檻且樣本足夠時才輸出，
  並在報告中提醒 AI 不可把單一因子當成漲跌的唯一解釋。
- **風險分數未經回測**：`0.6 × 技術面 + 0.4 × 總體面` 是**主觀設定**，只用於同一回合內的相對排序，
  沒有統計驗證、也不是機率；`partial` 標記代表該分數有部分分量因資料不足而算不出來。
- **免費資料源**：BLS / Fed RSS / Yahoo 都是公開端點，可能限流或改版；失敗只影響該區塊，
  快照會保留上次成功資料，因此報告可能出現「資料時間較舊」但仍有內容的情況。
- **免責聲明**：本專案僅為技術監控工具，輸出的報告不構成任何投資建議。

## 開發

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```
