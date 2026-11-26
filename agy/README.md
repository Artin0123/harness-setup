# Antigravity CLI (agy) 輔助工具集

本目錄包含針對 Google Antigravity CLI (`agy`) 設計的認證、Token 輔助與請求結構抓包工具，專門解決遠端裝置（如手機 SSH）、無 OAuth 介面環境（如 iframe 視窗）以及客製代理 Schema 檢驗的需求。

---

## 插件與腳本功能說明

### 1. `agy/agy-metrics/`（CLI / Remote Control 本地遙測後端與油猴內嵌插件）
- **主要用途**：專為 `agy` CLI 與 `agy remote-control` 設計的零依賴本地遙測後端搭配 Tampermonkey 油猴腳本（詳見 [`agy-metrics/README.md`](./agy-metrics/README.md)），直接解析本地 `conversations/*.db` 的 Protobuf 遙測資料並追蹤 5 小時模型額度扣減，無需安裝任何第三方套件。
- **核心設計**：
  - **自動啟停**：由 `hooks.json` 的 `SessionStart` Hook 自動於背景啟動 `http://127.0.0.1:47891`，並在所有 `agy.exe` 關閉（如 `/exit`）後約 1 秒自動優雅退出。
  - **內嵌上下文監控與輪次彙總**：透過 `agy-metrics-overlay.user.js` 直接嵌在 Remote Control 對話框模型選擇器右側，即時顯示上下文比例、快取命中率、耗時、純串流速度（`tok/s`）與 5 小時額度扣減，支援 `--lan` 區網與手機連線。

### 2. `agy/login.ps1`（遠端裝置登入輔助）
- **主要用途**：讓遠端連線裝置（例如透過手機 SSH 連進 Windows 電腦）能夠輕鬆完成 Google OAuth 登入。
- **解決的痛點**：
  - 原生 `agy` 產生的 Google OAuth 授權網址長達數百字元，在手機 SSH 終端中極難點擊與複製。
  - 本腳本會即時攔截網址並轉換為 **TinyURL 直達短網址**，點擊後手機瀏覽器即可直接登入 Google 取得授權碼，貼回終端機即完成登入。
  - 若已處於登入狀態，會自動辨識並提示「無需重複登入」，避免多餘等待。

### 3. `agy/get-agy-token.ps1`（無法 OAuth 環境的 Token 提取器）
- **主要用途**：提供給無法進行標準 OAuth 互動流程的環境（例如嵌入式 iframe 視窗、無頭工作環境），使其可透過已授權的 Access Token 連線。
- **解決的痛點**：
  - 免帶參數，執行時自動透過 `agy models` 發起一次認證請求以確保 Token 處於最新刷新狀態。
  - **雙來源對齊讀取**：Windows SSH 連線（Type 3 Network Logon）會隔離 Windows 憑證庫，本腳本對齊 `agy` 的 `ChainedAuth` 順序（桌面優先讀取 Windows 憑證管理員 `gemini:antigravity`，SSH 自動退回本地檔案 `~/.gemini/antigravity-cli/antigravity-oauth-token`；若兩者同時存在且帳號不同，自動對照最新 `cli-*.log` 選出當前生效帳號）。
  - **安全防洩漏**：不直接在終端機印出敏感的 raw access token，而是直接將 Token 複製到系統剪貼簿，僅在畫面上顯示帳號、長度與剩餘有效分鐘數。

### 4. `agy/capture_schema.py`（本地 Schema 抓包與代理檢驗）
- **主要用途**：利用 `agy` 原生支援的 `modelProvider: "gemini"` 與 `GOOGLE_GEMINI_BASE_URL` 環境變數，在本地啟動輕量 Gemini API 模擬伺服器，精準捕獲 `agy` 發送給模型的完整請求 JSON（包含 `systemInstruction`、`functionDeclarations`、`contents`、`generationConfig` 等）。
- **實測對照發現（預設代理 vs `clean-template`）**：
  - **系統提示詞精簡 99%**：原生預設代理的 `systemInstruction` 高達 **19,254 字元**（含 `<identity>`、`<artifacts>`、`<skills>`、`<slash_commands>`、`<user_rules>` 等）；而在啟用 `excludeDefaultComponents: true` 的 `clean-template` 下，提示詞縮水至僅 **125 字元**（僅保留自訂的 System Prompt 正文）。
  - **工具宣告（`functionDeclarations`）精確控制**：預設代理會掛載 13 支工具；在 `clean-template` 下會關閉子代理、排程、搜尋等預設組件建構器，僅掛載核心基礎的 6 支工具（`read_file`、`write_file`、`edit_file`、`run_command`、`manage_task`、`ask_question`）。若自訂 `tools:` 進一步精簡，更可縮減至指定的極少數工具。

### 5. `agy/sync-setup.ps1`（客製代理初始化、Skills／對話規則與輔助腳本同步）
- **主要用途**：建立或更新 `~\.gemini\config\agents\<agent>.md`（預設為 `build`），在使用 `excludeDefaultComponents: true` 保持乾淨提示詞的同時，自動補回 `~\.agents\skills` 清單與跨對話紀錄讀取規則；並同步 `agy/latest_cid.py` 至 `~\.gemini\config\scripts\latest_cid.py`。
- **解決的痛點**：
  - **自動建立範本**：若目標 `.md` 不存在，會自動建立含 `excludeDefaultComponents: true`、`inheritCustomizations: false`、`plugins: []` 與已驗證可用 `tools:` 清單（排除無效的 `swarm` 與強制附掛的 `manage_task`）的設定檔，並在檔頭附上遠端生成指令。
  - **冪等同步**：若檔案已存在，會更新受管欄位（`name`、`description`、`excludeDefaultComponents`、`inheritCustomizations`、`plugins`、`tools`）並保留其餘自訂 YAML 欄位與自訂正文，同時更新 `<conversation_transcript>` 與 `<skills>` 區塊，不產生多餘註解標記（`agy 1.5.16+` 雖已原生掃描 `~\.agents\skills`，但啟用 `excludeDefaultComponents: true` 會連同原生 `<skills>` 注入一起關閉，因此仍需由本腳本注入清單；而 `inheritCustomizations: false` + `plugins: []` 則用來隔離全域 hooks、plugins 與 workflows）。

### 6. `agy/audit_tokens.py`（Token 精確計數 vs 真實回傳比對與隱藏內容稽核）
- **主要用途**：比對 `agy` **本地實際組裝送出的內容**與**真實後端回傳的 `usage`（`input_tokens`、`output_tokens`、`thinking_tokens`、`cache_read_tokens`）**是否一致，並檢查是否有額外注入的隱藏標籤、系統提示詞或工具 Schema。
- **核心設計（預設 `--agent build`、`--model gemini-3.8-flash-low`）**：
  - **逐輪拆解（`stream-json` + `transcript.jsonl` 備援）**：複雜指令常觸發多輪工具呼叫，本腳本透過 `--output-format stream-json` 拆出每一輪獨立的 `input_tokens`，確保「第一輪抓包」只跟「第一輪實際消耗」比對，不被後續輪次干擾。
  - **A/B 雙檔差分探針（`--prompt-file` / `--probe-prompt-file`）**：預設讀取 `p1.txt` 與 `p2.txt` 並相減，抵銷掉固定的 `systemInstruction` 與 `tools` 開銷，精準抓出特定輸入是否觸發額外隱藏上下文。
  - **文字檔自動定位**：讀取多行 UTF-8 `.txt`；若目前工作目錄找不到相對路徑，會自動改搜尋腳本所在目錄（`agy/`）。
  - **強制官方 `:countTokens` 計數**：必須提供 `--gemini-api-key`（或環境變數 `GEMINI_API_KEY`），統一由官方端點取得精確分詞數，不進行本地粗估；亦可搭配 `--proxy-upstream` 在單一連線內完成抓包與回傳比對。

---

## 使用方式

### 一、`agy-metrics` 插件安裝與使用

- **安裝插件**：
  ```powershell
  agy plugin install https://github.com/Artin0123/harness-setup/tree/master/agy/agy-metrics
  ```
- **安裝油猴腳本**：啟動 `agy` 後會自動拉起背景後端服務，開啟 [GitHub Raw 油猴腳本](https://raw.githubusercontent.com/Artin0123/harness-setup/master/agy/agy-metrics/agy-metrics-overlay.user.js) 安裝到 Tampermonkey 即可在 Remote Control 頁面內嵌顯示並自動檢查更新；若需手動優雅關閉，可執行 `irm -Method Post http://127.0.0.1:47891/api/shutdown`。
- **VS Code 快捷鍵自動啟動範例（自動接續最新有效專案對話 + 就緒後直達網頁 + `--lan`）**：
  先執行 `irm https://raw.githubusercontent.com/Artin0123/harness-setup/master/agy/sync-setup.ps1 | iex` 同步 [`agy/latest_cid.py`](./latest_cid.py)（`latest_cid.py` 與 `ensure_server.py` 皆優先挑選所屬 `~\.gemini\config\projects\<project_id>.json` 存在的最新頂層對話，自動略過 `outside-of-project` 與已刪專案以避免 CLI 跳出 `Invalid Project` 警告；若全庫皆無有效專案對話則統一退回最新一筆頂層對話。純 CLI 也可單獨執行 `agy -r (python "$env:USERPROFILE\.gemini\config\scripts\latest_cid.py")` 直接進對話打 `/compact`）：
  ```json
  "args": {
    "text": "python \"$env:USERPROFILE\\.gemini\\config\\plugins\\agy-metrics\\hooks\\ensure_server.py\" --lan --open-remote; $cid = python \"$env:USERPROFILE\\.gemini\\config\\scripts\\latest_cid.py\"; if ($cid) { agy -r $cid --remote-control } else { agy --remote-control }\r"
  }
  ```

---

### 二、遠端執行一行指令 (One-Liners)

無需預先下載腳本至本機，直接在終端機執行即可：

> [!NOTE]
> 請注意：PowerShell 的 `irm` (`Invoke-RestMethod`) 必須請求 **Raw 原始碼網址**（`raw.githubusercontent.com` 或 `/raw/master/`），若使用 GitHub 網頁介面的 `/blob/` 網址會抓取到 HTML 網頁而無法被 `iex` 執行。

#### 1. 遠端登入（手機 SSH）

- **在 CMD / Bash / SSH 中直接執行**：
  ```bash
  pwsh -Command "irm https://raw.githubusercontent.com/Artin0123/harness-setup/master/agy/login.ps1 | iex"
  ```
- **在已開啟的 PowerShell (`pwsh`) 中執行**：
  ```powershell
  irm https://raw.githubusercontent.com/Artin0123/harness-setup/master/agy/login.ps1 | iex
  ```

#### 2. 獲取 Access Token 到剪貼簿（iframe / 嵌入視窗連線）

- **在 CMD / Bash / 本地終端中直接執行**：
  ```bash
  pwsh -Command "irm https://raw.githubusercontent.com/Artin0123/harness-setup/master/agy/get-agy-token.ps1 | iex"
  ```
- **在已開啟的 PowerShell (`pwsh`) 中執行**：
  ```powershell
  irm https://raw.githubusercontent.com/Artin0123/harness-setup/master/agy/get-agy-token.ps1 | iex
  ```

---

### 三、Schema 抓包測試使用方式 (`capture_schema.py`)

#### 方式 A：一鍵自動抓包（推薦，自動開服、調用 `agy`、存檔後退出）

```bash
# 自動檢驗 clean-template 代理送出的 Schema，並輸出到 captures 目錄
python agy/capture_schema.py --run-agy --agent clean-template --model gemini-3.8-flash-low --out-dir ./captures
```

#### 方式 B：常駐代理模式（手動執行自訂 `agy` 指令）

1. **視窗一（啟動本地代理監聽）**：
   ```bash
   python agy/capture_schema.py --port 8765 --out-dir ./captures
   ```
2. **視窗二（將 `agy` 導向本地代理發送請求）**：
   ```powershell
   $env:GEMINI_API_KEY="dummy"
   $env:GOOGLE_GEMINI_BASE_URL="http://127.0.0.1:8765"
   agy --settings='{"modelProvider":"gemini"}' --model=gemini-3.8-flash-low --agent clean-template -p="hii"
   ```
   產生的捕獲檔案會以 `capture_01.json`、`capture_02.json` 依序存放在指定輸出目錄中。

---

### 四、Token 稽核與 A/B 差分比對使用方式 (`audit_tokens.py`)

本腳本統一採用 **A/B 雙檔差分模式**（預設讀取 `p1.txt` 與 `p2.txt`，預設 `--agent build`、`--model gemini-3.8-flash-low`），並**強制透過 Gemini 官方 `:countTokens` 端點**精確計數，無論從任何工作目錄執行皆可自動定位同目錄下的 `.txt` 檔：

#### 1. 常用執行指令

```powershell
# 1. 直接執行（必須提供 Gemini API Key，自動使用 p1.txt + p2.txt 跑 A/B 差分）
python C:\Users\artin\Sync\coding\harness-plugins\agy\audit_tokens.py --gemini-api-key "YOUR_KEY"

# 2. 若已設定環境變數 $env:GEMINI_API_KEY，可直接省略參數
python C:\Users\artin\Sync\coding\harness-plugins\agy\audit_tokens.py

# 3. 手動指定其他基準檔與探針檔
python C:\Users\artin\Sync\coding\harness-plugins\agy\audit_tokens.py --gemini-api-key "YOUR_KEY" --prompt-file a.txt --probe-prompt-file b.txt

# 4. 只看本地抓包與官方 :countTokens 精確拆解、不打真實後端（不消耗 agy 額度）
python C:\Users\artin\Sync\coding\harness-plugins\agy\audit_tokens.py --gemini-api-key "YOUR_KEY" --skip-real
```

#### 2. 輸出數值含義說明

- **`[1] LOCAL CAPTURED PAYLOAD BREAKDOWN`（本地送出內容拆解）**
  - `contents` / `Injected tags`：實際送出的使用者訊息長度、官方精確 token 數，以及被 `agy` 自動包進去的標籤（例如 `<USER_REQUEST>`、`<ADDITIONAL_METADATA>`、`<USER_SETTINGS_CHANGE>`）。
  - `systemInstruction`：系統提示詞總字元數與官方 token 數；在 `Baseline` 會額外展開前幾大 XML 區塊（如 `<skills>`、`<Output>`、`<conversation_transcript>`）。
  - `tools (N declarations)`：實際掛載進模型請求的 `functionDeclarations` 數量、字元數與官方 token 數。
  - `Gemini countTokens (exact)`：官方 `:countTokens` 回傳的整包精確 token 數。
- **`[2] REAL BACKEND RESPONSE & PER-TURN USAGE`（真實後端回傳）**
  - `Init tools list`：`agy` 在 `stream-json` 初始化事件宣告的可用工具總數（例如 `build` 會列出 60 支）。
  - `Step N (...)`：每一輪模型請求各自真實回傳的 `in`（輸入）、`out`（輸出）、`think`（隱藏思考）、`cache`（快取命中）。
  - `Total usage`：整次對話所有輪次加總的 token 消耗（若遇到逾時中斷，會自動從 `transcript.jsonl` 補齊）。
- **`[3] TURN-1 COMPARISON & HIDDEN CONTENT CHECK`（第一輪比對與異常標記）**
  - `Expected (official) vs Turn-1 input_tokens`：第一輪官方 `:countTokens` 精確值與後端實際計費 `input_tokens` 的差值。
  - `Effective chars/token ratio`：整包送出字元數除以實際 `input_tokens`。
  - `Tool count mismatch`：提示 `Init` 宣告的工具總數與實際塞進請求的 `functionDeclarations` 數量不同（例如宣告 60 支但只送出 6 支）。
  - *(註：`AGY PROBE REPORT` 會自動略過與 `Baseline` 重複的結構拆解與警告，僅顯示新增的異常。)*
- **`A/B DIFFERENTIAL PROBE SUMMARY`（A/B 差分總結）**
  - `Δ_official`：`Probe` 與 `Baseline` 在官方 `:countTokens` 上的精確差值。
  - `Δ_actual`：`Probe` 與 `Baseline` 在真實第一輪 `input_tokens` 的差值。
  - `Unexplained delta (Δ_actual - Δ_official)`：抵銷所有固定系統開銷與模型版本差異後的「未解釋差值」。正常應接近 `0 tok`；若出現大幅正偏差（> 20 tok），即表示探針觸發了額外的隱藏內容或上下文。

#### 3. 完整參數一覽

| 參數 | 預設值 | 說明 |
| :--- | :--- | :--- |
| `--gemini-api-key` | **必填**（或讀 `GEMINI_API_KEY`） | Gemini API Key，用於呼叫官方 `:countTokens` 精確計數 |
| `--prompt-file` | `p1.txt` | 基準提示詞 `.txt` 檔案路徑 |
| `--probe-prompt-file` | `p2.txt` | 探針提示詞 `.txt` 檔案路徑 |
| `--model` | `gemini-3.8-flash-low` | 指定測試模型 ID（可用 `agy models` 查詢） |
| `--agent` | `build` | 指定代理名稱；傳入 `""` 可測原生預設代理 |
| `--settings` | `""` | 額外傳給 `agy --settings` 的 JSON 字串 |
| `--timeout` | `120` | 單次 `agy` 執行的逾時秒數（對應 `--print-timeout`） |
| `--disable-slash-commands` | 關閉 | 停用斜線指令與 Skills 展開 |
| `--skip-real` | 關閉 | 僅執行本地抓包與 `:countTokens`，不呼叫真實 `agy` |
| `--proxy-upstream` | `""` | 指定上游網址（如 `https://generativelanguage.googleapis.com`）進行單次透傳 |
| `--out-dir` | `""` | 將完整 `audit_report.json` 與 `captured_request.json` 存檔至指定目錄 |
